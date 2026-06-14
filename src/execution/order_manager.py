"""Owns open-order state. Thread-safe via internal locks. Do not import _open_orders directly."""
import json
import logging
import os
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timezone

from src.config import (
    LIVE_TRADES_JSONL,
    TAKE_PROFIT_BUFFER_CENTS,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position data helpers (module-level; no dependency on OrderManager instance)
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


def _wallet_held_token_ids() -> set:
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
        log.warning("[reconcile] wallet fetch failed: %s -- skipping reconciliation", e)
        return set()


class OrderManager:
    """Manages open-order state for the live trading loop.

    Thread-safe: all mutations to _open_orders go through _open_orders_lock.
    The _sold_positions, _stop_loss_strikes, and _partial_fill_shares dicts
    are only accessed from the polling thread and do not need their own lock.
    """

    def __init__(self) -> None:
        self._open_orders: set = set()          # token_id keys with a live GTC order
        self._open_orders_lock = threading.Lock()
        self._order_lock = threading.Lock()     # Serialize CLOB placements -- HTTP/2 pool not thread-safe
        self._sold_positions: set = set()       # no_token_ids sold this session
        self._stop_loss_strikes: dict = {}      # no_token_id -> consecutive polls with fair < entry
        self._partial_fill_shares: dict = {}    # no_token_id -> shares already sold via partial fills

    def reconcile_timeout_fills(self, ts: str) -> None:
        """Patch timeout JSONL records whose tokens still appear in the wallet.

        GTC limit orders sometimes fill after our 5-minute wait window expires.
        The follow-up cancel_order() can fail silently or lose a race with the
        matching engine, leaving the order live on the exchange.  When the order
        later fills, the wallet shows the position but our JSONL says
        outcome=timeout -- invisible to the dashboard enrichment and to the
        take-profit / METAR stop-loss exits.

        Rewriting these records to outcome=filled restores visibility everywhere
        that filters on outcome.  Reconciliation runs once per poll, before
        sync_open_orders so the dedup guard sees the patched records.
        """
        if not LIVE_TRADES_JSONL.exists():
            return
        held = _wallet_held_token_ids()
        if not held:
            return

        patched = 0
        new_lines: list = []
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
            log.warning("[reconcile] read failed: %s", e)
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
            log.info(
                "[reconcile] patched %s timeout record(s) -> filled (token present in wallet)",
                patched,
            )
        except OSError as e:
            log.warning("[reconcile] write failed: %s", e)

    def sync_open_orders(self, live_trader, db=None) -> None:
        """Refresh _open_orders from exchange open orders + today's filled positions.

        Called at the top of every live poll. Two sources feed the dedup guard:
        1. Exchange open orders (pending GTC orders not yet filled or cancelled).
        2. Today's filled positions from DB (preferred) or live_trades.jsonl (fallback).
        """
        today = datetime.now(timezone.utc).date().isoformat()

        with self._open_orders_lock:
            self._open_orders.clear()

            # Source 1: live exchange open orders
            try:
                from py_clob_client_v2.clob_types import OpenOrderParams
                orders = live_trader.client.get_open_orders(OpenOrderParams())
                for o in orders:
                    self._open_orders.add(o.get("asset_id", ""))
                log.info("[orders] %s open exchange orders synced to dedup guard", len(orders))
            except Exception as e:
                log.warning("[orders] failed to sync open orders: %s -- using filled-positions only", e)

            # Source 2: today's already-filled no_token_ids -- prefer DB, fall back to JSONL
            filled_count = 0
            if db is not None:
                try:
                    positions = db.get_open_positions()
                    for p in positions:
                        if p.get("no_token_id"):
                            self._open_orders.add(p["no_token_id"])
                            filled_count += 1
                except Exception as e:
                    log.warning("[orders] DB read failed: %s", e)
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
                                    self._open_orders.add(r["no_token_id"])
                                    filled_count += 1
                    except OSError:
                        pass
            if filled_count:
                log.info("[orders] %s today's filled position(s) added to dedup guard", filled_count)

    def check_take_profit_exits(self, live_trader, ts: str, db=None, risk_manager=None) -> None:
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
        # Lazy import for get_orderbook (avoids heavy network module at load time).
        # _load_open_no_positions and _record_sell_in_db live in this module now.
        # _append_live_trade stays in run.py (coordinator); lazy-import is fine here
        # because the circular-import problem was the back-import of *position helpers*
        # from run.py, not the forward import of a logging helper.
        from src.data.polymarket import get_orderbook
        from src.scripts.run import _append_live_trade  # noqa: PLC0415

        today = datetime.now(timezone.utc).date().isoformat()
        open_positions = _load_open_no_positions(today, db=db)
        if not open_positions:
            return

        by_token: dict = defaultdict(list)
        for pos in open_positions:
            by_token[pos["no_token_id"]].append(pos)

        for token_id, fills in by_token.items():
            if token_id in self._sold_positions:
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
                if "404" in str(e):
                    n = db.close_positions_by_token(token_id) if db is not None else 0
                    log.info("  [tp] %s... market resolved -- removed %s row(s) from open_positions", token_id[:14], n)
                else:
                    log.warning("  [tp] orderbook fetch failed for %s...: %s", token_id[:14], e)
                continue

            if best_bid_cents < target_cents:
                continue

            bracket_low = fills[0]["bracket_low"]
            bracket_high = fills[0]["bracket_high"]
            station = fills[0]["station"]
            total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
            total_eur = sum(f["size_eur"] for f in fills)
            question = fills[0].get("question", "")

            log.info(
                "  [tp] [%s] %.0f-%.0fF NO bid %sc >= target %sc (predicted %sc) -- selling %.1f shares",
                station, bracket_low, bracket_high, best_bid_cents, target_cents, predicted_price, total_shares,
            )
            try:
                sell_id, sell_price_cents = live_trader.sell_position(token_id, total_shares)
                self._sold_positions.add(token_id)
                if db is not None:
                    # open_positions rows are keyed by the BUY order_id, not the
                    # sell order — remove every fill for this token.
                    db.close_positions_by_token(token_id)
                avg_entry_cents = sum(
                    f["price_cents"] * (f["size_eur"] / (f["price_cents"] / 100))
                    for f in fills
                ) / total_shares
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
                    "trigger": f"take_profit@{best_bid_cents}c_target{target_cents}c_predicted{predicted_price}c",
                }, db=db)
                log.info(
                    "  [tp] sell %s... placed @ %sc -- [%s] %.0f-%.0fF NO  pnl=%+.2f",
                    sell_id[:12], sell_price_cents, station, bracket_low, bracket_high, pnl,
                )
            except Exception as e:
                err = str(e)
                if "balance" in err.lower() and ("0" in err or "not enough" in err.lower()):
                    n = db.close_positions_by_token(token_id) if db is not None else 0
                    self._sold_positions.add(token_id)
                    log.info(
                        "  [tp] [%s] %.0f-%.0fF balance=0 -- tokens already gone, removed %s row(s) from open_positions",
                        station, bracket_low, bracket_high, n,
                    )
                else:
                    log.warning("  [tp] sell failed for [%s] %.0f-%.0fF: %s", station, bracket_low, bracket_high, e)
