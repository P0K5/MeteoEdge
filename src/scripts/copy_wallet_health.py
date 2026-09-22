"""Scheduled wallet-health monitor + auto-pause job (epic #1138 story D2,
issue #1140).

``copy_wallet_promotion.py`` already supports pausing a followed wallet
(``--pause <address> --reason ...``), but only as a human-run, advisory-only
action -- nothing evaluates the two signals the architecture doc calls out
and pauses automatically. This is that automated check.

Structured like ``copy_settle.py``/``copy_wallet_screening.py``'s one-shot
shape (``run_once()``/``main()``) -- **not** a persistent loop like
``copy_signal_loop.py``. Scheduled daily, shortly after
``meteoedge-copy-screening.timer`` (03:00 UTC), by
``meteoedge-copy-health.timer`` (03:15 UTC) -- see docs/OPERATIONS.md.

For every ``status='active'`` followed wallet
(``db.get_followed_wallets(status="active")``):

1. **Stability check.** Pull the wallet's two most recent
   ``copy_wallet_candidates`` rows (``db.get_recent_wallet_screenings``).
   Reuse ``copy_wallet_screening.py::check_stability`` directly on those two
   rows -- this module never re-derives its sign/tolerance logic. Auto-pause
   (``paused_reason="stability_check_failed"``) if ``check_stability``
   returns ``False``, **or** if the latest row's ``eligible_to_follow`` is
   ``0`` -- a wallet can fail stability against its immediate predecessor
   even if some earlier run set ``eligible_to_follow=1``, so the *current*
   row's own flag is always checked too, not just pairwise agreement. A
   wallet with no screening history yet has nothing to check and is left
   alone.
2. **Realized-ROI check.** Skipped entirely if the wallet was just paused
   above (one pause reason per run -- the first one that fires wins).
   Wallets with at least ``MIN_SETTLED_TRADES_FOR_ROI_CHECK`` settled
   ``copy_positions`` rows (``db.get_settled_copy_positions``) have their
   median per-trade ROI (``settled_pnl_usd / stake_usd``) computed. Auto-pause
   (``paused_reason="realized_roi_negative"``) if that median is ``< 0``.
   Wallets below the minimum sample size are left alone -- one early loss
   should never trip this.
3. A wallet already ``status='paused'`` is skipped entirely by both checks
   (it's simply absent from ``get_followed_wallets(status="active")``) --
   never re-paused, never has its existing ``paused_reason`` overwritten.

Logs a summary line (checked / paused-for-stability / paused-for-roi counts)
at the end of the run.

**No live/paper trading of any kind.** This script only ever calls
``Database.update_followed_wallet_status`` -- it never places an order.

**Auto-pause already halts LIVE execution too, not just paper (issue
#1177, verified against #1167's live-execution loop).** This module never
touches ``copy_signal_loop.py`` directly, but ``update_followed_wallet_status``
above removes the wallet from ``db.get_followed_wallets(status="active")``
entirely, and that exact query is the ONE list ``copy_signal_loop.run_cycle``
iterates over for BOTH its paper path (``_process_wallet`` /
``_handle_buy_trade``) and its live path (``_handle_live_order``, added by
#1167) -- they are nested inside the same per-wallet loop over the same
active-wallets snapshot, not two separate gates. A wallet this job pauses is
therefore already excluded from live order placement the very next
``copy_signal_loop`` cycle, with zero code change needed here or there; see
``src/tests/test_copy_signal_loop.py::TestWalletAutoPauseHaltsLiveExecution``
for the regression coverage.

**Cross-process pause-vs-in-flight-cycle race, audited and found not
exploitable given the real schedules.** ``copy_signal_loop.run_cycle``
snapshots ``get_followed_wallets(status="active")`` once at the top of each
cycle, then processes that list; a wallet this job pauses *after* some other
process already took that snapshot but *before* the snapshot's loop reaches
it would still be processed once more, live included. In practice this
window cannot compound into an ongoing problem: ``copy_signal_loop`` cycles
are documented to complete in seconds (docs/OPERATIONS.md) against a 300s
default poll interval, so at most a single already-in-flight cycle could be
affected -- the very next cycle (<=5 minutes later) re-snapshots the list and
correctly excludes the now-paused wallet. This job itself runs once a day
(03:15 UTC) and does no network I/O (DB reads only), so it does not
compound the odds either. Given the bounded, self-correcting blast radius
and the two jobs' actual cadence, no additional synchronization was added --
re-derive this reasoning before adding any if the schedules above ever
change materially (e.g. a much longer poll interval or many more followed
wallets making a single cycle take minutes instead of seconds).

Usage::

    python -m src.scripts.copy_wallet_health
"""
from __future__ import annotations

import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.scripts.copy_wallet_screening import check_stability  # noqa: E402

log = logging.getLogger(__name__)

#: Minimum number of settled trades required before the realized-ROI check
#: can auto-pause a wallet -- don't pause on one early loss. Module constant,
#: mirroring copy_wallet_screening.py's own MAX_WALLETS_PER_RUN convention
#: rather than a new config key: this is an internal safety-net tuning knob,
#: not an operator-facing strategy parameter.
MIN_SETTLED_TRADES_FOR_ROI_CHECK = 5


def _open_db():
    """Return a Database handle, or None if the DB cannot be opened."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        log.warning("[copy_wallet_health] DB unavailable: %s -- skipping run", e)
        return None


def _stability_pause_reason(db, address: str) -> "str | None":
    """Return ``"stability_check_failed"`` if *address* should be paused on
    the stability signal, else ``None``.

    A wallet with no screening history at all (``recent`` empty) has
    nothing to check and is never paused by this function.
    """
    recent = db.get_recent_wallet_screenings(address, limit=2)
    if not recent:
        return None

    current = recent[0]
    previous = recent[1] if len(recent) > 1 else None

    if not check_stability(current, previous):
        return "stability_check_failed"
    if not current.get("eligible_to_follow"):
        return "stability_check_failed"
    return None


def _median_roi_pause_reason(db, address: str) -> "str | None":
    """Return ``"realized_roi_negative"`` if *address* should be paused on
    the realized-ROI signal, else ``None``.

    Wallets with fewer than ``MIN_SETTLED_TRADES_FOR_ROI_CHECK`` *usable*
    settled trades are never paused by this function (insufficient sample
    size). A row is usable when it has a non-null ``settled_pnl_usd`` and a
    positive ``stake_usd`` -- both are guaranteed by the ``copy_positions``
    schema's own constraints (``settled_pnl_usd`` is always set by
    ``settle_copy_position`` before ``status`` flips to ``'settled'``;
    ``stake_usd`` has a ``CHECK(stake_usd > 0)``) for every row this
    codebase's own writers produce, so this filter is not expected to drop
    anything in practice -- it exists so one malformed row (e.g. hand-edited
    test data, a future writer bug) can never divide by zero or crash the
    whole run; it is silently excluded from the sample instead (AI review
    #1141, BLOCK item).
    """
    settled = db.get_settled_copy_positions(address)
    usable = [
        row for row in settled
        if row.get("settled_pnl_usd") is not None and (row.get("stake_usd") or 0) > 0
    ]
    if len(usable) < MIN_SETTLED_TRADES_FOR_ROI_CHECK:
        return None

    per_trade_roi = [
        float(row["settled_pnl_usd"]) / float(row["stake_usd"]) for row in usable
    ]
    median_roi = statistics.median(per_trade_roi)
    if median_roi < 0:
        return "realized_roi_negative"
    return None


def run_once(db=None) -> dict:
    """Run a single wallet-health pass over every ``status='active'``
    followed wallet.

    Returns a summary dict ``{'checked': int, 'paused_stability': int,
    'paused_roi': int}``.

    If *db* is not given, opens (and owns) a real ``Database()`` handle.
    Passing *db* explicitly is how tests inject a seeded / mocked database.
    """
    if db is None:
        db = _open_db()
    if db is None:
        return {"checked": 0, "paused_stability": 0, "paused_roi": 0}

    wallets = db.get_followed_wallets(status="active")

    n_checked = 0
    n_paused_stability = 0
    n_paused_roi = 0
    for wallet in wallets:
        address = wallet["address"]
        n_checked += 1

        reason = _stability_pause_reason(db, address)
        if reason is not None:
            db.update_followed_wallet_status(address, "paused", reason)
            n_paused_stability += 1
            log.info(
                "[copy_wallet_health] paused %s: reason=%s", address, reason,
            )
            continue

        reason = _median_roi_pause_reason(db, address)
        if reason is not None:
            db.update_followed_wallet_status(address, "paused", reason)
            n_paused_roi += 1
            log.info(
                "[copy_wallet_health] paused %s: reason=%s", address, reason,
            )
            continue

    log.info(
        "[copy_wallet_health] run complete: checked=%s paused_stability=%s "
        "paused_roi=%s",
        n_checked, n_paused_stability, n_paused_roi,
    )
    return {
        "checked": n_checked,
        "paused_stability": n_paused_stability,
        "paused_roi": n_paused_roi,
    }


def main() -> None:
    from src.logging_config import setup_logging
    setup_logging()
    run_once()


if __name__ == "__main__":
    main()
