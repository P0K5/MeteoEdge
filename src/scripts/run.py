"""Main polling loop. Run during trading hours.

Usage:
    python -m src.scripts.run            # paper trading mode (default)
    python -m src.scripts.run --once     # single poll then exit
    python -m src.scripts.run --live     # live trading (requires POLYMARKET_API_KEY)
"""
import argparse
import csv
import json
import os
import tempfile
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
    POSITION_SIZE_EUR, POSITION_SIZE_WITH_FEES, TAKE_PROFIT_BUFFER_CENTS,
)
from src.data.metar import fetch_all_metars_today, compute_daily_high, now_local, sunset_local
from src.data.nws import fetch_nws_forecast_high
from src.data.open_meteo import fetch_secondary_forecast
from src.data.polymarket import get_orderbook, get_weather_markets
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
_sold_positions: set[str] = set()  # no_token_ids sold this session (METAR stop-loss)
_open_trades: list[dict] = []      # in-memory open positions written to live_state.json
_open_trades_lock = threading.Lock()


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

    predicted_price = round(candidate.confidence * 100)

    try:
        with _order_lock:  # Serialize HTTP/2 placements; fill-monitoring remains parallel
            try:
                order_id = trader.place_order(
                    token_id=token_id,
                    side=candidate.side,
                    price_cents=candidate.price_cents,
                    size_usdc=POSITION_SIZE_EUR,
                    station=candidate.station,
                    bracket_low=candidate.bracket.low_f,
                    bracket_high=candidate.bracket.high_f,
                    predicted_price=predicted_price,
                    predicted_edge=round(candidate.edge_cents, 2),
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
            from src.execution.live_trader import persist_state
            trade_record = {
                "order_id": order_id,
                "token_id": token_id,
                "station": candidate.station,
                "side": candidate.side,
                "bracket_low": candidate.bracket.low_f,
                "bracket_high": candidate.bracket.high_f,
                "entry_price": candidate.price_cents,
                "predicted_price": predicted_price,
                "predicted_edge": round(candidate.edge_cents, 2),
                "size_usdc": POSITION_SIZE_EUR,
                "placed_at": ts,
            }
            with _open_trades_lock:
                _open_trades.append(trade_record)
                persist_state(list(_open_trades))

        _append_live_trade({
            "ts": ts,
            "order_id": order_id,
            "station": candidate.station,
            "question": candidate.market.get("question") or candidate.market.get("groupItemTitle") or "",
            "end_date": (candidate.market.get("endDate") or candidate.market.get("end_date_iso") or "")[:10],
            "ticker": candidate.bracket.ticker,
            "asset_id": token_id,
            "no_token_id": token_id if candidate.side == "NO" else candidate.bracket.no_token_id,
            "bracket_low": candidate.bracket.low_f,
            "bracket_high": candidate.bracket.high_f,
            "side": candidate.side,
            "price_cents": candidate.price_cents,
            "predicted_price": predicted_price,
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


def _wallet_held_token_ids() -> set[str]:
    """Return non-redeemable token_ids currently held in the wallet (size > 0.01).

    Used to reconcile timeout records against actual exchange state — if a
    token is in the wallet, the order filled despite our local timeout marker.
    """
    wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "")
    if not wallet:
        return set()
    try:
        import httpx
        url = f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.01&limit=100"
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("positions") or rows.get("data") or []
        return {
            str(row.get("asset") or "")
            for row in rows
            if row.get("asset") and not row.get("redeemable")
        }
    except Exception as e:
        print(f"[reconcile] wallet fetch failed: {e} — skipping reconciliation")
        return set()


def _reconcile_timeout_fills(ts: str) -> None:
    """Patch timeout JSONL records whose tokens still appear in the wallet.

    GTC limit orders sometimes fill after our 5-minute wait window expires.
    The follow-up cancel_order() can fail silently or lose a race with the
    matching engine, leaving the order live on the exchange.  When the order
    later fills, the wallet shows the position but our JSONL says
    outcome=timeout — invisible to the dashboard enrichment and to the
    take-profit / METAR stop-loss exits.

    Rewriting these records to outcome=filled restores visibility everywhere
    that filters on outcome.  Reconciliation runs once per poll, before
    _sync_open_orders so the dedup guard sees the patched records.
    """
    if not LIVE_TRADES_JSONL.exists():
        return
    held = _wallet_held_token_ids()
    if not held:
        return

    patched = 0
    new_lines: list[str] = []
    try:
        with open(LIVE_TRADES_JSONL) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    new_lines.append(line)
                    continue
                try:
                    r = json.loads(stripped)
                except json.JSONDecodeError:
                    new_lines.append(line)
                    continue
                token = r.get("asset_id") or r.get("no_token_id") or ""
                if r.get("outcome") == "timeout" and token in held:
                    r["outcome"] = "filled"
                    r["reconciled_at"] = ts
                    new_lines.append(json.dumps(r, default=str) + "\n")
                    patched += 1
                else:
                    new_lines.append(line)
    except OSError as e:
        print(f"[reconcile] read failed: {e}")
        return

    if patched == 0:
        return

    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=LIVE_TRADES_JSONL.parent, delete=False, suffix=".tmp"
        ) as f:
            f.writelines(new_lines)
            tmp = f.name
        os.replace(tmp, LIVE_TRADES_JSONL)
        print(
            f"[reconcile] patched {patched} timeout record(s) → filled "
            f"(token present in wallet)"
        )
    except OSError as e:
        print(f"[reconcile] write failed: {e}")


def _sync_open_orders(live_trader) -> None:
    """Refresh _open_orders from exchange open orders + today's filled positions.

    Called at the top of every live poll. Two sources feed the dedup guard:
    1. Exchange open orders (pending GTC orders not yet filled or cancelled).
    2. Today's filled positions from live_trades.jsonl — once a fill is matched
       the exchange order disappears, but we must not re-buy the same token
       within the same trading day.
    """
    today = datetime.now(timezone.utc).date().isoformat()

    with _open_orders_lock:
        _open_orders.clear()

        # Source 1: live exchange open orders
        try:
            from py_clob_client_v2.clob_types import OpenOrderParams
            orders = live_trader.client.get_open_orders(OpenOrderParams())
            for o in orders:
                _open_orders.add(o.get("asset_id", ""))
            print(f"[orders] {len(orders)} open exchange orders synced to dedup guard")
        except Exception as e:
            print(f"[orders] failed to sync open orders: {e} — using filled-positions only")

        # Source 2: today's already-filled no_token_ids from live_trades.jsonl
        filled_count = 0
        if LIVE_TRADES_JSONL.exists():
            try:
                with open(LIVE_TRADES_JSONL) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (r.get("end_date", "")[:10] == today
                                and r.get("outcome") == "filled"
                                and r.get("no_token_id")):
                            _open_orders.add(r["no_token_id"])
                            filled_count += 1
            except OSError:
                pass
        if filled_count:
            print(f"[orders] {filled_count} today's filled position(s) added to dedup guard")


def _load_open_no_positions(today: str) -> list[dict]:
    """Return filled NO positions for today that have not yet been sold.

    Reads live_trades.jsonl and groups by no_token_id. A token is considered
    sold if any record with outcome='sold' exists for it today.
    """
    if not LIVE_TRADES_JSONL.exists():
        return []
    records: list[dict] = []
    sold_tokens: set[str] = set()
    try:
        with open(LIVE_TRADES_JSONL) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("end_date", "")[:10] != today:
                    continue
                if r.get("outcome") == "sold":
                    sold_tokens.add(r.get("no_token_id", ""))
                elif (r.get("outcome") == "filled"
                        and r.get("side") == "NO"
                        and r.get("no_token_id")
                        and r.get("bracket_low") is not None
                        and r.get("bracket_high") is not None):
                    records.append(r)
    except OSError:
        return []
    return [r for r in records if r.get("no_token_id") not in sold_tokens]


def _check_take_profit_exits(live_trader, ts: str) -> None:
    """Exit NO positions where the best bid has reached predicted_price - buffer.

    The model snapshot is frozen at entry time and cannot validate further price
    movement.  Once market price converges to our fair value, the edge we
    measured is fully captured — holding longer is a bet on the (stale) model
    being right that the market underprices the outcome.  Locking in the gain
    recycles capital into the next edge and reduces variance without giving up
    expected value.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today)
    if not open_positions:
        return

    from collections import defaultdict
    by_token: dict[str, list[dict]] = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    for token_id, fills in by_token.items():
        if token_id in _sold_positions:
            continue

        predicted_price = fills[0].get("predicted_price")
        if not predicted_price:
            continue
        target_cents = int(predicted_price) - TAKE_PROFIT_BUFFER_CENTS

        try:
            ob = get_orderbook(token_id)
            bids = ob.get("bids") or []
            if not bids:
                continue
            best_bid_cents = max(1, min(99, round(max(float(b["price"]) for b in bids) * 100)))
        except Exception as e:
            print(f"  [tp] orderbook fetch failed for {token_id[:14]}…: {e}")
            continue

        if best_bid_cents < target_cents:
            continue

        bracket_low = fills[0]["bracket_low"]
        bracket_high = fills[0]["bracket_high"]
        station = fills[0]["station"]
        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        total_eur = sum(f["size_eur"] for f in fills)
        question = fills[0].get("question", "")

        print(
            f"  [tp] {station} {bracket_low:.0f}-{bracket_high:.0f}°F NO bid "
            f"{best_bid_cents}¢ ≥ target {target_cents}¢ (predicted {predicted_price}¢) "
            f"— selling {total_shares:.1f} shares"
        )
        try:
            sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
            _sold_positions.add(token_id)
            from src.execution.live_trader import persist_state
            with _open_trades_lock:
                _open_trades[:] = [t for t in _open_trades if t.get("token_id") != token_id]
                persist_state(list(_open_trades))
            avg_entry_cents = sum(
                f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
                for f in fills
            ) / total_shares
            pnl = round((sell_price_cents - avg_entry_cents) / 100 * total_shares, 4)
            _append_live_trade({
                "ts": ts,
                "order_id": sell_id,
                "station": station,
                "question": question,
                "end_date": today,
                "ticker": fills[0].get("ticker", ""),
                "no_token_id": token_id,
                "bracket_low": bracket_low,
                "bracket_high": bracket_high,
                "side": "SELL",
                "price_cents": sell_price_cents,
                "entry_price_cents": round(avg_entry_cents),
                "shares": round(total_shares, 4),
                "size_eur": total_eur,
                "edge_cents": 0,
                "pnl": pnl,
                "outcome": "sold",
                "trigger": f"take_profit@{best_bid_cents}c_target{target_cents}c_predicted{predicted_price}c",
            })
            print(
                f"  [tp] sell {sell_id[:12]}… placed @ {sell_price_cents}¢ — "
                f"{station} {bracket_low:.0f}-{bracket_high:.0f}°F NO  pnl={pnl:+.2f}"
            )
        except Exception as e:
            print(f"  [tp] sell failed for {station} {bracket_low:.0f}-{bracket_high:.0f}°F: {e}")


def _check_metar_exits(weather: dict, live_trader, ts: str) -> None:
    """Exit NO positions where the running METAR daily high is inside the bracket.

    Called once per live poll after weather states are built. When the current
    high temperature has entered a bracket where we hold NO tokens, that position
    is losing and may resolve YES — we sell at the best available bid to recover
    whatever value remains rather than losing the full stake at settlement.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today)
    if not open_positions:
        return

    # Group fills by no_token_id so we can aggregate shares across DCA fills
    from collections import defaultdict
    by_token: dict[str, list[dict]] = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    for token_id, fills in by_token.items():
        if token_id in _sold_positions:
            continue

        # All fills for a token share the same bracket and station
        bracket_low = fills[0]["bracket_low"]
        bracket_high = fills[0]["bracket_high"]
        station = fills[0]["station"]

        if station not in weather:
            continue

        current_high = weather[station].current_high_f
        if not (bracket_low <= current_high <= bracket_high):
            continue

        # Narrow brackets (≤3°F) are often passed through while the temperature
        # is still rising in the morning.  Only exit after 13:00 local time when
        # the daily high is more firmly established.  Wide/open-ended brackets
        # (e.g. "≤59°F", width≈109°F) can fire at any time because the
        # temperature needs a large rise to exit them.
        bracket_width = bracket_high - bracket_low
        if bracket_width <= 3.0:
            local_hour = weather[station].now_local.hour
            if local_hour < 13:
                print(
                    f"  [exit] {station} {bracket_low:.0f}-{bracket_high:.0f}°F "
                    f"inside bracket but local time is {local_hour:02d}:xx — "
                    f"deferring stop-loss until after 13:00"
                )
                continue

        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        total_eur = sum(f["size_eur"] for f in fills)
        question = fills[0].get("question", "")
        print(
            f"  [exit] METAR high {current_high:.1f}°F is inside {bracket_low:.0f}-"
            f"{bracket_high:.0f}°F bracket — selling {total_shares:.1f} NO shares "
            f"for {station}"
        )
        try:
            sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
            _sold_positions.add(token_id)
            from src.execution.live_trader import persist_state
            with _open_trades_lock:
                _open_trades[:] = [t for t in _open_trades if t.get("token_id") != token_id]
                persist_state(list(_open_trades))
            # Weighted average entry price across all DCA fills
            avg_entry_cents = sum(
                f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
                for f in fills
            ) / total_shares
            # Actual realised PnL: (sell - avg_entry) per share × total shares, in EUR
            pnl = round((sell_price_cents - avg_entry_cents) / 100 * total_shares, 4)
            _append_live_trade({
                "ts": ts,
                "order_id": sell_id,
                "station": station,
                "question": question,
                "end_date": today,
                "ticker": fills[0].get("ticker", ""),
                "no_token_id": token_id,
                "bracket_low": bracket_low,
                "bracket_high": bracket_high,
                "side": "SELL",
                "price_cents": sell_price_cents,
                "entry_price_cents": round(avg_entry_cents),
                "shares": round(total_shares, 4),
                "size_eur": total_eur,
                "edge_cents": 0,
                "pnl": pnl,
                "outcome": "sold",
                "trigger": f"metar_high={current_high:.1f}F",
            })
            print(
                f"  [exit] sell {sell_id[:12]}… placed @ {sell_price_cents}¢ — "
                f"{station} {bracket_low:.0f}-{bracket_high:.0f}°F NO  pnl={pnl:+.2f}"
            )
        except Exception as e:
            print(f"  [exit] sell failed for {station} {bracket_low:.0f}-{bracket_high:.0f}°F: {e}")


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
        _reconcile_timeout_fills(ts)
        _sync_open_orders(live_trader)

    weather = _build_weather()
    if not weather:
        print("[run] No weather data for any station — skipping market scan")
        return

    if live_trader:
        _check_take_profit_exits(live_trader, ts)
        _check_metar_exits(weather, live_trader, ts)

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

    from src.monitoring.dashboard import start_dashboard
    start_dashboard()

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
