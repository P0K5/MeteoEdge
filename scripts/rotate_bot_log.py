#!/usr/bin/env python3
"""Daily rotation of bot.log with ownership fix and housekeeping.

Run daily via systemd timer (meteoedge-rotate-logs.timer). Rotates the current
bot.log to a dated file (copytruncate pattern), fixes ownership to p0k5:p0k5,
compresses and deletes aged files.

Must run as root to fix ownership on root:root files. If chown fails, the
script exits with error (fails loudly per acceptance criterion 2).

See docs/DEPLOY_BOT_LOG_ROTATION.md for deployment details.
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

def get_p0k5_uid_gid():
    """Get the UID and GID for user p0k5.

    Returns:
        tuple: (uid, gid) for p0k5, or (None, None) if lookup fails
    """
    try:
        import pwd
        import grp
        pw = pwd.getpwnam("p0k5")
        return (pw.pw_uid, pw.pw_gid)
    except (KeyError, ImportError):
        return (None, None)

def main():
    """Rotate bot.log, fix ownership, and housekeep old files."""
    log_path = repo_root / "logs" / "bot.log"

    try:
        # Get p0k5's UID/GID for ownership fix
        uid, gid = get_p0k5_uid_gid()
        if uid is None:
            print("[rotate_bot_log] WARNING: Could not look up p0k5 UID/GID", file=sys.stderr)
            uid = None
            gid = None

        # Rotation + ownership fix + housekeeping
        rotated = rotate_plaintext_log(log_path, owner_uid=uid, owner_gid=gid)
        print(f"[rotate_bot_log] Rotated to: {rotated.name}")

        housekeep_plaintext(log_path)
        print("[rotate_bot_log] Housekeeping complete")

        # Verify bot.log exists and is being written to (not left deleted)
        if not log_path.exists():
            print(f"[rotate_bot_log] ERROR: {log_path} does not exist after rotation!", file=sys.stderr)
            print("[rotate_bot_log] The service may not have reopened its log stream.", file=sys.stderr)
            sys.exit(1)

        print(f"[rotate_bot_log] Verified: {log_path} exists and ready for new writes")
        print("[rotate_bot_log] Rotation complete")

    except OSError as e:
        # Ownership fix failed — this is a real error that must be fixed
        print(f"[rotate_bot_log] ERROR: {e}", file=sys.stderr)
        print("[rotate_bot_log] Rotation failed (likely chown permission issue)", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[rotate_bot_log] ERROR: Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
