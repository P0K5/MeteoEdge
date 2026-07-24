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
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from dateutil import parser as dtparse

from src.utils.log_rotation import rotated_path, housekeep, SNAPSHOT_RETAIN_DAYS

from src.config import (
    POLL_INTERVAL_SECONDS, LOG_DIR,
    CANDIDATES_CSV, SNAPSHOTS_JSONL, LIVE_TRADES_JSONL,
    BRACKET_EVALS_JSONL, BRACKET_EVAL_RETAIN_DAYS,
    RISK_DAILY_LOSS_LIMIT_EUR, RISK_MAX_OPEN_POSITIONS,
    RISK_DRAWDOWN_STOP_PCT, RISK_MIN_LIQUIDITY, STARTING_CAPITAL_EUR,
    POSITION_SIZE_WITH_FEES, ENABLE_CLOB_ENRICHMENT,
    LIVE_ALLOW_BRACKET_REENTRY, CONFIG_DEFAULTS,
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
    _build_weather, _station_in_active_window, build_weather_for_pricing,
    build_weather_low_for_scanning, persist_metar_for_climb_stations,
)
from src.model.envelope_low import true_probability_low_in_bracket
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

# Zero-evaluation watchdog state (issue #686)
_zero_eval_consecutive_ticks: int = 0


def _shadow_bought_side_cost_cents(
    side: str, yes_ask_cents: int, no_ask_cents=None
) -> int:
    """Cost of the side actually "bought" for a shadow row's ``actual_price``.

    Issue #737: ``settle_shadow_trades()`` prices ``actual_price`` as the
    bought-side cost (win pays ``100 - cost``, loss pays ``-cost``), so a NO
    shadow row must store the NO ask, not the YES ask. Falls back to
    ``100 - yes_ask`` (clamped to [1, 99]) when ``no_ask`` is missing or
    degenerate. YES rows always store the YES ask.
    """
    if side == "NO":
        if not no_ask_cents or int(no_ask_cents) <= 0:
            return max(1, min(99, 100 - int(yes_ask_cents)))
        return int(no_ask_cents)
    return int(yes_ask_cents)


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


def _check_zero_eval_watchdog(ts: str, num_markets: int, num_evaluated: int, db=None) -> None:
    """Watchdog for silent zero-evaluation ticks: alert when N consecutive polls
    produce zero evaluated brackets while markets are listed.

    This catches the recurring daily ~12h blackout (issue #686, #687) and any
    other failure modes where markets exist but nothing passes evaluation.

    Args:
        ts: ISO timestamp of this poll
        num_markets: Number of markets fetched (from Polymarket)
        num_evaluated: Number of markets that passed evaluation (len(snapshots))
        db: Database connection for logging guardrail events
    """
    global _zero_eval_consecutive_ticks

    # Read threshold from database (allows dashboard tuning) with fallback
    threshold = 4  # default
    if db is not None:
        try:
            cfg_val = db.get_config("ZERO_EVAL_WATCHDOG_CONSECUTIVE_TICKS")
            if cfg_val is not None:
                threshold = int(cfg_val)
        except (ValueError, TypeError):
            pass

    # Only track when markets exist but nothing evaluated
    if num_markets > 0 and num_evaluated == 0:
        _zero_eval_consecutive_ticks += 1

        if _zero_eval_consecutive_ticks >= threshold:
            log.error(
                "[watchdog] ALERT: %d consecutive polls with zero evaluated brackets (threshold=%d, markets=%d)",
                _zero_eval_consecutive_ticks, threshold, num_markets
            )
            # Log guardrail event for dashboard visibility
            if db is not None:
                try:
                    db.log_guardrail_event(
                        ts, "GLOBAL", "zero_eval_ticks",
                        float(num_markets), float(_zero_eval_consecutive_ticks),
                    )
                except Exception as e:
                    log.warning("[watchdog] guardrail event write failed: %s", e)
    else:
        # Markets available and something evaluated, or no markets at all — reset counter
        if _zero_eval_consecutive_ticks > 0:
            log.info("[watchdog] zero-eval counter reset (was %d, now %d markets, %d evaluated)",
                     _zero_eval_consecutive_ticks, num_markets, num_evaluated)
        _zero_eval_consecutive_ticks = 0


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


# Hourly-dedup cache for bracket-evaluation snapshots (#826).  Keys are
# (station, ticker) tuples; values are the last hour-bucket string written
# (ISO-8601 truncated to hour, e.g. "2026-07-24T14").  When a snap lands in the
# same hour as the last write for that (station, ticker), it is silently
# skipped to keep volume at ~8K rows/day instead of ~66K.
_last_bracket_hour: "dict[tuple[str, str], str]" = {}


def _write_bracket_evaluations(
    snapshots: "list[dict]",
    has_live_trader: bool,
) -> None:
    """Persist hourly-deduped bracket-evaluation snapshots (#826).

    One row per (station, ticker, hour) -- the first evaluation within each
    hour bucket.  Written to a rotated, gzip-compressed JSONL file with 90-day
    retention.  This is the unbiased full-population store the Brier skill test
    (#822) will consume.

    Additive only -- does not touch any other log stream, the scan_decisions
    table, or the Edge tab.
    """
    for snap in snapshots:
        station = snap.get("station")
        ticker = snap.get("ticker")
        if not station or not ticker:
            continue

        # Hour bucket from the snap's poll_ts (ISO "2026-07-24T14:03:00" → "2026-07-24T14").
        poll_ts = snap.get("poll_ts", "")
        hour_bucket = poll_ts[:13] if len(poll_ts) >= 13 else poll_ts
        if not hour_bucket:
            continue

        key = (station, ticker)
        if _last_bracket_hour.get(key) == hour_bucket:
            continue  # already wrote one row for this (station, ticker, hour)
        _last_bracket_hour[key] = hour_bucket

        # Paper-mode override: scanner sets "live" when ANY side is enabled;
        # downgrade to "paper" when the run has no live trader.
        exec_mode = snap.get("execution_mode", "live")
        if exec_mode == "live" and not has_live_trader:
            exec_mode = "paper"

        row = {
            "station": station,
            "ticker": ticker,
            "bracket_low": snap.get("bracket_low"),
            "bracket_high": snap.get("bracket_high"),
            "poll_ts": hour_bucket + ":00:00+00:00",
            "yes_ask": snap.get("yes_ask"),
            "no_ask": snap.get("no_ask"),
            "p_yes": snap.get("p_yes"),
            "p_yes_raw": snap.get("raw_p_yes"),
            "emos_mode": snap.get("emos_mode"),
            "is_next_day": snap.get("is_next_day"),
            "minutes_to_settlement": snap.get("minutes_to_settlement"),
            "execution_mode": exec_mode,
            "settlement_date": snap.get("settlement_date"),
        }

        LOG_DIR.mkdir(exist_ok=True)
        dest = rotated_path(BRACKET_EVALS_JSONL)
        housekeep(BRACKET_EVALS_JSONL, retain_days=BRACKET_EVAL_RETAIN_DAYS)
        with _write_lock:
            with open(dest, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")


def _persist_scan_decisions(
    decisions: "dict[str, dict]",
    confirmed: "dict[str, tuple[str, str | None]]",
    live_trader,
    db,
) -> None:
    """Upsert this poll's per-bracket gate verdicts to scan_decisions (issue #756).

    *decisions* is keyed by ticker, built from scan_markets()'s snapshots --
    each already carries a scanner-side gate_verdict (see scan_markets'
    docstring / GATE_VERDICTS). *confirmed* carries the run.py-side overrides
    collected while resolving the entry-guard/execution seam this poll, keyed
    by ticker -> (verdict, detail); it takes precedence over the scanner's
    verdict when present.

    Any bracket left at the scanner's "traded_live" placeholder (a live
    candidate pending execution) that was never confirmed one way or the
    other this poll -- e.g. the wallet-cooldown skip, or the balance-
    insufficient break leaving later candidates unvisited -- is downgraded to
    entry_guard here so the table never claims a live trade that did not
    happen. Paper-mode polls (no live_trader) have no entry-guard/execution
    seam at all, so their placeholder is left as-is (best-effort "would trade
    live" reading, not a confirmed exchange fill).

    Every row also gets ``execution_mode`` (issue #780): ``'live'`` when this
    poll had a live trader configured, ``'paper'`` otherwise. Combined with
    the downgrade above, this is what lets a reader trust a persisted
    ``traded_live`` + ``execution_mode='live'`` row as a confirmed exchange
    fill, vs. ``traded_live`` + ``execution_mode='paper'``, which is only the
    scanner's unconfirmed "would trade live" placeholder.
    """
    if db is None:
        return
    execution_mode = "live" if live_trader else "paper"
    for ticker, decision in decisions.items():
        if ticker in confirmed:
            verdict, detail = confirmed[ticker]
            decision["gate_verdict"] = verdict
            decision["gate_detail"] = detail
        elif live_trader and decision.get("gate_verdict") == "traded_live":
            decision["gate_verdict"] = "entry_guard"
            decision["gate_detail"] = "did not reach execution this poll"
        decision["execution_mode"] = execution_mode
        try:
            db.upsert_scan_decision(**decision)
        except Exception as e:
            log.warning("[scan_decisions] upsert failed for %s...: %s", ticker[:20], e)


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
                size_eur=float(record["size_eur"]) if record.get("size_eur") is not None else None,  # issue #746
                end_date=record.get("end_date") or None,
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

    # Shared METAR fetch cache for this poll only: the high-side and low-side
    # builders below both need METAR observations for overlapping stations.
    # Passing the same dict into both means a station fetched by one builder
    # is reused by the other instead of hitting aviationweather.gov twice per
    # poll for the same station (issue #582). See _get_metars_for_station()
    # in src/weather/builder.py.
    _metars_cache: dict = {}

    weather_health: list = []
    weather = _build_weather(db=db, health_out=weather_health, metars_cache=_metars_cache)
    _dashboard_module.weather_health = weather_health  # surface feed health to the dashboard banner

    # Low-side shadow scan (Epic C, issue #457) -- shadow-only, never gates live
    # entries (see scanner.py's low-side block). Shares _metars_cache with the
    # high-side build above (issue #582).
    #
    # Issue #733 rollback: skip building low-side states entirely when
    # ENABLE_LOW_MARKETS is off (default) -- scan_markets() gates the low-side
    # block authoritatively from the same live config; this just avoids the
    # per-poll build cost for states that would never be scored.
    _low_raw = db.get_config("ENABLE_LOW_MARKETS") if db is not None else None
    if _low_raw is None:
        _low_raw = os.getenv("ENABLE_LOW_MARKETS", str(CONFIG_DEFAULTS["ENABLE_LOW_MARKETS"]))
    if str(_low_raw).strip().lower() in ("true", "1", "yes"):
        weather_low = build_weather_low_for_scanning(db=db, metars_cache=_metars_cache)
    else:
        weather_low = {}

    # Climb-table METAR persistence (issue #669) -- runs every poll regardless
    # of STATION_ACTIVE_HOURS or open positions, for the stations named in
    # config.CLIMB_BUILDER_24H_METAR_STATIONS. Does NOT feed the scanner or
    # pricer; it only ensures Database.get_hourly_obs_for_climb() has
    # early-local-morning METAR rows to work with when the climb table is
    # regenerated. Shares _metars_cache so it never double-fetches a station
    # already fetched by the builders above (issue #582 pattern).
    if db is not None:
        persist_metar_for_climb_stations(db=db, metars_cache=_metars_cache)

    # Collect open-position token IDs early so they can be included in the
    # batch orderbook fetch below (together with the scanner's YES/NO tokens).
    _open_token_ids: list = []
    _open_positions: list = []
    if live_trader:
        _today = datetime.now(timezone.utc).date().isoformat()
        _open_positions = _load_open_no_positions(_today, db=db)
        _open_token_ids = [p["no_token_id"] for p in _open_positions if p.get("no_token_id")]

    # Build always-on pricing weather for open positions (issue #425).
    # The scanner weather is gated by STATION_ACTIVE_HOURS to prevent bracket-blanketing
    # (KHOU 2026-05-27 incident).  Re-pricing held positions must work 24/7 — METARs
    # flow around the clock and stop-loss/take-profit must not go blind overnight.
    # Limit to open-position stations only to avoid unnecessary upstream API calls.
    _pricing_weather: dict = {}
    if live_trader and _open_positions:
        from src.config import STATIONS as _ALL_STATIONS
        _station_meta: dict = {s[0]: s for s in _ALL_STATIONS}
        _open_station_codes = {p["station"] for p in _open_positions if p.get("station")}
        _open_station_tuples = [
            _station_meta[code]
            for code in _open_station_codes
            if code in _station_meta
        ]
        if _open_station_tuples:
            _pricing_weather = build_weather_for_pricing(_open_station_tuples, db=db)
            # Merge scanner weather as fallback to avoid duplicate API calls for
            # stations already active in the scanner (setdefault = pricer wins).
            for _st, _ws in weather.items():
                _pricing_weather.setdefault(_st, _ws)

    if not weather:
        # No weather data: still run snapshots (bid line is weather-independent)
        # then bail out of the market scan.
        if live_trader:
            _snap_ob: dict = {}
            if _open_token_ids:
                _snap_ob = fetch_orderbooks_batch(_open_token_ids)
            # Use _pricing_weather (always-on) so stop-loss works overnight (#425)
            position_states = _log_open_position_snapshots(_pricing_weather, ts, db=db, orderbooks=_snap_ob)
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
            position_states = _log_open_position_snapshots(_pricing_weather, ts, db=db, orderbooks=_snap_ob2)
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
    # held positions get fair-value updates and stop-loss/take-profit fire even
    # when the station is outside its STATION_ACTIVE_HOURS scanner window.
    # scan_markets still uses `weather` (active-hours gated) — the KHOU
    # 2026-05-27 regression guard is preserved.
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

    candidates, snapshots = scan_markets(
        weather, markets, db=db, orderbooks=shared_orderbooks,
        weather_low=weather_low, prob_low_fn=true_probability_low_in_bracket,
    )

    # Check zero-evaluation watchdog (issue #686)
    _check_zero_eval_watchdog(ts, len(markets), len(snapshots), db=db)

    for snap in snapshots:
        _append_snapshot(snap)

    # Persist hourly-deduped bracket-evaluation snapshots (#826): the unbiased
    # full-population store consumed by the Brier skill test (#822).  Additive
    # only -- scan_decisions and the Edge tab are untouched.
    _write_bracket_evaluations(snapshots, has_live_trader=live_trader is not None)

    # scan_decisions (issue #756): one entry per evaluated high-side bracket,
    # keyed by ticker, seeded from the scanner's per-bracket gate_verdict.
    # _confirmed collects run.py-side overrides (entry-guard / execution
    # outcome) as the candidate loop below resolves them; _persist_scan_decisions
    # applies both and upserts at every poll-exit point from here on.
    _decisions: dict = {
        snap["ticker"]: snap for snap in snapshots if snap.get("gate_verdict") is not None
    }
    _confirmed: dict = {}

    # Log freshness for high-frequency sources (skip METAR outside active window).
    if db is not None:
        _freshness_monitor = FreshnessMonitor()
        for city_sources in [get_source_priority(c) for c in ["Tokyo", "Seoul", "Busan", "Singapore"]]:
            active_sources = [
                s for s in city_sources
                if s["source"] != "metar" or _station_in_active_window(s["station"])
            ]
            _freshness_monitor.check_all(db, active_sources)

    # Watchdog: alert if the forecast-capture job (separate oneshot systemd
    # unit, src/scripts/capture_forecasts.py) has gone silently stale (issue
    # #717). Read-only -- observes model_forecast_log via db, never touches
    # the capture process itself.
    if db is not None:
        from src.monitoring.capture_staleness import check_capture_staleness
        check_capture_staleness(db)

    global _balance_fail_count, _wallet_cooldown_until

    # Wallet-empty cooldown: skip the entire candidate loop until the window passes.
    if live_trader and time.time() < _wallet_cooldown_until:
        log.info("[balance] wallet-empty cooldown active (%.0fs remaining) — skipping placement",
                 _wallet_cooldown_until - time.time())
        _persist_scan_decisions(_decisions, _confirmed, live_trader, db)
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
    n_guard_blocked = 0
    # Entry-guard same-poll dedup (issue #611): (station, ticker, side, day)
    # keys that already passed the gate in THIS scan. If the scanner flags the
    # same bracket twice in one poll, only the first candidate passes.
    _entry_keys_this_poll: set = set()
    for _cand_idx, cand in enumerate(candidates):
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
            "p_yes_raw": round(cand.p_yes_raw, 4) if cand.p_yes_raw is not None else None,
            "ev_yes": round(cand.ev_yes, 2),
            "ev_no": round(cand.ev_no, 2),
            "ev_yes_raw": round(cand.ev_yes_raw, 2) if cand.ev_yes_raw is not None else None,
            "ev_no_raw": round(cand.ev_no_raw, 2) if cand.ev_no_raw is not None else None,
            "flagged_side": cand.side,
            "flagged_edge": round(cand.edge_cents, 2),
            "flagged_price": cand.price_cents,
            "flagged_confidence": round(cand.confidence, 4),
            "minutes_to_settlement": round(cand.minutes_to_settlement, 1),
        }
        _append_candidate(row)

        # Persist to the candidates table alongside the CSV write (issue #684).
        # The CSV had been the only candidate surface in production, leaving
        # the DB `candidates` table (and its p_yes_raw column, #564) orphaned.
        # This is a write-path addition only -- it does not affect what gets
        # evaluated or acted on (entry gates read `candidates`/`cand` in memory,
        # never this table).
        if db is not None:
            try:
                db.insert_candidate(
                    ts=ts,
                    station=cand.station,
                    ticker=cand.bracket.ticker,
                    bracket_low=cand.bracket.low_f,
                    bracket_high=cand.bracket.high_f,
                    side=cand.side,
                    predicted_price=round(cand.confidence * 100),
                    predicted_edge=round(cand.edge_cents, 2),
                    market_price=cand.price_cents,
                    confidence=round(cand.confidence, 4),
                    minutes_to_settlement=round(cand.minutes_to_settlement, 1),
                    direction=cand.direction,
                    p_yes_raw=cand.p_yes_raw,
                    is_next_day=int(cand.is_next_day),
                    today_position_open=int(cand.today_position_open),
                )
            except Exception as e:
                log.warning("  [candidates] DB insert failed: %s", e)

        # Shadow candidates: log to trades table as observation only.
        # No capital reserved, no order placed, no risk_manager gates checked.
        if cand.shadow:
            n_shadow += 1
            if db is not None:
                # Issue #737: store the cost of the side actually "bought" so
                # settle_shadow_trades() (which prices actual_price as the
                # bought-side cost) is correct for BOTH sides -- NO rows carry
                # the NO ask (~78c), not the YES ask (~22c).
                shadow_cost_cents = _shadow_bought_side_cost_cents(
                    cand.side,
                    cand.bracket.yes_ask_cents,
                    getattr(cand.bracket, "no_ask_cents", None),
                )
                try:
                    _row_id, _created = db.upsert_shadow_trade(
                        ts=ts,
                        station=cand.station,
                        ticker=cand.bracket.ticker,
                        bracket_low=cand.bracket.low_f,
                        bracket_high=cand.bracket.high_f,
                        side=cand.side,
                        predicted_price=int(round(cand.p_yes * 100)),
                        actual_price=shadow_cost_cents,
                        predicted_edge=cand.edge_cents,
                        capital_before=0.0,
                        direction=cand.direction,
                        p_yes_raw=cand.p_yes_raw,
                        is_next_day=int(cand.is_next_day),
                    )
                    if _created:
                        log.info(
                            "  [shadow] logged %s candidate %s @ %sc bought-side cost (no order placed)",
                            cand.side, cand.bracket.ticker[:14], shadow_cost_cents,
                        )
                    else:
                        log.debug(
                            "  [shadow] dedup: updated actual_price=%sc for %s %s (today already logged)",
                            shadow_cost_cents, cand.side, cand.bracket.ticker[:14],
                        )
                except Exception as e:
                    log.warning("  [shadow] DB upsert failed: %s", e)
            continue

        # ---- Entry gate (issue #611): never stack the same bracket ----
        # Live mode only: shadow candidates never reach here (continue above)
        # and paper mode places no orders. Blocks a candidate when:
        #   1. the same (station, ticker, side, day) key already passed the
        #      gate earlier in THIS poll (same-scan duplicate), or
        #   2. an OPEN position exists for the candidate's token
        #      (open_positions.token_id -- always blocks, in every config), or
        #   3. LIVE_ALLOW_BRACKET_REENTRY is false (default) and ANY live
        #      trade row already exists today for the key -- filled, sold,
        #      or timeout attempt all count (repeated timeout retries were
        #      part of the observed stacking).
        # Day semantics: the candidate's market end_date (matching how
        # has_live_trade_today() resolves a trade row's day, mirroring
        # settle.resolve_trade_date from #609), falling back to today's UTC
        # date when the market carries no endDate.
        if live_trader:
            _cand_day = row["end_date"] or datetime.now(timezone.utc).date().isoformat()
            _entry_key = (cand.station, cand.bracket.ticker, cand.side, _cand_day)
            _cand_token = (
                cand.bracket.yes_token_id if cand.side == "YES"
                else cand.bracket.no_token_id
            )
            _guard_reason = None
            if _entry_key in _entry_keys_this_poll:
                _guard_reason = "duplicate candidate for this bracket in the same poll"
            elif db is not None:
                try:
                    if _cand_token and db.get_open_position_by_token(_cand_token):
                        _guard_reason = "open position already exists for this token"
                    elif not LIVE_ALLOW_BRACKET_REENTRY and db.has_live_trade_today(
                        cand.station, cand.bracket.ticker, cand.side, _cand_day,
                    ):
                        _guard_reason = (
                            "already placed a live order for this bracket today "
                            "(LIVE_ALLOW_BRACKET_REENTRY=false)"
                        )
                except Exception as e:
                    # Fail open: a DB read glitch must not freeze all trading.
                    # order_manager._open_orders still prevents same-session
                    # GTC stacking at the execution seam.
                    log.warning("[entry-guard] DB check failed (allowing candidate): %s", e)
            if _guard_reason:
                n_guard_blocked += 1
                log.warning(
                    "[entry-guard] blocked %s %s %s...: %s",
                    cand.station, cand.side, cand.bracket.ticker[:14], _guard_reason,
                )
                _confirmed[cand.bracket.ticker] = ("entry_guard", _guard_reason)
                if db is not None:
                    try:
                        # Counter surface for the dashboard: rides the existing
                        # guardrail_events table + /api/guardrail-events endpoint.
                        db.log_guardrail_event(
                            ts, cand.station, "entry_guard_block",
                            float(cand.price_cents), float(cand.price_cents),
                            ticker=cand.bracket.ticker,
                        )
                    except Exception as e:
                        log.debug("[entry-guard] guardrail event write failed: %s", e)
                continue
            _entry_keys_this_poll.add(_entry_key)

        if live_trader and available_usdc < POSITION_SIZE_WITH_FEES:
            log.info("  [balance] insufficient (%.2f USDC < %.2f needed incl. fees), skipping remaining", available_usdc, POSITION_SIZE_WITH_FEES)
            _wallet_cooldown_until = time.time() + _WALLET_EMPTY_COOLDOWN_SECONDS
            # scan_decisions (issue #756): this candidate AND every later
            # non-shadow candidate in this poll never reach the entry-guard/
            # execution seam below -- mark them all so their row doesn't keep
            # the scanner's optimistic "traded_live" placeholder.
            for _c in candidates[_cand_idx:]:
                if not _c.shadow:
                    _confirmed.setdefault(
                        _c.bracket.ticker,
                        ("entry_guard", "insufficient USDC balance to place order this poll"),
                    )
            break

        raw_liquidity = cand.bracket.yes_ask_size + cand.bracket.no_ask_size
        liquidity = raw_liquidity if raw_liquidity > 0 else 9999
        allowed, reason = risk_manager.allow_trade(
            capital=STARTING_CAPITAL_EUR,
            liquidity_contracts=liquidity,
        )
        if not allowed:
            log.info("  [risk] blocked: %s", reason)
            _confirmed[cand.bracket.ticker] = ("entry_guard", f"blocked by risk manager: {reason}")
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
            futures = {
                executor.submit(_execute_live, cand, live_trader._client_factory, risk_manager, ts, db, available_usdc): cand
                for cand in approved
            }
            for future in as_completed(futures):
                cand = futures[future]
                try:
                    outcome = future.result()
                except Exception as e:
                    log.error("  [live] thread error: %s", e, exc_info=True)
                    outcome = None
                # scan_decisions verdict seam (issue #756): 'filled' is the only
                # outcome that confirms a real live trade -- everything else
                # (timeout/cancelled/place_failed/no_token_id/already_open/an
                # in-thread exception) downgrades to timeout_today, with the
                # raw outcome kept in gate_detail for diagnostics.
                if outcome == "filled":
                    _confirmed[cand.bracket.ticker] = ("traded_live", None)
                else:
                    _confirmed[cand.bracket.ticker] = (
                        "timeout_today",
                        f"execution outcome: {outcome}" if outcome else "execution attempt raised an exception",
                    )

    _persist_scan_decisions(_decisions, _confirmed, live_trader, db)

    log.info(
        "[scan] %s markets, %s evaluated, %s candidates, %s acted on, %s shadow, %s entry-guard blocked",
        len(markets), len(snapshots), len(candidates), n_acted, n_shadow, n_guard_blocked,
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
    # AmosCollector is retired (issue #740) -- METAR RKSI/RKPK is now the sole
    # Korea observation truth feed. run_loop() is a no-op that logs once and
    # returns; left wired (rather than removed) to match test_run_wiring.py's
    # "all four collector threads start" expectation and avoid a scheduler change.
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
