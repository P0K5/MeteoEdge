"""Regression for #1386: copy-health must not share a minute with either settle timer.

Follow-up to #1334 (copy-settle vs copy-live-settle stagger, tests/test_copy_settle_timer_stagger.py).
meteoedge-copy-health.timer is daily and used to fire at 03:45 UTC, the same second as
meteoedge-copy-settle.timer's :45 slot. Both write the one SQLite DB.

The settle slots are :00/:15/:30/:45 (copy-settle) and :07/:22/:37/:52 (copy-live-settle).
Their gaps alternate 7 and 8 minutes, so no minute is 5 or more minutes from every slot;
the best achievable distance is 4 minutes, which is what this test enforces.
"""
import re
from pathlib import Path

SYSTEMD = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
HEALTH_TIMER = "meteoedge-copy-health.timer"
SETTLE_TIMERS = ["meteoedge-copy-settle.timer", "meteoedge-copy-live-settle.timer"]


def _settle_minutes(timer: str) -> set[int]:
    text = (SYSTEMD / timer).read_text()
    m = re.search(r"^OnCalendar=\*:(\d+)/(\d+)$", text, re.M)
    assert m, f"{timer}: expected OnCalendar=*:S/N"
    start, step = int(m.group(1)), int(m.group(2))
    return set(range(start, 60, step))


def _health_minute() -> int:
    text = (SYSTEMD / HEALTH_TIMER).read_text()
    m = re.search(r"^OnCalendar=\*-\*-\* \d{2}:(\d{2}):00 UTC$", text, re.M)
    assert m, f"{HEALTH_TIMER}: expected OnCalendar=*-*-* HH:MM:00 UTC"
    return int(m.group(1))


def _circular_gap(a: int, b: int) -> int:
    return min((a - b) % 60, (b - a) % 60)


def test_copy_health_minute_is_not_a_settle_minute():
    minute = _health_minute()
    for timer in SETTLE_TIMERS:
        assert minute not in _settle_minutes(timer), (
            f"{HEALTH_TIMER} minute :{minute:02d} shares a minute with {timer}"
        )


def test_copy_health_keeps_max_achievable_gap_from_settle_timers():
    minute = _health_minute()
    settle = set().union(*(_settle_minutes(t) for t in SETTLE_TIMERS))
    gap = min(_circular_gap(minute, s) for s in settle)
    # 5 is unreachable on the :00/:15/:30/:45 + :07/:22/:37/:52 grid; 4 is the maximum.
    assert gap >= 4, f"{HEALTH_TIMER} minute :{minute:02d} is only {gap} min from a settle slot"
