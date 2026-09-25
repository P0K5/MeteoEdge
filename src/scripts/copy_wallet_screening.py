"""Scheduled wallet-screening runner (epic #1099, story 2).

Turns `copy_trade_backtest.py`'s screening logic into a script that
**persists every run** to `copy_wallet_candidates` (story 1, issue #1108)
and implements the stability check the architecture doc requires: a wallet
is only `eligible_to_follow` when its latest run agrees with the
immediately-previous one.

This is what would have caught the spike's own instability finding before
it reached a followed-wallet decision: wallet `0xd3b034d7...` looked like
the best candidate in one run (`n_resolved=7498`, `median_roi=+33.4%`) and
reversed completely 15 hours later (`n_resolved=2271`, `median_roi=-100%`),
because `get_wallet_trades()` (`src/data/polymarket_traders.py`) is capped
at `MAX_TRADE_PAGES * page_size` = 20,000 trades and returns a
recency-biased window for wallets whose true history exceeds that (see
`docs/design/copy-trading-architecture.md`, "Known limitation").

**No live/paper trading of any kind.** Output is rows in
`copy_wallet_candidates` only -- this script never places an order.

**Which stats get persisted/compared.** The architecture doc's
`copy_wallet_candidates` schema has one `win_rate`/`mean_roi`/`median_roi`
column each (not separate trader/copier columns) -- the copier's own
post-slippage numbers are persisted, since eligibility is about whether
*following* this wallet stays profitable, not whether the original trader
was profitable.

Usage::

    python -m src.scripts.copy_wallet_screening --window month --top 20
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.db import Database  # noqa: E402
from src.data.polymarket_traders import (  # noqa: E402
    DEFAULT_TRADE_PAGE_SIZE,
    MAX_TRADE_PAGES,
    get_leaderboard,
    wallet_address,
)
from src.scripts.copy_trade_backtest import (  # noqa: E402
    DEFAULT_SLIPPAGE_BPS,
    backtest_wallet,
)

log = logging.getLogger(__name__)

#: Rate-limit-budget decision (epic #1099 story 2/3): running this screening
#: job as its own process means its calls to gamma-api.polymarket.com are
#: NOT jointly throttled with the live weather bot's own traffic on that
#: host (src.http_client.DomainRateLimiter is a process-local singleton).
#: Bounding worst-case call volume per run via a hard cap is the v1
#: mitigation -- a cross-process shared limiter is explicitly out of scope.
MAX_WALLETS_PER_RUN = 50

#: Quality gate thresholds (issue #1209). `check_stability()` only proves a
#: screening run was *reproducible* -- these thresholds separately gate on
#: whether the wallet is actually worth following. Derived from the
#: 2026-09-25 screening audit (see issue #1209 for the measured evidence).

#: A wallet must be profitable under the flat-stake model we actually trade
#: (not merely under its own, possibly much larger, position sizing).
QUALITY_MIN_FLAT_DOLLAR_PNL = 0.0

#: The median trade itself must be profitable -- a positive mean built on a
#: negative/zero median means most trades lose money.
QUALITY_MIN_MEDIAN_ROI = 0.0

#: Reject tail-driven P&L: wallets whose mean ROI is propped up by a handful
#: of outsized winners do not survive flat-stake copying (see
#: docs/design/copy-trading-architecture.md, "Background"). Only applied
#: when median_roi > 0 -- see check_quality()'s docstring for why ordering
#: relative to the median_roi check below is not load-bearing.
QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO = 3.0

#: Reject wallets whose trade history was truncated by the fetch cap in
#: get_wallet_trades() (src/data/polymarket_traders.py) -- their metrics are
#: not run-to-run stable (see this module's docstring and issue #1209).
#: IMPORTANT: the fetch cap (MAX_TRADE_PAGES * page_size) bounds the TOTAL
#: number of trades fetched -- buys and sells interleaved -- not
#: `n_buy_trades` alone (that's only the buy half after backtest_wallet()
#: splits them, see copy_trade_backtest.py). Comparing `n_buy_trades` to
#: this cap directly is a unit mismatch that makes the gate inert (a truly
#: truncated wallet's n_buy_trades sits well under the cap, e.g. 10,500 of
#: 20,000). Always compare against `n_buy_trades + n_sell_excluded`.
QUALITY_MAX_TOTAL_TRADES = MAX_TRADE_PAGES * DEFAULT_TRADE_PAGE_SIZE


def sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def check_stability(current: dict, previous: "dict | None") -> bool:
    """True only if *current*'s run agrees with the immediately-previous run.

    Pure function, no I/O. ``previous=None`` (first-ever run for a wallet)
    is always unstable -- there's nothing yet to agree with. A wallet is
    stable only when BOTH:

    (a) ``sign(current["median_roi"]) == sign(previous["median_roi"])`` --
        a ``median_roi`` of exactly ``0`` never matches another ``0``
        (treated as unstable, not stable-at-zero).
    (b) ``n_resolved`` hasn't swung by more than 25% relative to the
        previous run's ``n_resolved`` (floor of 1 in the denominator so a
        previous ``n_resolved=0`` can't divide by zero).
    """
    if previous is None:
        return False

    current_sign = sign(current["median_roi"])
    previous_sign = sign(previous["median_roi"])
    if current_sign == 0 or previous_sign == 0 or current_sign != previous_sign:
        return False

    previous_n = previous["n_resolved"]
    delta_ratio = abs(current["n_resolved"] - previous_n) / max(previous_n, 1)
    if delta_ratio > 0.25:
        return False

    return True


def check_quality(current: dict) -> "tuple[bool, str]":
    """True (with reason ``"ok"``) only if *current* passes every
    profitability/tail-risk/history-completeness quality gate (issue #1209).

    Pure function, no I/O. Composable with -- not folded into --
    ``check_stability()``: a wallet must pass BOTH to be
    ``eligible_to_follow``. Checked in this order:

    (a) ``current["flat_dollar_pnl"]`` must be > ``QUALITY_MIN_FLAT_DOLLAR_PNL``
        -- profitable under the flat-stake model we actually trade, not just
        under the wallet's own position sizing. A missing (``None``)
        ``flat_dollar_pnl`` (e.g. the run was invoked without
        ``--flat-stake``) fails this check too, since flat-stake
        profitability cannot be confirmed without it.
    (b) When ``median_roi > 0``, ``mean_roi`` must be <=
        ``QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO * median_roi`` -- rejects
        tail-driven wallets whose P&L is concentrated in a few large
        winners, a profile that does not survive flat-stake copying (see
        docs/design/copy-trading-architecture.md, "Background").
    (c) ``median_roi`` must be > ``QUALITY_MIN_MEDIAN_ROI``.
    (d) ``current["n_buy_trades"] + current["n_sell_excluded"]`` -- the TOTAL
        number of trades fetched, not just the buy half -- must be <
        ``QUALITY_MAX_TOTAL_TRADES``. Rejects wallets whose history was
        truncated by the fetch cap (which bounds total fetched trades, see
        ``QUALITY_MAX_TOTAL_TRADES``'s comment), whose metrics are therefore
        not run-to-run stable.

    Checks (b) and (c) interact: (b) only fires when ``median_roi > 0``, so
    a wallet with ``median_roi <= 0`` always falls through to fail on (c)
    instead -- the relative order of (b) and (c) is not load-bearing for
    correctness, only for which failure reason gets logged first.
    """
    flat_pnl = current.get("flat_dollar_pnl")
    if flat_pnl is None or not flat_pnl > QUALITY_MIN_FLAT_DOLLAR_PNL:
        return False, "flat_dollar_pnl_not_positive"

    median_roi = current["median_roi"]
    mean_roi = current["mean_roi"]
    if median_roi > 0 and mean_roi > QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO * median_roi:
        return False, "tail_driven_pnl"

    if not median_roi > QUALITY_MIN_MEDIAN_ROI:
        return False, "median_roi_not_positive"

    n_total_trades = current["n_buy_trades"] + current["n_sell_excluded"]
    if n_total_trades >= QUALITY_MAX_TOTAL_TRADES:
        return False, "trade_history_truncated"

    return True, "ok"


def run(
    window: str, top: int, slippage_bps: float, min_trades: int = 0,
    flat_stake: "float | None" = None, db: "Database | None" = None,
    screened_at: "str | None" = None,
) -> int:
    screened_at = screened_at or datetime.now(timezone.utc).isoformat()

    if not flat_stake:
        log.warning(
            "[copy-wallet-screening] flat_stake is not set -- flat_dollar_pnl "
            "cannot be computed, so every wallet screened this run will fail "
            "the quality gate on flat_dollar_pnl_not_positive (see "
            "check_quality()). This is one bad invocation, not many bad wallets."
        )

    effective_top = top
    if top > MAX_WALLETS_PER_RUN:
        log.warning(
            "[copy-wallet-screening] --top=%s exceeds MAX_WALLETS_PER_RUN=%s -- "
            "clamping (rate-limit budget, see module docstring).",
            top, MAX_WALLETS_PER_RUN,
        )
        effective_top = MAX_WALLETS_PER_RUN

    leaderboard = get_leaderboard(window=window, limit=effective_top)
    addresses = [a for a in (wallet_address(e) for e in leaderboard) if a]
    # Belt-and-suspenders: enforce the cap regardless of what the leaderboard
    # endpoint actually returns for `limit` (defensive against a non-compliant
    # or future API response).
    addresses = addresses[:MAX_WALLETS_PER_RUN]
    if not addresses:
        log.error(
            "[copy-wallet-screening] leaderboard returned no usable wallet "
            "addresses (endpoint may be unavailable, or its response shape "
            "has changed -- see get_leaderboard's docstring in "
            "src/data/polymarket_traders.py)."
        )
        return 1

    db = db or Database()

    n_screened = 0
    for address in addresses:
        result = backtest_wallet(address, slippage_bps, flat_stake)
        if result["n_resolved"] < min_trades:
            log.info(
                "[copy-wallet-screening] %s: n_resolved=%s below --min-trades=%s, skipping.",
                address, result["n_resolved"], min_trades,
            )
            continue

        copier = result["copier"]

        flat_pnl = None
        if flat_stake is not None:
            flat_pnl = result.get("copier_flat", {}).get("dollar_pnl")

        current = {
            "n_buy_trades": result["n_buy_trades"],
            "n_sell_excluded": result["n_sell_excluded"],
            "n_resolved": result["n_resolved"],
            "win_rate": copier["win_rate"],
            "mean_roi": copier["mean_roi"],
            "median_roi": copier["median_roi"],
            "flat_dollar_pnl": flat_pnl,
        }

        previous_rows = db.get_recent_wallet_screenings(address, limit=1)
        previous = previous_rows[0] if previous_rows else None
        stable = check_stability(current, previous)
        quality_ok, quality_reason = check_quality(current)
        if not quality_ok:
            log.info(
                "[copy-wallet-screening] %s: failed quality check (%s), not eligible.",
                address, quality_reason,
            )
        eligible = stable and quality_ok

        db.insert_wallet_screening(
            address=address,
            window=window,
            screened_at=screened_at,
            n_buy_trades=current["n_buy_trades"],
            n_resolved=current["n_resolved"],
            win_rate=current["win_rate"],
            mean_roi=current["mean_roi"],
            median_roi=current["median_roi"],
            mirrored_dollar_pnl=copier["dollar_pnl"],
            flat_dollar_pnl=flat_pnl,
            flat_stake=flat_stake,
            slippage_bps=slippage_bps,
            eligible_to_follow=int(eligible),
        )
        n_screened += 1

    log.info(
        "[copy-wallet-screening] screened %s/%s wallets (window=%s, top=%s).",
        n_screened, len(addresses), window, effective_top,
    )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", default="month", choices=sorted(["day", "week", "month", "all"]))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument(
        "--flat-stake", type=float, default=5.0,
        help=(
            "Fixed $ amount spent on every trade for the flat-stake copier "
            "scenario, mirroring copy_trade_backtest.py's --flat-stake -- "
            "defaults to $5.00/trade, matching the spike's winning sizing."
        ),
    )
    ap.add_argument("--min-trades", type=int, default=0)
    args = ap.parse_args(argv)
    return run(
        args.window, args.top, args.slippage_bps, args.min_trades, args.flat_stake,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
