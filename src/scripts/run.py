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

from src.utils.log_rotation import rotated_path, housekeep, SNAPSHOT_RETAIN_DAYS

from src.config import (
    POLL_INTERVAL_SECONDS, LOG_DIR,
    CANDIDATES_CSV, SNAPSHOTS_JSONL, LIVE_TRADES_JSONL,
    RISK_DAILY_LOSS_LIMIT_EUR, RISK_MAX_OPEN_POSITIONS,
    RISK_DRAWDOWN_STOP_PCT, RISK_MIN_LIQUIDITY, STARTING_CAPITAL_EUR,
    POSITION_SIZE_WITH_FEES, ENABLE_CLOB_ENRICHMENT,
    get_source_priority, seed_config, seed_station_overrides,
)
from src.data.db import Database
from src.data.polymarket import get_weather_markets, fetch_orderbooks_batch
from src.data.taf_collector import TafCollector
from src.data.collectors.jma_ameidas import JmaAmedasCollector
from src.data.collectors.amos import AmosCollector
from src.data.collectors.mss import MssCollector
from src.data.freshness_monitor import FreshnessMonitor
from src.logging_config import setup_logging
from src.monitoring.alerts import AlertManager
from src.risk.manager import RiskManager
from src.strategy.scanner import scan_markets
from src.weather.builder import (
    _build_weather,
    build_weather_for_pricing,
    _station_in_active_window,
)
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import OrderManager, order_manager, _load_open_no_positions
from src.execution.order_executor import _execute_live
from src.execution.position_tracker import (
    _log_open_position_snapshots,
    _check_forced_exits,
    _check_stop_loss_exits,
    _check_metar_exits,
)
import src.monitoring.dashboard as _dashboard_module
from src.monitoring.dashboard import _load_trades, _compute_win_rate, start_dashboard

log = logging.getLogger(__name__)

_write_lock = threading.Lock()

# EMOS shadow daily calibration gate
_last_emos_shadow_date: str = ""


def _maybe_run_emos_shadow(db) -> None:
    """Run the EMOS shadow calibration once per calendar day."""
    global _last_emos_shadow_date
    from datetime import date
    today = date.today().isoformat()
    if today == _last_emos_shadow_date:
        return
    _last_emos_shadow_date = today
    try:
        import scripts.run_emos_shadow as _emos_runner
        _emos_runner.main_with_db(db)
    except Exception as e:
        log.warning("[emos_shadow] daily run failed: %s", e)


# Balance-check circuit-breaker state (issue #286)
_balance_fail_count: int = 0
_wallet_cooldown_until: float = 0.0
_BALANCE_FAIL_ALERT_THRESHOLD: int = 3   # fire alert after this many consecutive failures
_WALLET_EMPTY_COOLDOWN_SECONDS: float = 1800.0  # 30 min

# order_manager singleton is defined and exported by src.execution.order_manager
# to allow position_tracker and order_executor to import it directly without
# going through sys.modules.


def _append_snapshot(snap: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    dest = rotated_path(SNAPSHOTS_JSONL)
    housekeep(SNAPSHOTS_JSONL, retain_days=SNAPSHOT_RETAIN_DAYS)
    with open(dest, "a") as f:
        f.write(json.dumps(snap, default=str) + "\n")


def _append_candidate(row: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with _write_lock:
        dest = rotated_path(CANDIDATES_CSV)
        housekeep(CANDIDATES_CSV)
        new_file = dest.stat().st_size == 0
        with open(dest, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new_file:
                w.writeheader()
            w.writerow(row)


def _append_live_trade(record: dict, db=None) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with _write_lock:
        dest = rotated_path(LIVE_TRADES_JSONL)
        housekeep(LIVE_TRADES_JSONL)
        with open(dest, "a") as f:
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

    # Run EMOS shadow calibration once per day (no-op on subsequent polls same day)
    if db is not None:
        _maybe_run_emos_shadow(db)

    # Capture previous poll timestamp before overwriting (poll-missed alert needs the gap).
    prev_poll_ts_str = _dashboard_module.last_poll_ts

    def _finalize_poll() -> None:
        """Mark this poll as completed and run the periodic alert checks.

        Runs on every path that genuinely polled -- including the
        weather-outage path -- so the poll-missed alert keeps measuring real
        gaps instead of being silently skipped by an early return.
        """
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

    if live_trader:
        order_manager.reconcile_timeout_fills(ts, db=db)
        order_manager.sync_open_orders(live_trader, db=db)

    # Take-profit is weather-independent -- runs every poll, including pre-sunrise.
    if live_trader:
        order_manager.check_take_profit_exits(live_trader, ts, db=db, risk_manager=risk_manager)

    weather_health: list = []
    weather = _build_weather(db=db, health_out=weather_health)
    _dashboard_module.weather_health = weather_health  # surface feed health to the dashboard banner

    # Collect open-position token IDs and station tuples early so they can be
    # included in the batch orderbook fetch and pricing weather build below.
    _open_token_ids: list = []
    _open_positions: list = []
    if live_trader:
        _today = datetime.now(timezone.utc).date().isoformat()
        _open_positions = _load_open_no_positions(_today, db=db)
        _open_token_ids = [p["no_token_id"] for p in _open_positions if p.get("no_token_id")]

    # Build always-on pricing weather for held positions, bypassing the
    # active-hours gate.  Only query the stations that have open positions
    # (typically 1-3) to avoid unnecessary upstream API calls.
    # This is separate from the scanner weather (active-hours-gated) — see
    # issue #425 (KHOU 2026-05-27: overnight carryover fooling bracket logic).
    _pricing_weather: dict = {}
    if live_trader and _open_positions:
        from src.config import STATIONS as _ALL_STATIONS  # noqa: PLC0415
        _station_meta: dict = {s[0]: s for s in _ALL_STATIONS}
        _open_station_codes = {p["station"] for p in _open_positions if p.get("station")}
        _open_station_tuples = [
            _station_meta[code]
            for code in _open_station_codes
            if code in _station_meta
        ]
        if _open_station_tuples:
            _pricing_weather = build_weather_for_pricing(_open_station_tuples, db=db)
            # Merge scanner weather as a fallback: if the scanner already produced
            # a WeatherState for this station avoid a duplicate API call.
            for _st, _ws in weather.items():
                _pricing_weather.setdefault(_st, _ws)

    if not weather:
        # No weather data: still run snapshots (bid line is weather-independent)
        # then bail out of the market scan.
        if live_trader:
            _snap_ob: dict = {}
            if _open_token_ids:
                _snap_ob = fetch_orderbooks_batch(_open_token_ids)
            position_states = _log_open_position_snapshots(
                _pricing_weather, ts, db=db, orderbooks=_snap_ob,
            )
            _check_forced_exits(live_trader, ts, position_states, db=db, risk_manager=risk_manager)
            if _pricing_weather:
                _check_stop_loss_exits(live_trader, ts, position_states, db=db, risk_manager=risk_manager)
        log.warning("[run] No weather data for any station -- skipping market scan")
        _finalize_poll()  # poll DID run -- keep poll-missed alert and chart timestamp live
        return

    try:
        markets = get_weather_markets()
    except Exception as e:
        log.error("[polymarket] error: %s -- skipping this poll", e, exc_info=True)
        # Still run snapshots before bailing so the chart doesn't go stale.
        if live_trader:
            _snap_ob2: dict = {}
            if _open_token_ids:
                _snap_ob2 = fetch_orderbooks_batch(_open_token_ids)
            position_states = _log_open_position_snapshots(
                _pricing_weather, ts, db=db, orderbooks=_snap_ob2,
            )
        return
    log.info("[polymarket] %s weather markets fetched", len(markets))

    # Build one shared orderbook dict for all tokens needed this poll:
    # - YES + NO tokens from every candidate market (for CLOB enrichment in scan_markets)
    # - NO tokens for every open position (for position snapshot + stop-loss)
    # This eliminates duplicate HTTP round-trips when the same token appears in
    # multiple markets or in both the scanner and the position snapshot path.
    _market_token_ids: list = []
    if ENABLE_CLOB_ENRICHMENT:
        for _m in markets:
            try:
                _tids = json.loads(_m.get("clobTokenIds") or "[]") if isinstance(_m.get("clobTokenIds"), str) else (_m.get("clobTokenIds") or [])
                _market_token_ids.extend([t for t in _tids if t])
            except Exception:
                pass

    _all_token_ids = list(dict.fromkeys(_market_token_ids + _open_token_ids))
    _t_batch_start = time.monotonic()
    shared_orderbooks = fetch_orderbooks_batch(_all_token_ids) if _all_token_ids else {}
    _t_batch_end = time.monotonic()
    if _all_token_ids:
        log.info(
            "[clob-batch] fetched %d orderbooks (%d unique tokens) in %.2fs",
            len(shared_orderbooks), len(_all_token_ids), _t_batch_end - _t_batch_start,
        )

    # Snapshots and stop-loss use _pricing_weather (always-on, issue #425) so
    # that take-profit / stop-loss fire during off-hours when conditions are met.
    # scan_markets continues to use the active-hours-gated scanner weather so
    # the KHOU 2026-05-27 regression is preserved.
    if live_trader:
        position_states = _log_open_position_snapshots(
            _pricing_weather, ts, db=db, orderbooks=shared_orderbooks,
        )
        # Forced pre-settlement exit runs BEFORE stop-loss so it has priority.
        # No-op when FORCE_EXIT_MINUTES_TO_SETTLEMENT=0 (today's behaviour).
        _check_forced_exits(live_trader, ts, position_states, db=db, risk_manager=risk_manager)
        if _pricing_weather:
            _check_stop_loss_exits(live_trader, ts, position_states, db=db, risk_manager=risk_manager)
        # _check_metar_exits disabled 2026-05-29: 7/12 false positives, net -15.49 vs hold.
        # _check_metar_exits(weather, live_trader, ts, db=db)

    candidates, snapshots = scan_markets(weather, markets, db=db, orderbooks=shared_orderbooks)

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

    global _balance_fail_count, _wallet_cooldown_until

    # Wallet-empty cooldown: skip the entire candidate loop until the window passes.
    if live_trader and time.time() < _wallet_cooldown_until:
        log.info("[balance] wallet-empty cooldown active (%.0fs remaining) — skipping placement",
                 _wallet_cooldown_until - time.time())
        _finalize_poll()
        return

    available_usdc = float("inf")
    if live_trader:
        try:
            available_usdc = live_trader.get_usdc_balance()
            log.info("[balance] %.2f USDC available", available_usdc)
            _balance_fail_count = 0  # reset on success
        except Exception as e:
            _balance_fail_count += 1
            log.error("[balance] check failed: %s -- skipping order placement this cycle", e)
            available_usdc = 0.0  # explicit zero → downstream 'if available_usdc < POSITION_SIZE' skips
            if alert_manager is not None and _balance_fail_count >= _BALANCE_FAIL_ALERT_THRESHOLD:
                alert_manager._fire(
                    "balance_check_failure",
                    f"ERROR: balance check failed {_balance_fail_count} consecutive times",
                    f"Last error: {e}",
                )

    approved: list = []
    n_acted = 0
    n_shadow = 0
    for cand in candidates:
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

        # Shadow candidates: log to trades table as observation only.
        # No capital reserved, no order placed, no risk_manager gates checked.
        if cand.shadow:
            n_shadow += 1
            if db is not None:
                try:
                    _row_id, _created = db.upsert_shadow_trade(
                        ts=ts,
                        station=cand.station,
                        ticker=cand.bracket.ticker,
                        bracket_low=cand.bracket.low_f,
                        bracket_high=cand.bracket.high_f,
                        side=cand.side,
                        predicted_price=int(round(cand.p_yes * 100)),
                        actual_price=cand.bracket.yes_ask_cents,
                        predicted_edge=cand.edge_cents,
                        capital_before=0.0,
                    )
                    if _created:
                        log.info(
                            "  [shadow] logged %s candidate %s @ %sc (no order placed)",
                            cand.side, cand.bracket.ticker[:14], cand.bracket.yes_ask_cents,
                        )
                    else:
                        log.debug(
                            "  [shadow] dedup: updated actual_price=%sc for %s %s (today already logged)",
                            cand.bracket.yes_ask_cents, cand.side, cand.bracket.ticker[:14],
                        )
                except Exception as e:
                    log.warning("  [shadow] DB upsert failed: %s", e)
            continue

        if live_trader and available_usdc < POSITION_SIZE_WITH_FEES:
            log.info("  [balance] insufficient (%.2f USDC < %.2f needed incl. fees), skipping remaining", available_usdc, POSITION_SIZE_WITH_FEES)
            _wallet_cooldown_until = time.time() + _WALLET_EMPTY_COOLDOWN_SECONDS
            break

        raw_liquidity = cand.bracket.yes_ask_size + cand.bracket.no_ask_size
        liquidity = raw_liquidity if raw_liquidity > 0 else 9999
        allowed, reason = risk_manager.allow_trade(
            capital=STARTING_CAPITAL_EUR,
            liquidity_contracts=liquidity,
        )
        if not allowed:
            log.info("  [risk] blocked: %s", reason)
            continue

        n_acted += 1

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
                executor.submit(_execute_live, cand, live_trader._client_factory, risk_manager, ts, db, available_usdc)
                for cand in approved
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    log.error("  [live] thread error: %s", e, exc_info=True)

    log.info(
        "[scan] %s markets, %s evaluated, %s candidates, %s acted on, %s shadow",
        len(markets), len(snapshots), len(candidates), n_acted, n_shadow,
    )

    _finalize_poll()


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
    seed_config(db)
    seed_station_overrides(db)
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
