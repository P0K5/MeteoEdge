"""Tests for the Services section of the daily health report (issue #944).
systemctl is mocked; no real systemd needed."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

from src.scripts import daily_health_report as dhr

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
MANAGED = ["meteoedge.service", "meteoedge-settle.timer"]


def _fake(shows, host):
    def run(*args):
        if args[0] == "show":
            return shows[args[1]]
        return host
    return run


def _show(active="active", sub="running", n=0, load="loaded"):
    return f"LoadState={load}\nActiveState={active}\nSubState={sub}\nNRestarts={n}\n"


def _run(shows, host):
    with patch.object(dhr, "_managed_units", return_value=MANAGED), \
         patch.object(dhr, "_systemctl", side_effect=_fake(shows, host)):
        return dhr._build_services(None, NOW)


def test_healthy():
    lines, flags = _run(
        {"meteoedge.service": _show(), "meteoedge-settle.timer": _show(sub="waiting")},
        "meteoedge.service loaded active running x\nmeteoedge-settle.timer loaded active waiting y\n")
    assert flags["status"] == "OK"
    assert not any("[WARN]" in l for l in lines)


def test_failed_unit():
    lines, flags = _run(
        {"meteoedge.service": _show("failed", "failed"), "meteoedge-settle.timer": _show()}, "")
    assert flags["status"] == "WARN"
    assert "meteoedge.service" in flags["details"][0]


def test_high_restart_count_while_active():
    _, flags = _run(
        {"meteoedge.service": _show(n=78547), "meteoedge-settle.timer": _show()}, "")
    assert flags["status"] == "WARN"
    assert "78547 restarts" in flags["details"][0]


def test_unmanaged_unit():
    lines, flags = _run(
        {"meteoedge.service": _show(), "meteoedge-settle.timer": _show()},
        "meteoedge.service loaded active running x\nmeteoedge-shadow.service loaded failed failed y\n")
    assert flags["status"] == "WARN"
    assert any("unmanaged" in l and "meteoedge-shadow.service" in l for l in lines)


def test_not_installed_unit_skipped():
    lines, flags = _run(
        {"meteoedge.service": _show(load="not-found", active="inactive", sub="dead"),
         "meteoedge-settle.timer": _show()}, "")
    assert flags["status"] == "OK"


def test_systemctl_unavailable():
    with patch.object(dhr, "_managed_units", return_value=MANAGED), \
         patch.object(dhr, "_systemctl", side_effect=FileNotFoundError):
        lines, flags = dhr._build_services(None, NOW)
    assert "(unavailable" in "\n".join(lines)
    assert flags["status"] == "UNKNOWN"


def test_systemctl_wrapper_raises_on_failure():
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "systemctl")):
        try:
            dhr._systemctl("show", "x")
        except subprocess.CalledProcessError:
            return
    raise AssertionError("expected raise")


def test_managed_units_reads_deploy_dir():
    names = dhr._managed_units()
    assert "meteoedge.service" in names and "meteoedge-shadow.service" not in names


def test_verdict_surfaces_services_warning():
    out = dhr._build_verdict([("services", ["Services"], {"status": "WARN", "details": ["1 unit(s) failed/restarting: x"]})])
    assert "[WARN]" in "\n".join(out) and "failed/restarting" in "\n".join(out)
