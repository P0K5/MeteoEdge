"""Tests for the Scheduled Jobs heartbeat section (issue #1422).
systemctl is mocked; no real systemd needed."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.scripts import daily_health_report as dhr

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
JOBS = list(dhr.SCHEDULED_JOB_INTERVALS_S)


def _timer(active="active", last="Wed 2026-10-14 11:50:00 UTC", load="loaded"):
    return f"LoadState={load}\nActiveState={active}\nLastTriggerUSec={last}\n"


def _svc(result="success", status="0"):
    return f"Result={result}\nExecMainStatus={status}\n"


def _run(timers=None, svcs=None):
    timers, svcs = timers or {}, svcs or {}

    def fake(*args):
        unit = args[1]
        job, kind = unit.rsplit(".", 1)
        if kind == "timer":
            return timers.get(job, _timer())
        return svcs.get(job, _svc())
    with patch.object(dhr, "_systemctl", side_effect=fake):
        return dhr._build_job_heartbeat(None, NOW)


def test_healthy():
    lines, flags = _run()
    assert flags == {"status": "OK", "details": []}
    assert not any("[" in ln for ln in lines)


def test_overdue():
    old = _timer(last="Mon 2026-10-12 11:50:00 UTC")  # 48h ago
    _lines, flags = _run(timers={"meteoedge-copy-settle": old})
    assert flags["status"] == "WARN"
    assert "meteoedge-copy-settle overdue" in flags["details"][0]


def test_daily_job_within_two_intervals_ok():
    t = _timer(last="Tue 2026-10-13 06:00:00 UTC")  # 30h ago
    _lines, flags = _run(timers={"meteoedge-settle": t})
    assert flags["status"] == "OK"


def test_failed():
    lines, flags = _run(svcs={"meteoedge-settle": _svc("exit-code", "1")})
    assert flags["status"] == "WARN"
    assert any("[FAILED]" in ln for ln in lines)


def test_timer_inactive():
    lines, flags = _run(timers={"meteoedge-copy-health": _timer(active="inactive")})
    assert flags["status"] == "WARN"
    assert any("[INACTIVE]" in ln for ln in lines)


def test_not_installed_and_never_run_do_not_warn():
    missing = _timer(load="not-found", active="inactive", last="")
    fresh = _timer(last="n/a")
    lines, flags = _run(timers={"meteoedge-copy-settle": missing,
                                "meteoedge-settle": fresh})
    assert flags["status"] == "OK"
    assert any("not installed" in ln for ln in lines)
    assert any("never run" in ln for ln in lines)


def test_systemctl_unavailable():
    with patch.object(dhr, "_systemctl", side_effect=FileNotFoundError):
        lines, flags = dhr._build_job_heartbeat(None, NOW)
    assert flags["status"] == "UNKNOWN"
    assert any("unavailable" in ln for ln in lines)


def test_verdict_hook():
    sections = [("job_heartbeat", [], {"status": "WARN", "details": ["x overdue"]})]
    assert "x overdue" in dhr._build_verdict(sections)[2]


def _interval_from_timer(text: str) -> int:
    cal = re.findall(r"^OnCalendar=(.+)$", text, re.M)
    assert len(cal) == 1, "interval table assumes a single OnCalendar"
    m = re.fullmatch(r"\*:(\d+)/(\d+)", cal[0].strip())
    if m:
        return int(m.group(2)) * 60
    assert re.fullmatch(r"\*-\*-\* \d\d:\d\d:\d\d UTC", cal[0].strip()), cal[0]
    return 24 * 3600


@pytest.mark.parametrize("job", JOBS)
def test_interval_table_matches_timer_files(job):
    path = dhr._SYSTEMD_DIR / f"{job}.timer"
    assert path.exists()
    assert dhr.SCHEDULED_JOB_INTERVALS_S[job] == _interval_from_timer(path.read_text())
