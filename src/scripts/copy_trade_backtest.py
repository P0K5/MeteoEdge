"""Copy-trading hypothesis spike: would mirroring Polymarket's most
profitable wallets, with a conservative fill-price penalty standing in for
reaction latency, have been profitable historically? (Exploratory
data-collection spike requested 2026-09-16 -- no pre-registered issue; the
question was "is this hypothesis even worth building further", so this
report is the answer to that, not a FINDING/NULL-style committed result.)

**What this measures, precisely -- read before trusting the numbers.**
For each candidate wallet's BUY trades only (SELL/exit trades are counted
but excluded from scoring -- this does NOT attempt position-lifecycle
reconstruction, i.e. matching a SELL to the BUY(s) it closes. Every BUY is
treated as held to market resolution, which overstates a trader who exits
early and understates one who adds to winners):

    trader_pnl_per_contract  = resolution_payout - trade.price
    copier_fill_price        = trade.price worsened by --slippage-bps
    copier_pnl_per_contract  = resolution_payout - copier_fill_price

``resolution_payout`` is 1.0 if the trade's ``outcome`` side won, 0.0 if it
lost -- from ``src.data.polymarket.fetch_market_resolution``, reused
verbatim (the same UMA-resolution truth source the live scanner settles
against), never re-derived.

**--slippage-bps is a flat, single-number stand-in for reaction latency,
not a latency simulation.** It does not replay the market's own trade tape
at (trade.timestamp + latency) -- the Data API does not make that cheap to
do across hundreds of trades in a spike, and it was not attempted here.
Read the output as "does the edge survive an assumed N bps of adverse
fill", not as a latency-accurate backtest. Replaying each market's real
tape is the natural next step if this shows promise.

**Position sizing.** Aggregate $ PnL mirrors the original trader's own
``size`` (share count) verbatim -- "if I had bought exactly what they
bought, when they bought it, at a worse price." The per-trade ROI stats
next to it are size-independent (win rate, mean/median ROI per contract)
and are the more robust read when candidate wallets differ wildly in size.

Usage::

    python -m src.scripts.copy_trade_backtest --window month --top 20
    python -m src.scripts.copy_trade_backtest --wallets 0xabc...,0xdef...
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.polymarket import fetch_market_resolution  # noqa: E402
from src.data.polymarket_traders import (  # noqa: E402
    get_leaderboard,
    get_wallet_trades,
    normalize_trade,
    wallet_address,
)

log = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "backtest_results"
#: 1.5% -- a conservative, round-number stand-in for reaction-latency fill
#: slippage. Not derived from any measurement; see module docstring.
DEFAULT_SLIPPAGE_BPS = 150.0


def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    """Worse fill for the copier: higher price on a BUY, lower on a SELL.
    Clipped to a valid probability range -- a Polymarket YES/NO price is
    always in (0, 1)."""
    factor = slippage_bps / 10_000.0
    adjusted = price * (1 + factor) if side == "BUY" else price * (1 - factor)
    return min(max(adjusted, 0.0001), 0.9999)


def resolve_payout(market: str, outcome: "str | None", cache: dict) -> "float | None":
    """1.0 if *outcome* won, 0.0 if it lost, None if unresolved/unknown.

    Cached per market in *cache* -- a wallet's trade tape can hit the same
    market many times, and ``fetch_market_resolution`` has no caching of
    its own (it always hits the network).
    """
    if market not in cache:
        cache[market] = fetch_market_resolution(market)
    won = cache[market]
    if won is None or not outcome:
        return None
    outcome_is_yes = outcome.strip().lower() == "yes"
    return 1.0 if (won == outcome_is_yes) else 0.0


def _stats(rois: "list[float]") -> dict:
    if not rois:
        return {"n": 0, "win_rate": None, "mean_roi": None, "median_roi": None}
    wins = sum(1 for r in rois if r > 0)
    return {
        "n": len(rois),
        "win_rate": wins / len(rois),
        "mean_roi": mean(rois),
        "median_roi": median(rois),
    }


def backtest_wallet(address: str, slippage_bps: float) -> dict:
    """Run the copy-trade simulation for one wallet's BUY trades.

    Per-trade fields are only held in memory long enough to aggregate, not
    returned -- keeps the report a fixed size regardless of how many fills
    a wallet has.
    """
    raw_trades = get_wallet_trades(address)
    resolution_cache: dict = {}

    trader_rois: "list[float]" = []
    copier_rois: "list[float]" = []
    trader_dollar_pnl = 0.0
    copier_dollar_pnl = 0.0
    n_buy = 0
    n_sell_excluded = 0

    for raw in raw_trades:
        trade = normalize_trade(raw)
        if trade is None:
            continue
        if trade["side"] != "BUY":
            n_sell_excluded += 1
            continue
        n_buy += 1

        payout = resolve_payout(trade["market"], trade["outcome"], resolution_cache)
        if payout is None:
            continue

        trader_price = trade["price"]
        if trader_price <= 0:
            continue
        copier_price = apply_slippage(trader_price, "BUY", slippage_bps)

        trader_rois.append((payout - trader_price) / trader_price)
        copier_rois.append((payout - copier_price) / copier_price)
        trader_dollar_pnl += (payout - trader_price) * trade["size"]
        copier_dollar_pnl += (payout - copier_price) * trade["size"]

    return {
        "address": address,
        "n_buy_trades": n_buy,
        "n_sell_excluded": n_sell_excluded,
        "n_resolved": len(trader_rois),
        "n_unresolved_dropped": n_buy - len(trader_rois),
        "trader": {**_stats(trader_rois), "dollar_pnl": round(trader_dollar_pnl, 2)},
        "copier": {**_stats(copier_rois), "dollar_pnl": round(copier_dollar_pnl, 2)},
    }


def _fmt_pct(v: "float | None") -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def build_report(run_date: str, slippage_bps: float, results: "list[dict]") -> str:
    lines = ["# Copy-Trading Hypothesis Spike (data collection + backtest)\n"]
    lines.append(f"**Run date:** {run_date}  ")
    lines.append(
        "**Status: exploratory spike, not a pre-registered finding.** No "
        "live-trading changes of any kind. This only answers whether the "
        "hypothesis is worth building further -- see the module docstring "
        "(`src/scripts/copy_trade_backtest.py`) for exactly what is and "
        "isn't modeled: BUY-only (no position-lifecycle reconstruction), a "
        "flat slippage stand-in for reaction latency (not a tape replay), "
        "size-mirrored aggregate $ PnL.  \n"
    )
    lines.append(f"**Assumed copier slippage:** {slippage_bps:.0f} bps flat, worse fill only.  \n")
    lines.append("\n---\n")

    lines.append("## Per-wallet summary\n")
    lines.append(
        "| wallet | BUY trades | resolved | trader win% | trader mean ROI | "
        "copier win% | copier mean ROI | copier $ PnL (mirrored size) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        t, c = r["trader"], r["copier"]
        lines.append(
            f"| `{r['address'][:10]}…` | {r['n_buy_trades']} | {r['n_resolved']} | "
            f"{_fmt_pct(t['win_rate'])} | {_fmt_pct(t['mean_roi'])} | "
            f"{_fmt_pct(c['win_rate'])} | {_fmt_pct(c['mean_roi'])} | "
            f"${c['dollar_pnl']:,.2f} |"
        )

    total_resolved = sum(r["n_resolved"] for r in results)
    total_copier_pnl = sum(r["copier"]["dollar_pnl"] for r in results)
    total_trader_pnl = sum(r["trader"]["dollar_pnl"] for r in results)
    profitable_copier_wallets = sum(1 for r in results if r["copier"]["dollar_pnl"] > 0)

    lines.append("\n## Aggregate\n")
    lines.append(f"- Wallets evaluated: {len(results)}\n")
    lines.append(f"- Total resolved BUY trades: {total_resolved}\n")
    lines.append(
        f"- Wallets where the COPIER would have been net positive: "
        f"{profitable_copier_wallets}/{len(results)}\n"
    )
    lines.append(f"- Aggregate original-trader $ PnL (mirrored sizing): {total_trader_pnl:,.2f}\n")
    lines.append(
        f"- Aggregate copier $ PnL (mirrored sizing, {slippage_bps:.0f} bps slippage): "
        f"{total_copier_pnl:,.2f}\n"
    )
    return "\n".join(lines)


def run(
    window: str, top: int, wallets: "list[str] | None", slippage_bps: float,
    out_dir: Path, run_date: "str | None" = None,
) -> int:
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    if wallets:
        addresses = wallets
    else:
        leaderboard = get_leaderboard(window=window, limit=top)
        addresses = [a for a in (wallet_address(e) for e in leaderboard) if a]
        if not addresses:
            log.error(
                "[copy-trade] leaderboard returned no usable wallet addresses "
                "(endpoint may be unavailable, or its response shape has "
                "changed -- see get_leaderboard's docstring in "
                "src/data/polymarket_traders.py). Pass --wallets explicitly "
                "to bypass it."
            )
            return 1

    results = [backtest_wallet(addr, slippage_bps) for addr in addresses]
    results = [r for r in results if r["n_resolved"] > 0]
    if not results:
        log.info("[copy-trade] no wallet produced any resolved BUY trade -- nothing to report.")
        return 0

    report = build_report(run_date, slippage_bps, results)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"copy_trade_backtest_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[copy-trade] wrote %s (%s wallets)", out_path, len(results))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", default="month", choices=sorted(["day", "week", "month", "all"]))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument(
        "--wallets", default=None,
        help="Comma-separated wallet addresses, bypassing the leaderboard fetch.",
    )
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None)
    args = ap.parse_args(argv)
    wallets = [w.strip() for w in args.wallets.split(",")] if args.wallets else None
    return run(args.window, args.top, wallets, args.slippage_bps, args.out, args.run_date)


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
