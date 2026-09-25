"""Tests for the copy-health systemd service/timer configuration and
installation (epic #1138 story D2, issue #1140). Mirrors
test_copy_settle_systemd.py's structure, adapted for the wallet-health
oneshot service + timer pair.
"""
import configparser
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
SERVICE_PATH = DEPLOY_DIR / "meteoedge-copy-health.service"
TIMER_PATH = DEPLOY_DIR / "meteoedge-copy-health.timer"
INSTALL_SH_PATH = DEPLOY_DIR / "install.sh"


def test_copy_health_service_file_exists():
    assert SERVICE_PATH.exists(), f"Service file not found at {SERVICE_PATH}"


def test_copy_health_timer_file_exists():
    assert TIMER_PATH.exists(), f"Timer file not found at {TIMER_PATH}"


def test_copy_health_service_valid_ini():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)
    assert "Unit" in config, "Missing [Unit] section"
    assert "Service" in config, "Missing [Service] section"


def test_copy_health_service_unit_section():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)
    assert config.get("Unit", "Description") == "MeteoEdge copy-trading wallet health monitor (auto-pause)"
    assert config.get("Unit", "After") == "network-online.target"
    assert config.get("Unit", "Wants") == "network-online.target"


def test_copy_health_service_service_section():
    config = configparser.ConfigParser()
    config.read(SERVICE_PATH)

    # Type must be 'oneshot' -- this runs once per invocation and exits,
    # matching copy_settle.py/copy_wallet_screening.py's shape, not a
    # persistent loop like copy_signal_loop.py.
    assert config.get("Service", "Type") == "oneshot"

    assert config.get("Service", "User") == "p0k5"
    assert config.get("Service", "WorkingDirectory") == "/home/p0k5/MeteoEdge"
    assert config.get("Service", "Environment") == "PYTHONUNBUFFERED=1"
    assert config.get("Service", "EnvironmentFile") == "/home/p0k5/MeteoEdge/.env"

    exec_start = config.get("Service", "ExecStart")
    assert "copy_wallet_health" in exec_start, f"ExecStart should reference copy_wallet_health: {exec_start}"
    assert "-u" in exec_start, "Should run with unbuffered Python"
    assert "-m src.scripts.copy_wallet_health" in exec_start, "Should use -m flag for module execution"

    assert config.get("Service", "StandardOutput") == "append:/home/p0k5/MeteoEdge/logs/copy_health.log"
    assert config.get("Service", "StandardError") == "append:/home/p0k5/MeteoEdge/logs/copy_health.log"


def test_copy_health_timer_valid_ini():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    assert "Unit" in config, "Missing [Unit] section"
    assert "Timer" in config, "Missing [Timer] section"
    assert "Install" in config, "Missing [Install] section"


def test_copy_health_timer_runs_daily_at_0345_utc():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    # Cadence decision (issue #1140, updated issue #1213): 03:45 UTC, after
    # meteoedge-copy-screening.timer's 03:00 UTC run. The 45-minute gap is
    # sized for a 50-wallet screening pool (50 wallets × 40 pages/wallet =
    # 2000 API calls at 1 req/sec ≈ 33 minutes worst-case), ensuring a fresh
    # screening row exists to stability-check against on the same day before
    # health-monitor fires. Future pool widening should re-check this timing.
    assert config.get("Timer", "OnCalendar") == "*-*-* 03:45:00 UTC"
    assert config.get("Timer", "Persistent") == "true"


def test_copy_health_timer_install_section():
    config = configparser.ConfigParser()
    config.read(TIMER_PATH)
    assert config.get("Install", "WantedBy") == "timers.target"


def test_install_sh_includes_copy_health_units():
    content = INSTALL_SH_PATH.read_text()
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-health.service"' in content, \
        "install.sh should install meteoedge-copy-health.service"
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-health.timer"' in content, \
        "install.sh should install meteoedge-copy-health.timer"
    assert '"$UNIT_DIR/meteoedge-copy-health.service"' in content
    assert '"$UNIT_DIR/meteoedge-copy-health.timer"' in content


def test_install_sh_enables_copy_health_timer():
    content = INSTALL_SH_PATH.read_text()
    assert "systemctl enable --now meteoedge-copy-health.timer" in content, \
        "install.sh should enable and start meteoedge-copy-health.timer"


def test_install_sh_includes_copy_health_in_status_check():
    content = INSTALL_SH_PATH.read_text()
    assert "meteoedge-copy-health.timer" in content, \
        "install.sh should include meteoedge-copy-health.timer in status output"


def test_copy_health_timer_starts_after_screening_timer_with_adequate_gap():
    """Enforce the timing relationship: health timer must fire after screening
    completes, not just as a literal string value.

    The screening pool (50 wallets × 40 pages/wallet = 2000 API calls at 1
    req/sec) takes ~33 minutes worst-case. Health timer must fire at least
    40 minutes after screening starts to allow completion before
    stability-checking against its output.

    This test catches the same bug as issue #1213 if someone later widens the
    pool without re-checking the gap — a prose comment alone didn't prevent
    it the first time.
    """
    import re

    screening_timer_path = DEPLOY_DIR / "meteoedge-copy-screening.timer"
    assert screening_timer_path.exists(), \
        f"Screening timer not found at {screening_timer_path}"

    # Parse both timer files' OnCalendar values
    screening_config = configparser.ConfigParser()
    screening_config.read(screening_timer_path)
    screening_oncal = screening_config.get("Timer", "OnCalendar")

    health_config = configparser.ConfigParser()
    health_config.read(TIMER_PATH)
    health_oncal = health_config.get("Timer", "OnCalendar")

    # Extract time-of-day from OnCalendar strings (format: *-*-* HH:MM:SS UTC)
    screening_match = re.search(r"(\d{2}):(\d{2}):(\d{2})", screening_oncal)
    health_match = re.search(r"(\d{2}):(\d{2}):(\d{2})", health_oncal)

    assert screening_match, \
        f"Could not parse screening timer time from OnCalendar: {screening_oncal}"
    assert health_match, \
        f"Could not parse health timer time from OnCalendar: {health_oncal}"

    # Convert to minutes since midnight for comparison
    screening_minutes = (int(screening_match.group(1)) * 60
                         + int(screening_match.group(2)))
    health_minutes = (int(health_match.group(1)) * 60
                      + int(health_match.group(2)))

    gap_minutes = health_minutes - screening_minutes
    min_gap_required = 40

    assert gap_minutes >= min_gap_required, \
        f"Health timer fires only {gap_minutes} min after screening timer starts. " \
        f"Screening (50-wallet pool) takes ~33 min worst-case, so must have at least " \
        f"{min_gap_required} min gap to complete before health checks stability-check " \
        f"against its output. Screening: {screening_oncal} ({screening_minutes} min), " \
        f"Health: {health_oncal} ({health_minutes} min). " \
        f"If widening the screening pool again, re-check this gap."
