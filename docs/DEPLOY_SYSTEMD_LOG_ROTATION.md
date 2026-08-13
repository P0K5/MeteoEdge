# Systemd Log Rotation Deployment Runbook

## Overview

This document describes how to deploy the systemd log rotation mechanism on the live production host. The rotation uses **copytruncate** (safe for systemd's `StandardOutput=append:` targets) and applies to all eight MeteoEdge service logs.

## Logs Rotated

| Service | Log Path |
|---------|----------|
| `meteoedge.service` | `logs/bot.log` |
| `meteoedge-archive.service` | `logs/archive.log` |
| `meteoedge-capture-forecasts.service` | `logs/capture_forecasts.log` |
| `meteoedge-health-report.service` | `logs/health_report.log` |
| `meteoedge-prob-cap-report.service` | `logs/prob_cap_report.log` |
| `meteoedge-purge-retention.service` | `logs/purge.log` |
| `meteoedge-resolve-outcomes.service` | `logs/resolve_outcomes.log` |
| `meteoedge-settle.service` | `logs/settle.log` |

## Pre-deployment Checklist

- [ ] Code is deployed: `src/utils/log_rotation.py` contains `rotate_plaintext_log()` and `housekeep_plaintext()`
- [ ] Systemd unit files deployed: `deploy/systemd/meteoedge-rotate-logs.{service,timer}`
- [ ] Rotation script deployed: `scripts/rotate_bot_log.py` (now handles all logs)
- [ ] Tests pass: `pytest src/tests/test_log_rotation.py` (tests cover all rotation scenarios)
- [ ] No live KORD positions are open (coordinate with #977)
- [ ] You have `sudo` access on the p0k5 host

## Architecture

### How It Works (Copytruncate Pattern)

1. **Read the current log** from each `logs/*.log` file
2. **Write to a dated file** (e.g., `bot.2026-08-11.log`)
3. **Truncate the original** to empty
4. **Fix ownership** of the dated file to `p0k5:p0k5` (from any previous owner)
5. **Compress/delete old files** based on retention policy

### Why Copytruncate Is Safe for `StandardOutput=append:`

- systemd opens the log file itself at unit start and hands the process file descriptor
- The service process never opens or closes the log file; it just writes to the fd
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
- **One-time ownership fix required**: All logs must be chowned to p0k5:p0k5 before timer starts (see deployment steps)
- **Ownership fix fails loudly**: If chown fails (e.g., file still root:root), the script exits with error
- **Copytruncate write loss**: Writes between copy-start and truncate-end are lost (inherent to copytruncate; window is narrow ~100ms)
- **No service restart needed**: copytruncate is safe with held fd + O_APPEND; timer-driven services reopen naturally on next execution

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

### Step 2: Fix Log Ownership (One-Time)

The rotation script runs as p0k5, so all logs must be owned by p0k5:p0k5 beforehand.

Check current ownership of all logs:

```bash
ls -la /home/p0k5/MeteoEdge/logs/*.log
```

For any log showing `root:root` or other non-p0k5 ownership (e.g., `archive.log`), fix it:

```bash
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/bot.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/archive.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/capture_forecasts.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/health_report.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/prob_cap_report.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/purge.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/resolve_outcomes.log
sudo chown p0k5:p0k5 /home/p0k5/MeteoEdge/logs/settle.log
```

Or, if all logs are in one directory and you want to bulk-fix:

```bash
sudo chown -R p0k5:p0k5 /home/p0k5/MeteoEdge/logs/
```

Verify:

```bash
ls -la /home/p0k5/MeteoEdge/logs/*.log
```

All should show: `-rw-r--r--  1 p0k5 p0k5 ...`

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
sudo journalctl -u meteoedge-rotate-logs.service -n 50 -f
```

### Step 5: Verify the Rotation Worked

Check dated files were created for all logs:

```bash
ls -la /home/p0k5/MeteoEdge/logs/*.log* | head -20
```

Expected output (with varying dates and sizes):

```
-rw-r--r--  1 p0k5 p0k5      12345 2026-08-11 00:05 /home/p0k5/MeteoEdge/logs/bot.2026-08-11.log
-rw-r--r--  1 p0k5 p0k5       3421 2026-08-10 00:05 /home/p0k5/MeteoEdge/logs/bot.2026-08-10.log.gz
-rw-r--r--  1 p0k5 p0k5        567 2026-08-11 00:05 /home/p0k5/MeteoEdge/logs/archive.2026-08-11.log
-rw-r--r--  1 p0k5 p0k5        234 2026-08-11 00:05 /home/p0k5/MeteoEdge/logs/settle.2026-08-11.log
... (similar for other logs)
```

Verify all bare log files still exist and are writable:

```bash
ls -la /home/p0k5/MeteoEdge/logs/*.log
```

Expected: `-rw-r--r--  1 p0k5 p0k5  <size> (recent timestamp)` for each

Verify services are writing to them:

```bash
# Should see recent timestamps across all logs
tail -5 /home/p0k5/MeteoEdge/logs/bot.log
tail -5 /home/p0k5/MeteoEdge/logs/settle.log
tail -5 /home/p0k5/MeteoEdge/logs/archive.log
```

### Step 6: Monitor for Errors

Watch the service log during future rotations:

```bash
sudo journalctl -u meteoedge-rotate-logs.service -f
```

Watch meteoedge's output for any issues:

```bash
sudo journalctl -u meteoedge -f | grep -i "rotation\|error"
```

## Rollback / Disable Rotation

If the rotation causes problems:

```bash
# Disable the timer
sudo systemctl stop meteoedge-rotate-logs.timer
sudo systemctl disable meteoedge-rotate-logs.timer

# Restart affected services if needed
sudo systemctl restart meteoedge
```

The existing log files will continue to grow, but services will keep working. A new fix will be deployed if issues arise.

## Troubleshooting

### Issue: "chown failed: Permission denied"

**Cause**: The one-time ownership fix step (Step 2) was never run, or logs were created as root since by a `sudo` invocation.

**Verify**: Check the current ownership:

```bash
ls -la /home/p0k5/MeteoEdge/logs/*.log
```

If any output shows `root:root`, the fix is needed.

**Fix**: Run the one-time ownership fix:

```bash
sudo chown -R p0k5:p0k5 /home/p0k5/MeteoEdge/logs/
```

Verify:

```bash
ls -la /home/p0k5/MeteoEdge/logs/*.log
```

All should show: `-rw-r--r--  1 p0k5 p0k5 ...`

Then retry the rotation:

```bash
sudo systemctl start meteoedge-rotate-logs.service
```

Do NOT change `User=` in the timer — it must be `p0k5`, not root.

### Issue: "Log file does not exist" or "Log file is empty/small after rotation"

**Cause**: Unexpected error during rotation, or service not writing to log.

**Check**: Look at the service log:

```bash
sudo journalctl -u meteoedge-rotate-logs.service -n 50
```

Also check the specific service's status:

```bash
sudo systemctl status meteoedge-archive
sudo systemctl status meteoedge-settle
# ... check other services as needed
```

If a service is not running, restart it:

```bash
sudo systemctl restart meteoedge-settle
```

Then verify the service is writing to its log:

```bash
tail -f /home/p0k5/MeteoEdge/logs/settle.log
# (watch for new log lines)
```

### Issue: "Rotation runs but logs keep growing"

**Cause**: The rotation may have failed silently, or the truncate didn't work.

**Fix**: Manually check the logs directory:

```bash
du -sh /home/p0k5/MeteoEdge/logs/
ls -lh /home/p0k5/MeteoEdge/logs/*.log
```

If any log is huge, check if rotation is actually running:

```bash
sudo systemctl status meteoedge-rotate-logs.timer
sudo journalctl -u meteoedge-rotate-logs.service -n 10
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
ls -ltr /home/p0k5/MeteoEdge/logs/*.log.gz | head -20
# Delete the oldest few (adjust dates as needed):
sudo rm /home/p0k5/MeteoEdge/logs/bot.2026-07-*.log.gz
sudo rm /home/p0k5/MeteoEdge/logs/settle.2026-07-*.log.gz
# ... etc for other logs
```

Then check if housekeeping is running properly. If rotation was successful but files keep accumulating, you may need to manually verify the retention settings or trigger housekeeping manually.

## Testing in Development

To test rotation locally without systemd:

```bash
cd /path/to/MeteoEdge
python3 -c "
from pathlib import Path
from src.utils.log_rotation import rotate_plaintext_log, housekeep_plaintext

# Create test logs
for log_name in ['test_bot.log', 'test_settle.log', 'test_archive.log']:
    test_log = Path('logs/' + log_name)
    test_log.parent.mkdir(exist_ok=True)
    test_log.write_text('test data\n')

    # Rotate
    rotated = rotate_plaintext_log(test_log)
    print(f'{log_name}: rotated to {rotated.name}')
    print(f'  Original exists: {test_log.exists()}')
    print(f'  Dated exists: {rotated.exists()}')

    # Housekeep
    housekeep_plaintext(test_log)
    print(f'{log_name}: housekeeping done')
"
```

## References

- `src/utils/log_rotation.py`: Implementation (rotate_plaintext_log, housekeep_plaintext)
- `src/tests/test_log_rotation.py`: Test suite
- `scripts/rotate_bot_log.py`: The rotation script (runs daily via timer for all logs)
- `deploy/systemd/meteoedge-rotate-logs.service`: systemd service unit
- `deploy/systemd/meteoedge-rotate-logs.timer`: systemd timer unit
- systemd documentation: `man systemd.service` (StandardOutput=append:)
- Issue #978: bot.log grows unbounded (original rotation implementation)
- Issue #979: bot.log rotation PR (copytruncate mechanism)
- Issue #980: bot.log retention bug fix
- Issue #981: Generalize rotation to other seven logs
