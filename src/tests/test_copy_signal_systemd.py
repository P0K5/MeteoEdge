"""Tests for copy-signal systemd service configuration and installation."""
import configparser
import os
from pathlib import Path


def test_copy_signal_service_file_exists():
    """Verify meteoedge-copy-signals.service file exists."""
    service_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "meteoedge-copy-signals.service"
    assert service_path.exists(), f"Service file not found at {service_path}"


def test_copy_signal_service_valid_ini():
    """Verify meteoedge-copy-signals.service is valid systemd INI format."""
    service_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "meteoedge-copy-signals.service"

    # systemd units are INI-like; parse as configparser
    config = configparser.ConfigParser()
    config.read(service_path)

    # Verify required sections exist
    assert "Unit" in config, "Missing [Unit] section"
    assert "Service" in config, "Missing [Service] section"
    assert "Install" in config, "Missing [Install] section"


def test_copy_signal_service_unit_section():
    """Verify [Unit] section has correct configuration."""
    service_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "meteoedge-copy-signals.service"
    config = configparser.ConfigParser()
    config.read(service_path)

    assert config.get("Unit", "Description") == "MeteoEdge copy-signal detection loop"
    assert config.get("Unit", "After") == "network-online.target"
    assert config.get("Unit", "Wants") == "network-online.target"


def test_copy_signal_service_service_section():
    """Verify [Service] section has correct configuration."""
    service_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "meteoedge-copy-signals.service"
    config = configparser.ConfigParser()
    config.read(service_path)

    # Type must be 'simple' for a persistent process
    assert config.get("Service", "Type") == "simple"

    # User should be p0k5
    assert config.get("Service", "User") == "p0k5"

    # Must run from the correct directory
    assert config.get("Service", "WorkingDirectory") == "/home/p0k5/MeteoEdge"

    # Environment variables
    assert config.get("Service", "Environment") == "PYTHONUNBUFFERED=1"

    # Must load from .env file
    assert config.get("Service", "EnvironmentFile") == "/home/p0k5/MeteoEdge/.env"

    # ExecStart points to copy_signal_loop module
    exec_start = config.get("Service", "ExecStart")
    assert "copy_signal_loop" in exec_start, f"ExecStart should reference copy_signal_loop: {exec_start}"
    assert "-u" in exec_start, "Should run with unbuffered Python"
    assert "-m src.scripts.copy_signal_loop" in exec_start, "Should use -m flag for module execution"

    # Restart behavior
    assert config.get("Service", "Restart") == "always"
    assert config.get("Service", "RestartSec") == "10s"

    # Logging to a dedicated file
    assert config.get("Service", "StandardOutput") == "append:/home/p0k5/MeteoEdge/logs/copy_signals.log"
    assert config.get("Service", "StandardError") == "append:/home/p0k5/MeteoEdge/logs/copy_signals.log"


def test_copy_signal_service_install_section():
    """Verify [Install] section has correct configuration."""
    service_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "meteoedge-copy-signals.service"
    config = configparser.ConfigParser()
    config.read(service_path)

    assert config.get("Install", "WantedBy") == "multi-user.target"


def test_install_sh_includes_copy_signal_service():
    """Verify install.sh copies the copy-signal service file."""
    install_sh_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "install.sh"
    content = install_sh_path.read_text()

    # Verify copy-signal service is installed
    assert 'install -m 0644 "$SRC_DIR/meteoedge-copy-signals.service"' in content, \
        "install.sh should install meteoedge-copy-signals.service"

    # Verify it's being installed to the right place
    assert '"$UNIT_DIR/meteoedge-copy-signals.service"' in content, \
        "install.sh should install service to $UNIT_DIR"


def test_install_sh_enables_copy_signal_service():
    """Verify install.sh enables the copy-signal service."""
    install_sh_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "install.sh"
    content = install_sh_path.read_text()

    # Verify copy-signal service is enabled
    assert "systemctl enable --now meteoedge-copy-signals.service" in content, \
        "install.sh should enable and start meteoedge-copy-signals.service"


def test_install_sh_includes_copy_signal_in_status_check():
    """Verify install.sh includes copy-signal in the final status check."""
    install_sh_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "install.sh"
    content = install_sh_path.read_text()

    # Verify copy-signal is in the status command
    assert "meteoedge-copy-signals.service" in content, \
        "install.sh should include meteoedge-copy-signals.service in status output"


def test_install_sh_includes_copy_signal_in_help():
    """Verify install.sh includes copy-signal in the help text."""
    install_sh_path = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "install.sh"
    content = install_sh_path.read_text()

    # Verify copy-signal is mentioned in the help output
    assert 'journalctl -u meteoedge-copy-signals.service' in content, \
        "install.sh should include copy-signal in the help text"


def test_copy_signal_script_supports_persistent_mode():
    """Verify copy_signal_loop.py has a persistent loop mode."""
    script_path = Path(__file__).resolve().parents[2] / "src" / "scripts" / "copy_signal_loop.py"
    content = script_path.read_text()

    # Verify main() function exists
    assert "def main(" in content, "copy_signal_loop.py should have a main() function"

    # Verify it has the persistent loop (while True)
    assert "while True:" in content, "copy_signal_loop.py should have a persistent while loop"

    # Verify it supports --once flag
    assert '"--once"' in content, "copy_signal_loop.py should support --once flag"

    # Verify it has run_cycle function
    assert "def run_cycle(" in content, "copy_signal_loop.py should have run_cycle() function"


def test_copy_signal_script_graceful_shutdown():
    """Verify copy_signal_loop.py handles Ctrl-C gracefully."""
    script_path = Path(__file__).resolve().parents[2] / "src" / "scripts" / "copy_signal_loop.py"
    content = script_path.read_text()

    # Verify it catches KeyboardInterrupt
    assert "KeyboardInterrupt" in content, "copy_signal_loop.py should handle KeyboardInterrupt"

    # Verify it logs shutdown
    assert "Stopping" in content, "copy_signal_loop.py should log when stopping"
