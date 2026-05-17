"""Main polling loop. Run during trading hours.

Usage:
    python -m src.scripts.run            # paper trading mode (default)
    python -m src.scripts.run --once     # single poll then exit
    python -m src.scripts.run --live     # live trading (requires POLYMARKET_API_KEY)
"""
import argparse
import csv
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    STATIONS, STATION_TZ, POLL_INTERVAL_SECONDS, LOG_DIR,
    CANDIDATES_CSV, SNAPSHOTS_JSONL, LIVE_TRADES_JSONL,
    RISK_DAILY_LOSS_LIMIT_EUR, RISK_MAX_OPEN_POSITIONS,
    RISK_DRAWDOWN_STOP_PCT, RISK_MIN_LIQUIDITY, STARTING_CAPITAL_EUR,
    POSITION_SIZE_EUR, POSITION_SIZE_WITH_FEES,
)
from src.data.metar import fetch_all_metars_today, compute_daily_high, now_local, sunset_local
from src.data.nws import fetch_nws_forecast_high
from src.data.open_meteo import fetch_secondary_forecast
from src.data.polymarket import get_weather_markets
from src.model.envelope import WeatherState
from src.monitoring.alerts import AlertManager
from src.risk.manager import RiskManager
from src.strategy.scanner import scan_markets

FILL_POLL_INTERVAL_S = 30
FILL_MAX_WAIT_S = 300  # 5 minutes, 10 attempts

_write_lock = threading.Lock()
_order_lock = threading.Lock()  # Serialize CLOB placements — HTTP/2 pool not thread-safe
_open_orders: set[tuple[str, str]] = set()  # (ticker, side) pairs with a live GTC order
_open_orders_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _append_snapshot(snap: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with open(SNAPSHOTS_JSONL, "a") as f:
        f.write(json.dumps(snap, default=str) + "\n")


def _append_candidate(row: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with _write_lock:
        new_file = not CANDIDATES_CSV.exists()
        with open(CANDIDATES_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new_file:
                w.writeheader()
            w.writerow(row)


def _append_live_trade(record: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with _write_lock:
        with open(LIVE_TRADES_JSONL, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------------------
# Live order lifecycle
# ---------------------------------------------------------------------------

def _execute_live(candidate, clob_client_factory, risk_manager, ts: str) -> None:
    """Place one order and wait for fill/timeout. open_position() already called by caller.

    Each thread creates its own LiveTrader/ClobClient to avoid HTTP/2 stream
    collisions when multiple orders are placed concurrently.
    """
    from src.execution.live_trader import LiveTrader
    trader = LiveTrader(clob_client_factory())

    token_id = (
        candidate.bracket.yes_token_id if candidate.side == "YES"
        else candidate.bracket.no_token_id
    )
    if not token_id:
        print(f"  [live] no token_id for {candidate.bracket.ticker[:16]}…, skipping")
        risk_manager.close_position()
        return

    order_key = token_id
    with _open_orders_lock:
        if order_key in _open_orders:
            print(f"  [live] skip {candidate.side} {candidate.bracket.ticker[:16]}… — GTC order already open on exchange")
            risk_manager.close_position()
            return
        _open_orders.add(order_key)

    try:
        with _order_lock:  # Serialize HTTP/2 placements; fill-monitoring remains parallel
            try:
                order_id = trader.place_order(
                    token_id=token_id,
                    side=candidate.side,
                    price_cents=candidate.price_cents,
                    size_usdc=POSITION_SIZE_EUR,
                )
            except Exception as e:
                print(f"  [live] place_order failed: {e}")
                risk_manager.close_position()
                return

        print(f"  [live] placed {order_id[:12]}… {candidate.side} @ {candidate.price_cents}¢")

        deadline = time.monotonic() + FILL_MAX_WAIT_S
        outcome = "timeout"
        while time.monotonic() < deadline:
            time.sleep(FILL_POLL_INTERVAL_S)
            status = trader.check_fill(order_id)
            if status == "filled":
                outcome = "filled"
                break
            if status == "cancelled":
                outcome = "cancelled"
                break

        if outcome == "timeout":
            trader.cancel_order(order_id)

        risk_manager.close_position()
        if outcome == "filled":
            risk_manager.record_pnl(0.0)  # Actual PnL resolved at settlement

        _append_live_trade({
            "ts": ts,
            "order_id": order_id,
            "station": candidate.station,
            "question": candidate.market.get("question") or candidate.market.get("groupItemTitle") or "",
            "end_date": (candidate.market.get("endDate") or candidate.market.get("end_date_iso") or "")[:10],
            "ticker": candidate.bracket.ticker,
            "side": candidate.side,
            "price_cents": candidate.price_cents,
            "size_eur": POSITION_SIZE_EUR,
            "edge_cents": round(candidate.edge_cents, 2),
            "outcome": outcome,
        })
        print(f"  [live] {outcome} {order_id[:12]}…")
    finally:
        with _open_orders_lock:
            _open_orders.discard(order_key)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def _build_weather() -> dict[str, WeatherState]:
    weather: dict[str, WeatherState] = {}
    utc_date = datetime.now(timezone.utc).date()
    for station, lat, lon, city, _ in STATIONS:
        import pytz
        local_date = datetime.now(pytz.timezone(STATION_TZ[station])).date()
        if local_date < utc_date:
            print(f"[{station}] local date {local_date} behind UTC {utc_date} — skipping to avoid yesterday's METAR data")
            continue

        metars = fetch_all_metars_today(station)
        if not metars:
            print(f"[{station}] no METAR data, skipping")
            continue

        result = compute_daily_high(metars, STATION_TZ[station])
        if not result:
            print(f"[{station}] could not compute daily high, skipping")
            continue
        high_f, high_time = result

        latest = metars[0]
        latest_temp_c = latest.get("temp")
        if latest_temp_c is None:
            print(f"[{station}] latest METAR missing temp, skipping")
            continue

        obs_str = latest.get("reportTime") or latest.get("obsTime")
        if not obs_str:
            print(f"[{station}] latest METAR missing time, skipping")
            continue

        try:
            from dateutil import parser as dtparse
            latest_temp_f = (float(latest_temp_c) * 9 / 5) + 32
            latest_time = dtparse.parse(obs_str)
            if latest_time.tzinfo is None:
                latest_time = latest_time.replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"[{station}] METAR parse error: {e}, skipping")
            continue

        forecast_nws = fetch_nws_forecast_high(lat, lon)
        forecast_secondary = fetch_secondary_forecast(lat, lon)

        weather[station] = WeatherState(
            station=station,
            now_local=now_local(station),
            sunset_local=sunset_local(station, lat, lon),
            current_high_f=high_f,
            current_high_time=high_time,
            latest_temp_f=latest_temp_f,
            latest_temp_time=latest_time,
            forecast_high_f=forecast_nws,
            secondary_forecast_f=forecast_secondary,
        )
        print(f"[{station}] high={high_f:.1f}°F latest={latest_temp_f:.1f}°F nws={forecast_nws}")
    return weather


def _sync_open_orders(live_trader) -> None:
    """Refresh _open_orders from Polymarket's actual open order list.

    Called at the top of every live poll so the dedup guard reflects reality —
    not just in-memory state that can drift after timeouts or restarts.
    """
    try:
        from py_clob_client_v2.clob_types import OpenOrderParams
        orders = live_trader.client.get_open_orders(OpenOrderParams())
        # Key by token_id (asset_id) only — YES/NO tokens already have distinct IDs,
        # and we always place side="BUY" on the exchange regardless of YES/NO direction,
        # so mapping exchange side→YES/NO is unreliable.
        with _open_orders_lock:
            _open_orders.clear()
            for o in orders:
                _open_orders.add(o.get("asset_id", ""))
        print(f"[orders] {len(orders)} open orders on exchange synced to dedup guard")
    except Exception as e:
        print(f"[orders] failed to sync open orders: {e} — dedup guard uses in-memory state")


def poll_once(risk_manager, live_trader=None, alert_manager=None) -> None:
    """Run one full poll: build weather states, fetch markets, scan, log candidates."""
    ts = datetime.now(timezone.utc).isoformat()
    mode_label = "LIVE" if live_trader else "PAPER"
    print(f"\n=== Poll [{mode_label}] at {ts} ===")

    # Capture the previous poll's timestamp BEFORE overwriting it so the
    # poll-missed alert can measure the gap between the last and current cycle.
    import src.monitoring.dashboard as _dashboard
    prev_poll_ts_str = _dashboard.last_poll_ts  # None on first poll, ISO string on subsequent

    if live_trader:
        _sync_open_orders(live_trader)

    weather = _build_weather()
    if not weather:
        print("[run] No weather data for any station — skipping market scan")
        return

    try:
        markets = get_weather_markets()
    except Exception as e:
        print(f"[polymarket] error: {e} — skipping this poll")
        return
    print(f"[polymarket] {len(markets)} weather markets fetched")

    candidates, snapshots = scan_markets(weather, markets)

    for snap in snapshots:
        _append_snapshot(snap)

    # Filter candidates through risk manager sequentially so position counts are accurate,
    # then execute all approved live orders in parallel so they hit the market simultaneously.
    available_usdc = float("inf")
    if live_trader:
        try:
            available_usdc = live_trader.get_usdc_balance()
            print(f"[balance] {available_usdc:.2f} USDC available")
        except Exception as e:
            print(f"[balance] check failed: {e} — proceeding without balance gate")

    approved: list = []
    n_acted = 0
    for cand in candidates:
        raw_liquidity = cand.bracket.yes_ask_size + cand.bracket.no_ask_size
        liquidity = raw_liquidity if raw_liquidity > 0 else 9999
        allowed, reason = risk_manager.allow_trade(
            capital=STARTING_CAPITAL_EUR,
            liquidity_contracts=liquidity,
        )
        if not allowed:
            print(f"  [risk] blocked: {reason}")
            continue

        if live_trader and available_usdc < POSITION_SIZE_WITH_FEES:
            print(f"  [balance] insufficient ({available_usdc:.2f} USDC < {POSITION_SIZE_WITH_FEES:.2f} needed incl. fees), skipping remaining")
            break

        n_acted += 1
        row = {
            "ts": ts,
            "station": cand.station,
            "question": cand.market.get("question") or cand.market.get("groupItemTitle") or "",
            "end_date": (cand.market.get("endDate") or cand.market.get("end_date_iso") or "")[:10],
            "ticker": cand.bracket.ticker,
            "bracket_low": cand.bracket.low_f,
            "bracket_high": cand.bracket.high_f,
            "yes_ask": cand.bracket.yes_ask_cents,
            "no_ask": cand.bracket.no_ask_cents,
            "p_yes": round(cand.p_yes, 4),
            "ev_yes": round(cand.ev_yes, 2),
            "ev_no": round(cand.ev_no, 2),
            "flagged_side": cand.side,
            "flagged_edge": round(cand.edge_cents, 2),
            "flagged_price": cand.price_cents,
            "flagged_confidence": round(cand.confidence, 4),
            "minutes_to_settlement": round(cand.minutes_to_settlement, 1),
        }
        _append_candidate(row)

        if live_trader:
            risk_manager.open_position()  # Reserve slot before spawning thread
            available_usdc -= POSITION_SIZE_WITH_FEES
            approved.append(cand)
        else:
            risk_manager.open_position()
            risk_manager.close_position()
            risk_manager.record_pnl(0.0)

    if live_trader and approved:
        with ThreadPoolExecutor(max_workers=len(approved)) as executor:
            futures = [
                executor.submit(_execute_live, cand, live_trader._client_factory, risk_manager, ts)
                for cand in approved
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"  [live] thread error: {e}")

    print(
        f"[scan] {len(markets)} markets, {len(snapshots)} evaluated, "
        f"{len(candidates)} candidates, {n_acted} acted on"
    )

    # Update dashboard last_poll timestamp for the next cycle
    _dashboard.last_poll_ts = ts

    # Fire email alerts if thresholds are crossed
    if alert_manager is not None:
        from src.monitoring.dashboard import _load_trades, _compute_win_rate
        _trades = _load_trades()
        _win_rate_20 = _compute_win_rate(_trades, n=20)
        # Resolve the previous poll's ISO string to a datetime so the poll-missed
        # check can compute the gap correctly.  On the very first poll this is None,
        # which the alert manager handles by skipping the poll-missed check.
        prev_poll_dt = None
        if prev_poll_ts_str:
            from dateutil import parser as dtparse
            prev_poll_dt = dtparse.parse(prev_poll_ts_str)
        alert_manager.check(
            daily_pnl=risk_manager._daily_pnl,
            win_rate_20=_win_rate_20,
            last_poll_time=prev_poll_dt,  # previous poll's time, not now()
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="MeteoEdge polling loop")
    parser.add_argument(
        "--paper", action="store_true", default=False,
        help="Paper trading mode — log candidates but place no real orders (default)",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Live trading mode — place real orders via Polymarket CLOB (requires POLYMARKET_API_KEY)",
    )
    parser.add_argument(
        "--once", action="store_true", default=False,
        help="Run a single poll then exit",
    )
    args = parser.parse_args()

    if args.live and args.paper:
        parser.error("--live and --paper are mutually exclusive")

    live_trader = None
    if args.live:
        from src.execution.auth import get_clob_client, check_clob_health
        from src.execution.live_trader import LiveTrader
        print("Checking CLOB connectivity…")
        if not check_clob_health():
            raise SystemExit("[run] CLOB health check failed — verify POLYMARKET_API_KEY and connectivity")
        live_trader = LiveTrader(get_clob_client())
        live_trader._client_factory = get_clob_client  # Each order thread creates its own client
        print("MeteoEdge starting in LIVE mode. Real orders will be placed.")
    else:
        print("MeteoEdge starting in PAPER mode.")

    print("Logs will be written to ./logs/")

    risk_manager = RiskManager(
        daily_loss_limit_eur=RISK_DAILY_LOSS_LIMIT_EUR,
        max_open_positions=RISK_MAX_OPEN_POSITIONS,
        drawdown_stop_pct=RISK_DRAWDOWN_STOP_PCT,
        min_market_liquidity=RISK_MIN_LIQUIDITY,
        starting_capital=STARTING_CAPITAL_EUR,
    )
    alert_manager = AlertManager()

    if args.once:
        poll_once(risk_manager, live_trader, alert_manager)
        print("[run] --once mode: exiting after single poll.")
        return

    print(f"Polling every {POLL_INTERVAL_SECONDS}s. Press Ctrl-C to stop.")
    while True:
        try:
            poll_once(risk_manager, live_trader, alert_manager)
        except KeyboardInterrupt:
            print("\n[run] Stopping.")
            break
        except Exception as e:
            print(f"[run] Unhandled error in poll: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
