"""Stuck-position escalation thresholds and classification (issue #1301).

A copy-trading paper position is "stuck" when its market has not resolved
(``fetch_market_resolution`` returned ``None``) long after entry. This module
only *classifies age*; it never decides an outcome and never closes anything.
The only way out of ``open`` other than a real resolution is the
operator-driven ``Database.abandon_copy_position``.

``RESOLVED_YES_MIN_CENTS`` / ``RESOLVED_NO_MAX_CENTS`` (polymarket.py) are
deliberately NOT touched or referenced here (#644).

Thresholds:

- ``STUCK_HOURS = 48`` -- level 1 "aged": the pre-existing threshold, ~8x the
  observed ~6h average settle latency and past the >24h tail.
- ``PATHOLOGICAL_HOURS = 168`` (7 days) -- level 2: UMA's standard liveness
  is ~2h, and a dispute escalates to a DVM vote that takes roughly 2-4 days
  end to end; a market still unpinned after a full week has outlived both the
  normal and the disputed path, so waiting longer is no longer informative and
  a human must look. For PAST_END_UNRESOLVED both thresholds run from the
  market's ``endDate`` (the moment resolution could start), not from entry.
- level 3 -- CLOSED_AMBIGUOUS: deterministic and verifiable (closed, UMA
  resolved, price in the ambiguous band), so no waiting period applies.
Every escalated position (level >= 1) is reported as excluded from
ROI/win-rate denominators.
"""
from __future__ import annotations

from datetime import datetime, timezone

STUCK_HOURS = 48
PATHOLOGICAL_HOURS = 168

# Categories (market-state based; age alone is NOT a signal -- 23 of the 29
# positions open >48h on 2026-10-13 were healthy long-dated markets):
#   NOT_DUE              closed=false and endDate in the future -> never escalated
#   PAST_END_UNRESOLVED  endDate passed, not pinned; 48h / 7d measured from endDate
#   CLOSED_AMBIGUOUS     closed + UMA resolved + YES price inside the ambiguous
#                        band (e.g. a 50/50 void) -> escalated immediately
#   UNRETRIEVABLE        no row from either Gamma lookup -> aged from entry
NOT_DUE = "NOT_DUE"
PAST_END_UNRESOLVED = "PAST_END_UNRESOLVED"
CLOSED_AMBIGUOUS = "CLOSED_AMBIGUOUS"
UNRETRIEVABLE = "UNRETRIEVABLE"

#: level -> label. 3 is the most urgent (verified ambiguous close).
LEVEL_LABELS = {1: "AGED_48H", 2: "PATHOLOGICAL_7D", 3: "CLOSED_AMBIGUOUS"}


def level_for_hours(hours: float) -> int:
    if hours > PATHOLOGICAL_HOURS:
        return 2
    if hours > STUCK_HOURS:
        return 1
    return 0


def classify_market(state: "dict | None", entry_ts: str, now: datetime) -> "tuple[str, int]":
    """Return ``(category, level)`` for an unresolved open position.

    *state* is ``polymarket.fetch_market_state`` output (or None when neither
    Gamma lookup returned the market). Pure; never decides an outcome."""
    from src.data.polymarket import RESOLVED_NO_MAX_CENTS, RESOLVED_YES_MIN_CENTS

    if state is None:
        return UNRETRIEVABLE, level_for_hours(age_hours(entry_ts, now))
    price = state.get("yes_price_cents")
    if (
        state.get("closed")
        and str(state.get("uma") or "").lower() == "resolved"
        and price is not None
        and RESOLVED_NO_MAX_CENTS < price < RESOLVED_YES_MIN_CENTS
    ):
        return CLOSED_AMBIGUOUS, 3
    end = state.get("end_date")
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if not state.get("closed") and end is not None and end > now:
        return NOT_DUE, 0
    ref_hours = (now - end).total_seconds() / 3600.0 if end is not None else age_hours(entry_ts, now)
    return PAST_END_UNRESOLVED, level_for_hours(ref_hours)


def stuck_level(entry_ts: str, now: datetime) -> int:
    """0 = fine, 1 = aged past STUCK_HOURS, 2 = past PATHOLOGICAL_HOURS.
    Raises ValueError on a malformed *entry_ts*."""
    entry = datetime.fromisoformat(entry_ts)
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    hours = (now - entry).total_seconds() / 3600.0
    if hours > PATHOLOGICAL_HOURS:
        return 2
    if hours > STUCK_HOURS:
        return 1
    return 0


def age_hours(entry_ts: str, now: datetime) -> float:
    entry = datetime.fromisoformat(entry_ts)
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    return (now - entry).total_seconds() / 3600.0
