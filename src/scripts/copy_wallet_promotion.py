"""Advisory-only wallet-promotion CLI: follow / pause / resume (issue #1122,
epic #1101 story B2).

Epic A's screening pipeline (``copy_wallet_screening.py``) produces
``copy_wallet_candidates`` rows with ``eligible_to_follow`` computed per run,
but nothing turns an eligible candidate into an actually-followed wallet --
that's this script's only job.

Mirrors ``src/model/promotion_gate.py``'s advisory framing (see
``docs/OPERATIONS.md``, "Station Shadow->Live Promotion": "Advisory only --
this tool never promotes anything"). **This tool never runs unattended and
never auto-follows a wallet.** Promotion into ``copy_wallets_followed`` only
happens when a human explicitly runs ``--follow <address>``. The default
(no-flag) invocation is a read-only advisory report -- it changes nothing.

Usage::

    python -m src.scripts.copy_wallet_promotion
    python -m src.scripts.copy_wallet_promotion --follow 0xabc... [--stake 10]
    python -m src.scripts.copy_wallet_promotion --pause 0xabc... --reason "unstable"
    python -m src.scripts.copy_wallet_promotion --resume 0xabc...
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import get_live_config  # noqa: E402
from src.data.db import Database  # noqa: E402


def active_follow_count(db) -> int:
    return len(db.get_followed_wallets(status="active"))


def report(db, max_followed: int) -> int:
    """Print the advisory report; performs no DB writes.

    Lists wallets whose *latest* ``copy_wallet_candidates`` screening run
    has ``eligible_to_follow=1`` and are not already in
    ``copy_wallets_followed`` (in any status), sorted by ``median_roi``
    descending, plus a note of how many follow slots remain.
    """
    followed_addresses = {w["address"] for w in db.get_followed_wallets()}
    active_count = active_follow_count(db)
    slots_remaining = max(max_followed - active_count, 0)

    latest = db.get_latest_wallet_screenings()
    candidates = [
        row for row in latest
        if row.get("eligible_to_follow") and row["address"] not in followed_addresses
    ]
    candidates.sort(
        key=lambda r: r["median_roi"] if r["median_roi"] is not None else float("-inf"),
        reverse=True,
    )

    print(
        f"Advisory report: {len(candidates)} eligible, unfollowed wallet(s). "
        f"{slots_remaining}/{max_followed} follow slot(s) remain "
        f"({active_count} active)."
    )
    for row in candidates:
        print(
            f"  {row['address']}  median_roi={row['median_roi']!r}  "
            f"n_resolved={row['n_resolved']}  window={row['window']!r}  "
            f"screened_at={row['screened_at']}"
        )
    return 0


def follow(db, address: str, stake: float, max_followed: int) -> int:
    """Promote *address* into ``copy_wallets_followed``. Refuses (clear
    error, no DB write) unless all safety rails pass."""
    already = {w["address"]: w["status"] for w in db.get_followed_wallets()}
    if address in already:
        print(
            f"Refusing to follow {address}: already followed "
            f"(status={already[address]!r}) -- use --resume/--pause instead."
        )
        return 1

    active_count = active_follow_count(db)
    if active_count >= max_followed:
        print(
            f"Refusing to follow {address}: {active_count}/{max_followed} "
            f"active wallets already followed (COPY_MAX_WALLETS_FOLLOWED)."
        )
        return 1

    recent = db.get_recent_wallet_screenings(address, limit=1)
    if not recent:
        print(
            f"Refusing to follow {address}: no screening run found in "
            f"copy_wallet_candidates."
        )
        return 1
    if not recent[0].get("eligible_to_follow"):
        print(
            f"Refusing to follow {address}: latest screening run "
            f"(screened_at={recent[0].get('screened_at')}) has "
            f"eligible_to_follow=0."
        )
        return 1

    added_at = datetime.now(timezone.utc).isoformat()
    db.insert_followed_wallet(address=address, stake_per_trade=stake, added_at=added_at)
    print(f"Followed {address} at stake=${stake:.2f}/trade (added_at={added_at}).")
    return 0


def pause(db, address: str, reason: str) -> int:
    known = {w["address"] for w in db.get_followed_wallets()}
    if address not in known:
        print(f"Refusing to pause {address}: not a followed wallet.")
        return 1
    db.update_followed_wallet_status(address, "paused", reason)
    print(f"Paused {address} (reason: {reason}).")
    return 0


def resume(db, address: str, max_followed: int) -> int:
    known = {w["address"] for w in db.get_followed_wallets()}
    if address not in known:
        print(f"Refusing to resume {address}: not a followed wallet.")
        return 1

    active_count = active_follow_count(db)
    if active_count >= max_followed:
        print(
            f"Refusing to resume {address}: {active_count}/{max_followed} "
            f"active wallets already followed (COPY_MAX_WALLETS_FOLLOWED)."
        )
        return 1

    db.update_followed_wallet_status(address, "active", None)
    print(f"Resumed {address} (now active).")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--follow", metavar="ADDRESS", help="Promote ADDRESS into copy_wallets_followed.")
    ap.add_argument(
        "--stake", type=float, default=None,
        help="Stake per trade in USD for --follow; defaults to COPY_DEFAULT_FLAT_STAKE_USD.",
    )
    ap.add_argument("--pause", metavar="ADDRESS", help="Pause a followed wallet.")
    ap.add_argument("--reason", default=None, help="Required with --pause.")
    ap.add_argument("--resume", metavar="ADDRESS", help="Resume a paused wallet.")
    args = ap.parse_args(argv)

    actions = [a for a in (args.follow, args.pause, args.resume) if a is not None]
    if len(actions) > 1:
        ap.error("only one of --follow / --pause / --resume may be given at a time")
    if args.pause is not None and not args.reason:
        ap.error("--pause requires --reason")

    db = Database()
    live_cfg = get_live_config(db)
    max_followed = live_cfg["COPY_MAX_WALLETS_FOLLOWED"]

    if args.follow is not None:
        stake = args.stake if args.stake is not None else live_cfg["COPY_DEFAULT_FLAT_STAKE_USD"]
        return follow(db, args.follow, stake, max_followed)
    if args.pause is not None:
        return pause(db, args.pause, args.reason)
    if args.resume is not None:
        return resume(db, args.resume, max_followed)

    return report(db, max_followed)


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
