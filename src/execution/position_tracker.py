"""Position tracking helpers — snapshot logging and exit checks.

These functions were extracted from src.scripts.run (issue #210) to keep
run.py focused on the polling loop.  They are re-exported from run.py so
existing callers and tests that reference src.scripts.run.* continue to work.

Design notes:
- _log_open_position_snapshots and _check_stop_loss_exits are called from
  poll_once() in run.py and receive the module-level order_manager instance
  as a parameter to avoid a circular import.
- _check_metar_exits is currently disabled in poll_once() (2026-05-29) but
  kept here for future re-enablement.
- _append_live_trade stays in run.py (the coordinator); it is imported here
  via a lazy import inside each function to avoid a module-level circular
  dependency (run → position_tracker → run).
"""
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone

from src.utils.log_rotation import rotated_path, housekeep, SNAPSHOT_RETAIN_DAYS
from src.config import (
    LOG_DIR,
    POSITION_SNAPSHOTS_JSONL,
    FORECAST_STDDEV_F,
    STOP_LOSS_MIN_BID_CENTS,
    STOP_LOSS_CONSECUTIVE_POLLS,
    STOP_LOSS_MIN_DEPTH_SHARES,
    STOP_LOSS_SELL_AGGRESSION_CENTS,
    STOP_LOSS_MIN_BRACKET_PROXIMITY_F,
    STOP_LOSS_RESPECT_FORECAST_OVERSHOOT,
    FORCE_EXIT_MINUTES_TO_SETTLEMENT,
)
from src.data.polymarket import get_orderbook
from src.model.envelope import Bracket, true_probability_yes
from src.model.climb_rates import expected_additional_rise
from src.execution.order_manager import (
    _load_open_no_positions, _record_sell_in_db, order_manager as _order_manager,
)
from src.strategy.fee import estimate_fee_cents

# Separate lock for POSITION_SNAPSHOTS_JSONL writes (not shared with run._write_lock
# which protects CANDIDATES_CSV and LIVE_TRADES_JSONL).
_write_lock = threading.Lock()

log = logging.getLogger(__name__)


def _log_open_position_snapshots(
    weather: dict,
    ts: str,
    db=None,
    orderbooks: "dict[str, dict] | None" = None,
) -> list:
    """Write one snapshot per open NO position per poll, capturing weather +
    orderbook + live model probability.

    snapshots.jsonl only contains rows for markets the scanner evaluates, which
    drops to zero past noon UTC each day.  This logger runs every poll for
    every open position regardless of market scan state -- gives us continuous
    intra-day data (especially the 14-22 UTC peak window) needed to backtest
    exit strategies against real intra-day orderbook movement.

    Args:
        weather: dict mapping station code → WeatherState.
        ts: ISO timestamp string for this poll.
        db: optional Database instance.
        orderbooks: optional pre-fetched dict mapping token_id → orderbook dict.
            When provided, the cached result is used instead of calling
            ``get_orderbook()`` per token, avoiding duplicate HTTP requests
            within a single poll cycle. When ``None``, falls back to per-token
            ``get_orderbook()`` calls (original behaviour).

    Returns a list of per-token position states ({token_id, fills, snap}) so
    the stop-loss check can reuse the orderbook + model evaluation without a
    second round of API calls.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today, db=db)
    if not open_positions:
        return []

    by_token: dict = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    LOG_DIR.mkdir(exist_ok=True)
    position_states: list = []
    for token_id, fills in by_token.items():
        first = fills[0]
        station = first.get("station", "")
        bracket_low = first.get("bracket_low")
        bracket_high = first.get("bracket_high")
        if bracket_low is None or bracket_high is None:
            continue

        # When weather is missing for this station the model line cannot be
        # computed, but the market bid (orderbook) is weather-independent -- we
        # still write a snapshot so the dashboard chart keeps updating and the
        # gap is visible (fair_value paused, weather_missing flagged) rather
        # than the chart silently going stale.  See issue: weather failure
        # silencing stop-loss / the 8h chart gap.
        state = weather.get(station)
        weather_missing = state is None

        # Fetch live orderbook for the NO token — use the shared pre-fetched
        # dict when available to avoid a redundant HTTP request per token.
        no_no_bid = no_no_ask = None
        no_best_bid_size = None
        try:
            if orderbooks is not None:
                ob = orderbooks.get(token_id) or {}
            else:
                ob = get_orderbook(token_id)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if bids:
                best = max(bids, key=lambda b: float(b["price"]))
                no_no_bid = max(1, min(99, round(float(best["price"]) * 100)))
                no_best_bid_size = float(best.get("size") or 0)
            if asks:
                no_no_ask = max(1, min(99, round(min(float(a["price"]) for a in asks) * 100)))
        except Exception as e:
            if "404" in str(e):
                n = db.close_positions_by_token(token_id) if db is not None else 0
                log.info("  [snap] %s... market resolved -- removed %s row(s) from open_positions", token_id[:14], n)
                continue
            log.warning("  [snap] orderbook %s... error: %s", token_id[:14], e)

        # Live p_yes by re-running the envelope model with current state.
        # Skipped when weather is missing -- fair_value pauses on the chart.
        p_yes_now = None
        fair_value_now = None
        if state is not None:
            try:
                bracket_stub = Bracket(
                    ticker=first.get("ticker", ""),
                    low_f=float(bracket_low),
                    high_f=float(bracket_high),
                    yes_ask_cents=50, yes_ask_size=0,
                    no_ask_cents=50, no_ask_size=0,
                )
                p_yes_now = true_probability_yes(bracket_stub, state)
                fair_value_now = max(1, min(99, round((1 - p_yes_now) * 100)))
            except Exception as e:
                log.warning("  [snap] model eval %s %s-%s error: %s", station, bracket_low, bracket_high, e)
                p_yes_now = None
                fair_value_now = None

        snap = {
            "ts": ts,
            "ticker": first.get("ticker", ""),
            "no_token_id": token_id,
            "station": station,
            "bracket_low": bracket_low,
            "bracket_high": bracket_high,
            "entry_price": first.get("price_cents"),
            "predicted_price": first.get("predicted_price"),
            "current_high": state.current_high_f if state is not None else None,
            "latest_temp": state.latest_temp_f if state is not None else None,
            "forecast_nws": state.forecast_high_f if state is not None else None,
            "forecast_secondary": state.secondary_forecast_f if state is not None else None,
            "no_best_bid": no_no_bid,
            "no_best_bid_size": no_best_bid_size,
            "no_best_ask": no_no_ask,
            "p_yes_now": round(p_yes_now, 4) if p_yes_now is not None else None,
            "fair_value_now": fair_value_now,
            "weather_missing": weather_missing,
        }
        with _write_lock:
            dest = rotated_path(POSITION_SNAPSHOTS_JSONL)
            housekeep(POSITION_SNAPSHOTS_JSONL, retain_days=SNAPSHOT_RETAIN_DAYS)
            with open(dest, "a") as f:
                f.write(json.dumps(snap, default=str) + "\n")
        position_states.append({"token_id": token_id, "fills": fills, "snap": snap})
    return position_states


def _check_stop_loss_exits(live_trader, ts: str, position_states: list,
                           db=None, risk_manager=None) -> None:
    """Exit NO positions when the live model itself no longer supports the entry.

    Trigger: fair_value_now (re-running the envelope model on current weather)
    drops below the volume-weighted average entry price for
    STOP_LOSS_CONSECUTIVE_POLLS consecutive polls.  This is a MODEL-confidence
    stop, not a price stop -- price-based stops were backtested twice (May 27 -
    Jun 4 and May 27 - Jun 12) and were net harmful at every threshold because
    winning positions routinely dip to 10-30c before recovering.  The model
    stop instead fires while the bid is still high (median exit bid ~70c in
    backtest), cutting the -5 EUR full-stake loss tail roughly by a third at
    ~zero EV cost (issue #177).

    Guards before selling:
    - bid floor: never sell below STOP_LOSS_MIN_BID_CENTS -- below that the
      loss is mostly realised already and recovery upside dominates;
    - depth: best-bid size must cover STOP_LOSS_MIN_DEPTH_SHARES so the fill
      is real, not a 1-share phantom quote.

    Execution is immediate-or-cancel via sell_position_immediate(): the limit
    is priced *through* the best bid so it crosses instantly; if it does not
    match it is cancelled at once.  We never leave a resting sell order that a
    falling market could strand unfilled -- unfilled attempts simply retry on
    the next poll while strikes persist.
    """
    # Lazy import to avoid a module-level circular dependency
    # (run → position_tracker → run). By call time run.py is fully loaded.
    from src.scripts.run import _append_live_trade  # noqa: PLC0415

    today = datetime.now(timezone.utc).date().isoformat()
    for ps in position_states:
        token_id = ps["token_id"]
        fills = ps["fills"]
        snap = ps["snap"]
        if token_id in _order_manager._sold_positions:
            continue

        fair = snap.get("fair_value_now")
        bid = snap.get("no_best_bid")
        depth = snap.get("no_best_bid_size")
        if fair is None:
            continue

        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        avg_entry_cents = sum(
            f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
            for f in fills
        ) / total_shares

        if fair >= avg_entry_cents:
            _order_manager._stop_loss_strikes.pop(token_id, None)
            continue

        strikes = _order_manager._stop_loss_strikes.get(token_id, 0) + 1
        _order_manager._stop_loss_strikes[token_id] = strikes
        station = fills[0]["station"]
        bracket_low = fills[0]["bracket_low"]
        bracket_high = fills[0]["bracket_high"]
        if strikes < STOP_LOSS_CONSECUTIVE_POLLS:
            log.info(
                "  [sl] [%s] %.0f-%.0fF fair %sc < entry %.0fc -- strike %s/%s",
                station, bracket_low, bracket_high, fair, avg_entry_cents,
                strikes, STOP_LOSS_CONSECUTIVE_POLLS,
            )
            continue

        # Proximity guard: when the running daily high is still well below the
        # bracket, an intraday fair-value crash is usually the model panicking
        # on a temp spike that ends up overshooting. Hold; strikes persist so
        # we can fire fast once the temp actually nears the bracket.
        cur_high = snap.get("current_high")
        if (STOP_LOSS_MIN_BRACKET_PROXIMITY_F > 0
                and cur_high is not None
                and cur_high < bracket_low - STOP_LOSS_MIN_BRACKET_PROXIMITY_F):
            log.info(
                "  [sl] [%s] %.0f-%.0fF triggered but current_high %.1fF still %.1fF"
                " below bracket_low (> %.1fF buffer) -- holding",
                station, bracket_low, bracket_high, cur_high,
                bracket_low - cur_high, STOP_LOSS_MIN_BRACKET_PROXIMITY_F,
            )
            continue

        # Overshoot guard: if any available forecast predicts the daily high
        # to break above bracket_high, NO wins on overshoot -- the path through
        # the bracket on the way higher is exactly the winning scenario.
        forecast = snap.get("forecast_nws")
        if forecast is None:
            forecast = snap.get("forecast_secondary")
        if (STOP_LOSS_RESPECT_FORECAST_OVERSHOOT
                and forecast is not None
                and bracket_high < 200
                and forecast > bracket_high):
            log.info(
                "  [sl] [%s] %.0f-%.0fF triggered but forecast %.1fF > bracket_high"
                " %.0fF (overshoot expected) -- holding",
                station, bracket_low, bracket_high, forecast, bracket_high,
            )
            continue

        if bid is None or bid < STOP_LOSS_MIN_BID_CENTS:
            log.info(
                "  [sl] [%s] %.0f-%.0fF triggered (fair %sc < entry %.0fc) but bid %s < floor %sc -- holding",
                station, bracket_low, bracket_high, fair, avg_entry_cents,
                bid, STOP_LOSS_MIN_BID_CENTS,
            )
            continue
        if depth is None or depth < STOP_LOSS_MIN_DEPTH_SHARES:
            log.warning(
                "  [sl] [%s] %.0f-%.0fF triggered but best-bid depth %s < %s shares -- skipping this poll",
                station, bracket_low, bracket_high, depth, STOP_LOSS_MIN_DEPTH_SHARES,
            )
            continue

        total_eur = sum(f["size_eur"] for f in fills)
        question = fills[0].get("question", "")

        # Account for any shares already sold via previous partial fills.
        already_sold = _order_manager._partial_fill_shares.get(token_id, 0.0)
        remaining_shares = total_shares - already_sold
        min_lot = float(os.environ.get("STOP_LOSS_MIN_LOT_SHARES", "0.5"))
        if remaining_shares < min_lot:
            log.info(
                "  [sl] [%s] %.0f-%.0fF remaining shares %.4f < min lot %.4f after partial fills"
                " -- skipping dust sell",
                station, bracket_low, bracket_high, remaining_shares, min_lot,
            )
            _order_manager._sold_positions.add(token_id)
            _order_manager._stop_loss_strikes.pop(token_id, None)
            _order_manager._partial_fill_shares.pop(token_id, None)
            if db is not None:
                db.close_positions_by_token(token_id)
            continue

        log.info(
            "  [sl] [%s] %.0f-%.0fF model fair %sc < entry %.0fc for %s polls, bid %sc"
            " -- selling %.4f shares (%.4f already sold)",
            station, bracket_low, bracket_high, fair, avg_entry_cents,
            strikes, bid, remaining_shares, already_sold,
        )
        try:
            result = live_trader.sell_position_immediate(
                token_id, remaining_shares, STOP_LOSS_SELL_AGGRESSION_CENTS,
            )
            sell_id, sell_price_or_order = result
            if sell_id is None:
                # Order was cancelled (may have partially filled before cancel).
                # Query fill size so the next retry sells only the true remainder.
                cancelled_order_id = sell_price_or_order
                if cancelled_order_id:
                    partial = live_trader.get_order_fill_size(cancelled_order_id)
                    if partial > 0:
                        _order_manager._partial_fill_shares[token_id] = already_sold + partial
                        log.info(
                            "  [sl] partial fill %.4f shares on %s... -- tracking remainder",
                            partial, cancelled_order_id[:12],
                        )
                # Strikes persist so the very next poll retries at the then-current bid.
                continue
            sell_price_cents = sell_price_or_order
            sold_shares = remaining_shares
            _order_manager._sold_positions.add(token_id)
            _order_manager._stop_loss_strikes.pop(token_id, None)
            _order_manager._partial_fill_shares.pop(token_id, None)
            if db is not None:
                db.close_positions_by_token(token_id)
            pnl = round((sell_price_cents - avg_entry_cents) / 100 * sold_shares, 4)
            _record_sell_in_db(
                fills, sell_price_cents, ts, db=db,
                close_reason="stop_loss",
                bid_depth=int(depth) if depth is not None else None,
            )
            if risk_manager is not None:
                risk_manager.record_pnl(pnl)
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
                "shares": round(sold_shares, 4),
                "size_eur": total_eur,
                "edge_cents": 0,
                "pnl": pnl,
                "outcome": "sold",
                "actual_fee_cents": estimate_fee_cents(sell_price_cents),
                "close_reason": "stop_loss",
                "trigger": f"stop_loss@{bid}c_fair{fair}c_entry{round(avg_entry_cents)}c",
            }, db=db)
            log.info(
                "  [sl] sell %s... filled >= %sc -- [%s] %.0f-%.0fF NO  pnl=%+.2f",
                sell_id[:12], sell_price_cents, station, bracket_low, bracket_high, pnl,
            )
        except Exception as e:
            log.warning("  [sl] sell failed for [%s] %.0f-%.0fF: %s", station, bracket_low, bracket_high, e)


def _check_forced_exits(
    live_trader,
    ts: str,
    position_states: list,
    db=None,
    risk_manager=None,
    force_exit_minutes: "int | None" = None,
) -> None:
    """Force-close NO positions within *force_exit_minutes* of settlement.

    When a position's settlement is within FORCE_EXIT_MINUTES_TO_SETTLEMENT
    minutes AND the NO bid has depth >= STOP_LOSS_MIN_DEPTH_SHARES, cross the
    spread and close rather than holding to settlement.

    Rationale: live data (2026-06-07..06-16) shows hold-to-settlement is
    net-negative (−€1.72 at 64% win rate) vs early exits (+€29.79 at 90%).
    This function runs in poll_once() BEFORE _check_stop_loss_exits so it has
    priority over the model-confidence stop.

    When FORCE_EXIT_MINUTES_TO_SETTLEMENT=0 this function is a no-op, exactly
    reproducing today's behaviour.

    Args:
        live_trader: LiveTrader instance (None in paper mode → this is a no-op).
        ts: ISO timestamp for this poll.
        position_states: list of {token_id, fills, snap} from
            _log_open_position_snapshots (reuses already-fetched orderbook data).
        db: Database instance (optional).
        risk_manager: RiskManager instance (optional).
        force_exit_minutes: Override the config value (used in tests).
    """
    from src.scripts.run import _append_live_trade  # noqa: PLC0415

    if live_trader is None:
        return

    threshold = force_exit_minutes if force_exit_minutes is not None else FORCE_EXIT_MINUTES_TO_SETTLEMENT
    if threshold <= 0:
        return

    today = datetime.now(timezone.utc).date().isoformat()

    for ps in position_states:
        token_id = ps["token_id"]
        fills = ps["fills"]
        snap = ps["snap"]

        if token_id in _order_manager._sold_positions:
            continue

        # Determine minutes to settlement from the open_positions record.
        # The entry_ts and market end_date are both stored in the fills.
        # We compute minutes remaining using the open_positions.take_profit_cents
        # field doesn't carry settlement time — we need to derive it from market
        # context.  The snap dict doesn't have settlement time either; it comes
        # from the market fetch in scan_markets.  The simplest reliable source is
        # the `entry_ts` and knowing settlement is the market end_date at 23:59 UTC.
        #
        # However open_positions does NOT store end_date.  The safe fallback is
        # to look at fills[0].get("entry_ts") and compute days since entry.
        # Since all positions in MeteoEdge are same-day markets (resolve same UTC
        # day they're created), we estimate minutes to settlement as:
        #   minutes_remaining = (end-of-today-UTC in mins) - (current UTC minute)
        #
        # This is approximate but correct for same-day markets and avoids
        # needing the settlement time in every open_positions row.
        now_utc = datetime.now(timezone.utc)
        # Settlement is end of current UTC day (23:59:59 UTC, approx midnight)
        from datetime import date as _date
        eod_utc = datetime(
            now_utc.year, now_utc.month, now_utc.day, 23, 59, 59,
            tzinfo=timezone.utc,
        )
        minutes_remaining = (eod_utc - now_utc).total_seconds() / 60.0

        if minutes_remaining > threshold:
            continue

        # Check depth at best bid from snapshot data
        depth = snap.get("no_best_bid_size")
        bid = snap.get("no_best_bid")
        if depth is None or depth < STOP_LOSS_MIN_DEPTH_SHARES:
            station_log = fills[0]["station"]
            bracket_low_log = fills[0]["bracket_low"]
            bracket_high_log = fills[0]["bracket_high"]
            log.info(
                "  [fe] [%s] %.0f-%.0fF within %.0f min of settlement but bid depth %s < %s"
                " -- holding (thin book)",
                station_log, bracket_low_log, bracket_high_log,
                minutes_remaining, depth, STOP_LOSS_MIN_DEPTH_SHARES,
            )
            continue

        if bid is None:
            continue

        station = fills[0]["station"]
        bracket_low = fills[0]["bracket_low"]
        bracket_high = fills[0]["bracket_high"]
        question = fills[0].get("question", "")
        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        total_eur = sum(f["size_eur"] for f in fills)
        avg_entry_cents = sum(
            f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
            for f in fills
        ) / total_shares

        log.info(
            "  [fe] [%s] %.0f-%.0fF within %.0f min of settlement (threshold %d min)"
            " bid %sc depth %.0f -- forced exit %.4f shares",
            station, bracket_low, bracket_high, minutes_remaining, threshold,
            bid, depth, total_shares,
        )

        try:
            sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
            _order_manager._sold_positions.add(token_id)
            _order_manager._stop_loss_strikes.pop(token_id, None)
            if db is not None:
                db.close_positions_by_token(token_id)
            pnl = round((sell_price_cents - avg_entry_cents) / 100 * total_shares, 4)
            _record_sell_in_db(
                fills, sell_price_cents, ts, db=db,
                close_reason="forced_exit",
                minutes_to_settlement=round(minutes_remaining, 2),
                bid_depth=int(depth),
            )
            if risk_manager is not None:
                risk_manager.record_pnl(pnl)
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
                "actual_fee_cents": estimate_fee_cents(sell_price_cents),
                "close_reason": "forced_exit",
                "minutes_to_settlement_at_close": round(minutes_remaining, 2),
                "trigger": (
                    f"forced_exit@{sell_price_cents}c_bid{bid}c"
                    f"_depth{int(depth)}_mts{minutes_remaining:.0f}min"
                ),
            }, db=db)
            log.info(
                "  [fe] sell %s... filled @ %sc -- [%s] %.0f-%.0fF NO  pnl=%+.2f",
                sell_id[:12], sell_price_cents, station, bracket_low, bracket_high, pnl,
            )
        except Exception as e:
            err = str(e)
            if "balance" in err.lower() and ("0" in err or "not enough" in err.lower()):
                n = db.close_positions_by_token(token_id) if db is not None else 0
                _order_manager._sold_positions.add(token_id)
                log.info(
                    "  [fe] [%s] %.0f-%.0fF balance=0 -- tokens already gone, removed %s row(s)",
                    station, bracket_low, bracket_high, n,
                )
            else:
                log.warning(
                    "  [fe] sell failed for [%s] %.0f-%.0fF: %s",
                    station, bracket_low, bracket_high, e,
                )


def _check_metar_exits(weather: dict, live_trader, ts: str, db=None, risk_manager=None) -> None:
    """Exit NO positions where the running daily high is inside the bracket
    AND the temperature is unlikely to climb past it.

    The original "any time the temp enters the bracket -> exit" rule fired
    false positives whenever the daily high overshot the bracket on its way
    to a higher peak.  Counter-factual on 10 May exits: 3 were correct
    (saved $7.86), 7 were wrong (lost $18.75) -- net -$10.89 vs holding.

    Reform: combine the in-bracket check with a climb-rate forecast.  Use
    expected_additional_rise(now_local) -- the p95 ceiling on remaining F
    rise from current hour to end-of-day -- and require the upper-bound
    end-of-day high to stay within the bracket before firing.  If the
    pessimistic estimate already exceeds bracket_high, the temperature is
    most likely passing through and NO will still win at settlement.
    """
    from src.scripts.run import _append_live_trade  # noqa: PLC0415

    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today, db=db)
    if not open_positions:
        return

    by_token: dict = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    for token_id, fills in by_token.items():
        if token_id in _order_manager._sold_positions:
            continue

        bracket_low = fills[0]["bracket_low"]
        bracket_high = fills[0]["bracket_high"]
        station = fills[0]["station"]

        if station not in weather:
            continue

        current_high = weather[station].current_high_f
        if not (bracket_low <= current_high <= bracket_high):
            continue

        now_local_val = weather[station].now_local
        remaining_rise = expected_additional_rise(now_local_val)
        climb_ceiling = current_high + remaining_rise
        # Safeguard: when the latest observation is well below the running daily
        # high AND the p95 climb from current latest can't reach it, the day has
        # peaked and temp won't climb above current_high.  Use current_high as
        # the realistic ceiling instead of an unreachable climb estimate.
        latest_temp = weather[station].latest_temp_f
        if latest_temp + remaining_rise < current_high:
            climb_ceiling = current_high
        nws_forecast = weather[station].forecast_high_f
        # Use the TIGHTER of the climb ceiling and the NWS forecast (with a buffer
        # for forecast error). NWS is today-specific; the climb table is a generic
        # historical p95 that can dramatically over-estimate (e.g. 99F when NWS
        # says 81). Taking min() defers only when both signals agree the temp can
        # plausibly escape the bracket.
        if nws_forecast is not None:
            expected_high = min(climb_ceiling, nws_forecast + FORECAST_STDDEV_F)
            bound_source = f"min(climb={climb_ceiling:.1f}, nws+{FORECAST_STDDEV_F:.0f}={nws_forecast + FORECAST_STDDEV_F:.1f})"
        else:
            expected_high = climb_ceiling
            bound_source = f"climb={climb_ceiling:.1f}"
        if expected_high > bracket_high:
            log.info(
                "  [exit] [%s] %.0f-%.0fF current high %.1fF inside bracket but expected "
                "end-of-day high is %.1fF [%s] -- deferring, temp likely passing through",
                station, bracket_low, bracket_high, current_high, expected_high, bound_source,
            )
            continue

        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        total_eur = sum(f["size_eur"] for f in fills)
        question = fills[0].get("question", "")
        log.info(
            "  [exit] METAR high %.1fF is inside [%s] %.0f-%.0fF bracket -- selling %.1f NO shares",
            current_high, station, bracket_low, bracket_high, total_shares,
        )
        try:
            sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
            _order_manager._sold_positions.add(token_id)
            if db is not None:
                # open_positions rows are keyed by the BUY order_id, not the
                # sell order — remove every fill for this token.
                db.close_positions_by_token(token_id)
            # Weighted average entry price across all DCA fills
            avg_entry_cents = sum(
                f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
                for f in fills
            ) / total_shares
            # Actual realised PnL: (sell - avg_entry) per share x total shares, in EUR
            pnl = round((sell_price_cents - avg_entry_cents) / 100 * total_shares, 4)
            _record_sell_in_db(fills, sell_price_cents, ts, db=db)
            if risk_manager is not None:
                risk_manager.record_pnl(pnl)
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
                "actual_fee_cents": estimate_fee_cents(sell_price_cents),
                "trigger": f"metar_high={current_high:.1f}F_expected={expected_high:.1f}F_nws={nws_forecast}",
            }, db=db)
            log.info(
                "  [exit] sell %s... placed @ %sc -- [%s] %.0f-%.0fF NO  pnl=%+.2f",
                sell_id[:12], sell_price_cents, station, bracket_low, bracket_high, pnl,
            )
        except Exception as e:
            err = str(e)
            if "balance" in err.lower() and ("0" in err or "not enough" in err.lower()):
                n = db.close_positions_by_token(token_id) if db is not None else 0
                _order_manager._sold_positions.add(token_id)
                log.info(
                    "  [exit] [%s] %.0f-%.0fF balance=0 -- tokens already gone, removed %s row(s) from open_positions",
                    station, bracket_low, bracket_high, n,
                )
            else:
                log.warning("  [exit] sell failed for [%s] %.0f-%.0fF: %s", station, bracket_low, bracket_high, e)
