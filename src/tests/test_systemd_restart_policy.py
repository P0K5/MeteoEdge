"""Crash-loop audit of every unit in deploy/systemd/ (issue #1384, follow-up
to #944, where two units with Restart=always exited 0 and were restarted
~78,000 times).

Rules:
* A Type=oneshot service must never set Restart=always/on-success (systemd
  itself rejects always for oneshot, but a typo'd Type must not slip through).
* Any service with Restart=always/on-success must be an intentionally
  persistent one (PERSISTENT_UNITS), and must set RestartSec and declare StartLimitIntervalSec.
* A service with no Type=oneshot but a timer is short-lived and must not
  restart on success.
"""
import configparser
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"

# Intentionally persistent daemons; adding a unit here is a deliberate review.
PERSISTENT_UNITS = {
    "meteoedge.service",
    "meteoedge-copy-signals.service",
    "cryptoedge.service",
}
RESTART_ON_SUCCESS = {"always", "on-success"}


def _parse(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser(strict=False, interpolation=None)
    config.optionxform = str
    # Join "\" continuation lines like systemd does.
    config.read_string(path.read_text().replace("\\\n", " "))
    return config


def _services():
    return sorted(DEPLOY_DIR.glob("*.service"))


def _opt(config, section, key):
    return config.get(section, key, fallback=None)


def test_units_found():
    assert len(_services()) >= len(PERSISTENT_UNITS)


@pytest.mark.parametrize("path", _services(), ids=lambda p: p.name)
def test_no_oneshot_restarts_on_success(path):
    config = _parse(path)
    restart = _opt(config, "Service", "Restart")
    is_oneshot = _opt(config, "Service", "Type") == "oneshot"
    timer = path.with_suffix(".timer")
    if is_oneshot or timer.exists():
        assert restart not in RESTART_ON_SUCCESS, (
            f"{path.name} is short-lived (oneshot/timer-driven) but sets "
            f"Restart={restart}: it would be restarted forever after exiting 0"
        )


@pytest.mark.parametrize("path", _services(), ids=lambda p: p.name)
def test_restart_always_only_on_known_persistent_units(path):
    config = _parse(path)
    restart = _opt(config, "Service", "Restart")
    if restart in RESTART_ON_SUCCESS:
        assert path.name in PERSISTENT_UNITS, (
            f"{path.name} sets Restart={restart} but is not in PERSISTENT_UNITS; "
            "if it is a real long-running daemon add it there, otherwise drop Restart="
        )
        assert _opt(config, "Service", "Type") != "oneshot"


@pytest.mark.parametrize("name", sorted(PERSISTENT_UNITS))
def test_persistent_units_have_delay_and_start_limit(name):
    config = _parse(DEPLOY_DIR / name)
    assert _opt(config, "Service", "Type") == "simple"
    # Never weaken restart-on-failure.
    assert _opt(config, "Service", "Restart") == "always"
    restart_sec = _opt(config, "Service", "RestartSec")
    assert restart_sec and int(restart_sec.rstrip("s")) >= 5, "RestartSec must be >= 5s"
    # Must be declared explicitly. 0 (no start limit) is expected here: a
    # persistent trading process must never be left `failed` by a transient
    # outage; crash loops surface via the health report's NRestarts check.
    interval = _opt(config, "Unit", "StartLimitIntervalSec")
    assert interval is not None, "StartLimitIntervalSec must be declared explicitly"
    assert int(interval) >= 0
    assert not (DEPLOY_DIR / name).with_suffix(".timer").exists()


def test_every_service_has_known_shape():
    # Every service is either a oneshot (timer-driven) or a known persistent unit.
    for path in _services():
        config = _parse(path)
        is_oneshot = _opt(config, "Service", "Type") == "oneshot"
        assert is_oneshot or path.name in PERSISTENT_UNITS, (
            f"{path.name} is neither Type=oneshot nor in PERSISTENT_UNITS"
        )
