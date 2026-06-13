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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from dateutil import parser as dtparse

from src.config import (
    POLL_INTERVAL_SECONDS, LOG_DIR,
    CANDIDATES_CSV, SNAPSHOTS_JSONL, LIVE_TRADES_JSONL, POSITION_SNAPSHOTS_JSONL,
    RISK_DAILY_LOSS_LIMIT_EUR, RISK_MAX_OPEN_POSITIONS,
    RISK_DRAWDOWN_STOP_PCT, RISK_MIN_LIQUIDITY, STARTING_CAPITAL_EUR,
    POSITION_SIZE_EUR, POSITION_SIZE_WITH_FEES,
    FORECAST_STDDEV_F,
    STOP_LOSS_MIN_BID_CENTS, STOP_LOSS_CONSECUTIVE_POLLS,
    STOP_LOSS_MIN_DEPTH_SHARES, STOP_LOSS_SELL_AGGRESSION_CENTS,
    STOP_LOSS_MIN_BRACKET_PROXIMITY_F, STOP_LOSS_RESPECT_FORECAST_OVERSHOOT,
    get_source_priority,
)
from src.data.db import Database
from src.data.polymarket import get_orderbook, get_weather_markets
from src.data.taf_collector import TafCollector
from src.data.collectors.jma_ameidas import JmaAmedasCollector
from src.data.collectors.amos import AmosCollector
from src.data.collectors.mss import MssCollector
from src.data.freshness_monitor import FreshnessMonitor
from src.model.envelope import Bracket, true_probability_yes
from src.model.climb_rates import expected_additional_rise
from src.logging_config import setup_logging
from src.monitoring.alerts import AlertManager
from src.risk.manager import RiskManager
from src.strategy.scanner import scan_markets
from src.weather.builder import _build_weather, _station_in_active_window
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import OrderManager
import src.monitoring.dashboard as _dashboard_module
from src.monitoring.dashboard import _load_trades, _compute_win_rate, start_dashboard

log = logging.getLogger(__name__)

FILL_POLL_INTERVAL_S = 30
FILL_MAX_WAIT_S = 300  # 5 minutes, 10 attempts

_write_lock = threading.Lock()

# Module-level OrderManager instance: owns _open_orders, _open_orders_lock,
# _order_lock, _sold_positions, _stop_loss_strikes, _partial_fill_shares.
order_manager = OrderManager()


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


def _append_live_trade(record: dict, db=None) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with _write_lock:
        with open(LIVE_TRADES_JSONL, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    if db is None:
        return
    if record.get("side") == "SELL":
        # SELL records violate trades.side CHECK(side IN ('YES','NO')); the
        # sell paths update the original BUY rows per fill instead.
        return
    try:
        # place_order() already inserted the trade row at placement time —
        # update it with the market ticker and final order outcome.
        updated = db.update_trade_by_order(
            record.get("order_id") or "",
            ticker=record.get("ticker"),
            outcome=record.get("outcome"),
            pnl=float(record["pnl"]) if record.get("pnl") is not None else None,
        )
        if not updated:
            # Placement-time insert failed (logged CRITICAL) — insert now so
            # the trade is not lost.
            db.insert_trade(
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


# ---------------------------------------------------------------------------
# Live order lifecycle
# ---------------------------------------------------------------------------

def _execute_live(
    candidate,
    clob_client_factory,
    risk_manager: "RiskManager",
    ts: str,
    db=None,
) -> None:
    """Place one order and wait for fill/timeout. open_position() already called by caller.

    Each thread creates its own LiveTrader/ClobClient to avoid HTTP/2 stream
    collisions when multiple orders are placed concurrently.
    """
    trader = LiveTrader(clob_client_factory(), db)

    token_id = (
        candidate.bracket.yes_token_id if candidate.side == "YES"
        else candidate.bracket.no_token_id
    )
    if not token_id:
        log.info("  [live] no token_id for %s..., skipping", candidate.bracket.ticker[:16])
        risk_manager.close_position()
        return

    order_key = token_id
    with order_manager._open_orders_lock:
        if order_key in order_manager._open_orders:
            log.info("  [live] skip %s %s... -- GTC order already open on exchange", candidate.side, candidate.bracket.ticker[:16])
            risk_manager.close_position()
            return
        order_manager._open_orders.add(order_key)

    predicted_price = round(candidate.confidence * 100)

    try:
        with order_manager._order_lock:  # Serialize HTTP/2 placements; fill-monitoring remains parallel
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
                log.error("  [live] place_order failed: %s", e, exc_info=True)
                risk_manager.close_position()
                return

        log.info("  [live] placed %s... %s @ %sc", order_id[:12], candidate.side, candidate.price_cents)

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
        }, db=db)
        log.info("  [live] %s %s...", outcome, order_id[:12])
    finally:
        with order_manager._open_orders_lock:
            order_manager._open_orders.discard(order_key)


# ---------------------------------------------------------------------------
# Position helpers (kept here — tightly coupled to poll_once weather state)
# ---------------------------------------------------------------------------

def _load_open_no_positions(today: str, db=None) -> list:
    """Return filled NO positions for today that have not yet been sold.

    Reads from DB when available; falls back to live_trades.jsonl.
    A token is considered sold if any record with outcome='sold' exists for it today.
    """
    if db is not None:
        try:
            positions = db.get_open_positions()
            return [p for p in positions if p.get("side") == "NO"]
        except Exception as e:
            log.warning("[positions] DB read failed: %s", e)
    # Fallback: existing JSONL path
    if not LIVE_TRADES_JSONL.exists():
        return []
    records: list = []
    sold_tokens: set = set()
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


def _log_open_position_snapshots(weather: dict, ts: str, db=None) -> list:
    """Write one snapshot per open NO position per poll, capturing weather +
    orderbook + live model probability.

    snapshots.jsonl only contains rows for markets the scanner evaluates, which
    drops to zero past noon UTC each day.  This logger runs every poll for
    every open position regardless of market scan state -- gives us continuous
    intra-day data (especially the 14-22 UTC peak window) needed to backtest
    exit strategies against real intra-day orderbook movement.

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
        if station not in weather or bracket_low is None or bracket_high is None:
            continue

        state = weather[station]

        # Fetch live orderbook for the NO token
        no_yes_ask = no_no_ask = no_yes_bid = no_no_bid = None
        no_best_bid_size = None
        try:
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

        # Live p_yes by re-running the envelope model with current state
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
            "current_high": state.current_high_f,
            "latest_temp": state.latest_temp_f,
            "forecast_nws": state.forecast_high_f,
            "forecast_secondary": state.secondary_forecast_f,
            "no_best_bid": no_no_bid,
            "no_best_bid_size": no_best_bid_size,
            "no_best_ask": no_no_ask,
            "p_yes_now": round(p_yes_now, 4) if p_yes_now is not None else None,
            "fair_value_now": fair_value_now,
        }
        with _write_lock:
            with open(POSITION_SNAPSHOTS_JSONL, "a") as f:
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
    today = datetime.now(timezone.utc).date().isoformat()
    for ps in position_states:
        token_id = ps["token_id"]
        fills = ps["fills"]
        snap = ps["snap"]
        if token_id in order_manager._sold_positions:
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
            order_manager._stop_loss_strikes.pop(token_id, None)
            continue

        strikes = order_manager._stop_loss_strikes.get(token_id, 0) + 1
        order_manager._stop_loss_strikes[token_id] = strikes
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
        already_sold = order_manager._partial_fill_shares.get(token_id, 0.0)
        remaining_shares = total_shares - already_sold
        min_lot = float(os.environ.get("STOP_LOSS_MIN_LOT_SHARES", "0.5"))
        if remaining_shares < min_lot:
            log.info(
                "  [sl] [%s] %.0f-%.0fF remaining shares %.4f < min lot %.4f after partial fills"
                " -- skipping dust sell",
                station, bracket_low, bracket_high, remaining_shares, min_lot,
            )
            order_manager._sold_positions.add(token_id)
            order_manager._stop_loss_strikes.pop(token_id, None)
            order_manager._partial_fill_shares.pop(token_id, None)
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
                        order_manager._partial_fill_shares[token_id] = already_sold + partial
                        log.info(
                            "  [sl] partial fill %.4f shares on %s... -- tracking remainder",
                            partial, cancelled_order_id[:12],
                        )
                # Strikes persist so the very next poll retries at the then-current bid.
                continue
            sell_price_cents = sell_price_or_order
            sold_shares = remaining_shares
            order_manager._sold_positions.add(token_id)
            order_manager._stop_loss_strikes.pop(token_id, None)
            order_manager._partial_fill_shares.pop(token_id, None)
            if db is not None:
                db.close_positions_by_token(token_id)
            pnl = round((sell_price_cents - avg_entry_cents) / 100 * sold_shares, 4)
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
                "shares": round(sold_shares, 4),
                "size_eur": total_eur,
                "edge_cents": 0,
                "pnl": pnl,
                "outcome": "sold",
                "actual_fee_cents": None,
                "trigger": f"stop_loss@{bid}c_fair{fair}c_entry{round(avg_entry_cents)}c",
            }, db=db)
            log.info(
                "  [sl] sell %s... filled >= %sc -- [%s] %.0f-%.0fF NO  pnl=%+.2f",
                sell_id[:12], sell_price_cents, station, bracket_low, bracket_high, pnl,
            )
        except Exception as e:
            log.warning("  [sl] sell failed for [%s] %.0f-%.0fF: %s", station, bracket_low, bracket_high, e)


def _record_sell_in_db(fills: list, sell_price_cents: int, ts: str, db=None) -> None:
    """Mark the BUY trade row of each fill as sold with its realised PnL."""
    if db is None:
        return
    for f in fills:
        order_id = f.get("order_id")
        if not order_id:
            continue
        try:
            shares = f["size_eur"] / (f["price_cents"] / 100)
            fill_pnl = round((sell_price_cents - f["price_cents"]) / 100 * shares, 4)
            db.update_trade_by_order(
                order_id, outcome="sold", pnl=fill_pnl, settled_at=ts,
            )
        except Exception as e:
            log.warning("[run] DB sell update failed for %s...: %s", str(order_id)[:12], e)


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
    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today, db=db)
    if not open_positions:
        return

    by_token: dict = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    for token_id, fills in by_token.items():
        if token_id in order_manager._sold_positions:
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
            order_manager._sold_positions.add(token_id)
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
                "actual_fee_cents": None,
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
                order_manager._sold_positions.add(token_id)
                log.info(
                    "  [exit] [%s] %.0f-%.0fF balance=0 -- tokens already gone, removed %s row(s) from open_positions",
                    station, bracket_low, bracket_high, n,
                )
            else:
                log.warning("  [exit] sell failed for [%s] %.0f-%.0fF: %s", station, bracket_low, bracket_high, e)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

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

    # Capture the previous poll's timestamp BEFORE overwriting it so the
    # poll-missed alert can measure the gap between the last and current cycle.
    prev_poll_ts_str = _dashboard_module.last_poll_ts  # None on first poll, ISO string on subsequent

    if live_trader:
        order_manager.reconcile_timeout_fills(ts)
        order_manager.sync_open_orders(live_trader, db=db)

    # Take-profit is weather-independent -- runs on every poll so the safety
    # net stays live during pre-sunrise hours when no station yet has
    # qualifying METAR data (post-06:00 local rule from the overnight-bug fix).
    if live_trader:
        order_manager.check_take_profit_exits(live_trader, ts, db=db, risk_manager=risk_manager)

    weather = _build_weather(db=db)
    if not weather:
        log.warning("[run] No weather data for any station -- skipping market scan")
        return

    if live_trader:
        position_states = _log_open_position_snapshots(weather, ts, db=db)
        _check_stop_loss_exits(live_trader, ts, position_states, db=db, risk_manager=risk_manager)
        # METAR stop-loss disabled 2026-05-29 pending review.
        # Audit of 12 stops over May 20-28 showed 7 false positives
        # (NO would have won at settlement) for a net -15.49 vs hold-to-expiry.
        # Position_snapshots keep recording for offline rule design.
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

    # Log freshness status for all configured high-frequency sources.
    # METAR is only persisted while its station is inside the active window,
    # so skip metar checks outside it to avoid overnight false alarms.
    if db is not None:
        _freshness_monitor = FreshnessMonitor()
        for city_sources in [get_source_priority(c) for c in ["Tokyo", "Seoul", "Busan", "Singapore"]]:
            active_sources = [
                s for s in city_sources
                if s["source"] != "metar" or _station_in_active_window(s["station"])
            ]
            _freshness_monitor.check_all(db, active_sources)

    # Filter candidates through risk manager sequentially so position counts are accurate,
    # then execute all approved live orders in parallel so they hit the market simultaneously.
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

    # Update dashboard last_poll timestamp for the next cycle
    _dashboard_module.last_poll_ts = ts

    # Fire email alerts if thresholds are crossed
    if alert_manager is not None:
        _trades = _load_trades()
        _win_rate_20 = _compute_win_rate(_trades, n=20)
        # Resolve the previous poll's ISO string to a datetime so the poll-missed
        # check can compute the gap correctly.  On the very first poll this is None,
        # which the alert manager handles by skipping the poll-missed check.
        prev_poll_dt = None
        if prev_poll_ts_str:
            prev_poll_dt = dtparse.parse(prev_poll_ts_str)
        alert_manager.check(
            daily_pnl=risk_manager._daily_pnl,
            win_rate_20=_win_rate_20,
            last_poll_time=prev_poll_dt,  # previous poll's time, not now()
        )


# ---------------------------------------------------------------------------
# Collector thread helper
# ---------------------------------------------------------------------------

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
