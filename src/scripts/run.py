"""Main polling loop. Run during trading hours.

Usage:
    python -m src.scripts.run            # paper trading mode (default)
    python -m src.scripts.run --once     # single poll then exit
    python -m src.scripts.run --live     # live trading (requires POLYMARKET_API_KEY)
"""
import argparse
import csv
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from dateutil import parser as dtparse

from src.config import (
    POLL_INTERVAL_SECONDS, LOG_DIR,
    CANDIDATES_CSV, SNAPSHOTS_JSONL, LIVE_TRADES_JSONL,
    RISK_DAILY_LOSS_LIMIT_EUR, RISK_MAX_OPEN_POSITIONS,
    RISK_DRAWDOWN_STOP_PCT, RISK_MIN_LIQUIDITY, STARTING_CAPITAL_EUR,
    POSITION_SIZE_WITH_FEES,
    get_source_priority,
)
from src.data.db import Database
from src.data.polymarket import get_weather_markets
from src.data.taf_collector import TafCollector
from src.data.collectors.jma_ameidas import JmaAmedasCollector
from src.data.collectors.amos import AmosCollector
from src.data.collectors.mss import MssCollector
from src.data.freshness_monitor import FreshnessMonitor
from src.logging_config import setup_logging
from src.monitoring.alerts import AlertManager
from src.risk.manager import RiskManager
from src.strategy.scanner import scan_markets
from src.weather.builder import _build_weather, _station_in_active_window
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import OrderManager
from src.execution.order_executor import _execute_live
from src.execution.position_tracker import (
    _log_open_position_snapshots,
    _check_stop_loss_exits,
    _check_metar_exits,
)
import src.monitoring.dashboard as _dashboard_module
from src.monitoring.dashboard import _load_trades, _compute_win_rate, start_dashboard

log = logging.getLogger(__name__)

_write_lock = threading.Lock()

# Module-level OrderManager instance: owns _open_orders, _open_orders_lock,
# _order_lock, _sold_positions, _stop_loss_strikes, _partial_fill_shares.
order_manager = OrderManager()


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


def _append_live_trade(record: dict, db=None) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with _write_lock:
        with open(LIVE_TRADES_JSONL, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    if db is None:
        return
    if record.get("side") == "SELL":
        return  # SELL updates the original BUY row via _record_sell_in_db
    try:
        updated = db.update_trade_by_order(
            record.get("order_id") or "",
            ticker=record.get("ticker"),
            outcome=record.get("outcome"),
            pnl=float(record["pnl"]) if record.get("pnl") is not None else None,
        )
        if not updated:
            db.insert_trade(  # Placement-time insert failed; insert now so trade is not lost
                ts=record.get("ts", ""),
                station=record.get("station", ""),
                ticker=record.get("ticker", ""),
                bracket_low=float(record.get("bracket_low", 0)),
                bracket_high=float(record.get("bracket_high", 0)),
                side=record.get("side", "NO"),
                predicted_price=int(record.get("predicted_price", 0)),
                actual_price=int(record.get("price_cents", 0)),
                predicted_edge=float(record.get("edge_cents", 0)),
                mode="live",
                order_id=record.get("order_id"),
                outcome=record.get("outcome"),
                pnl=float(record["pnl"]) if record.get("pnl") is not None else None,
                capital_before=float(record.get("size_eur", 5.0)),
            )
    except Exception as e:
        log.warning("[run] DB live trade write failed: %s", e)


def poll_once(
    risk_manager: "RiskManager",
    live_trader=None,
    alert_manager=None,
    db=None,
) -> None:
    """Run one full poll: build weather states, fetch markets, scan, log candidates."""
    ts = datetime.now(timezone.utc).isoformat()
    mode_label = "LIVE" if live_trader else "PAPER"
    log.info("=== Poll [%s] at %s ===", mode_label, ts)

    # Capture previous poll timestamp before overwriting (poll-missed alert needs the gap).
    prev_poll_ts_str = _dashboard_module.last_poll_ts

    if live_trader:
        order_manager.reconcile_timeout_fills(ts)
        order_manager.sync_open_orders(live_trader, db=db)

    # Take-profit is weather-independent -- runs every poll, including pre-sunrise.
    if live_trader:
        order_manager.check_take_profit_exits(live_trader, ts, db=db, risk_manager=risk_manager)

    weather = _build_weather(db=db)
    if not weather:
        log.warning("[run] No weather data for any station -- skipping market scan")
        return

    if live_trader:
        position_states = _log_open_position_snapshots(weather, ts, db=db)
        _check_stop_loss_exits(live_trader, ts, position_states, db=db, risk_manager=risk_manager)
        # _check_metar_exits disabled 2026-05-29: 7/12 false positives, net -15.49 vs hold.
        # _check_metar_exits(weather, live_trader, ts, db=db)

    try:
        markets = get_weather_markets()
    except Exception as e:
        log.error("[polymarket] error: %s -- skipping this poll", e, exc_info=True)
        return
    log.info("[polymarket] %s weather markets fetched", len(markets))

    candidates, snapshots = scan_markets(weather, markets, db=db)

    for snap in snapshots:
        _append_snapshot(snap)

    # Log freshness for high-frequency sources (skip METAR outside active window).
    if db is not None:
        _freshness_monitor = FreshnessMonitor()
        for city_sources in [get_source_priority(c) for c in ["Tokyo", "Seoul", "Busan", "Singapore"]]:
            active_sources = [
                s for s in city_sources
                if s["source"] != "metar" or _station_in_active_window(s["station"])
            ]
            _freshness_monitor.check_all(db, active_sources)

    available_usdc = float("inf")
    if live_trader:
        try:
            available_usdc = live_trader.get_usdc_balance()
            log.info("[balance] %.2f USDC available", available_usdc)
        except Exception as e:
            log.warning("[balance] check failed: %s -- proceeding without balance gate", e)

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
            log.info("  [risk] blocked: %s", reason)
            continue

        if live_trader and available_usdc < POSITION_SIZE_WITH_FEES:
            log.info("  [balance] insufficient (%.2f USDC < %.2f needed incl. fees), skipping remaining", available_usdc, POSITION_SIZE_WITH_FEES)
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
                executor.submit(_execute_live, cand, live_trader._client_factory, risk_manager, ts, db)
                for cand in approved
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    log.error("  [live] thread error: %s", e, exc_info=True)

    log.info(
        "[scan] %s markets, %s evaluated, %s candidates, %s acted on",
        len(markets), len(snapshots), len(candidates), n_acted,
    )

    _dashboard_module.last_poll_ts = ts

    if alert_manager is not None:
        _trades = _load_trades()
        _win_rate_20 = _compute_win_rate(_trades, n=20)
        prev_poll_dt = dtparse.parse(prev_poll_ts_str) if prev_poll_ts_str else None
        alert_manager.check(
            daily_pnl=risk_manager._daily_pnl,
            win_rate_20=_win_rate_20,
            last_poll_time=prev_poll_dt,
        )



def _start_collector_thread(collector_fn, name: str) -> None:
    """Start a collector in a daemon thread; log WARNING on any startup exception."""
    def _run():
        try:
            collector_fn()
        except Exception as e:
            logging.warning(f"[{name}] collector thread exited: {e}")

    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="MeteoEdge polling loop")
    parser.add_argument(
        "--paper", action="store_true", default=False,
        help="Paper trading mode -- log candidates but place no real orders (default)",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Live trading mode -- place real orders via Polymarket CLOB (requires POLYMARKET_API_KEY)",
    )
    parser.add_argument(
        "--once", action="store_true", default=False,
        help="Run a single poll then exit",
    )
    args = parser.parse_args()

    if args.live and args.paper:
        parser.error("--live and --paper are mutually exclusive")

    db = Database()
    open_positions = db.get_open_positions()
    if open_positions:
        log.info("[startup] recovered %s open position(s) from DB", len(open_positions))

    # Start Sprint 1 data-collection threads (daemon — will not block shutdown).
    # Each thread catches all exceptions and logs WARNING so a collector failure
    # never crashes run.py.
    _start_collector_thread(lambda: TafCollector(db).run_loop(), "taf-collector")
    _start_collector_thread(lambda: JmaAmedasCollector(db).run_loop(), "jma-collector")
    _start_collector_thread(lambda: AmosCollector(db).run_loop(), "amos-collector")
    _start_collector_thread(lambda: MssCollector(db).run_loop(), "mss-collector")

    live_trader = None
    if args.live:
        from src.execution.auth import get_clob_client, check_clob_health
        log.info("Checking CLOB connectivity...")
        if not check_clob_health():
            raise SystemExit("[run] CLOB health check failed -- verify POLYMARKET_API_KEY and connectivity")
        live_trader = LiveTrader(get_clob_client(), db)
        live_trader._client_factory = get_clob_client  # Each order thread creates its own client
        log.info("MeteoEdge starting in LIVE mode. Real orders will be placed.")
    else:
        log.info("MeteoEdge starting in PAPER mode.")

    log.info("Logs will be written to ./logs/")

    risk_manager = RiskManager(
        daily_loss_limit_eur=RISK_DAILY_LOSS_LIMIT_EUR,
        max_open_positions=RISK_MAX_OPEN_POSITIONS,
        drawdown_stop_pct=RISK_DRAWDOWN_STOP_PCT,
        min_market_liquidity=RISK_MIN_LIQUIDITY,
        starting_capital=STARTING_CAPITAL_EUR,
        db=db,
    )
    alert_manager = AlertManager()

    start_dashboard()
    _dashboard_module.set_db(db)

    if args.once:
        poll_once(risk_manager, live_trader, alert_manager, db=db)
        log.info("[run] --once mode: exiting after single poll.")
        return

    log.info("Polling every %ss. Press Ctrl-C to stop.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            poll_once(risk_manager, live_trader, alert_manager, db=db)
        except KeyboardInterrupt:
            log.info("[run] Stopping.")
            break
        except Exception as e:
            log.error("[run] Unhandled error in poll: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
