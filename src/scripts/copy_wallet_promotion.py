"""Advisory-only wallet-promotion CLI: follow / pause / resume / live-on /
live-off / live-stake (issue #1122, epic #1101 story B2; live opt-in added
by issue #1253; live-only stake override added by issue #1259).

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

``--live-on``/``--live-off`` (issue #1253) set a followed wallet's
per-wallet live opt-in flag -- the CLI counterpart to the dashboard's live
toggle endpoint, mirroring ``--pause``/``--resume``'s "must already be
followed" refusal style.

``--live-stake ADDRESS VALUE`` (issue #1259) sets a followed wallet's
live-only per-trade stake override -- the CLI counterpart to the
dashboard's ``PATCH .../live-stake`` endpoint. ``VALUE`` of ``none`` or
``inherit`` clears the override back to "same as the paper stake"
(``--stake``'s value); any other ``VALUE`` must parse as a finite positive
USD amount.

Usage::

    python -m src.scripts.copy_wallet_promotion
    python -m src.scripts.copy_wallet_promotion --follow 0xabc... [--stake 10]
    python -m src.scripts.copy_wallet_promotion --pause 0xabc... --reason "unstable"
    python -m src.scripts.copy_wallet_promotion --resume 0xabc...
    python -m src.scripts.copy_wallet_promotion --live-on 0xabc...
    python -m src.scripts.copy_wallet_promotion --live-off 0xabc...
    python -m src.scripts.copy_wallet_promotion --live-stake 0xabc... 2.0
    python -m src.scripts.copy_wallet_promotion --live-stake 0xabc... none
"""
from __future__ import annotations

import argparse
import math
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
    (i.e., rows with screened_at equal to the newest run timestamp) has
    ``eligible_to_follow=1`` and are not already in ``copy_wallets_followed``
    (in any status), sorted by ``median_roi`` descending, plus a note of how
    many follow slots remain. Reports how many wallets were excluded due to
    having stale (pre-latest-run) screening data.
    """
    followed_addresses = {w["address"] for w in db.get_followed_wallets()}
    active_count = active_follow_count(db)
    slots_remaining = max(max_followed - active_count, 0)

    latest = db.get_latest_wallet_screenings()
    if not latest:
        # Empty table; report zero candidates and zero excluded
        print(
            f"Advisory report: 0 eligible, unfollowed wallet(s). "
            f"{slots_remaining}/{max_followed} follow slot(s) remain "
            f"({active_count} active)."
        )
        return 0

    # Find the newest run timestamp (MAX(screened_at))
    max_screened_at = max(row["screened_at"] for row in latest)

    # Scope to wallets from the newest run only
    latest_run = [row for row in latest if row["screened_at"] == max_screened_at]

    # Count how many were excluded for being stale
    stale_count = len(latest) - len(latest_run)

    # Per-candidate line includes screened_at (issue #1226 AC #2): staleness
    # stays visible even if the latest-run scoping above is later loosened.
    # Filter to eligible, unfollowed wallets from the latest run
    candidates = [
        row for row in latest_run
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
    if stale_count > 0:
        print(
            f"  ({stale_count} wallet(s) with screening data from runs "
            f"before {max_screened_at} excluded from recommendation.)"
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
    if not math.isfinite(stake) or stake <= 0:
        print(
            f"Refusing to follow {address}: --stake must be a finite positive "
            f"number in USD, got {stake!r}."
        )
        return 1

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
    known = {w["address"]: w["status"] for w in db.get_followed_wallets()}
    if address not in known:
        print(f"Refusing to resume {address}: not a followed wallet.")
        return 1
    if known[address] != "paused":
        print(
            f"Refusing to resume {address}: current status is "
            f"{known[address]!r}, not 'paused' -- nothing to resume."
        )
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


def live_on(db, address: str) -> int:
    """Set the per-wallet live opt-in flag for *address* (issue #1253).

    Refuses (mirrors ``pause``/``resume``'s style) if the wallet isn't
    followed, or is currently paused -- matching the dashboard endpoint's
    409 rule (a paused wallet placing nothing in either mode makes opting
    it into live meaningless, and risks it going live unattended the
    moment it's resumed with a stale, no-longer-reviewed opt-in).
    """
    known = {w["address"]: w["status"] for w in db.get_followed_wallets()}
    if address not in known:
        print(f"Refusing to enable live trading for {address}: not a followed wallet.")
        return 1
    if known[address] == "paused":
        print(f"Refusing to enable live trading for {address}: wallet is paused.")
        return 1
    db.set_followed_wallet_live_enabled(address, True)
    print(f"Enabled live trading for {address}.")
    return 0


def live_off(db, address: str) -> int:
    """Clear the per-wallet live opt-in flag for *address* (issue #1253).

    Unlike ``live_on``, always allowed regardless of ``status`` -- turning
    live off can only ever reduce what a wallet is eligible to do.
    """
    known = {w["address"] for w in db.get_followed_wallets()}
    if address not in known:
        print(f"Refusing to disable live trading for {address}: not a followed wallet.")
        return 1
    db.set_followed_wallet_live_enabled(address, False)
    print(f"Disabled live trading for {address}.")
    return 0


def live_stake(db, address: str, value: str) -> int:
    """Set or clear *address*'s live-only per-trade stake override (issue
    #1259) -- the CLI counterpart to ``PATCH .../live-stake``.

    ``value`` of ``'none'``/``'inherit'`` (case-insensitive) clears the
    override back to "same as the paper stake"
    (``Database.set_followed_wallet_live_stake(address, None)``); any other
    value must parse as a finite positive USD amount, mirroring
    ``--stake``/``follow()``'s own validation. Refuses (mirrors
    ``live_on``/``live_off``'s style) if the wallet isn't followed --
    unlike ``live_on``, always allowed regardless of ``status`` (setting a
    live-only stake, like disabling live, never itself causes anything to
    execute).
    """
    known = {w["address"] for w in db.get_followed_wallets()}
    if address not in known:
        print(f"Refusing to set live stake for {address}: not a followed wallet.")
        return 1

    if value.strip().lower() in ("none", "inherit"):
        db.set_followed_wallet_live_stake(address, None)
        print(f"Cleared live stake override for {address} (inherits paper stake).")
        return 0

    try:
        stake = float(value)
    except ValueError:
        print(
            f"Refusing to set live stake for {address}: VALUE must be a number, "
            f"'none', or 'inherit', got {value!r}."
        )
        return 1

    if not math.isfinite(stake) or stake <= 0:
        print(
            f"Refusing to set live stake for {address}: must be a finite positive "
            f"number in USD, got {stake!r}."
        )
        return 1

    db.set_followed_wallet_live_stake(address, stake)
    print(f"Set live stake for {address} to ${stake:.2f}/trade.")
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
    ap.add_argument(
        "--live-on", metavar="ADDRESS",
        help="Enable per-wallet live trading opt-in for a followed wallet.",
    )
    ap.add_argument(
        "--live-off", metavar="ADDRESS",
        help="Disable per-wallet live trading opt-in for a followed wallet.",
    )
    ap.add_argument(
        "--live-stake", nargs=2, metavar=("ADDRESS", "VALUE"),
        help=(
            "Set a followed wallet's live-only per-trade stake override in "
            "USD. VALUE of 'none' or 'inherit' clears the override (falls "
            "back to the wallet's --stake)."
        ),
    )
    args = ap.parse_args(argv)

    actions = [
        a for a in (
            args.follow, args.pause, args.resume, args.live_on, args.live_off,
            args.live_stake,
        )
        if a is not None
    ]
    if len(actions) > 1:
        ap.error(
            "only one of --follow / --pause / --resume / --live-on / "
            "--live-off / --live-stake may be given at a time"
        )
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
    if args.live_on is not None:
        return live_on(db, args.live_on)
    if args.live_off is not None:
        return live_off(db, args.live_off)
    if args.live_stake is not None:
        return live_stake(db, args.live_stake[0], args.live_stake[1])

    return report(db, max_followed)


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
