#!/usr/bin/env python3
"""Daily rotation of bot.log with housekeeping.

Run daily via systemd timer (meteoedge-rotate-logs.timer). Rotates the current
bot.log to a dated file (copytruncate pattern), compresses and deletes aged files.

Runs as p0k5 (not root). Assumes bot.log ownership has been fixed to p0k5:p0k5
beforehand via a one-time sudo chown step (documented in DEPLOY_BOT_LOG_ROTATION.md).

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
        pw = pwd.getpwnam("p0k5")
        return (pw.pw_uid, pw.pw_gid)
    except (KeyError, ImportError):
        return (None, None)

def main():
    """Rotate bot.log and housekeep old files."""
    log_path = repo_root / "logs" / "bot.log"

    try:
        # Get p0k5's UID/GID for ownership management
        # (chown will be a no-op for files already owned by p0k5, but will raise
        #  if ownership hasn't been fixed beforehand)
        uid, gid = get_p0k5_uid_gid()
        if uid is None:
            print("[rotate_bot_log] WARNING: Could not look up p0k5 UID/GID", file=sys.stderr)

        # Rotation + ownership verification + housekeeping
        rotated = rotate_plaintext_log(log_path, owner_uid=uid, owner_gid=gid)
        print(f"[rotate_bot_log] Rotated to: {rotated.name}")

        housekeep_plaintext(log_path)
        print("[rotate_bot_log] Housekeeping complete")
        print("[rotate_bot_log] Rotation complete")

    except OSError as e:
        # Ownership fix failed — this is a real error that must be fixed
        # (likely bot.log is still root:root and hasn't been chowned beforehand)
        print(f"[rotate_bot_log] ERROR: {e}", file=sys.stderr)
        print("[rotate_bot_log] Check that bot.log is owned by p0k5:p0k5", file=sys.stderr)
        print("[rotate_bot_log] (See DEPLOY_BOT_LOG_ROTATION.md for the one-time sudo chown step)", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[rotate_bot_log] ERROR: Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
