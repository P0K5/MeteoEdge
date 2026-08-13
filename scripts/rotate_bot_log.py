#!/usr/bin/env python3
"""Daily rotation of systemd logs with housekeeping.

Run daily via systemd timer (meteoedge-rotate-logs.timer). Rotates all
StandardOutput=append: logs to dated files (copytruncate pattern), compresses
and deletes aged files.

Rotates:
  - logs/bot.log (from meteoedge.service)
  - logs/archive.log (from meteoedge-archive.service)
  - logs/capture_forecasts.log (from meteoedge-capture-forecasts.service)
  - logs/health_report.log (from meteoedge-health-report.service)
  - logs/prob_cap_report.log (from meteoedge-prob-cap-report.service)
  - logs/purge.log (from meteoedge-purge-retention.service)
  - logs/resolve_outcomes.log (from meteoedge-resolve-outcomes.service)
  - logs/settle.log (from meteoedge-settle.service)

All services run as User=p0k5 (or should have p0k5 ownership). Ownership must be
fixed via one-time sudo chown step (documented in DEPLOY_SYSTEMD_LOG_ROTATION.md).

See docs/DEPLOY_SYSTEMD_LOG_ROTATION.md for deployment details.
"""
import os
import sys
from pathlib import Path

# Ensure we're in the repo root
repo_root = Path(__file__).parent.parent
os.chdir(repo_root)

# Add repo to path
sys.path.insert(0, str(repo_root))

from src.utils.log_rotation import rotate_plaintext_log, housekeep_plaintext

# Define all systemd logs to rotate, with their service user
# All should be owned by p0k5:p0k5 after a one-time chown step
LOGS_TO_ROTATE = [
    "logs/bot.log",
    "logs/archive.log",
    "logs/capture_forecasts.log",
    "logs/health_report.log",
    "logs/prob_cap_report.log",
    "logs/purge.log",
    "logs/resolve_outcomes.log",
    "logs/settle.log",
]


def get_p0k5_uid_gid():
    """Get the UID and GID for user p0k5.

    Returns:
        tuple: (uid, gid) for p0k5, or (None, None) if lookup fails
    """
    try:
        import pwd
        pw = pwd.getpwnam("p0k5")
        return (pw.pw_uid, pw.pw_gid)
    except (KeyError, ImportError):
        return (None, None)


def main():
    """Rotate all systemd logs and housekeep old files."""
    try:
        # Get p0k5's UID/GID for ownership management
        uid, gid = get_p0k5_uid_gid()
        if uid is None:
            print("[rotate_logs] WARNING: Could not look up p0k5 UID/GID", file=sys.stderr)

        # Rotate and housekeep each log
        for log_name in LOGS_TO_ROTATE:
            log_path = repo_root / log_name

            try:
                # Rotation + ownership verification
                rotated = rotate_plaintext_log(log_path, owner_uid=uid, owner_gid=gid)
                print(f"[rotate_logs] {log_path.name}: rotated to {rotated.name}")

                # Housekeeping
                housekeep_plaintext(log_path)

            except OSError as e:
                # Ownership fix failed — this is a real error that must be fixed
                print(f"[rotate_logs] ERROR on {log_path.name}: {e}", file=sys.stderr)
                print(f"[rotate_logs] Check that {log_path.name} is owned by p0k5:p0k5", file=sys.stderr)
                print("[rotate_logs] (See DEPLOY_SYSTEMD_LOG_ROTATION.md for the one-time sudo chown step)", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"[rotate_logs] ERROR on {log_path.name}: Unexpected error: {e}", file=sys.stderr)
                sys.exit(1)

        print("[rotate_logs] Rotation and housekeeping complete for all logs")

    except Exception as e:
        print(f"[rotate_logs] ERROR: Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
