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
    STATIONS, STATION_TZ, STATION_ACTIVE_HOURS, POLL_INTERVAL_SECONDS, LOG_DIR,
    CANDIDATES_CSV, SNAPSHOTS_JSONL, LIVE_TRADES_JSONL, POSITION_SNAPSHOTS_JSONL,
    RISK_DAILY_LOSS_LIMIT_EUR, RISK_MAX_OPEN_POSITIONS,
    RISK_DRAWDOWN_STOP_PCT, RISK_MIN_LIQUIDITY, STARTING_CAPITAL_EUR,
    POSITION_SIZE_EUR, POSITION_SIZE_WITH_FEES, TAKE_PROFIT_BUFFER_CENTS,
    FORECAST_STDDEV_F,
    get_source_priority,
)
from src.data.db import Database
from src.data.metar import fetch_all_metars_today, compute_daily_high, now_local, sunset_local
from src.data.nws import fetch_nws_forecast_high
from src.data.open_meteo import fetch_secondary_forecast, fetch_hourly_temp_now
from src.data.polymarket import get_orderbook, get_weather_markets
from src.data.taf_collector import TafCollector
from src.data.collectors.jma_ameidas import JmaAmedasCollector
from src.data.collectors.amos import AmosCollector
from src.data.collectors.mss import MssCollector
from src.data.freshness_monitor import FreshnessMonitor
from src.model.envelope import WeatherState
from src.monitoring.alerts import AlertManager
from src.risk.manager import RiskManager
from src.strategy.scanner import scan_markets

FILL_POLL_INTERVAL_S = 30
FILL_MAX_WAIT_S = 300  # 5 minutes, 10 attempts

_write_lock = threading.Lock()
_order_lock = threading.Lock()  # Serialize CLOB placements -- HTTP/2 pool not thread-safe
_open_orders: set[tuple[str, str]] = set()  # (ticker, side) pairs with a live GTC order
_open_orders_lock = threading.Lock()
_sold_positions: set[str] = set()  # no_token_ids sold this session (METAR stop-loss)


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
    if db is not None:
        try:
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
            print(f"[run] DB live trade write failed: {e}")


# ---------------------------------------------------------------------------
# Live order lifecycle
# ---------------------------------------------------------------------------

def _execute_live(candidate, clob_client_factory, risk_manager, ts: str, db=None) -> None:
    """Place one order and wait for fill/timeout. open_position() already called by caller.

    Each thread creates its own LiveTrader/ClobClient to avoid HTTP/2 stream
    collisions when multiple orders are placed concurrently.
    """
    from src.execution.live_trader import LiveTrader
    trader = LiveTrader(clob_client_factory(), db)

    token_id = (
        candidate.bracket.yes_token_id if candidate.side == "YES"
        else candidate.bracket.no_token_id
    )
    if not token_id:
        print(f"  [live] no token_id for {candidate.bracket.ticker[:16]}..., skipping")
        risk_manager.close_position()
        return

    order_key = token_id
    with _open_orders_lock:
        if order_key in _open_orders:
            print(f"  [live] skip {candidate.side} {candidate.bracket.ticker[:16]}... -- GTC order already open on exchange")
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

        print(f"  [live] placed {order_id[:12]}... {candidate.side} @ {candidate.price_cents}c")

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
        print(f"  [live] {outcome} {order_id[:12]}...")
    finally:
        with _open_orders_lock:
            _open_orders.discard(order_key)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def _build_weather(db: "Database | None" = None) -> dict[str, WeatherState]:
    weather: dict[str, WeatherState] = {}
    for station, lat, lon, city, *_ in STATIONS:
        import pytz
        now_local_dt = datetime.now(pytz.timezone(STATION_TZ[station]))
        active_start, active_end = STATION_ACTIVE_HOURS.get(station, (6, 23))
        if not (active_start <= now_local_dt.hour < active_end):
            print(
                f"[{station}] local {now_local_dt.strftime('%H:%M')} outside "
                f"active window {active_start:02d}:00-{active_end:02d}:00 -- skipping"
            )
            continue

        metars = fetch_all_metars_today(station)
        if not metars:
            print(f"[{station}] no METAR data, skipping")
            continue

        result = compute_daily_high(metars, STATION_TZ[station], min_local_hour=active_start)
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

        # Attempt to upgrade latest_temp_f from a fresher high-freq obs
        obs_bias_offset_f = None
        if db is not None:
            for src_cfg in get_source_priority(city):
                if src_cfg["source"] == "metar":
                    continue
                obs = db.get_latest_observation(src_cfg["source"], src_cfg["station"])
                if obs is None:
                    continue
                try:
                    obs_ts = dtparse.parse(obs["ts"])
                    if obs_ts.tzinfo is None:
                        obs_ts = obs_ts.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                age_min = (datetime.now(timezone.utc) - obs_ts).total_seconds() / 60
                if age_min > 2 * src_cfg["cadence_min"]:
                    continue  # stale — skip
                latest_temp_f = float(obs["temp_f"])
                latest_time = obs_ts
                break  # highest-priority fresh source wins
            hourly_model_f = fetch_hourly_temp_now(lat, lon)
            if hourly_model_f is not None:
                obs_bias_offset_f = latest_temp_f - hourly_model_f

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
            obs_bias_offset_f=obs_bias_offset_f,
        )
        print(f"[{station}] high={high_f:.1f}F latest={latest_temp_f:.1f}F nws={forecast_nws}")
    return weather


def _wallet_held_token_ids() -> set[str]:
    """Return non-redeemable token_ids currently held in the wallet (size > 0.01).

    Used to reconcile timeout records against actual exchange state -- if a
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
        print(f"[reconcile] wallet fetch failed: {e} -- skipping reconciliation")
        return set()


def _reconcile_timeout_fills(ts: str) -> None:
    """Patch timeout JSONL records whose tokens still appear in the wallet.

    GTC limit orders sometimes fill after our 5-minute wait window expires.
    The follow-up cancel_order() can fail silently or lose a race with the
    matching engine, leaving the order live on the exchange.  When the order
    later fills, the wallet shows the position but our JSONL says
    outcome=timeout -- invisible to the dashboard enrichment and to the
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
            f"[reconcile] patched {patched} timeout record(s) -> filled "
            f"(token present in wallet)"
        )
    except OSError as e:
        print(f"[reconcile] write failed: {e}")


def _sync_open_orders(live_trader, db=None) -> None:
    """Refresh _open_orders from exchange open orders + today's filled positions.

    Called at the top of every live poll. Two sources feed the dedup guard:
    1. Exchange open orders (pending GTC orders not yet filled or cancelled).
    2. Today's filled positions from DB (preferred) or live_trades.jsonl (fallback).
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
            print(f"[orders] failed to sync open orders: {e} -- using filled-positions only")

        # Source 2: today's already-filled no_token_ids -- prefer DB, fall back to JSONL
        filled_count = 0
        if db is not None:
            try:
                positions = db.get_open_positions()
                for p in positions:
                    if p.get("no_token_id"):
                        _open_orders.add(p["no_token_id"])
                        filled_count += 1
            except Exception as e:
                print(f"[orders] DB read failed: {e}")
        else:
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


def _load_open_no_positions(today: str, db=None) -> list[dict]:
    """Return filled NO positions for today that have not yet been sold.

    Reads from DB when available; falls back to live_trades.jsonl.
    A token is considered sold if any record with outcome='sold' exists for it today.
    """
    if db is not None:
        try:
            positions = db.get_open_positions()
            return [p for p in positions if p.get("side") == "NO"]
        except Exception as e:
            print(f"[positions] DB read failed: {e}")
    # Fallback: existing JSONL path
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


def _log_open_position_snapshots(weather: dict, ts: str, db=None) -> None:
    """Write one snapshot per open NO position per poll, capturing weather +
    orderbook + live model probability.

    snapshots.jsonl only contains rows for markets the scanner evaluates, which
    drops to zero past noon UTC each day.  This logger runs every poll for
    every open position regardless of market scan state -- gives us continuous
    intra-day data (especially the 14-22 UTC peak window) needed to backtest
    exit strategies against real intra-day orderbook movement.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today, db=db)
    if not open_positions:
        return

    from collections import defaultdict
    from src.model.envelope import true_probability_yes, Bracket
    by_token: dict[str, list[dict]] = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    LOG_DIR.mkdir(exist_ok=True)
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
        try:
            ob = get_orderbook(token_id)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if bids:
                no_no_bid = max(1, min(99, round(max(float(b["price"]) for b in bids) * 100)))
            if asks:
                no_no_ask = max(1, min(99, round(min(float(a["price"]) for a in asks) * 100)))
        except Exception as e:
            print(f"  [snap] orderbook {token_id[:14]}... error: {e}")

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
            print(f"  [snap] model eval {station} {bracket_low}-{bracket_high} error: {e}")
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
            "no_best_ask": no_no_ask,
            "p_yes_now": round(p_yes_now, 4) if p_yes_now is not None else None,
            "fair_value_now": fair_value_now,
        }
        with _write_lock:
            with open(POSITION_SNAPSHOTS_JSONL, "a") as f:
                f.write(json.dumps(snap, default=str) + "\n")


def _check_take_profit_exits(live_trader, ts: str, db=None) -> None:
    """Exit NO positions where the best bid has reached predicted_price - buffer.

    The model snapshot is frozen at entry time and cannot validate further price
    movement.  Once market price converges to our fair value, the edge we
    measured is fully captured -- holding longer is a bet on the (stale) model
    being right that the market underprices the outcome.  Locking in the gain
    recycles capital into the next edge and reduces variance without giving up
    expected value.

    NOTE: A price-based stop-loss was backtested against position_snapshots.jsonl
    (May 27 - Jun 4) and found to be actively harmful at every threshold (10c-55c).
    Many winning positions dip to 10-30c before recovering to 98-99c -- the
    stop would cut exactly those positions.  Net degradation: -$28 at 55c,
    -$33 at 20c, -$40 at 10c vs baseline.  Do not re-add without a time-of-day
    condition (only fire in the final hour of trading, once temperature is
    locked in and recovery is impossible).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    open_positions = _load_open_no_positions(today, db=db)
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
            print(f"  [tp] orderbook fetch failed for {token_id[:14]}...: {e}")
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
            f"  [tp] {station} {bracket_low:.0f}-{bracket_high:.0f}F NO bid "
            f"{best_bid_cents}c >= target {target_cents}c (predicted {predicted_price}c) "
            f"-- selling {total_shares:.1f} shares"
        )
        try:
            sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
            _sold_positions.add(token_id)
            if db is not None:
                db.close_position(sell_id)
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
            }, db=db)
            print(
                f"  [tp] sell {sell_id[:12]}... placed @ {sell_price_cents}c -- "
                f"{station} {bracket_low:.0f}-{bracket_high:.0f}F NO  pnl={pnl:+.2f}"
            )
        except Exception as e:
            print(f"  [tp] sell failed for {station} {bracket_low:.0f}-{bracket_high:.0f}F: {e}")


def _check_metar_exits(weather: dict, live_trader, ts: str, db=None) -> None:
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

    from collections import defaultdict
    from src.model.climb_rates import expected_additional_rise
    by_token: dict[str, list[dict]] = defaultdict(list)
    for pos in open_positions:
        by_token[pos["no_token_id"]].append(pos)

    for token_id, fills in by_token.items():
        if token_id in _sold_positions:
            continue

        bracket_low = fills[0]["bracket_low"]
        bracket_high = fills[0]["bracket_high"]
        station = fills[0]["station"]

        if station not in weather:
            continue

        current_high = weather[station].current_high_f
        if not (bracket_low <= current_high <= bracket_high):
            continue

        now_local = weather[station].now_local
        remaining_rise = expected_additional_rise(now_local)
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
            print(
                f"  [exit] {station} {bracket_low:.0f}-{bracket_high:.0f}F "
                f"current high {current_high:.1f}F inside bracket but expected "
                f"end-of-day high is {expected_high:.1f}F [{bound_source}] "
                f"-- deferring, temp likely passing through"
            )
            continue

        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        total_eur = sum(f["size_eur"] for f in fills)
        question = fills[0].get("question", "")
        print(
            f"  [exit] METAR high {current_high:.1f}F is inside {bracket_low:.0f}-"
            f"{bracket_high:.0f}F bracket -- selling {total_shares:.1f} NO shares "
            f"for {station}"
        )
        try:
            sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
            _sold_positions.add(token_id)
            if db is not None:
                db.close_position(sell_id)
            # Weighted average entry price across all DCA fills
            avg_entry_cents = sum(
                f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
                for f in fills
            ) / total_shares
            # Actual realised PnL: (sell - avg_entry) per share x total shares, in EUR
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
                "trigger": f"metar_high={current_high:.1f}F_expected={expected_high:.1f}F_nws={nws_forecast}",
            }, db=db)
            print(
                f"  [exit] sell {sell_id[:12]}... placed @ {sell_price_cents}c -- "
                f"{station} {bracket_low:.0f}-{bracket_high:.0f}F NO  pnl={pnl:+.2f}"
            )
        except Exception as e:
            print(f"  [exit] sell failed for {station} {bracket_low:.0f}-{bracket_high:.0f}F: {e}")


def poll_once(risk_manager, live_trader=None, alert_manager=None, db=None) -> None:
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
        _sync_open_orders(live_trader, db=db)

    # Take-profit is weather-independent -- runs on every poll so the safety
    # net stays live during pre-sunrise hours when no station yet has
    # qualifying METAR data (post-06:00 local rule from the overnight-bug fix).
    if live_trader:
        _check_take_profit_exits(live_trader, ts, db=db)

    weather = _build_weather(db=db)
    if not weather:
        print("[run] No weather data for any station -- skipping market scan")
        return

    if live_trader:
        _log_open_position_snapshots(weather, ts, db=db)
        # METAR stop-loss disabled 2026-05-29 pending review.
        # Audit of 12 stops over May 20-28 showed 7 false positives
        # (NO would have won at settlement) for a net -15.49 vs hold-to-expiry.
        # Position_snapshots keep recording for offline rule design.
        # _check_metar_exits(weather, live_trader, ts, db=db)

    try:
        markets = get_weather_markets()
    except Exception as e:
        print(f"[polymarket] error: {e} -- skipping this poll")
        return
    print(f"[polymarket] {len(markets)} weather markets fetched")

    candidates, snapshots = scan_markets(weather, markets, db=db)

    for snap in snapshots:
        _append_snapshot(snap)

    # Log freshness status for all configured high-frequency sources.
    if db is not None:
        _freshness_monitor = FreshnessMonitor()
        for city_sources in [get_source_priority(c) for c in ["Tokyo", "Seoul", "Busan", "Singapore"]]:
            _freshness_monitor.check_all(db, city_sources)

    # Filter candidates through risk manager sequentially so position counts are accurate,
    # then execute all approved live orders in parallel so they hit the market simultaneously.
    available_usdc = float("inf")
    if live_trader:
        try:
            available_usdc = live_trader.get_usdc_balance()
            print(f"[balance] {available_usdc:.2f} USDC available")
        except Exception as e:
            print(f"[balance] check failed: {e} -- proceeding without balance gate")

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
                executor.submit(_execute_live, cand, live_trader._client_factory, risk_manager, ts, db)
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


# ---------------------------------------------------------------------------
# Collector thread helper
# ---------------------------------------------------------------------------

def _start_collector_thread(collector_fn, name: str) -> None:
    """Start a collector in a daemon thread; log WARNING on any startup exception."""
    import logging as _logging

    def _run():
        try:
            collector_fn()
        except Exception as e:
            _logging.warning(f"[{name}] collector thread exited: {e}")

    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()


def main() -> None:
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
        print(f"[startup] recovered {len(open_positions)} open position(s) from DB")

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
        from src.execution.live_trader import LiveTrader
        print("Checking CLOB connectivity...")
        if not check_clob_health():
            raise SystemExit("[run] CLOB health check failed -- verify POLYMARKET_API_KEY and connectivity")
        live_trader = LiveTrader(get_clob_client(), db)
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
        db=db,
    )
    alert_manager = AlertManager()

    from src.monitoring.dashboard import start_dashboard
    import src.monitoring.dashboard as _dashboard_mod
    start_dashboard()
    _dashboard_mod.set_db(db)

    if args.once:
        poll_once(risk_manager, live_trader, alert_manager, db=db)
        print("[run] --once mode: exiting after single poll.")
        return

    print(f"Polling every {POLL_INTERVAL_SECONDS}s. Press Ctrl-C to stop.")
    while True:
        try:
            poll_once(risk_manager, live_trader, alert_manager, db=db)
        except KeyboardInterrupt:
            print("\n[run] Stopping.")
            break
        except Exception as e:
            print(f"[run] Unhandled error in poll: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
