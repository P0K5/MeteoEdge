"""Read-only analysis: would following a copied wallet's *exit* (its first
SELL after our entry) have beaten holding our copy to market resolution?
(issue #1222.)

**This produces a report, not a strategy change.** Copy-trading is
BUY-only today -- ``copy_signal_loop.py`` never copies SELLs, positions
close solely via resolution in ``copy_settle.py``. This script never
writes to any ``copy_*`` table (or any table at all) and never touches the
live/paper trading path; it only reads already-settled ``copy_positions``
rows and each followed wallet's public trade tape, and writes a Markdown
report to ``backtest_results/``, mirroring ``copy_trade_backtest.py``'s
``--out``/``DEFAULT_OUT_DIR`` pattern.

**Method, precisely -- read before trusting the numbers.**
For each ``status='settled'`` copy-trading position:

1. Find the source wallet's first SELL trade on the same ``(market,
   outcome_index)`` with a ``timestamp`` strictly after our ``entry_ts``.
   Multiple qualifying sells -> the earliest one is used (a later partial
   sell of the same lingering position is not modeled -- see
   "first-sell-only" below).
2. If no such SELL exists, the position lands in the **never-exited**
   bucket -- reported as a distinct count, not dropped. Under the
   hypothetical exit-following strategy this position would still have
   been held to resolution (there is no signal to follow), so its
   exit-following P&L is defined to equal its hold-to-resolution P&L —
   an explicit modelling choice, not an invented number.
3. If found, the counterfactual exit-following P&L is: sell our
   ``stake_usd``-sized position (``stake_usd / entry_price`` shares,
   using our own already-stored fill price, never re-derived) at that
   trade's price, worsened by ``apply_slippage(price, "SELL",
   DEFAULT_SLIPPAGE_BPS)`` -- the same 150bps cost model the entry side
   already assumes (``copy_trade_backtest.py``). The actual
   hold-to-resolution P&L is read verbatim from ``settled_pnl_usd``
   (``src/data/copy_pnl.py``'s convention) -- never recomputed.

**Outcome_index disambiguation -- read before trusting the "exited"
bucket.** A SELL trade on the same *market* is only counted as an exit of
our copied position if it shares that position's ``outcome_index`` (0 or
1) -- ``outcome_index`` is the positional slot documented in
``polymarket_traders.normalize_trade`` as the reliable way to know which
side of a binary market a trade is on, so matching it is the most the
public trade tape supports for identifying "the wallet reduced its
holding of the side we copied." **What this does NOT establish, and this
script does not claim to:** Polymarket outcome tokens are fungible with no
per-lot tracking, so a matching SELL cannot be proven to be closing
*our specific copied lot* rather than trimming a larger or differently
-timed position in the same token; nor can it distinguish a full close
from a partial one. A SELL trade with no ``outcome_index`` in the raw
record (some historical rows lack it) is excluded from the sell index
entirely rather than guessed into a side -- counted separately as a data
-quality note if non-zero, never silently matched by market alone.

**Other limitations, stated explicitly per the acceptance criteria:**
- **First-sell-only.** Only the earliest qualifying SELL is used; partial
  exits and any subsequent re-entries/adds are not reconstructed.
- **No latency modelling.** The SELL-side slippage above is the same flat
  reaction-latency stand-in ``copy_trade_backtest.py`` uses for entries --
  not a tape replay at (trade.timestamp + latency).
- **A real implementation would exit later than this counterfactual.** A
  live exit-following strategy learns of the source wallet's SELL through
  a poll loop, so it would fill at least one poll interval after the
  timestamp used here, at a plausibly worse price than modelled.
- **hours_to_resolution is measured to our own ``settled_at``**, i.e. when
  ``copy_settle.py`` observed and recorded the resolution, not the
  market's true resolution instant -- accurate to within one settlement
  run's cadence (currently 15 minutes; see
  ``deploy/systemd/meteoedge-copy-settle.timer``).

Usage::

    python -m src.scripts.copy_exit_analysis
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.scripts.copy_trade_backtest import (  # noqa: E402
    DEFAULT_SLIPPAGE_BPS,
    apply_slippage,
)
from src.data.polymarket_traders import get_wallet_trades, normalize_trade  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "backtest_results"


def _parse_ts_to_unix(ts: "str | None") -> "int | None":
    """Parse an ISO-8601 timestamp (as written to ``entry_ts``/``settled_at``)
    to unix seconds. Returns ``None`` on a missing/unparseable value rather
    than raising -- one bad row must never abort the whole run."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def index_wallet_sells(address: str) -> dict:
    """Fetch *address*'s full trade tape **once** and index its SELL trades
    by ``(market, outcome_index)``, each bucket sorted ascending by
    timestamp -- so every settled position for this wallet is checked
    against an in-memory index rather than a fresh API call per position
    (the shared Polymarket 1 req/sec budget is already contended, see
    #1221).

    SELL trades with no usable ``outcome_index`` are excluded (not
    guessed into a side) -- see the module docstring's disambiguation
    section. Returns ``{}`` for a wallet with no SELL trades (or no trades
    at all) -- callers see an empty index, not an error.
    """
    raw_trades = get_wallet_trades(address)
    index: dict = {}
    n_sell_no_index = 0
    for raw in raw_trades:
        trade = normalize_trade(raw)
        if trade is None or trade["side"] != "SELL":
            continue
        if trade.get("outcome_index") is None:
            n_sell_no_index += 1
            continue
        key = (trade["market"], trade["outcome_index"])
        index.setdefault(key, []).append(trade)
    for bucket in index.values():
        bucket.sort(key=lambda t: t["timestamp"])
    if n_sell_no_index:
        log.info(
            "[copy-exit-analysis] %s: %s SELL trade(s) dropped (no outcome_index) -- "
            "excluded from the exit index, not guessed into a side",
            address, n_sell_no_index,
        )
    return index


def find_first_exit(sell_index: dict, market: str, outcome_index: int, entry_ts_unix: int) -> "dict | None":
    """Return the earliest SELL trade on *market*/*outcome_index* with a
    timestamp strictly after *entry_ts_unix*, or ``None`` if none exists.

    A sell at or before ``entry_ts_unix`` cannot be an exit of a position
    that did not exist yet, so it is ignored. ``sell_index``'s buckets are
    pre-sorted ascending (see ``index_wallet_sells``), so the first match
    found here is already the earliest one.
    """
    for trade in sell_index.get((market, outcome_index), ()):
        if trade["timestamp"] > entry_ts_unix:
            return trade
    return None


def exit_following_pnl_usd(
    entry_price: float, stake_usd: float, sell_price: float, slippage_bps: float,
) -> "float | None":
    """Counterfactual $ P&L of exiting a ``stake_usd``-sized position
    (bought at our own already-stored *entry_price*, never re-derived)
    into *sell_price* worsened by ``apply_slippage(..., "SELL", ...)``.

    ``None`` if *entry_price* is non-positive -- a real fill can't happen
    at price <= 0 (mirrors ``compute_realized_pnl_usd``'s winning-branch
    guard in ``src/data/copy_pnl.py``), so this is a data-integrity
    signal to skip, not a value to divide by.
    """
    if entry_price <= 0:
        return None
    copier_exit_price = apply_slippage(sell_price, "SELL", slippage_bps)
    shares = stake_usd / entry_price
    return shares * copier_exit_price - stake_usd


def analyze_position(position: dict, sell_index: dict, slippage_bps: float) -> dict:
    """Compare one settled position's hold-to-resolution outcome against
    its exit-following counterfactual.

    Returns a dict with (at minimum): ``hold_pnl_usd`` (verbatim
    ``settled_pnl_usd``), ``is_winner``, ``exited`` (bool), and, only when
    ``exited`` is True, ``exit_pnl_usd``/``hours_to_exit``. Never raises --
    an unparseable ``entry_ts`` or a non-positive ``entry_price`` lands
    this position in the never-exited bucket rather than crashing the run.
    """
    hold_pnl = position.get("settled_pnl_usd")
    hold_pnl = float(hold_pnl) if hold_pnl is not None else 0.0
    result = {
        "market": position["market"],
        "outcome_index": position["outcome_index"],
        "hold_pnl_usd": hold_pnl,
        "is_winner": hold_pnl > 0,
        "exited": False,
        "exit_pnl_usd": None,
        "hours_to_exit": None,
        "hours_to_resolution": None,
    }

    entry_ts_unix = _parse_ts_to_unix(position.get("entry_ts"))
    settled_at_unix = _parse_ts_to_unix(position.get("settled_at"))
    if entry_ts_unix is not None and settled_at_unix is not None:
        result["hours_to_resolution"] = (settled_at_unix - entry_ts_unix) / 3600.0

    if entry_ts_unix is None:
        return result

    exit_trade = find_first_exit(
        sell_index, position["market"], position["outcome_index"], entry_ts_unix,
    )
    if exit_trade is None:
        return result

    exit_pnl = exit_following_pnl_usd(
        float(position["entry_price"]), float(position["stake_usd"]),
        exit_trade["price"], slippage_bps,
    )
    if exit_pnl is None:
        return result

    result["exited"] = True
    result["exit_pnl_usd"] = exit_pnl
    result["hours_to_exit"] = (exit_trade["timestamp"] - entry_ts_unix) / 3600.0
    return result


def _effective_exit_pnl(row: dict) -> float:
    """Exit-following P&L for one analyzed position: the counterfactual
    when a qualifying exit was found, else the same hold-to-resolution
    P&L (no exit signal exists for the hypothetical strategy to follow --
    see the module docstring's never-exited-bucket rationale)."""
    return row["exit_pnl_usd"] if row["exited"] else row["hold_pnl_usd"]


def _bucket_summary(rows: "list[dict]") -> dict:
    """Aggregate one bucket of analyzed positions (all positions, or a
    winners-only / losers-only slice) into the report's summary figures."""
    n = len(rows)
    exited_rows = [r for r in rows if r["exited"]]
    hold_total = sum(r["hold_pnl_usd"] for r in rows)
    exit_total = sum(_effective_exit_pnl(r) for r in rows)
    hours_to_exit = [r["hours_to_exit"] for r in exited_rows if r["hours_to_exit"] is not None]
    hours_to_resolution = [
        r["hours_to_resolution"] for r in rows if r["hours_to_resolution"] is not None
    ]
    return {
        "n": n,
        "n_exited": len(exited_rows),
        "n_never_exited": n - len(exited_rows),
        "pct_exited": (len(exited_rows) / n) if n else None,
        "hold_total_pnl": round(hold_total, 2),
        "exit_total_pnl": round(exit_total, 2),
        "hold_avg_pnl": round(hold_total / n, 4) if n else None,
        "exit_avg_pnl": round(exit_total / n, 4) if n else None,
        "median_hours_to_exit": round(median(hours_to_exit), 2) if hours_to_exit else None,
        "median_hours_to_resolution": (
            round(median(hours_to_resolution), 2) if hours_to_resolution else None
        ),
    }


def analyze_wallet(address: str, positions: "list[dict]", slippage_bps: float) -> dict:
    """Run the full exit-vs-hold analysis for one wallet's settled
    positions.

    Fetches *address*'s trade tape exactly once (via
    ``index_wallet_sells``), regardless of how many settled positions it
    has. Returns ``{"address", "no_data": True}`` without any network call
    at all when *positions* is empty -- a wallet with zero settled
    positions is reported, not silently skipped, but never costs a wasted
    fetch against the shared rate budget.
    """
    if not positions:
        return {"address": address, "no_data": True}

    sell_index = index_wallet_sells(address)
    rows = [analyze_position(p, sell_index, slippage_bps) for p in positions]

    winners = [r for r in rows if r["is_winner"]]
    losers = [r for r in rows if not r["is_winner"]]

    return {
        "address": address,
        "no_data": False,
        "overall": _bucket_summary(rows),
        "winners": _bucket_summary(winners),
        "losers": _bucket_summary(losers),
    }


def _fmt_pct(v: "float | None") -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _fmt_hours(v: "float | None") -> str:
    return "n/a" if v is None else f"{v:.1f}h"


def _fmt_usd(v: "float | None") -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def build_report(run_date: str, slippage_bps: float, wallet_results: "list[dict]") -> str:
    lines = ["# Copy-Trading Exit-Following Analysis\n"]
    lines.append(f"**Run date:** {run_date}  ")
    lines.append(
        "**Read-only report -- no live/paper trading changes of any kind.** "
        "Compares actual hold-to-resolution P&L (``settled_pnl_usd``) against a "
        "counterfactual \"follow the source wallet's first exit\" scenario. See "
        "the module docstring (`src/scripts/copy_exit_analysis.py`) for exactly "
        "what is and isn't modeled -- first-sell-only, no latency modelling, "
        "and the outcome_index disambiguation limits.  \n"
    )
    lines.append(f"**Assumed copier exit slippage:** {slippage_bps:.0f} bps flat, worse fill only.  \n")
    lines.append("\n---\n")

    with_data = [r for r in wallet_results if not r["no_data"]]
    no_data = [r for r in wallet_results if r["no_data"]]

    lines.append("## Per-wallet summary\n")
    lines.append(
        "| wallet | positions | exited (n / %) | never exited | hold $ PnL | "
        "exit-following $ PnL | median h to exit | median h to resolution |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in with_data:
        o = r["overall"]
        lines.append(
            f"| `{r['address'][:10]}…` | {o['n']} | {o['n_exited']} / {_fmt_pct(o['pct_exited'])} | "
            f"{o['n_never_exited']} | {_fmt_usd(o['hold_total_pnl'])} | "
            f"{_fmt_usd(o['exit_total_pnl'])} | {_fmt_hours(o['median_hours_to_exit'])} | "
            f"{_fmt_hours(o['median_hours_to_resolution'])} |"
        )
    lines.append("")

    if no_data:
        lines.append(
            f"**No settled positions** ({len(no_data)} wallet(s), reported not dropped): "
            + ", ".join(f"`{r['address'][:10]}…`" for r in no_data) + "  \n"
        )

    lines.append("\n## Winners vs losers (hold-to-resolution outcome)\n")
    lines.append(
        "Splits each wallet's positions by their *actual* hold-to-resolution "
        "outcome (win = positive `settled_pnl_usd`) to show whether "
        "exit-following mostly cuts losses, mostly caps gains, or both.\n"
    )
    lines.append(
        "| wallet | winners: n / hold $ / exit-following $ | losers: n / hold $ / exit-following $ |"
    )
    lines.append("|---|---|---|")
    for r in with_data:
        w, lo = r["winners"], r["losers"]
        lines.append(
            f"| `{r['address'][:10]}…` | {w['n']} / {_fmt_usd(w['hold_total_pnl'])} / "
            f"{_fmt_usd(w['exit_total_pnl'])} | {lo['n']} / {_fmt_usd(lo['hold_total_pnl'])} / "
            f"{_fmt_usd(lo['exit_total_pnl'])} |"
        )

    lines.append("\n## Aggregate (all wallets)\n")
    all_rows_overall = [r["overall"] for r in with_data]
    total_n = sum(o["n"] for o in all_rows_overall)
    total_exited = sum(o["n_exited"] for o in all_rows_overall)
    total_hold = sum(o["hold_total_pnl"] for o in all_rows_overall)
    total_exit = sum(o["exit_total_pnl"] for o in all_rows_overall)
    all_hours_to_exit = [
        r for wr in with_data for r in [wr["overall"]["median_hours_to_exit"]] if r is not None
    ]
    all_hours_to_resolution = [
        r for wr in with_data for r in [wr["overall"]["median_hours_to_resolution"]] if r is not None
    ]
    lines.append(f"- Wallets with settled positions analyzed: {len(with_data)}\n")
    lines.append(f"- Wallets with no settled positions (reported, not dropped): {len(no_data)}\n")
    lines.append(f"- Total settled positions: {total_n}\n")
    lines.append(
        f"- Positions where the wallet exited before resolution: {total_exited} "
        f"({_fmt_pct(total_exited / total_n) if total_n else 'n/a'})\n"
    )
    lines.append(f"- Aggregate hold-to-resolution $ PnL: {_fmt_usd(round(total_hold, 2))}\n")
    lines.append(
        f"- Aggregate exit-following $ PnL ({slippage_bps:.0f} bps SELL slippage): "
        f"{_fmt_usd(round(total_exit, 2))}\n"
    )
    lines.append(
        f"- Exit-following minus hold-to-resolution: "
        f"{_fmt_usd(round(total_exit - total_hold, 2))}\n"
    )
    if all_hours_to_exit and all_hours_to_resolution:
        lines.append(
            f"- Median of per-wallet median hours-to-exit: {_fmt_hours(median(all_hours_to_exit))} "
            f"vs. median of per-wallet median hours-to-resolution: "
            f"{_fmt_hours(median(all_hours_to_resolution))} -- the turnover signal.\n"
        )

    lines.append("\n## Limitations\n")
    lines.append(
        "- **First-sell-only**: only the earliest qualifying SELL per position is "
        "used; partial exits and later re-entries are not reconstructed.\n"
    )
    lines.append(
        "- **No latency modelling**: SELL-side slippage is a flat stand-in "
        "(`apply_slippage`, same convention as `copy_trade_backtest.py`'s entry "
        "side), not a tape replay at (trade.timestamp + reaction latency).\n"
    )
    lines.append(
        "- **A real implementation would exit later than modelled here**: a live "
        "exit-following strategy only learns of the source wallet's SELL through "
        "a poll loop, so it would fill at least one poll interval after the "
        "timestamp used above, at a plausibly worse price.\n"
    )
    lines.append(
        "- **outcome_index disambiguation is the most the public trade tape "
        "supports, not a proof of lot-level closure**: a matching SELL on "
        "(market, outcome_index) is treated as \"the wallet reduced its holding "
        "of the side we copied,\" but Polymarket outcome tokens are fungible with "
        "no per-lot tracking -- this cannot distinguish closing our specific "
        "copied lot from trimming an unrelated, larger, or differently-timed "
        "position in the same token, nor a full close from a partial one. This "
        "is not reliably determinable from the tape alone; treat the exited/"
        "never-exited split as an approximation on that basis, not a certainty.\n"
    )
    lines.append(
        "- **hours_to_resolution is measured to our own `settled_at`** (when "
        "`copy_settle.py` observed the resolution), not the market's true "
        "resolution instant -- accurate to within one settlement run's cadence.\n"
    )
    return "\n".join(lines)


def run(
    db, slippage_bps: float, out_dir: Path, run_date: "str | None" = None,
) -> int:
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    # Union of currently-/previously-followed addresses and any address with
    # settled history -- mirrors the dashboard's copy_trading_positions()
    # rationale (src/dashboard/api.py): unfollowing a wallet doesn't delete
    # its copy_positions rows, and a wallet with zero settled positions must
    # still be reported (not silently absent), never crash.
    addresses = {row["address"] for row in db.get_copy_realized_pnl_by_wallet()}
    addresses |= {w["address"] for w in db.get_followed_wallets()}

    if not addresses:
        log.info("[copy-exit-analysis] no followed wallets and no settled positions found.")
        return 0

    wallet_results = []
    for address in sorted(addresses):
        positions = db.get_settled_copy_positions(address)
        wallet_results.append(analyze_wallet(address, positions, slippage_bps))

    report = build_report(run_date, slippage_bps, wallet_results)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"copy_exit_analysis_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[copy-exit-analysis] wrote %s (%s wallets)", out_path, len(wallet_results))
    return 0


def _open_db():
    """Return a Database handle, or None if the DB cannot be opened."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        log.warning("[copy-exit-analysis] DB unavailable: %s -- skipping run", e)
        return None


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None)
    args = ap.parse_args(argv)

    db = _open_db()
    if db is None:
        return 1
    return run(db, args.slippage_bps, args.out, args.run_date)


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
