# bot.log Rotation Deployment Runbook

## Overview

This document describes how to deploy the bot.log rotation mechanism on the live production host. The rotation uses a safe **move-then-reopen** pattern that does not corrupt systemd's held file descriptor.

## Pre-deployment Checklist

- [ ] Code is deployed: `src/utils/log_rotation.py` contains `rotate_plaintext_log()` and `housekeep_plaintext()`
- [ ] Tests pass: `pytest src/tests/test_log_rotation.py` (41 tests)
- [ ] No live KORD positions are open (coordinate with #977)
- [ ] You have `sudo` access on the p0k5 host

## Architecture

### How It Works

1. **Move-then-reopen pattern** (safe for systemd logs):
   - `rotate_plaintext_log()` moves `/home/p0k5/MeteoEdge/logs/bot.log` → `/home/p0k5/MeteoEdge/logs/bot.2026-08-11.log`
   - The move operation leaves systemd's held file descriptor pointing to the now-orphaned inode (still valid, data preserved)
   - Signal systemd to reopen its stdout stream, which opens `/home/p0k5/MeteoEdge/logs/bot.log` anew (now empty, at offset 0)

2. **Ownership fix**:
   - The moved dated file is chown'd to `p0k5:p0k5` (service user), fixing the root:root inconsistency
   - Future rotations automatically apply this fix (see `_fix_file_ownership()`)

3. **Retention policy**:
   - Aged files (default `SNAPSHOT_RETAIN_DAYS=365` days) are gzip-compressed after 1 day, then deleted after 365 days
   - Matches the retention for irreproducible snapshot data (weather observations, market data)

### Key Constraints

- **Never truncate in place** (`: > bot.log`). This produces a sparse, corrupt file: systemd has an fd at offset 298 MB, the truncate is instantaneous, the next write lands past the gap, and the kernel zero-fills.
- **Signal systemd after rotation**. The move alone is not enough; systemd must be told to close and reopen.
- **Rotation must be atomic** from systemd's perspective. The sequence is:
  1. Rotate (move) the file
  2. Fix ownership
  3. Compress/delete old files
  4. Signal systemd to reopen
  5. Verify the new stream is working

## Deployment Steps

### Step 1: Ensure Systemd Service Is Running

```bash
sudo systemctl status meteoedge
```

Expected output:
```
● meteoedge.service - MeteoEdge live trading bot
   Loaded: loaded (/etc/systemd/system/meteoedge.service; enabled; ...)
   Active: active (running) since ...
```

If not running, do NOT proceed. Coordinate with the team.

### Step 2: Run the Rotation Script (One-Time + Scheduled)

#### Option A: Manual rotation (one-time)

On the p0k5 host, as the service user (or sudo):

```bash
cd /home/p0k5/MeteoEdge
python3 -c "
from pathlib import Path
import os
from src.utils.log_rotation import rotate_plaintext_log, housekeep_plaintext

# Rotate bot.log to dated file + fix ownership
log_path = Path('/home/p0k5/MeteoEdge/logs/bot.log')
owner_uid = os.getuid()  # Preserve current user (p0k5)
owner_gid = os.getgid()

rotated = rotate_plaintext_log(log_path, owner_uid=owner_uid, owner_gid=owner_gid)
print(f'Rotated: {rotated}')

# Housekeep old dated files
housekeep_plaintext(log_path)
print('Housekeeping complete')
"
```

#### Option B: Scheduled via systemd timer (recommended)

Create a daily timer service:

**File: `/etc/systemd/system/meteoedge-rotate-bot-log.service`**

```ini
[Unit]
Description=Rotate MeteoEdge bot.log
After=meteoedge.service
Documentation=docs/DEPLOY_BOT_LOG_ROTATION.md

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/p0k5/MeteoEdge/scripts/rotate_bot_log.py
StandardOutput=journal
StandardError=journal
```

**File: `/etc/systemd/system/meteoedge-rotate-bot-log.timer`**

```ini
[Unit]
Description=Daily rotation timer for MeteoEdge bot.log
Documentation=docs/DEPLOY_BOT_LOG_ROTATION.md

[Timer]
# Run at 00:05 UTC daily (after midnight, before market opens)
OnCalendar=daily
OnCalendar=*-*-* 00:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

**Script: `/home/p0k5/MeteoEdge/scripts/rotate_bot_log.py`**

```python
#!/usr/bin/env python3
"""One-time rotation of bot.log with ownership fix and housekeeping.

Run daily via systemd timer. Rotates the current bot.log to a dated file,
fixes ownership, compresses old files, then signals systemd to reopen the stream.
"""
import os
import subprocess
import sys
from pathlib import Path

# Ensure we're in the repo root
repo_root = Path(__file__).parent.parent
os.chdir(repo_root)

# Add repo to path
sys.path.insert(0, str(repo_root))

from src.utils.log_rotation import rotate_plaintext_log, housekeep_plaintext

def main():
    log_path = repo_root / "logs" / "bot.log"
    
    # Rotation + ownership fix + housekeeping
    try:
        # Get p0k5 user's UID/GID
        # (On Linux: getpwnam("p0k5").pw_uid / pw_gid)
        # For simplicity, chown to p0k5:p0k5 by name (systemd does this too)
        owner_uid = None  # Will be set by -o in rotate_plaintext_log call, or chown p0k5 post-rotate
        owner_gid = None
        
        rotated = rotate_plaintext_log(log_path, owner_uid=owner_uid, owner_gid=owner_gid)
        print(f"[bot.log rotation] Rotated to: {rotated.name}")
        
        housekeep_plaintext(log_path)
        print("[bot.log rotation] Housekeeping complete")
        
        # Signal systemd to reopen bot.log
        # This is the critical step: tells systemd to close its held fd and open a new one
        result = subprocess.run(
            ["sudo", "systemctl", "kill", "-s", "SIGHUP", "meteoedge"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("[bot.log rotation] Signaled meteoedge (SIGHUP) to reopen log stream")
        else:
            print(f"[bot.log rotation] WARNING: signal failed: {result.stderr}")
            sys.exit(1)
        
    except Exception as e:
        print(f"[bot.log rotation] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

Enable the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meteoedge-rotate-bot-log.timer
sudo systemctl start meteoedge-rotate-bot-log.timer
```

Verify:

```bash
sudo systemctl status meteoedge-rotate-bot-log.timer
sudo systemctl list-timers meteoedge-rotate-bot-log.timer
```

### Step 3: Verify the Rotation Works

Check the timer's status:

```bash
sudo systemctl status meteoedge-rotate-bot-log.timer
```

Check recent runs:

```bash
sudo journalctl -u meteoedge-rotate-bot-log.service -n 20
```

Verify the dated file was created:

```bash
ls -la /home/p0k5/MeteoEdge/logs/bot.*.log* | head -5
```

Example output:

```
-rw-r--r--  1 p0k5 p0k5      12345 2026-08-11 00:05 /home/p0k5/MeteoEdge/logs/bot.2026-08-11.log
-rw-r--r--  1 p0k5 p0k5       3421 2026-08-10 00:05 /home/p0k5/MeteoEdge/logs/bot.2026-08-10.log.gz
```

Verify `bot.log` is now writable as p0k5:

```bash
ls -la /home/p0k5/MeteoEdge/logs/bot.log
```

Expected: `-rw-r--r--  1 p0k5 p0k5  ...`

### Step 4: Monitor for Errors

Watch the service log during the first few rotations:

```bash
sudo journalctl -u meteoedge -f | grep -i "bot.log\|rotation\|error"
```

If you see `ERROR` or `WARNING`, check:
1. Disk space: `df -h /home/p0k5/MeteoEdge/logs`
2. Permissions: `ls -la /home/p0k5/MeteoEdge/logs`
3. Systemd status: `sudo systemctl status meteoedge`

## Rollback / Disable Rotation

If the rotation causes problems:

```bash
# Disable the timer
sudo systemctl stop meteoedge-rotate-bot-log.timer
sudo systemctl disable meteoedge-rotate-bot-log.timer

# Restart the service (if needed)
sudo systemctl restart meteoedge
```

The existing bot.log will continue to grow, but the service will keep working. A new fix will be deployed if issues arise.

## Troubleshooting

### Issue: "bot.log is still root:root after rotation"

**Cause:** Ownership fix failed, probably due to permissions.

**Fix:** Manually fix ownership:

```bash
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/bot.*.log
```

Then verify the rotation script has permission to call `os.chown()`. If running as p0k5 (not root), this will fail; adjust the script to run as root or use `sudo chown p0k5:p0k5 $new_log` in the script.

### Issue: "bot.log grows in size after rotation"

**Cause:** Systemd did not reopen the stream (the signal was missed or didn't work).

**Fix:** Manually signal the service:

```bash
sudo systemctl kill -s SIGHUP meteoedge
sleep 1
ls -la /home/p0k5/MeteoEdge/logs/bot.log  # Should be small or empty
```

If still large, check:
- Is `meteoedge.service` actually running? `sudo systemctl status meteoedge`
- Are there other processes writing to bot.log? `lsof | grep bot.log`

### Issue: "Compression fails (disk full)"

**Cause:** Insufficient disk space.

**Fix:** Delete old compressed files manually:

```bash
ls -ltr /home/p0k5/MeteoEdge/logs/bot.*.log.gz | head -10
# Delete the oldest few:
rm /home/p0k5/MeteoEdge/logs/bot.2026-07-*.log.gz
```

Then check if there's a quota or capacity planning issue.

## Integration with #977

The deployment of this rotation is sequenced with #977 (KORD position resolution). Key coordination points:

1. Do **not** rotate the existing 298 MB `bot.log` until KORD positions are closed (#977).
2. Once #977 is done, run the one-time rotation via Step 2A above.
3. Then enable the scheduled timer (Step 2B) for future rotations.

## Testing in Development

To test rotation locally without systemd:

```bash
cd /path/to/MeteoEdge
python3 -c "
from pathlib import Path
from src.utils.log_rotation import rotate_plaintext_log, housekeep_plaintext

# Create a test log
test_log = Path('logs/test_bot.log')
test_log.parent.mkdir(exist_ok=True)
test_log.write_text('test data\n')

# Rotate
rotated = rotate_plaintext_log(test_log)
print(f'Rotated to: {rotated}')
print(f'Original exists: {test_log.exists()}')
print(f'Dated exists: {rotated.exists()}')

# Housekeep
housekeep_plaintext(test_log)
print('Housekeeping done')
"
```

## References

- `src/utils/log_rotation.py`: Implementation of rotation functions
- `src/tests/test_log_rotation.py`: Test suite (41 tests)
- systemd documentation: `man systemd.service` (StandardOutput=append:)
- Issue #978: bot.log grows unbounded
- Issue #977: KORD position resolution (coordinated deployment)
