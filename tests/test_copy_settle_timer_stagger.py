"""Regression for #1334: copy-settle and copy-live-settle must not fire together."""
import re
from pathlib import Path

SYSTEMD = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def _minutes(timer: str) -> set[int]:
    text = (SYSTEMD / timer).read_text()
    m = re.search(r"^OnCalendar=\*:(\d+)/(\d+)$", text, re.M)
    assert m, f"{timer}: expected OnCalendar=*:S/N"
    start, step = int(m.group(1)), int(m.group(2))
    return set(range(start, 60, step))


def test_settle_timers_never_share_a_minute():
    a = _minutes("meteoedge-copy-settle.timer")
    b = _minutes("meteoedge-copy-live-settle.timer")
    assert not a & b
    # keep >=5 minutes (circular) between any two firings
    gaps = [min((x - y) % 60, (y - x) % 60) for x in a for y in b]
    assert min(gaps) >= 5
