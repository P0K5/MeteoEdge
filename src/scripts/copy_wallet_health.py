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
   returns ``(False, reason)`` for any reason **other than**
   ``"stale_comparison"``, **or** if the latest row's ``eligible_to_follow``
   is ``0`` -- a wallet can fail stability against its immediate predecessor
   even if some earlier run set ``eligible_to_follow=1``, so the *current*
   row's own flag is always checked too, not just pairwise agreement. A
   wallet with no screening history yet has nothing to check and is left
   alone.

   **``"stale_comparison"`` drops ONLY the pairwise comparison, not the
   whole check (issue #1298, tightened after initial review).** When the
   two most recent rows' ``screened_at`` timestamps are more than
   ``check_stability``'s ``STABILITY_MAX_COMPARISON_GAP_HOURS`` apart (e.g.
   the wallet dropped out of the ``--top N`` screening pool for a while and
   the next run it reappears in is compared against a stale row), normal
   growth in ``n_resolved`` across that gap can exceed the 25% tolerance
   even though nothing about the wallet actually changed -- that pairwise
   comparison (same-sign median_roi, volume within 25%) is skipped and
   logged, not failed. See the 0x924379a7 incident this issue was filed for
   (2026-10-04 run compared against 2026-09-20, a two-week gap).

   This does NOT skip the latest row's own quality standing, though: this
   module separately re-runs ``copy_wallet_screening.py::check_quality``
   against the CURRENT row (minimum resolved-trade sample size, positive
   flat-stake P&L, tail risk -- none of which are gap-contaminated the way
   the pairwise stability comparison is; its only use of ``previous`` is the
   truncated-wallet reproduction criterion, which still compares against the
   same, possibly stale, ``previous["flat_dollar_pnl"]`` -- acceptable
   because that comparison fails CLOSED: a stale/wrong previous row can at
   worst wrongly deny reproduction and pause a wallet that was actually
   fine, never wrongly confirm one that wasn't). A wallet whose current row
   fails quality outright is still paused even on a stale-comparison cycle.
   Skipping both stability AND quality on a stale gap would fail OPEN (keep
   trading an already-bad wallet) precisely when a wallet has been out of
   the screening pool -- which correlates with having gone quiet or
   degraded, not an independent coincidence -- so only the pairwise
   comparison is dropped; quality is still enforced directly, not through
   the (potentially gap-contaminated) composite ``eligible_to_follow`` flag.
   This does not make the check more permissive in the general case: a
   wallet with a fresh, consecutive comparison that actually disagrees is
   still paused exactly as before, and a wallet that fails quality is still
   paused regardless of the comparison gap.
2. **Realized-P&L check.** Skipped entirely if the wallet was just paused
   above (one pause reason per run -- the first one that fires wins).
   ``db.get_settled_copy_positions`` rows are deduped in Python into
   *decisions* -- one entry per distinct ``(market, outcome_index)``, fills
   summed together -- since a single signal split across several fills must
   count once, not once per fill (issue #1207's dedupe fix, applied here
   too; issue #1225). Wallets with at least
   ``COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK`` deduped decisions have their
   **total realized P&L** summed across those decisions. Auto-pause
   (``paused_reason="realized_roi_negative"``, kept as-is even though the
   signal is no longer a median-ROI figure -- it is persisted in
   ``copy_wallets_followed.paused_reason`` and surfaced on the dashboard)
   if that total is ``< 0``. This is deliberately a P&L check, not a
   win-rate/median-ROI check: a wallet with a sub-50% hit rate can still be
   solidly profitable (e.g. buying at 0.25 with a 40% hit rate), and a
   median-per-trade signal would auto-pause it on win rate alone. Wallets
   below the minimum sample size are left alone (logged at DEBUG with the
   current decision count) -- one early loss should never trip this, and
   neither should a handful of decisions that happen to net negative before
   the sample is large enough to mean anything.

   **Paper-only today.** ``get_settled_copy_positions`` reads
   ``copy_positions``, never ``copy_live_positions`` -- this check has no
   opinion on live positions and no live loop calls it (that's epic H,
   #1159). When epic H builds a live path, it must use its own, deliberately
   TIGHTER threshold -- live's purpose is capital preservation, not sample
   accumulation for measurement, so waiting for 30 deduped live decisions
   before ever pausing a losing live wallet would be unacceptable. Do not
   repurpose ``COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK`` for that; add a
   separate live-specific key.
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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import CONFIG_DEFAULTS, get_live_config  # noqa: E402
from src.scripts.copy_wallet_screening import (  # noqa: E402
    _min_resolved_trades_threshold,
    check_quality,
    check_stability,
)

log = logging.getLogger(__name__)

#: Fallback minimum number of deduped decisions required before the
#: realized-P&L check can auto-pause a wallet, used only if the live
#: config lookup itself cannot be completed (see
#: ``_realized_pnl_pause_reason``). Mirrors
#: ``CONFIG_DEFAULTS["COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK"]`` -- keep
#: these two in sync; the live-editable config value (issue #1225) is what
#: actually governs behaviour in the normal, DB-available path.
MIN_DECISIONS_FOR_ROI_CHECK = CONFIG_DEFAULTS["COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK"]


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

    **On a stale comparison (issue #1298), only the pairwise agreement is
    dropped -- quality is still checked directly, so this fails CLOSED, not
    open.** When the two most recent rows are too far apart in time to
    compare at all (``check_stability`` returns reason
    ``"stale_comparison"``), that specific signal -- same-sign median_roi,
    volume within 25% -- is logged and skipped as "insufficient history for
    this cycle". The latest row's own ``eligible_to_follow`` flag is
    deliberately NOT consulted as a substitute in this branch, because it
    may itself have been computed from the very same stale comparison (see
    ``copy_wallet_screening.py::run()``) -- using it here would silently let
    the gap-contaminated verdict back in through the side door. Instead,
    ``check_quality()`` is re-run directly against the CURRENT row: its
    conditions (minimum resolved-trade sample size via
    ``_min_resolved_trades_threshold()``, positive flat-stake P&L, tail
    risk) read off ``current`` alone and are not gap-contaminated, so a
    wallet that is independently failing quality -- too few resolved trades,
    negative flat P&L -- is still paused even on a stale-comparison cycle.
    The one exception is ``check_quality()``'s truncated-wallet reproduction
    criterion, which does compare against ``previous["flat_dollar_pnl"]`` --
    still acceptable here because it fails CLOSED (a stale/wrong ``previous``
    can at worst wrongly deny reproduction and pause a wallet that was
    actually fine; it can never wrongly confirm one that wasn't).
    """
    recent = db.get_recent_wallet_screenings(address, limit=2)
    if not recent:
        return None

    current = recent[0]
    previous = recent[1] if len(recent) > 1 else None

    stable, reason = check_stability(current, previous)
    if reason == "stale_comparison":
        quality_ok, quality_reason = check_quality(
            current, previous, min_resolved_trades=_min_resolved_trades_threshold(db),
        )
        if not quality_ok:
            log.info(
                "[copy_wallet_health] %s: stability comparison skipped as stale "
                "(screened_at=%s vs %s, gap exceeds STABILITY_MAX_COMPARISON_GAP_HOURS), "
                "but the latest row still fails the quality gate (%s) -- "
                "pausing on that signal instead of silently passing (issue #1298).",
                address, current.get("screened_at"),
                previous.get("screened_at") if previous else None, quality_reason,
            )
            return "stability_check_failed"
        log.info(
            "[copy_wallet_health] %s: skipping stability check this cycle -- "
            "latest screening row (screened_at=%s) is being compared against "
            "one too old to be meaningful (screened_at=%s, gap exceeds "
            "STABILITY_MAX_COMPARISON_GAP_HOURS) -- not treating this as a "
            "stability failure; quality gate still passes (issue #1298).",
            address, current.get("screened_at"), previous.get("screened_at") if previous else None,
        )
        return None
    if not stable:
        return "stability_check_failed"
    if not current.get("eligible_to_follow"):
        return "stability_check_failed"
    return None


def _min_decisions_threshold(db) -> int:
    """Return the live ``COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK`` value as
    an ``int``, falling back to ``MIN_DECISIONS_FOR_ROI_CHECK`` -- logged at
    WARNING, not silently -- if the live-config lookup itself fails (e.g.
    DB corruption) or somehow returns a non-numeric value (``get_live_config``
    already guarantees an ``int`` for every key whose ``CONFIG_DEFAULTS``
    default is an ``int``, per its own type-coercion loop, but this is a
    trading-safety-relevant threshold: never let a broken read silently
    change *when* a wallet gets auto-paused -- AI review #1225 BLOCK items).
    """
    try:
        raw = get_live_config(db).get(
            "COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK", MIN_DECISIONS_FOR_ROI_CHECK,
        )
        return int(raw)
    except Exception as e:
        log.warning(
            "[copy_wallet_health] failed to read COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK "
            "from live config (%s) -- falling back to module default %d",
            e, MIN_DECISIONS_FOR_ROI_CHECK,
        )
        return MIN_DECISIONS_FOR_ROI_CHECK


def _parse_settled_at(ts: "str | None") -> "datetime | None":
    """Best-effort ISO-8601 parse of a ``settled_at`` value, tolerant of a
    trailing ``Z`` and of a naive (offset-less) timestamp (treated as
    UTC). Returns ``None`` on anything missing/unparseable -- used only to
    ORDER candidate ``settled_at`` strings correctly in
    ``dedupe_decisions`` below (comparing raw strings breaks the moment a
    ``Z``-suffixed row and a ``+00:00``-suffixed row are compared, since
    ``'Z' > '+'`` lexicographically even though ``Z`` means the earlier
    offset notation for the same instant); the *string* value is still
    what gets stored and returned, never this parsed form.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def dedupe_decisions(settled_rows: "list[dict]") -> "list[dict]":
    """Collapse individual settled ``copy_positions`` rows into *decisions*:
    one entry per distinct ``(market, outcome_index)``, with each decision's
    fills summed together -- shared by ``_realized_pnl_pause_reason`` below
    and ``copy_live_readiness.py`` (issue #1255) so the two never disagree
    about what counts as one decision.

    *settled_rows* is first filtered to *usable* rows (a row is usable when
    it has a non-null ``settled_pnl_usd`` and a positive ``stake_usd`` --
    both are guaranteed by the ``copy_positions`` schema's own constraints
    for every row this codebase's own writers produce, so this filter is
    not expected to drop anything in practice; it exists so one malformed
    row (e.g. hand-edited test data, a future writer bug) can never crash a
    caller -- it is silently excluded from the sample instead, AI review
    #1141, BLOCK item). A wallet whose 5 settled rows are all fills of one
    market/outcome collapses to 1 decision, not 5 (issue #1207's dedupe
    fix -- see issue #1225's ``0x684baa57c3`` real-world case).

    Returns one dict per decision: ``{'market', 'outcome_index', 'pnl'
    (fills summed), 'n_fills', 'settled_at'}``, in no guaranteed order.
    ``settled_at`` is the LATEST of the contributing fills' own
    ``settled_at`` (compared as PARSED timestamps via
    ``_parse_settled_at``, not as raw strings -- a raw-string compare
    would order a ``Z``-suffixed row incorrectly against a
    ``+00:00``-suffixed one) -- used by callers (e.g. a "last N days"
    window) that need to place a multi-fill decision in time; a decision
    is never attributed to a moment before all of its own fills existed.
    A fill whose own ``settled_at`` fails to parse is never allowed to win
    that comparison (fail-safe: an unparseable timestamp can't be "the
    latest" of anything).
    """
    usable = [
        row for row in settled_rows
        if row.get("settled_pnl_usd") is not None and (row.get("stake_usd") or 0) > 0
    ]

    grouped: "dict[tuple, dict]" = {}
    for row in usable:
        key = (row["market"], row["outcome_index"])
        settled_at = row.get("settled_at")
        settled_at_dt = _parse_settled_at(settled_at)
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "market": row["market"],
                "outcome_index": row["outcome_index"],
                "pnl": float(row["settled_pnl_usd"]),
                "n_fills": 1,
                "settled_at": settled_at,
                "_settled_at_dt": settled_at_dt,
            }
        else:
            entry["pnl"] += float(row["settled_pnl_usd"])
            entry["n_fills"] += 1
            if settled_at_dt is not None and (
                entry["_settled_at_dt"] is None or settled_at_dt > entry["_settled_at_dt"]
            ):
                entry["settled_at"] = settled_at
                entry["_settled_at_dt"] = settled_at_dt

    for entry in grouped.values():
        del entry["_settled_at_dt"]

    return list(grouped.values())


def _realized_pnl_pause_reason(db, address: str) -> "str | None":
    """Return ``"realized_roi_negative"`` if *address* should be paused on
    the realized-P&L signal, else ``None``.

    Settled ``copy_positions`` rows are deduped into *decisions* via
    ``dedupe_decisions`` (one entry per distinct ``(market,
    outcome_index)``, fills summed together -- see that function's
    docstring).

    Wallets with fewer than ``COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK``
    deduped decisions are never paused by this function (insufficient
    sample size) -- logged at DEBUG with the current decision count so
    "why was nothing paused" is answerable from the log.

    The pause signal is the **total realized P&L** summed across the
    deduped decisions -- not median per-trade ROI. Under flat staking a
    loss is exactly -100% ROI, so "median ROI < 0" is precisely "more than
    half the trades lost": a win-rate check wearing an ROI label, unable to
    distinguish a profitable low-win-rate wallet (e.g. buying at 0.25 with
    a 40% hit rate: mostly losing trades, but solidly net positive) from an
    unprofitable one. Total P&L cannot make that mistake.
    """
    settled = db.get_settled_copy_positions(address)
    decisions = dedupe_decisions(settled)
    n_decisions = len(decisions)

    threshold = _min_decisions_threshold(db)

    if n_decisions < threshold:
        log.debug(
            "[copy_wallet_health] %s: %d deduped decision(s) < threshold %d -- "
            "realized-P&L check skipped (insufficient sample)",
            address, n_decisions, threshold,
        )
        return None

    total_pnl = sum(d["pnl"] for d in decisions)
    if total_pnl < 0:
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

        reason = _realized_pnl_pause_reason(db, address)
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
