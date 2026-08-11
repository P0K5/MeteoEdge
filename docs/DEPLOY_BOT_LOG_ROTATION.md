# bot.log Rotation Deployment Runbook

## Overview

This document describes how to deploy the bot.log rotation mechanism on the live production host. The rotation uses **copytruncate** (safe for systemd's `StandardOutput=append:` targets).

## Pre-deployment Checklist

- [ ] Code is deployed: `src/utils/log_rotation.py` contains `rotate_plaintext_log()` and `housekeep_plaintext()`
- [ ] Systemd unit files deployed: `deploy/systemd/meteoedge-rotate-logs.{service,timer}`
- [ ] Rotation script deployed: `scripts/rotate_bot_log.py`
- [ ] Tests pass: `pytest src/tests/test_log_rotation.py` (43 tests)
- [ ] No live KORD positions are open (coordinate with #977)
- [ ] You have `sudo` access on the p0k5 host

## Architecture

### How It Works (Copytruncate Pattern)

1. **Read the current log** from `/home/p0k5/MeteoEdge/logs/bot.log`
2. **Write to a dated file** (e.g., `bot.2026-08-11.log`)
3. **Truncate the original** to empty
4. **Fix ownership** of the dated file to `p0k5:p0k5` (from root:root)
5. **Compress/delete old files** based on retention policy

### Why Copytruncate Is Safe for `StandardOutput=append:`

- systemd opens `bot.log` itself at unit start and hands the process file descriptor 1
- The bot never opens or closes `bot.log`; it just writes to fd 1
- When we truncate, systemd's held fd remains valid
- On the next write, systemd's `O_APPEND` flag repositions to EOF (now offset 0), so the next write lands at offset 0, not past a gap
- **No zero-fill corruption** (unlike truncation without O_APPEND)

### Retention Policy

- **Default**: `SNAPSHOT_RETAIN_DAYS = 365` days (matches irreproducible data retention)
- Compression: after 1 day
- Deletion: after 365 days
- Both plaintext and `.gz` files are deleted when aged out

### Key Constraints

- **Timer runs as p0k5** (not root): Avoids security issues (Python script executed from p0k5-writable directory)
- **One-time ownership fix required**: bot.log must be chowned to p0k5:p0k5 before timer starts (documented below)
- **Ownership fix fails loudly**: If chown fails (e.g., file still root:root), the script exits with error
- **Copytruncate write loss**: Writes between copy-start and truncate-end are lost (inherent to copytruncate; window is narrow ~100ms)
- **No service restart needed**: copytruncate is safe with held fd + O_APPEND

## Deployment Steps

### Step 1: Verify Systemd Service Is Running

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

### Step 2: Fix bot.log Ownership (One-Time)

The rotation script runs as p0k5, so bot.log must be owned by p0k5:p0k5 beforehand.

Check current ownership:

```bash
ls -la /home/p0k5/MeteoEdge/logs/bot.log
```

If output shows `root:root`, fix it:

```bash
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/bot.log
```

Verify:

```bash
ls -la /home/p0k5/MeteoEdge/logs/bot.log
```

Should show: `-rw-r--r--  1 p0k5 p0k5 ...`

### Step 3: Install Systemd Units

Copy the unit files (no installation yet):

```bash
cd /home/p0k5/MeteoEdge
sudo cp deploy/systemd/meteoedge-rotate-logs.service /etc/systemd/system/
sudo cp deploy/systemd/meteoedge-rotate-logs.timer /etc/systemd/system/
```

Enable and start the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meteoedge-rotate-logs.timer
sudo systemctl start meteoedge-rotate-logs.timer
```

Verify:

```bash
sudo systemctl status meteoedge-rotate-logs.timer
sudo systemctl list-timers meteoedge-rotate-logs.timer
```

### Step 4: Run Initial Rotation (One-Time, Before Timer)

The timer is scheduled for 00:05 UTC. For immediate testing:

```bash
sudo systemctl start meteoedge-rotate-logs.service
```

Monitor the run:

```bash
sudo journalctl -u meteoedge-rotate-logs.service -n 20 -f
```

### Step 5: Verify the Rotation Worked

Check the dated file was created:

```bash
ls -la /home/p0k5/MeteoEdge/logs/bot.*.log* | head -5
```

Expected output:

```
-rw-r--r--  1 p0k5 p0k5      12345 2026-08-11 00:05 /home/p0k5/MeteoEdge/logs/bot.2026-08-11.log
-rw-r--r--  1 p0k5 p0k5       3421 2026-08-10 00:05 /home/p0k5/MeteoEdge/logs/bot.2026-08-10.log.gz
-rw-r--r--  1 p0k5 p0k5      .... (timestamp varies, now empty)
```

Verify `bot.log` exists and is writable:

```bash
ls -la /home/p0k5/MeteoEdge/logs/bot.log
```

Expected: `-rw-r--r--  1 p0k5 p0k5  <size> (recent timestamp)`

Verify the service is writing to it:

```bash
# Should see recent timestamps
tail -20 /home/p0k5/MeteoEdge/logs/bot.log | head -5
```

### Step 6: Monitor for Errors

Watch the service log during future rotations:

```bash
sudo journalctl -u meteoedge-rotate-logs.service -f
```

Watch meteoedge's output for any issues:

```bash
sudo journalctl -u meteoedge -f | grep -i "bot.log\|rotation\|error"
```

## Rollback / Disable Rotation

If the rotation causes problems:

```bash
# Disable the timer
sudo systemctl stop meteoedge-rotate-logs.timer
sudo systemctl disable meteoedge-rotate-logs.timer

# Restart the service (if needed)
sudo systemctl restart meteoedge
```

The existing `bot.log` will continue to grow, but the service will keep working. A new fix will be deployed if issues arise.

## Troubleshooting

### Issue: "chown failed: Permission denied"

**Cause**: Timer is not running as root, or there's a permission issue.

**Verify**: Check the unit file:

```bash
sudo grep "User=" /etc/systemd/system/meteoedge-rotate-logs.service
```

Should show `User=root`.

**Fix**: Edit the unit file if needed:

```bash
sudo systemctl edit meteoedge-rotate-logs.service
```

Add:

```ini
[Service]
User=root
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl start meteoedge-rotate-logs.service
```

### Issue: "bot.log does not exist after rotation"

**Cause**: The truncate operation may have failed, or there was an unexpected error.

**Check**: Look at the service log:

```bash
sudo journalctl -u meteoedge-rotate-logs.service -n 20
```

Also check meteoedge's status:

```bash
sudo systemctl status meteoedge
```

If meteoedge is not running, start it:

```bash
sudo systemctl start meteoedge
```

### Issue: "Rotation runs but bot.log keeps growing"

**Cause**: The rotation may have failed silently, or the truncate didn't work.

**Fix**: Manually check the logs directory:

```bash
du -sh /home/p0k5/MeteoEdge/logs/
ls -lh /home/p0k5/MeteoEdge/logs/bot.log
```

If bot.log is huge, check if rotation is actually running:

```bash
sudo systemctl status meteoedge-rotate-logs.timer
sudo journalctl -u meteoedge-rotate-logs.service -n 5
```

If the service never ran, the timer may not have fired. Check:

```bash
sudo systemctl list-timers meteoedge-rotate-logs.timer
```

Look at "NEXT" column — should be soon. If "NEXT" is in the past, reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart meteoedge-rotate-logs.timer
```

### Issue: "Disk fills up with .gz files"

**Cause**: Retention policy not being enforced, or retention_days too high.

**Fix**: Manually delete old files:

```bash
ls -ltr /home/p0k5/MeteoEdge/logs/bot.*.log.gz | head -20
# Delete the oldest few:
sudo rm /home/p0k5/MeteoEdge/logs/bot.2026-07-*.log.gz
```

Then check if housekeeping is running. If bot.log is 298 MB and it's all "old" data (from before the fix), you may need to manually rotate it once to clear the backlog.

## Integration with #977

The deployment of this rotation is sequenced with #977 (KORD position resolution). Key coordination points:

1. Do **not** rotate the existing 298 MB `bot.log` until KORD positions are closed (#977).
2. Once #977 is done, install the systemd units and run the initial rotation (Step 3 above).
3. Then the scheduled timer takes over for daily rotations.

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
print(f'Original content: {test_log.read_text()!r}')
print(f'Dated exists: {rotated.exists()}')
print(f'Dated content: {rotated.read_text()!r}')

# Housekeep
housekeep_plaintext(test_log)
print('Housekeeping done')
"
```

## References

- `src/utils/log_rotation.py`: Implementation (rotate_plaintext_log, housekeep_plaintext)
- `src/tests/test_log_rotation.py`: Test suite (43 tests)
- `scripts/rotate_bot_log.py`: The rotation script (runs daily via timer)
- `deploy/systemd/meteoedge-rotate-logs.service`: systemd service unit
- `deploy/systemd/meteoedge-rotate-logs.timer`: systemd timer unit
- systemd documentation: `man systemd.service` (StandardOutput=append:)
- Issue #978: bot.log grows unbounded
- Issue #977: KORD position resolution (deployment blocker)
