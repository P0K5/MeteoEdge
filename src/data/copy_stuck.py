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
  a human must look. Positions at level 2 are reported as excluded from
  ROI/win-rate denominators.
"""
from __future__ import annotations

from datetime import datetime, timezone

STUCK_HOURS = 48
PATHOLOGICAL_HOURS = 168

LEVEL_LABELS = {1: "AGED_48H", 2: "PATHOLOGICAL_7D"}


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
