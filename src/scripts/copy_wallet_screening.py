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
from src.data.polymarket_traders import get_leaderboard, wallet_address  # noqa: E402
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


def run(
    window: str, top: int, slippage_bps: float, min_trades: int = 0,
    flat_stake: "float | None" = None, db: "Database | None" = None,
    screened_at: "str | None" = None,
) -> int:
    screened_at = screened_at or datetime.now(timezone.utc).isoformat()

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
        current = {
            "n_buy_trades": result["n_buy_trades"],
            "n_resolved": result["n_resolved"],
            "win_rate": copier["win_rate"],
            "mean_roi": copier["mean_roi"],
            "median_roi": copier["median_roi"],
        }

        previous_rows = db.get_recent_wallet_screenings(address, limit=1)
        previous = previous_rows[0] if previous_rows else None
        eligible = check_stability(current, previous)

        flat_pnl = None
        if flat_stake is not None:
            flat_pnl = result.get("copier_flat", {}).get("dollar_pnl")

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
