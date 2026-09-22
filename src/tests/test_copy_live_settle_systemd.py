"""Tests for the copy-live-settle systemd service/timer configuration and
installation (epic I #1160, issue #1174). Mirrors
test_copy_settle_systemd.py's structure, adapted for the live-trading
one-shot service + timer pair.
"""
import configparser
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
SERVICE_PATH = DEPLOY_DIR / "meteoedge-copy-live-settle.service"
TIMER_PATH = DEPLOY_DIR / "meteoedge-copy-live-settle.timer"
INSTALL_SH_PATH = DEPLOY_DIR / "install.sh"


def test_copy_live_settle_service_file_exists():
    assert SERVICE_PATH.exists(), f"Service file not found at {SERVICE_PATH}"


def test_copy_live_settle_timer_file_exists():
    assert TIMER_PATH.exists(), f"Timer file not found at {TIMER_PATH}"


def test_copy_live_settle_service_valid_ini():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)
    assert "Unit" in config, "Missing [Unit] section"
    assert "Service" in config, "Missing [Service] section"


def test_copy_live_settle_service_unit_section():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)
    assert config.get("Unit", "Description") == (
        "MeteoEdge live copy-trading settlement, wallet reconciliation, "
        "and ghost-order recovery"
    )
    assert config.get("Unit", "After") == "network-online.target"
    assert config.get("Unit", "Wants") == "network-online.target"


def test_copy_live_settle_service_service_section():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)

    # Type must be 'oneshot' -- one pass per invocation, not a persistent loop.
    assert config.get("Service", "Type") == "oneshot"

    assert config.get("Service", "User") == "p0k5"
    assert config.get("Service", "WorkingDirectory") == "/home/p0k5/MeteoEdge"
    assert config.get("Service", "Environment") == "PYTHONUNBUFFERED=1"
    assert config.get("Service", "EnvironmentFile") == "/home/p0k5/MeteoEdge/.env"

    exec_start = config.get("Service", "ExecStart")
    assert "copy_live_settle" in exec_start, \
        f"ExecStart should reference copy_live_settle: {exec_start}"
    assert "-u" in exec_start, "Should run with unbuffered Python"
    assert "-m src.scripts.copy_live_settle" in exec_start, "Should use -m flag for module execution"

    assert config.get("Service", "StandardOutput") == "append:/home/p0k5/MeteoEdge/logs/copy_live_settle.log"
    assert config.get("Service", "StandardError") == "append:/home/p0k5/MeteoEdge/logs/copy_live_settle.log"


def test_copy_live_settle_timer_valid_ini():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    assert "Unit" in config, "Missing [Unit] section"
    assert "Timer" in config, "Missing [Timer] section"
    assert "Install" in config, "Missing [Install] section"


def test_copy_live_settle_timer_runs_hourly():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    # Same hourly cadence as meteoedge-copy-settle.timer (issue #1174):
    # real-money reconciliation latency matters at least as much as paper's.
    assert config.get("Timer", "OnCalendar") == "hourly"
    assert config.get("Timer", "Persistent") == "true"


def test_copy_live_settle_timer_install_section():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    assert config.get("Install", "WantedBy") == "timers.target"


def test_install_sh_includes_copy_live_settle_units():
    content = INSTALL_SH_PATH.read_text()
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-live-settle.service"' in content, \
        "install.sh should install meteoedge-copy-live-settle.service"
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-live-settle.timer"' in content, \
        "install.sh should install meteoedge-copy-live-settle.timer"
    assert '"$UNIT_DIR/meteoedge-copy-live-settle.service"' in content
    assert '"$UNIT_DIR/meteoedge-copy-live-settle.timer"' in content


def test_install_sh_enables_copy_live_settle_timer():
    content = INSTALL_SH_PATH.read_text()
    assert "systemctl enable --now meteoedge-copy-live-settle.timer" in content, \
        "install.sh should enable and start meteoedge-copy-live-settle.timer"


def test_install_sh_includes_copy_live_settle_in_status_check():
    content = INSTALL_SH_PATH.read_text()
    assert "meteoedge-copy-live-settle.timer" in content, \
        "install.sh should include meteoedge-copy-live-settle.timer in status output"
