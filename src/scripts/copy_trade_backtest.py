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

**Median ROI, not mean, is the sustainability signal -- verified against
live top-by-profit wallets 2026-09-16.** Mean ROI (and dollar PnL even more
so) is easily dominated by one or two huge tail-bet payouts: a wallet can
show a +119% mean copier ROI across 200 trades while its median is +0.01%,
i.e. the typical trade is flat and the mean is a longshot-payout artifact.
The report sorts by median ROI and shows both, precisely so a wide
mean/median gap is visible rather than hidden behind an eye-catching
average. Treat "high mean, near-zero median" as a lottery-style wallet, not
a repeatable edge, regardless of its dollar PnL.

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


def resolve_payout(
    market: str, outcome: "str | None", cache: dict, outcome_index: "int | None" = None,
) -> "float | None":
    """1.0 if the trade's side won, 0.0 if it lost, None if unresolved/unknown.

    Winner is decided positionally via *outcome_index* when available: 0
    is the same index-0/"YES" price slot ``fetch_market_resolution`` (via
    ``fetch_market_final_price``) reads from the Gamma API. This is NOT
    the same thing as *outcome*'s text label -- verified 2026-09-18 against
    live ``data-api.polymarket.com`` trades, most markets label their two
    outcomes "Up"/"Down", team names, etc., not literally "Yes"/"No", so a
    naive ``outcome.strip().lower() == "yes"`` match silently misclassifies
    almost every non-Yes/No-labeled market. That text match is kept only as
    a fallback for callers that don't have ``outcome_index`` available.

    Cached per market in *cache* -- a wallet's trade tape can hit the same
    market many times, and ``fetch_market_resolution`` has no caching of
    its own (it always hits the network).
    """
    if market not in cache:
        cache[market] = fetch_market_resolution(market)
    won = cache[market]
    if won is None:
        return None
    if outcome_index is not None:
        side_is_yes_position = outcome_index == 0
    else:
        if not outcome:
            return None
        side_is_yes_position = outcome.strip().lower() == "yes"
    return 1.0 if (won == side_is_yes_position) else 0.0


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


def backtest_wallet(
    address: str, slippage_bps: float, flat_stake: "float | None" = None,
) -> dict:
    """Run the copy-trade simulation for one wallet's BUY trades.

    *flat_stake*, when given, adds a second copier scenario alongside the
    size-mirrored one: instead of buying the same *share count* as the
    trader (at a worse price), the copier spends a fixed dollar amount on
    every trade -- "the position from our side is always the same" instead
    of scaling with whatever the trader risked. ROI stats (win rate,
    mean/median) are identical between the two scenarios -- ROI is
    per-contract, not sizing-dependent -- so only a second dollar-PnL
    figure is added (``copier_flat``), not a second full stats block.

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
    copier_flat_dollar_pnl = 0.0
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

        payout = resolve_payout(
            trade["market"], trade["outcome"], resolution_cache, trade.get("outcome_index"),
        )
        if payout is None:
            continue

        trader_price = trade["price"]
        if trader_price <= 0:
            continue
        copier_price = apply_slippage(trader_price, "BUY", slippage_bps)

        trader_roi = (payout - trader_price) / trader_price
        copier_roi = (payout - copier_price) / copier_price
        trader_rois.append(trader_roi)
        copier_rois.append(copier_roi)
        trader_dollar_pnl += (payout - trader_price) * trade["size"]
        copier_dollar_pnl += (payout - copier_price) * trade["size"]
        if flat_stake is not None:
            # flat_stake / copier_price shares bought -> PnL = flat_stake * copier_roi.
            copier_flat_dollar_pnl += flat_stake * copier_roi

    result = {
        "address": address,
        "n_buy_trades": n_buy,
        "n_sell_excluded": n_sell_excluded,
        "n_resolved": len(trader_rois),
        "n_unresolved_dropped": n_buy - len(trader_rois),
        "trader": {**_stats(trader_rois), "dollar_pnl": round(trader_dollar_pnl, 2)},
        "copier": {**_stats(copier_rois), "dollar_pnl": round(copier_dollar_pnl, 2)},
    }
    if flat_stake is not None:
        result["copier_flat"] = {
            **_stats(copier_rois),
            "dollar_pnl": round(copier_flat_dollar_pnl, 2),
        }
    return result


def _fmt_pct(v: "float | None") -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def build_report(
    run_date: str, slippage_bps: float, results: "list[dict]",
    flat_stake: "float | None" = None,
) -> str:
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
        "Sorted by copier **median** ROI, not mean or $ PnL -- mean and "
        "size-mirrored $ PnL are both easily dominated by one or two huge "
        "tail bets (a wallet can show a triple-digit mean ROI or a "
        "six-figure $ PnL while its *typical* trade is flat or a loser). "
        "Median ROI is the size-independent read on whether the typical "
        "trade actually wins, which is what \"is this a repeatable edge\" "
        "requires. A wide mean/median gap is itself a flag, not noise.\n"
    )
    has_flat = bool(results) and "copier_flat" in results[0]
    header = (
        "| wallet | BUY trades | resolved | trader win% | trader mean ROI | "
        "trader median ROI | copier win% | copier mean ROI | copier median ROI | "
        "copier $ PnL (mirrored size)"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    if has_flat:
        header += " | copier $ PnL (flat stake)"
        sep += "---|"
    header += " |"
    lines.append(header)
    lines.append(sep)
    for r in results:
        t, c = r["trader"], r["copier"]
        row = (
            f"| `{r['address'][:10]}…` | {r['n_buy_trades']} | {r['n_resolved']} | "
            f"{_fmt_pct(t['win_rate'])} | {_fmt_pct(t['mean_roi'])} | "
            f"{_fmt_pct(t['median_roi'])} | "
            f"{_fmt_pct(c['win_rate'])} | {_fmt_pct(c['mean_roi'])} | "
            f"{_fmt_pct(c['median_roi'])} | "
            f"${c['dollar_pnl']:,.2f}"
        )
        if has_flat:
            row += f" | ${r['copier_flat']['dollar_pnl']:,.2f}"
        row += " |"
        lines.append(row)

    total_resolved = sum(r["n_resolved"] for r in results)
    total_copier_pnl = sum(r["copier"]["dollar_pnl"] for r in results)
    total_trader_pnl = sum(r["trader"]["dollar_pnl"] for r in results)
    profitable_copier_wallets = sum(1 for r in results if r["copier"]["dollar_pnl"] > 0)

    lines.append("\n## Aggregate\n")
    lines.append(f"- Wallets evaluated: {len(results)}\n")
    lines.append(f"- Total resolved BUY trades: {total_resolved}\n")
    lines.append(
        f"- Wallets where the COPIER would have been net positive (mirrored sizing): "
        f"{profitable_copier_wallets}/{len(results)}\n"
    )
    lines.append(f"- Aggregate original-trader $ PnL (mirrored sizing): {total_trader_pnl:,.2f}\n")
    lines.append(
        f"- Aggregate copier $ PnL (mirrored sizing, {slippage_bps:.0f} bps slippage): "
        f"{total_copier_pnl:,.2f}\n"
    )
    if has_flat:
        total_flat_pnl = sum(r["copier_flat"]["dollar_pnl"] for r in results)
        profitable_flat_wallets = sum(1 for r in results if r["copier_flat"]["dollar_pnl"] > 0)
        stake_note = f" (${flat_stake:.2f}/trade)" if flat_stake is not None else ""
        lines.append(
            f"- Wallets where the COPIER would have been net positive (flat stake{stake_note}): "
            f"{profitable_flat_wallets}/{len(results)}\n"
        )
        lines.append(
            f"- Aggregate copier $ PnL (flat stake{stake_note}, {slippage_bps:.0f} bps "
            f"slippage): {total_flat_pnl:,.2f}\n"
        )
    return "\n".join(lines)


def run(
    window: str, top: int, wallets: "list[str] | None", slippage_bps: float,
    out_dir: Path, run_date: "str | None" = None, min_trades: int = 0,
    flat_stake: "float | None" = None,
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

    results = [backtest_wallet(addr, slippage_bps, flat_stake) for addr in addresses]
    # min_trades screens out small-sample wallets (a handful of huge tail bets
    # can swing size-mirrored $ PnL wildly without reflecting a repeatable
    # edge) -- floor of 1 always applies so an unresolved wallet never renders.
    min_resolved = max(min_trades, 1)
    results = [r for r in results if r["n_resolved"] >= min_resolved]
    if not results:
        log.info("[copy-trade] no wallet cleared min_trades=%s resolved BUY trades.", min_resolved)
        return 0

    # Median ROI, not dollar PnL or even mean ROI, is the sustainability
    # signal: dollar PnL and mean ROI are both dominated by one or two huge
    # tail bets (verified against live data 2026-09-18 -- a wallet with a
    # +119% mean copier ROI turned out to have a +0.01% *median*, i.e. a
    # dead-flat typical trade inflated by rare longshot payouts). Median ROI
    # reflects whether the typical trade actually wins.
    results.sort(key=lambda r: r["copier"]["median_roi"], reverse=True)

    report = build_report(run_date, slippage_bps, results, flat_stake)
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
    ap.add_argument(
        "--min-trades", type=int, default=0,
        help=(
            "Drop wallets with fewer resolved BUY trades than this from the "
            "report -- screens out small-sample wallets whose size-mirrored "
            "$ PnL is dominated by one or two huge tail bets rather than a "
            "repeatable edge."
        ),
    )
    ap.add_argument(
        "--flat-stake", type=float, default=None,
        help=(
            "Add a second copier scenario that spends this fixed $ amount "
            "on every trade instead of mirroring the trader's share count -- "
            "'our position is always the same' regardless of what the "
            "trader risked. Omit to report mirrored sizing only."
        ),
    )
    args = ap.parse_args(argv)
    wallets = [w.strip() for w in args.wallets.split(",")] if args.wallets else None
    return run(
        args.window, args.top, wallets, args.slippage_bps, args.out, args.run_date,
        args.min_trades, args.flat_stake,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
