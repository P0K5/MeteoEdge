"""Tests for the copy-screening systemd service/timer configuration and
installation (epic #1099 story 2, issue #1213). Mirrors
test_copy_settle_systemd.py's structure, adapted for the wallet-screening
oneshot service + timer pair.
"""
import configparser
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
SERVICE_PATH = DEPLOY_DIR / "meteoedge-copy-screening.service"
TIMER_PATH = DEPLOY_DIR / "meteoedge-copy-screening.timer"
INSTALL_SH_PATH = DEPLOY_DIR / "install.sh"


def test_copy_screening_service_file_exists():
    assert SERVICE_PATH.exists(), f"Service file not found at {SERVICE_PATH}"


def test_copy_screening_timer_file_exists():
    assert TIMER_PATH.exists(), f"Timer file not found at {TIMER_PATH}"


def test_copy_screening_service_valid_ini():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)
    assert "Unit" in config, "Missing [Unit] section"
    assert "Service" in config, "Missing [Service] section"


def test_copy_screening_service_unit_section():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)
    assert config.get("Unit", "Description") == "MeteoEdge copy-wallet screening (leaderboard scan)"
    assert config.get("Unit", "After") == "network-online.target"
    assert config.get("Unit", "Wants") == "network-online.target"


def test_copy_screening_service_service_section():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)

    # Type must be 'oneshot' -- this runs once per invocation and exits,
    # matching copy_settle.py/copy_wallet_health.py's shape, not a
    # persistent loop like copy_signal_loop.py.
    assert config.get("Service", "Type") == "oneshot"

    assert config.get("Service", "User") == "p0k5"
    assert config.get("Service", "WorkingDirectory") == "/home/p0k5/MeteoEdge"
    assert config.get("Service", "Environment") == "PYTHONUNBUFFERED=1"
    assert config.get("Service", "EnvironmentFile") == "/home/p0k5/MeteoEdge/.env"

    exec_start = config.get("Service", "ExecStart")
    assert "copy_wallet_screening" in exec_start, f"ExecStart should reference copy_wallet_screening: {exec_start}"
    assert "-u" in exec_start, "Should run with unbuffered Python"
    assert "-m src.scripts.copy_wallet_screening" in exec_start, "Should use -m flag for module execution"
    assert "--top 50" in exec_start, "ExecStart must include --top 50 to screen 50 wallets (issue #1213)"

    assert config.get("Service", "StandardOutput") == "append:/home/p0k5/MeteoEdge/logs/copy_screening.log"
    assert config.get("Service", "StandardError") == "append:/home/p0k5/MeteoEdge/logs/copy_screening.log"


def test_copy_screening_timer_valid_ini():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    assert "Unit" in config, "Missing [Unit] section"
    assert "Timer" in config, "Missing [Timer] section"
    assert "Install" in config, "Missing [Install] section"


def test_copy_screening_timer_runs_daily_at_0300_utc():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    # Cadence decision (issue #1099): 03:00 UTC, isolated from the 12:00–14:00
    # UTC settlement/report cluster and the 01:00 UTC purge-retention run.
    assert config.get("Timer", "OnCalendar") == "*-*-* 03:00:00 UTC"
    assert config.get("Timer", "Persistent") == "true"


def test_copy_screening_timer_install_section():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    assert config.get("Install", "WantedBy") == "timers.target"


def test_install_sh_includes_copy_screening_units():
    content = INSTALL_SH_PATH.read_text()
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-screening.service"' in content, \
        "install.sh should install meteoedge-copy-screening.service"
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-screening.timer"' in content, \
        "install.sh should install meteoedge-copy-screening.timer"
    assert '"$UNIT_DIR/meteoedge-copy-screening.service"' in content
    assert '"$UNIT_DIR/meteoedge-copy-screening.timer"' in content


def test_install_sh_enables_copy_screening_timer():
    content = INSTALL_SH_PATH.read_text()
    assert "systemctl enable --now meteoedge-copy-screening.timer" in content, \
        "install.sh should enable and start meteoedge-copy-screening.timer"


def test_install_sh_includes_copy_screening_in_status_check():
    content = INSTALL_SH_PATH.read_text()
    assert "meteoedge-copy-screening.timer" in content, \
        "install.sh should include meteoedge-copy-screening.timer in status output"
