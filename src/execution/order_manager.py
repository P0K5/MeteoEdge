"""Owns open-order state. Thread-safe via internal locks. Do not import _open_orders directly."""
import json
import logging
import math
import os
import re
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timezone

from src.config import (
    LIVE_TRADES_JSONL,
    TAKE_PROFIT_BUFFER_CENTS,
    get_take_profit_buffer_cents,
)
from src.strategy.fee import estimate_fee_cents
from src.utils.log_rotation import iter_rotated_jsonl, rotated_sources

log = logging.getLogger(__name__)

# Polymarket encodes share quantities with 6 decimal places of precision:
# 5_000_000 internal units == 5.000000 shares.
_POLY_PRECISION = 1_000_000


def _parse_polymarket_balance(err: str) -> "int | None":
    """Extract the available balance (in Polymarket internal units) from an error string.

    Polymarket reports insufficient-balance errors in a form like:
        "balance: 5000000, order amount: 6580000"

    Returns the integer value after "balance:" when parseable, else None.
    A return value of 0 means the wallet is genuinely empty.
    """
    m = re.search(r"balance\s*:\s*(\d+)", err, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


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


def _load_open_all_positions(today: str, db=None) -> list:
    """Return filled YES and NO positions for today that have not yet been sold.

    DB path returns all sides; JSONL fallback covers YES positions too by
    matching on either no_token_id or asset_id.
    """
    if db is not None:
        try:
            return db.get_open_positions()
        except Exception as e:
            log.warning("[positions] DB read failed: %s", e)
    # Fallback: JSONL path covering both sides
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
                token = r.get("no_token_id") or r.get("asset_id") or ""
                if r.get("outcome") == "sold":
                    sold_tokens.add(token)
                elif (r.get("outcome") == "filled"
                        and token
                        and r.get("bracket_low") is not None
                        and r.get("bracket_high") is not None):
                    records.append(r)
    except OSError:
        return []
    return [r for r in records if (r.get("no_token_id") or r.get("asset_id") or "") not in sold_tokens]


def _load_open_fills_for_token(token_id: str, today: str, db=None) -> list:
    """Return today's filled, not-yet-sold fills for *token_id* (any side).

    Unlike _load_open_no_positions this does not restrict to NO positions, so
    it can size a manual sell of either a YES or a NO holding.  Prefers the DB;
    falls back to live_trades.jsonl when no DB is available.
    """
    if db is not None:
        try:
            return db.get_open_position_by_token(token_id)
        except Exception as e:
            log.warning("[manual] DB read failed: %s", e)
    if not LIVE_TRADES_JSONL.exists():
        return []
    records: list = []
    sold = False
    partial_shares: float = 0.0
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
                tok = r.get("no_token_id") or r.get("asset_id") or ""
                if tok != token_id:
                    continue
                # Scope to today's market, mirroring _load_open_no_positions:
                # token_ids are unique per daily market, but a settled prior-day
                # 'filled' record (hold-to-expiry, never 'sold') would otherwise
                # be returned as sellable and trigger a sell of tokens we no
                # longer hold.
                if r.get("end_date", "")[:10] != today:
                    continue
                if r.get("outcome") == "sold":
                    sold = True
                elif r.get("outcome") == "partial_fill":
                    partial_shares += float(r.get("shares") or 0.0)
                elif r.get("outcome") == "filled":
                    records.append(r)
    except OSError:
        return []
    if sold:
        return []
    if partial_shares > 0 and records:
        # Distribute the already-sold partial shares across fills proportionally.
        total_fill_shares = sum(
            f["size_eur"] / (f["price_cents"] / 100) for f in records
            if f.get("price_cents")
        )
        if total_fill_shares > partial_shares:
            ratio = 1.0 - partial_shares / total_fill_shares
            records = [
                {**f, "size_eur": round(f["size_eur"] * ratio, 4)}
                for f in records
            ]
        else:
            return []  # entire position already partially sold
    return records


def _record_sell_in_db(
    fills: list,
    sell_price_cents: int,
    ts: str,
    db=None,
    close_reason: "str | None" = None,
    minutes_to_settlement: "float | None" = None,
    bid_depth: "int | None" = None,
) -> None:
    """Mark the BUY trade row of each fill as sold with its realised PnL.

    When close_reason/minutes_to_settlement/bid_depth are provided, also writes
    the close telemetry columns via update_trade_close_telemetry().
    """
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
            try:
                db.update_trade_costs(
                    order_id,
                    actual_fee_cents=estimate_fee_cents(sell_price_cents),
                    size_eur=f.get("size_eur"),
                )
            except Exception as ce:
                log.warning("[run] DB trade cost update failed for %s...: %s", str(order_id)[:12], ce)
            if close_reason is not None or minutes_to_settlement is not None or bid_depth is not None:
                try:
                    db.update_trade_close_telemetry(
                        order_id,
                        close_reason=close_reason,
                        minutes_to_settlement_at_close=minutes_to_settlement,
                        bid_depth_at_close=bid_depth,
                    )
                except Exception as te:
                    log.warning("[run] DB close telemetry update failed for %s...: %s", str(order_id)[:12], te)
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


def _wallet_held_positions() -> list:
    """Return full position data for non-redeemable wallet tokens (size > 0.01).

    Each entry is a dict with at least:
      - ``asset_id``: the token_id string
      - ``size``: float number of shares held (from Polymarket API ``size`` field)
      - ``avg_price``: float price in [0, 1] (from Polymarket API ``avgPrice`` field)

    Returns an empty list when the wallet env-var is unset or the API call fails.
    This is used by reconcile_wallet_to_db() to get authoritative share/price data.
    """
    wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "")
    if not wallet:
        return []
    try:
        import httpx
        url = f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.01&limit=100"
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("positions") or rows.get("data") or []
        result = []
        for row in rows:
            asset = str(row.get("asset") or "")
            if not asset or row.get("redeemable"):
                continue
            result.append({
                "asset_id": asset,
                "size": float(row.get("size") or 0.0),
                "avg_price": float(row.get("avgPrice") or 0.0),
            })
        return result
    except Exception as e:
        log.warning("[reconcile] wallet fetch failed: %s -- skipping wallet reconciliation", e)
        return []


def _reconcile_db_row(record: dict, ts: str, db=None) -> None:
    """Update (or insert) the DB trades row for a just-reconciled JSONL record.

    Called once per JSONL line that was patched from outcome='timeout' to
    outcome='filled'.  If no DB row exists for the order_id, we insert one so
    the trade is not invisible to DB-backed views.

    Also inserts an open_positions row when the token has no existing entry,
    so downstream take-profit / stop-loss logic can see the position.
    """
    if db is None:
        return
    order_id = record.get("order_id") or ""
    price_cents = int(record.get("price_cents") or record.get("actual_price") or 0)
    try:
        updated = db.update_trade_by_order(
            order_id,
            outcome="filled",
        )
        if not updated:
            # No existing trade row — insert a minimal one so the position is tracked.
            db.insert_trade(
                ts=record.get("ts", ts),
                station=record.get("station", ""),
                ticker=record.get("ticker", ""),
                bracket_low=float(record.get("bracket_low", 0)),
                bracket_high=float(record.get("bracket_high", 0)),
                side=record.get("side", "NO"),
                predicted_price=int(record.get("predicted_price", price_cents)),
                actual_price=price_cents,
                predicted_edge=float(record.get("edge_cents", 0)),
                mode="live",
                order_id=order_id or None,
                outcome="filled",
                capital_before=float(record.get("size_eur", 0)),
            )
    except Exception as e:
        log.warning("[reconcile] DB trade update failed for order %s...: %s", str(order_id)[:12], e)
        return

    # Ensure open_positions has an entry for this token so the position is visible.
    token_id = record.get("asset_id") or record.get("no_token_id") or ""
    if not token_id or not order_id:
        return
    try:
        existing = [p for p in db.get_open_positions() if p.get("order_id") == order_id]
        if not existing:
            shares = float(record.get("size_matched") or record.get("shares") or 0)
            # Look up the trade_id we just inserted/updated
            trade_row = db.get_trade_by_order_id(order_id)
            trade_id = trade_row["id"] if trade_row else 0
            db.open_position(
                trade_id=trade_id,
                station=record.get("station", ""),
                ticker=record.get("ticker", ""),
                token_id=token_id,
                side=record.get("side", "NO"),
                order_id=order_id,
                entry_price=price_cents,
                shares=shares,
                entry_ts=record.get("ts", ts),
            )
    except Exception as e:
        log.warning("[reconcile] DB open_positions insert failed for %s...: %s", str(order_id)[:12], e)


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
        self._reconcile_warned_tokens: set = set()  # tokens warned for missing JSONL (flood protection)

    def reconcile_timeout_fills(self, ts: str, db=None) -> None:
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

        After log rotation (Epic #345), new records go to dated files
        (live_trades.YYYY-MM-DD.jsonl) instead of the legacy plain file.
        This method iterates all sources returned by rotated_sources(), skipping
        immutable .gz archives, so no records are missed regardless of rotation
        state.  For every JSONL record patched, the DB trades row is also updated
        (or inserted if absent) so outcome='filled' is consistent across both
        stores.
        """
        sources = rotated_sources(LIVE_TRADES_JSONL)
        if not sources:
            return
        held = _wallet_held_token_ids()
        if not held:
            return

        total_patched = 0
        files_patched = 0

        for source in sources:
            # .gz archives are immutable — read-only, never rewrite
            if source.suffix == ".gz":
                continue

            new_lines: list = []
            file_patched = 0
            try:
                with open(source) as f:
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
                            file_patched += 1
                            # Sync the DB row so both stores stay consistent
                            _reconcile_db_row(r, ts, db)
                        else:
                            new_lines.append(line)
            except OSError as e:
                log.warning("[reconcile] read failed for %s: %s", source.name, e)
                continue

            if file_patched == 0:
                continue

            try:
                with tempfile.NamedTemporaryFile(
                    "w", dir=source.parent, delete=False, suffix=".tmp"
                ) as f:
                    f.writelines(new_lines)
                    tmp = f.name
                os.replace(tmp, source)
                total_patched += file_patched
                files_patched += 1
            except OSError as e:
                log.warning("[reconcile] write failed for %s: %s", source.name, e)

        if total_patched > 0:
            log.info(
                "[reconcile] patched %s timeout record(s) across %s file(s)",
                total_patched,
                files_patched,
            )

    def reconcile_wallet_to_db(self, db=None) -> None:
        """Insert DB rows for wallet positions that have no open_positions entry.

        Runs once per poll after reconcile_timeout_fills().  Three classes of
        wallet tokens are handled:

        1. Token already in open_positions → skip (no action needed).
        2. Token has a JSONL record with outcome 'timeout' or 'filled' → insert
           into trades (if not already present by order_id) and open_positions,
           using wallet shares/avgPrice as the authoritative size and price.
           Log at INFO: "[reconcile] recovered orphan position: STATION bracket SIDE token_id=..."
        3. Token has no JSONL record at all → log at WARNING once per token per
           process (flood-protected via _reconcile_warned_tokens).  No DB insert.
        4. Token whose latest JSONL record has outcome 'sold' → skip
           (redemption ghost; token still appears briefly in the wallet after sell).

        Wallet avgPrice is converted to cents: max(1, min(99, round(avg_price * 100))).
        For manual trades with no order_id, a synthetic order_id is used:
        f"orphan-recovery-{token_id[:12]}-{ts}".
        """
        if db is None:
            return

        positions = _wallet_held_positions()
        if not positions:
            return

        # Build set of token_ids already tracked in open_positions.
        try:
            existing_rows = db.get_open_positions()
        except Exception as e:
            log.warning("[reconcile] DB get_open_positions failed: %s -- skipping wallet reconcile", e)
            return
        existing_tokens: set = {str(row.get("token_id") or row.get("no_token_id") or "") for row in existing_rows}

        ts = datetime.now(timezone.utc).isoformat()

        for pos in positions:
            token_id = pos["asset_id"]
            if not token_id:
                continue

            # 1. Already tracked → skip.
            if token_id in existing_tokens:
                continue

            # Search rotated JSONL for the latest record matching this asset_id.
            latest_record: "dict | None" = None
            try:
                for record in iter_rotated_jsonl(LIVE_TRADES_JSONL):
                    asset = str(record.get("asset_id") or record.get("no_token_id") or "")
                    if asset == token_id:
                        latest_record = record  # keep iterating — we want the *latest*
            except Exception as e:
                log.warning("[reconcile] JSONL scan failed for %s...: %s", token_id[:14], e)
                continue

            # 4. Latest record is 'sold' → redemption ghost, skip.
            if latest_record is not None and latest_record.get("outcome") == "sold":
                continue

            # 3. No JSONL record → warn once per token per process.
            if latest_record is None:
                if token_id not in self._reconcile_warned_tokens:
                    self._reconcile_warned_tokens.add(token_id)
                    log.warning(
                        "[reconcile] wallet token %s... has no JSONL history"
                        " -- likely manual trade, cannot auto-recover enrichment",
                        token_id[:14],
                    )
                continue

            # 2. Found a 'timeout' or 'filled' record → recover the orphan position.
            outcome = latest_record.get("outcome", "")
            if outcome not in ("timeout", "filled"):
                continue

            wallet_shares = pos["size"]
            avg_price_raw = pos["avg_price"]
            entry_price_cents = max(1, min(99, round(avg_price_raw * 100)))

            order_id = latest_record.get("order_id") or ""
            if not order_id:
                order_id = f"orphan-recovery-{token_id[:12]}-{ts}"

            station = latest_record.get("station", "")
            ticker = latest_record.get("ticker", "")
            bracket_low = float(latest_record.get("bracket_low") or 0)
            bracket_high = float(latest_record.get("bracket_high") or 0)
            side = latest_record.get("side", "NO")
            record_ts = latest_record.get("ts", ts)
            predicted_price = int(latest_record.get("predicted_price") or entry_price_cents)

            # Insert trade row if not already present.
            try:
                existing_trade = db.get_trade_by_order_id(order_id)
                if existing_trade:
                    trade_id = existing_trade["id"]
                else:
                    trade_id = db.insert_trade(
                        ts=record_ts,
                        station=station,
                        ticker=ticker,
                        bracket_low=bracket_low,
                        bracket_high=bracket_high,
                        side=side,
                        predicted_price=predicted_price,
                        actual_price=entry_price_cents,
                        predicted_edge=float(latest_record.get("edge_cents") or 0),
                        mode="live",
                        order_id=order_id,
                        outcome="filled",
                        capital_before=float(latest_record.get("size_eur") or 0),
                    )
            except Exception as e:
                log.warning("[reconcile] DB trade insert failed for %s...: %s", token_id[:14], e)
                continue

            # Insert open_positions row using wallet's authoritative shares/price.
            try:
                db.open_position(
                    trade_id=trade_id,
                    station=station,
                    ticker=ticker,
                    token_id=token_id,
                    side=side,
                    order_id=order_id,
                    entry_price=entry_price_cents,
                    shares=wallet_shares,
                    entry_ts=record_ts,
                )
                log.info(
                    "[reconcile] recovered orphan position: %s %s-%s %s token_id=%s...",
                    station, bracket_low, bracket_high, side, token_id[:14],
                )
                # Add to local set so same-process duplicate calls are safe.
                existing_tokens.add(token_id)
            except Exception as e:
                log.warning("[reconcile] DB open_position insert failed for %s...: %s", token_id[:14], e)

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

        # Wallet-to-DB reconciliation: recover open_positions rows for wallet
        # tokens that have no DB entry (e.g. GTC fills after timeout window,
        # manual external trades, or historical orphans from the balance-error bug).
        # Runs after reconcile_timeout_fills() has already patched timeout→filled.
        self.reconcile_wallet_to_db(db=db)

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
            station_code = fills[0].get("station", "")
            buffer_cents = get_take_profit_buffer_cents(station_code)
            target_cents = int(predicted_price) - buffer_cents

            try:
                ob = get_orderbook(token_id)
                bids = ob.get("bids") or []
                if not bids:
                    continue
                best_bid_entry = max(bids, key=lambda b: float(b["price"]))
                best_bid_cents = max(1, min(99, round(float(best_bid_entry["price"]) * 100)))
                best_bid_depth = int(float(best_bid_entry.get("size") or 0))
            except Exception as e:
                if "404" in str(e):
                    n = db.close_positions_by_token(token_id) if db is not None else 0
                    log.info("  [tp] %s... market resolved -- removed %s row(s) from open_positions", token_id[:14], n)
                else:
                    log.warning("  [tp] orderbook fetch failed for %s...: %s", token_id[:14], e)
                continue

            if best_bid_cents < target_cents:
                continue

            if not fills:
                log.warning("  [tp] empty fills for token %s... -- skipping", token_id[:14])
                continue
            bracket_low = fills[0]["bracket_low"]
            bracket_high = fills[0]["bracket_high"]
            station = fills[0]["station"]
            total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
            if total_shares <= 0:
                log.warning("  [tp] zero total_shares for token %s... -- skipping", token_id[:14])
                continue
            total_eur = sum(f["size_eur"] for f in fills)
            question = fills[0].get("question", "")
            minutes_to_settlement = fills[0].get("minutes_to_settlement")

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
                _record_sell_in_db(
                    fills, sell_price_cents, ts, db=db,
                    close_reason="take_profit",
                    minutes_to_settlement=minutes_to_settlement,
                    bid_depth=best_bid_depth,
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
                    "close_reason": "take_profit",
                    "trigger": f"take_profit@{best_bid_cents}c_target{target_cents}c_predicted{predicted_price}c",
                }, db=db)
                log.info(
                    "  [tp] sell %s... placed @ %sc -- [%s] %.0f-%.0fF NO  pnl=%+.2f",
                    sell_id[:12], sell_price_cents, station, bracket_low, bracket_high, pnl,
                )
            except Exception as e:
                err = str(e)
                if "balance" in err.lower() or "invalid maker amount" in err.lower():
                    available = _parse_polymarket_balance(err)
                    avail_shares = math.floor(available / _POLY_PRECISION) if available is not None else 0
                    log.info(
                        "  [tp] [%s] %.0f-%.0fF partial balance -- wallet=%.6f requested=%.4f,"
                        " selling %s and closing remainder",
                        station, bracket_low, bracket_high,
                        available / _POLY_PRECISION if available is not None else 0.0,
                        total_shares, avail_shares,
                    )
                    if avail_shares > 0:
                        try:
                            sell_id2, sell_price2 = live_trader.sell_position(token_id, avail_shares)
                            self._sold_positions.add(token_id)
                            if db is not None:
                                db.close_positions_by_token(token_id)
                            pnl2 = round((sell_price2 - avg_entry_cents) / 100 * avail_shares, 4)
                            _record_sell_in_db(
                                fills, sell_price2, ts, db=db,
                                close_reason="take_profit",
                                minutes_to_settlement=minutes_to_settlement,
                                bid_depth=best_bid_depth,
                            )
                            if risk_manager is not None:
                                risk_manager.record_pnl(pnl2)
                            _append_live_trade({
                                "ts": ts,
                                "order_id": sell_id2,
                                "station": station,
                                "question": question,
                                "end_date": today,
                                "ticker": fills[0].get("ticker", ""),
                                "no_token_id": token_id,
                                "bracket_low": bracket_low,
                                "bracket_high": bracket_high,
                                "side": "SELL",
                                "price_cents": sell_price2,
                                "entry_price_cents": round(avg_entry_cents),
                                "shares": avail_shares,
                                "size_eur": total_eur,
                                "edge_cents": 0,
                                "pnl": pnl2,
                                "outcome": "sold",
                                "actual_fee_cents": estimate_fee_cents(sell_price2),
                                "close_reason": "take_profit_partial_balance",
                                "trigger": (
                                    f"take_profit_partial@{sell_price2}c"
                                    f"_wallet{avail_shares}shares"
                                    f"_requested{total_shares:.4f}shares"
                                ),
                            }, db=db)
                            log.info(
                                "  [tp] partial sell %s... placed @ %sc -- [%s] %.0f-%.0fF NO pnl=%+.2f",
                                sell_id2[:12], sell_price2, station, bracket_low, bracket_high, pnl2,
                            )
                        except Exception as e2:
                            log.warning(
                                "  [tp] partial-balance retry also failed for [%s] %.0f-%.0fF: %s",
                                station, bracket_low, bracket_high, e2,
                            )
                            n = db.close_positions_by_token(token_id) if db is not None else 0
                            self._sold_positions.add(token_id)
                    else:
                        n = db.close_positions_by_token(token_id) if db is not None else 0
                        self._sold_positions.add(token_id)
                        log.info(
                            "  [tp] [%s] %.0f-%.0fF balance=0 -- tokens already gone, removed %s row(s)",
                            station, bracket_low, bracket_high, n,
                        )
                else:
                    log.warning("  [tp] sell failed for [%s] %.0f-%.0fF: %s", station, bracket_low, bracket_high, e)

    def manual_sell_position(
        self, live_trader, token_id: str, ts: str, db=None, risk_manager=None,
    ) -> dict:
        """Operator-triggered immediate sell of one open position.

        Fired on demand from the dashboard rather than by a price trigger, but
        records the exit identically to check_take_profit_exits: sells the full
        remaining size at the current best bid (immediate-or-cancel via
        sell_position_immediate), closes the DB rows, marks the token sold so
        the bot's exit loops skip it, and appends a 'sold' trade with a
        ``manual@`` trigger so the position moves to the closed list.

        Returns a result dict with a ``status`` of:
          - "sold"        — filled; includes order_id, sell_price_cents, shares, pnl
          - "no_fill"     — order did not cross at market and was cancelled
          - "not_found"   — no open fills for this token
          - "already_sold" — token already sold this session
        """
        from src.scripts.run import _append_live_trade  # noqa: PLC0415

        today = datetime.now(timezone.utc).date().isoformat()
        if token_id in self._sold_positions:
            return {"status": "already_sold", "detail": "Position already sold this session."}

        # Cross-process guard: if another process (e.g. the bot's exit loop) has
        # already sold this position, close_positions_by_token() will have removed
        # the DB row, so _load_open_fills_for_token returns empty and we land on
        # not_found here rather than attempting a duplicate sell. _sold_positions
        # only guards a same-process replay, so the DB lookup is the real
        # cross-process coordination gate -- do not bypass it.
        fills = _load_open_fills_for_token(token_id, today, db=db)
        if not fills:
            return {
                "status": "not_found",
                "detail": "No open position found for this token (it may have already been sold).",
            }

        total_shares = sum(f["size_eur"] / (f["price_cents"] / 100) for f in fills)
        total_eur = sum(f["size_eur"] for f in fills)
        bracket_low = fills[0].get("bracket_low")
        bracket_high = fills[0].get("bracket_high")
        station = fills[0].get("station", "")
        question = fills[0].get("question", "")
        side = fills[0].get("side", "NO")

        log.info(
            "  [manual] operator sell [%s] %s %s... -- %.1f shares",
            station, side, token_id[:14], total_shares,
        )
        try:
            sell_id, sell_price_or_order = live_trader.sell_position_immediate(token_id, total_shares)
        except Exception as e:
            err = str(e)
            if "balance" in err.lower() or "invalid maker amount" in err.lower():
                available = _parse_polymarket_balance(err)
                avail_shares = math.floor(available / _POLY_PRECISION) if available is not None else 0
                log.warning(
                    "  [manual] [%s] balance error -- wallet=%.6f requested=%.4f, selling %s shares",
                    station,
                    available / _POLY_PRECISION if available is not None else 0.0,
                    total_shares,
                    avail_shares,
                )
                if avail_shares > 0:
                    try:
                        sell_id, sell_price_or_order = live_trader.sell_position_immediate(
                            token_id, avail_shares,
                        )
                        total_shares = avail_shares
                    except Exception as e2:
                        log.error("  [manual] retry sell failed: %s", e2)
                        return {"status": "error", "detail": f"Balance error, retry failed: {e2}"}
                else:
                    if db is not None:
                        db.close_positions_by_token(token_id)
                    return {"status": "error", "detail": "Balance error: no shares available to sell."}
            else:
                raise
        if sell_id is None:
            # Not matched at market and cancelled -- record any partial fill so a
            # retry sells only the true remainder, then ask the caller to retry.
            cancelled_order_id = sell_price_or_order
            if cancelled_order_id:
                partial = live_trader.get_order_fill_size(cancelled_order_id)
                if partial > 0:
                    self._partial_fill_shares[token_id] = (
                        self._partial_fill_shares.get(token_id, 0.0) + partial
                    )
                    _append_live_trade({
                        "ts": ts,
                        "order_id": cancelled_order_id,
                        "station": station,
                        "end_date": today,
                        "ticker": fills[0].get("ticker", ""),
                        "no_token_id": token_id,
                        "bracket_low": bracket_low,
                        "bracket_high": bracket_high,
                        "side": "SELL",
                        "entry_side": side,
                        "shares": partial,
                        "price_cents": 0,
                        "outcome": "partial_fill",
                    }, db=db)
            return {"status": "no_fill", "detail": "Order did not fill at market -- try again."}

        sell_price_cents = sell_price_or_order
        # Cross-process coordination contract: when the dashboard runs as its
        # own process (run_dashboard.py), this _sold_positions set is NOT shared
        # with the bot process -- it only guards a same-process replay. The real
        # gate that stops the bot re-selling is close_positions_by_token(): all
        # of the bot's exit loops (check_take_profit_exits, _check_stop_loss_exits,
        # _check_metar_exits) source open positions from db.get_open_positions(),
        # so once these rows are gone the token disappears from their view. The
        # only residual race -- bot already loaded the token this poll, dashboard
        # sells concurrently -- ends in a balance-zero sell error the bot's exit
        # paths catch; no order is stranded and no position is double-sold.
        self._sold_positions.add(token_id)
        self._stop_loss_strikes.pop(token_id, None)
        self._partial_fill_shares.pop(token_id, None)
        if db is not None:
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
            "entry_side": side,
            "price_cents": sell_price_cents,
            "entry_price_cents": round(avg_entry_cents),
            "shares": round(total_shares, 4),
            "size_eur": total_eur,
            "edge_cents": 0,
            "pnl": pnl,
            "outcome": "sold",
            "actual_fee_cents": estimate_fee_cents(sell_price_cents),
            "slippage": sell_price_cents - round(avg_entry_cents),
            "trigger": f"manual@{sell_price_cents}c_entry{round(avg_entry_cents)}c",
        }, db=db)
        log.info(
            "  [manual] sell %s... filled @ %sc -- [%s] %.0f-%.0fF %s  pnl=%+.2f",
            sell_id[:12], sell_price_cents, station,
            bracket_low or 0, bracket_high or 0, side, pnl,
        )
        return {
            "status": "sold",
            "order_id": sell_id,
            "sell_price_cents": sell_price_cents,
            "shares": round(total_shares, 4),
            "pnl": pnl,
        }


# Module-level singleton: imported by run.py, position_tracker, and order_executor
# so all modules share the same instance without sys.modules indirection.
order_manager = OrderManager()
