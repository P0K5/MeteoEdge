"""DRY-RUN report: recover the real USD fill cost (and corrected settled
P&L) for ``copy_live_positions`` rows written before issue #1336, via the
Data API trade tape instead of the CLOB order-status endpoint.

**This script is read-only and never writes to any database.** It opens the
SQLite file with ``mode=ro`` and has no write path at all. Applying the
corrections to a live database is a SEPARATE, operator-approved step --
deliberately not implemented here (same safety contract #1336/#1338
established; see issue #1342's own acceptance criteria).

**Why this is a rewrite, not a tweak (issue #1342).** The original version
of this script (#1336/#1338) recovered a row's real fill size via the CLOB
``get_order()`` endpoint. That endpoint returns an empty response for any
order older than roughly 1-2 days -- confirmed on a local run and
independently on the production host on 2026-10-07. Every pre-#1336 row
this script exists to fix is, by definition, older than that, so the CLOB
path was permanently dead for this script's actual job. The fix: match each
row against ``src.data.polymarket_traders.get_wallet_trades()``, the public,
unauthenticated Data API trade tape for *our own* deposit wallet
(``POLYMARKET_DEPOSIT_WALLET`` -- never the followed wallet's address stored
on the row itself), which a manual investigation (2026-10-07, see PM
session notes on issue #1342) verified back to May 2026.

**The Data API lookup has its own known gap -- it can still miss real
fills.** The same 2026-10-07 investigation matched 19 pre-fix settled
``copy_live_positions`` rows for wallet
``0xbca08c1bc204a34f2fddbe47b438b9bd42ac9705`` against this trade tape:
15 matched cleanly (one loss cost $0.83 on-chain but was booked at the full
$3.00 -- net correction across those 15 rows alone: +$7.50, i.e. true losses
smaller than the ledger showed). The remaining 4 had **no match** in the
Data API feed, yet ``logs/copy_signals.log`` independently confirms they
filled for real, straight from the exchange's own authoritative order-status
endpoint (``clob.polymarket.com/data/order/...`` -- e.g. ``[copy-live] filled
0x45ed7fd684...``). These are NOT fabricated/ghost rows -- the Data API's
``/trades`` feed appears to miss some real fills (suspected maker-side
indexing gap). ``KNOWN_DATA_API_GAP_ORDER_IDS`` below records the exact
order IDs this was confirmed against, for a future regression fixture. A
20th row that initially looked ambiguous (its stored ``fill_price`` didn't
match the apparent candidate trade) resolved cleanly once matched by market
directly rather than by user+time -- i.e. this script's match key
(``conditionId``/``outcomeIndex``/timestamp window), not an identity lookup.

**This script never guesses.** Exactly one matching BUY trade -> resolved,
with a corrected cost and P&L reported alongside the stored value. Zero or
more than one match -> unresolved, with a reason, and the row's stored
ledger value is left exactly as-is in the report (and, obviously, in the
DB -- nothing is written). The summary never blends resolved and unresolved
rows into one number -- see ``build_report``'s ``summary`` shape.

Per row (scope: ``status='settled'`` and ``filled_stake_usd IS NULL``):

- Find the deposit wallet's BUY trades on the same ``(market,
  outcome_index)`` with ``|trade.timestamp - entry_ts| <= window_seconds``
  (default ``DEFAULT_WINDOW_SECONDS`` -- see its own docstring for why
  that value).
- Exactly one match -> ``real_cost = size * price``; corrected P&L via
  ``compute_realized_pnl_usd`` (loss: ``-real_cost``; win:
  ``size - real_cost``, since ``compute_realized_pnl_usd(entry_price=price,
  stake_usd=real_cost, ...)`` divides back out to exactly ``size`` shares).
- Zero matches -> ``unresolved`` / ``no_match``.
- More than one match -> ``unresolved`` / ``ambiguous_match`` (the
  candidate count and timestamps are included for a human to disambiguate
  manually -- this script will not guess which one).
- An unparseable/missing ``entry_ts`` -> ``unresolved`` /
  ``unparseable_entry_ts``.
- A matched trade but an unresolved market (``get_resolution`` returns
  ``None``) -> ``unresolved`` / ``market_unresolved`` (should not happen for
  a ``status='settled'`` row in practice, but handled rather than assumed).

Drift context (``--capital``/``--actual-balance``, optional, same formula as
``copy_live_settle.check_wallet_balance_drift``:
``expected_balance = capital - committed_over_open_rows + realized_over_settled_rows``)
is reported before/after applying ONLY the resolved corrections -- open
(``filled``/``partial``) rows are untouched by this script (out of scope per
issue #1342's acceptance criteria; they are recent enough that the CLOB path
or #1336's go-forward fix already covers them) and unresolved settled rows
keep their current stored (conservative, upper-bound) value in both the
before and after totals.

Usage (read-only, no network credentials needed -- the Data API is public)::

    python -m src.scripts.copy_live_fill_cost_backfill --db data/meteoedge.db \\
        --capital 40 --actual-balance 21.53
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.copy_pnl import compute_realized_pnl_usd, effective_stake_usd  # noqa: E402
from src.data.polymarket_traders import get_wallet_trades, normalize_trade  # noqa: E402

#: Scope: pre-#1336 settled rows that never got a real fill cost recorded.
_SCOPE_SQL = (
    "SELECT id, market, outcome_index, status, order_id, fill_price, "
    "stake_usd, filled_stake_usd, settled_pnl_usd, entry_ts FROM copy_live_positions "
    "WHERE status='settled' ORDER BY id"
)

#: Committed exposure over still-open rows -- untouched by this script, but
#: needed for the optional --capital/--actual-balance drift context, same
#: query shape as copy_live_settle.check_wallet_balance_drift ('pending'
#: rows are not counted -- see that function's own docstring).
_OPEN_SQL = (
    "SELECT stake_usd, filled_stake_usd FROM copy_live_positions "
    "WHERE status IN ('filled','partial')"
)

#: 1200s (20 minutes) was sufficient to disambiguate every row in the
#: 2026-10-07 investigation (19 of 20 matched cleanly in one pass; the one
#: apparent ambiguity resolved once matched by market rather than by
#: user+time -- see the module docstring) and is still short enough that two
#: genuinely distinct BUY fills on the same market/outcome within 20 minutes
#: correctly surface as ambiguous rather than silently picking one.
#: Configurable via --window-seconds for a future wallet/window where this
#: default isn't right.
DEFAULT_WINDOW_SECONDS = 1200

#: Confirmed exchange-side fills NOT present in the Data API's /trades feed
#: for wallet 0xbca08c1bc204a34f2fddbe47b438b9bd42ac9705, cross-checked
#: against logs/copy_signals.log's "[copy-live] filled ..." lines (which read
#: Polymarket's own authoritative order-status endpoint,
#: clob.polymarket.com/data/order/...) on 2026-10-05/06. These are real
#: fills the Data API's user-trades feed misses (suspected maker-side
#: indexing gap) -- NOT fabricated/ghost rows. Kept here as a fixture/
#: regression reference: a backfill run against this wallet must report
#: the corresponding rows as unresolved/no_match, never guess a cost for
#: them. See issue #1342.
KNOWN_DATA_API_GAP_ORDER_IDS = (
    "0x45ed7fd68415c34c0be53c70556fba6691b31bc0b34a064f29e7eb2618f770a5",
    "0xaa2e3e77059dbba4a0bfc13f6ff6d553a66ca33e4ec8aa4ecc63caf028eea23c",
    "0xddae64624da9fc5ed808827e54348891307ffb4c40356f838f5145a4f54ee6f6",
    "0x58ee7964cf2039d49a707fb26251659f1bd9baced104fc14eb8e2a533c241e7a",
    "0x4119097b5a696f466e83f00615040be966811ff107f2cbfcb216c6c4416f0ed1",
)


def _parse_ts_to_unix(ts: "str | None") -> "int | None":
    """Parse an ISO-8601 timestamp (as written to ``entry_ts``) to unix
    seconds. Returns ``None`` on a missing/unparseable value rather than
    raising -- one bad row must never abort the whole run. Mirrors
    ``copy_exit_analysis._parse_ts_to_unix`` (same format, same contract)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def index_wallet_buys(address: str) -> dict:
    """Fetch *address*'s full trade tape **once** and index its BUY trades
    by ``(market, outcome_index)``, each bucket sorted ascending by
    timestamp. Mirrors ``copy_exit_analysis.index_wallet_sells`` (same
    "fetch once, index in memory" rationale -- the shared Polymarket API
    budget is already contended, see #1221).

    BUY trades with no usable ``outcome_index`` are excluded (not guessed
    into a side), matching ``index_wallet_sells``'s own exclusion rule.
    Returns ``{}`` for a wallet with no BUY trades (or no trades at all).
    """
    raw_trades = get_wallet_trades(address)
    index: dict = {}
    for raw in raw_trades:
        trade = normalize_trade(raw)
        if trade is None or trade["side"] != "BUY":
            continue
        if trade.get("outcome_index") is None:
            continue
        key = (trade["market"], trade["outcome_index"])
        index.setdefault(key, []).append(trade)
    for bucket in index.values():
        bucket.sort(key=lambda t: t["timestamp"])
    return index


def find_matching_buys(
    buy_index: dict, market: str, outcome_index: int, entry_ts_unix: int,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> list[dict]:
    """Return every BUY trade on *market*/*outcome_index* within
    *window_seconds* of *entry_ts_unix* (inclusive). Deliberately returns
    ALL candidates, not just the closest one -- the caller decides exactly-
    one-match vs ambiguous vs no-match; this function never picks a winner.
    """
    bucket = buy_index.get((market, outcome_index), ())
    return [t for t in bucket if abs(t["timestamp"] - entry_ts_unix) <= window_seconds]


def _r(x: "float | None", nd: int = 6):
    return None if x is None else round(x, nd)


def build_report(
    conn: sqlite3.Connection,
    *,
    buy_index: dict,
    get_resolution: Callable[[str], "bool | None"],
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    capital_usd: "float | None" = None,
    actual_balance_usd: "float | None" = None,
) -> dict:
    """Compute the dry-run report. Only SELECTs are issued on *conn*.

    *buy_index* is the deposit wallet's trade tape, pre-indexed by
    ``index_wallet_buys`` -- injected rather than fetched here so this stays
    unit-testable without network access and so one wallet fetch is shared
    across the whole run. *get_resolution* is injected for the same reason
    (mirrors the pre-#1342 script's own DI pattern).
    """
    rows = [dict(r) for r in conn.execute(_SCOPE_SQL).fetchall()]
    resolution_cache: dict = {}

    def _resolution(market: str) -> "bool | None":
        if market not in resolution_cache:
            try:
                resolution_cache[market] = get_resolution(market)
            except Exception:
                resolution_cache[market] = None
        return resolution_cache[market]

    out_rows = []
    n_in_scope = 0
    n_already_recorded = 0
    n_resolved = 0
    reason_counts = {
        "no_match": 0, "ambiguous_match": 0,
        "market_unresolved": 0, "unparseable_entry_ts": 0,
    }

    realized_before_all = 0.0  # every settled row's stored pnl (untouched baseline)
    resolved_realized_delta = 0.0
    unresolved_stake_usd = 0.0

    for r in rows:
        stake = float(r["stake_usd"])
        cost_before = effective_stake_usd(r["filled_stake_usd"], stake)
        stored = None if r["settled_pnl_usd"] is None else float(r["settled_pnl_usd"])
        if stored is not None:
            realized_before_all += stored

        if r["filled_stake_usd"] is not None:
            # Already carries a recorded real cost (post-#1336, or a prior
            # correction): out of scope, never re-priced.
            n_already_recorded += 1
            continue
        n_in_scope += 1

        base_row = {
            "id": r["id"], "market": r["market"], "outcome_index": r["outcome_index"],
            "fill_price": _r(r["fill_price"], 4), "stake_usd": _r(stake, 2),
            "cost_before": _r(cost_before, 6), "pnl_stored": _r(stored, 6),
        }

        entry_ts_unix = _parse_ts_to_unix(r["entry_ts"])
        if entry_ts_unix is None:
            reason_counts["unparseable_entry_ts"] += 1
            unresolved_stake_usd += stake
            out_rows.append({
                **base_row, "match_status": "unresolved", "reason": "unparseable_entry_ts",
                "candidate_count": 0, "cost_after": None, "pnl_after": None, "pnl_delta": None,
            })
            continue

        matches = find_matching_buys(
            buy_index, r["market"], int(r["outcome_index"]), entry_ts_unix, window_seconds,
        )
        if len(matches) != 1:
            reason = "no_match" if not matches else "ambiguous_match"
            reason_counts[reason] += 1
            unresolved_stake_usd += stake
            out_rows.append({
                **base_row, "match_status": "unresolved", "reason": reason,
                "candidate_count": len(matches), "cost_after": None, "pnl_after": None,
                "pnl_delta": None,
            })
            continue

        trade = matches[0]
        real_cost = round(float(trade["size"]) * float(trade["price"]), 6)
        yes_won = _resolution(r["market"])
        if yes_won is None:
            reason_counts["market_unresolved"] += 1
            unresolved_stake_usd += stake
            out_rows.append({
                **base_row, "match_status": "unresolved", "reason": "market_unresolved",
                "candidate_count": 1, "cost_after": _r(real_cost), "pnl_after": None,
                "pnl_delta": None,
            })
            continue

        pnl_after = round(compute_realized_pnl_usd(
            entry_price=float(trade["price"]), stake_usd=real_cost,
            outcome_index=int(r["outcome_index"]), yes_won=yes_won,
        ), 6)
        pnl_delta = round(pnl_after - (stored or 0.0), 6)
        n_resolved += 1
        resolved_realized_delta += pnl_delta
        out_rows.append({
            **base_row, "match_status": "resolved", "reason": None, "candidate_count": 1,
            "matched_trade_timestamp": trade["timestamp"], "cost_after": _r(real_cost),
            "pnl_after": _r(pnl_after), "pnl_delta": _r(pnl_delta),
        })

    n_unresolved = n_in_scope - n_resolved
    summary = {
        "rows_in_scope": n_in_scope,
        "rows_already_recorded_untouched": n_already_recorded,
        "resolved": {
            "count": n_resolved,
            "realized_pnl_delta_usd": _r(resolved_realized_delta),
        },
        "unresolved": {
            "count": n_unresolved,
            "by_reason": dict(reason_counts),
            "stake_usd_still_unverified": _r(unresolved_stake_usd, 2),
        },
    }

    if capital_usd is not None:
        committed = 0.0
        for o in conn.execute(_OPEN_SQL).fetchall():
            committed += effective_stake_usd(o["filled_stake_usd"], float(o["stake_usd"]))
        expected_before = capital_usd - committed + realized_before_all
        expected_after = expected_before + resolved_realized_delta
        summary["expected_balance_before_usd"] = _r(expected_before)
        summary["expected_balance_after_usd"] = _r(expected_after)
        if actual_balance_usd is not None:
            summary["actual_balance_usd"] = _r(actual_balance_usd)
            summary["drift_before_usd"] = _r(actual_balance_usd - expected_before)
            summary["drift_after_usd"] = _r(actual_balance_usd - expected_after)

    return {"dry_run": True, "rows": out_rows, "summary": summary}


def format_report(report: dict) -> str:
    lines = [
        "DRY RUN -- nothing has been written. Corrections are NOT applied.",
        f"{'id':>5} {'status':<10} {'px':>6} {'stake':>7} {'cost_b':>8} {'cost_a':>8} "
        f"{'pnl_st':>9} {'pnl_a':>9} {'pnl_d':>9}  reason",
    ]
    for r in report["rows"]:
        lines.append(
            f"{r['id']:>5} {r['match_status']:<10} {str(r['fill_price']):>6} "
            f"{str(r['stake_usd']):>7} {str(r['cost_before']):>8} {str(r['cost_after']):>8} "
            f"{str(r['pnl_stored']):>9} {str(r['pnl_after']):>9} {str(r['pnl_delta']):>9}  "
            f"{r['reason'] or ''}"
        )
    lines.append("")
    lines.append("SUMMARY (resolved and unresolved are reported separately -- never blended)")
    s = report["summary"]
    lines.append(f"  rows_in_scope: {s['rows_in_scope']}")
    lines.append(f"  rows_already_recorded_untouched: {s['rows_already_recorded_untouched']}")
    lines.append(f"  resolved.count: {s['resolved']['count']}")
    lines.append(f"  resolved.realized_pnl_delta_usd: {s['resolved']['realized_pnl_delta_usd']}")
    lines.append(f"  unresolved.count: {s['unresolved']['count']}")
    lines.append(f"  unresolved.by_reason: {s['unresolved']['by_reason']}")
    lines.append(f"  unresolved.stake_usd_still_unverified: {s['unresolved']['stake_usd_still_unverified']}")
    for k in (
        "expected_balance_before_usd", "expected_balance_after_usd",
        "actual_balance_usd", "drift_before_usd", "drift_after_usd",
    ):
        if k in s:
            lines.append(f"  {k}: {s[k]}")
    if s["unresolved"]["count"]:
        lines.append(
            f"NOTE: {s['unresolved']['count']} row(s) could not be verified against the Data "
            "API trade tape (see unresolved.by_reason) -- their stored ledger value is kept "
            "as-is (a conservative upper bound on loss), never guessed."
        )
    return "\n".join(lines)


def _open_readonly(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/meteoedge.db"))
    parser.add_argument("--wallet", default=os.getenv("POLYMARKET_DEPOSIT_WALLET"),
                        help="Deposit wallet to fetch the trade tape for (default: "
                             "POLYMARKET_DEPOSIT_WALLET env var -- always OUR wallet, "
                             "never the followed wallet's)")
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS,
                        help=f"Match window around entry_ts (default {DEFAULT_WINDOW_SECONDS}s)")
    parser.add_argument("--capital", type=float, default=None,
                        help="COPY_LIVE_CAPITAL_USD at ledger start (enables drift before/after)")
    parser.add_argument("--actual-balance", type=float, default=None,
                        help="Exchange USDC balance observed now (enables drift before/after)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    if not args.wallet:
        print(
            "ABORTED: no deposit wallet (pass --wallet or set POLYMARKET_DEPOSIT_WALLET) -- "
            "the dry-run needs a trade tape to match against and will not report without it.",
            file=sys.stderr,
        )
        return 2

    from src.data.polymarket import fetch_market_resolution  # noqa: PLC0415

    buy_index = index_wallet_buys(args.wallet)

    conn = _open_readonly(args.db)
    try:
        report = build_report(
            conn,
            buy_index=buy_index,
            get_resolution=fetch_market_resolution,
            window_seconds=args.window_seconds,
            capital_usd=args.capital,
            actual_balance_usd=args.actual_balance,
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
