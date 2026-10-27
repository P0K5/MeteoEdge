# Connectivity Watchdog Deployment Runbook

## Overview

This document describes how to deploy the connectivity watchdog on the live
production host (`p0k5-macmini`). The watchdog checks real external
reachability every ~2 minutes and reboots the host after a sustained (not
transient) total loss of outbound connectivity.

Background: issue #1491. The host had repeated outbound-only outages
(`tailscaled` unable to reach its DERP relay, Telegram `deleteWebhook` calls
timing out, `cryptoedge.service` crash-looping on network errors) with no
kernel crash trace -- the OS stayed up, it just lost the ability to reach the
outside world, and needed a human to notice and manually restart it. This
watchdog closes that gap.

**Out of scope**: the separate kernel-hang/Wi-Fi-driver-crash failure mode
seen in the same incident window. No hardware watchdog device exists on this
host (no `/dev/watchdog`, no `iTCO_wdt`), so a userspace process cannot catch
a hard freeze -- only the OS-alive-but-unreachable failure mode is addressed
here.

## How It Works

1. `scripts/connectivity_watchdog.py` runs every ~2 minutes via
   `meteoedge-connectivity-watchdog.timer`.
2. Each run attempts a TCP connection to two independent, reliable targets
   (`1.1.1.1:53` and `8.8.8.8:53`), each with a 5s timeout. The check counts
   as "down" only if **both** targets fail -- a single reachable target means
   the host is up.
3. Consecutive failures are counted across runs using a small JSON state
   file (`data/connectivity_watchdog_state.json`). Any successful check
   resets the counter to 0 immediately.
4. Once the counter reaches `CONNECTIVITY_WATCHDOG_FAILURE_THRESHOLD`
   (default 6, i.e. ~12 minutes of total, sustained outbound loss), the
   watchdog reboots the host via `sudo /sbin/reboot` and resets its own
   counter so a reboot that doesn't clear the outage does not reboot-loop
   every 2 minutes -- the next reboot still requires a fresh full threshold
   of consecutive failures.
5. Every check (pass/fail, consecutive-failure count) and every reboot
   decision is logged to the journal, so "why did it reboot at 03:14" is
   answerable from `journalctl` alone.

Transient blips (the DERP-relay errors observed in #1491 self-resolved within
seconds to a couple of minutes) never reach the threshold and never trigger a
reboot.

## Pre-deployment Checklist

- [ ] Code is deployed: `scripts/connectivity_watchdog.py`
- [ ] Systemd unit files deployed:
      `deploy/systemd/meteoedge-connectivity-watchdog.{service,timer}`
- [ ] Tests pass: `pytest src/tests/test_connectivity_watchdog.py`
- [ ] You have `sudo` access on the `p0k5` host
- [ ] The one-time sudoers step below has NOT already been applied by this
      runbook (check `sudo -l -U p0k5` first if unsure)

## Step 1: One-Time Sudoers Entry (Manual -- Not Scripted)

The watchdog's own process runs unprivileged as `p0k5` (same as every other
timer-driven script in this repo); only the reboot call itself needs root.
Do **not** grant broad sudo to `p0k5` for this. Add a sudoers entry that
permits passwordless execution of exactly the reboot command and nothing
else.

This step is deliberately a manual, one-time action -- it is never applied
automatically by the service itself, so a compromised or buggy script can
never grant itself more privilege than this file exists to prevent.

```bash
sudo visudo -f /etc/sudoers.d/meteoedge-connectivity-watchdog
```

Add exactly this line (no wildcards beyond the single binary path):

```
p0k5 ALL=(root) NOPASSWD: /sbin/reboot
```

Save and verify the file is syntactically valid (`visudo -f` already checks
this on save) and has safe permissions:

```bash
sudo chmod 0440 /etc/sudoers.d/meteoedge-connectivity-watchdog
sudo -l -U p0k5
```

Expected output includes:

```
(root) NOPASSWD: /sbin/reboot
```

Confirm `p0k5` can invoke exactly this command without a password, and
nothing else requires one:

```bash
sudo -u p0k5 sudo -n /sbin/reboot --help   # should run without prompting
sudo -u p0k5 sudo -n /sbin/shutdown -r now # should FAIL (not permitted)
```

## Step 2: Install Systemd Units

```bash
cd /home/p0k5/MeteoEdge
sudo cp deploy/systemd/meteoedge-connectivity-watchdog.service /etc/systemd/system/
sudo cp deploy/systemd/meteoedge-connectivity-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meteoedge-connectivity-watchdog.timer
```

Verify:

```bash
sudo systemctl status meteoedge-connectivity-watchdog.timer
sudo systemctl list-timers meteoedge-connectivity-watchdog.timer
```

## Step 3: Run an Initial Check (One-Time, Before Waiting for the Timer)

```bash
sudo systemctl start meteoedge-connectivity-watchdog.service
sudo journalctl -u meteoedge-connectivity-watchdog.service -n 20
```

Expected output on a healthy host:

```
[connectivity_watchdog] check PASSED {'1.1.1.1:53': True, '8.8.8.8:53': True}
```

Confirm the state file was created:

```bash
cat /home/p0k5/MeteoEdge/data/connectivity_watchdog_state.json
# {"consecutive_failures": 0}
```

## Step 4: Monitor

```bash
sudo journalctl -u meteoedge-connectivity-watchdog.service -f
```

Over the following checks you should see a `check PASSED` line roughly every
2 minutes and nothing else, on a healthy connection.

## Manual Verification of the Reboot Path (Do This Once, Deliberately)

This confirms the watchdog actually detects and acts on a real outage without
waiting for one to happen naturally. **Only do this on a host where an
unplanned reboot is acceptable** -- confirm `no_enabled=1` on all stations and
no open live positions first (this issue does not touch trading config, but a
reboot still interrupts whatever is running).

1. Temporarily lower the threshold for a fast test:
   ```bash
   sudo systemctl stop meteoedge-connectivity-watchdog.timer
   export CONNECTIVITY_WATCHDOG_FAILURE_THRESHOLD=2
   export CONNECTIVITY_WATCHDOG_DRY_RUN=1   # first pass: confirm detection without actually rebooting
   ```
2. Block outbound traffic to both targets:
   ```bash
   sudo iptables -I OUTPUT -d 1.1.1.1 -j DROP
   sudo iptables -I OUTPUT -d 8.8.8.8 -j DROP
   ```
3. Run the script directly a few times (simulating the timer) and confirm
   escalating failure counts and a logged "would reboot" decision:
   ```bash
   cd /home/p0k5/MeteoEdge
   for i in 1 2 3; do .venv/bin/python scripts/connectivity_watchdog.py; sleep 1; done
   ```
4. Remove the DROP rules and confirm the next run PASSES and resets the
   counter:
   ```bash
   sudo iptables -D OUTPUT -d 1.1.1.1 -j DROP
   sudo iptables -D OUTPUT -d 8.8.8.8 -j DROP
   .venv/bin/python scripts/connectivity_watchdog.py
   ```
5. Once detection is confirmed, repeat steps 2-3 with
   `CONNECTIVITY_WATCHDOG_DRY_RUN` unset (or `=0`) to confirm an actual
   reboot fires, then confirm the host comes back up cleanly and the timer
   re-arms:
   ```bash
   sudo systemctl status meteoedge-connectivity-watchdog.timer
   ```
6. Unset the test env vars and re-enable the timer with default settings:
   ```bash
   unset CONNECTIVITY_WATCHDOG_FAILURE_THRESHOLD CONNECTIVITY_WATCHDOG_DRY_RUN
   sudo systemctl start meteoedge-connectivity-watchdog.timer
   ```

## Rollback / Disable

If the watchdog causes problems (e.g. false-positive reboots):

```bash
sudo systemctl stop meteoedge-connectivity-watchdog.timer
sudo systemctl disable meteoedge-connectivity-watchdog.timer
```

The host will simply stop being auto-rebooted on connectivity loss; nothing
else is affected (no trading config, no other services are touched). To fully
remove the reboot capability, also remove the sudoers entry:

```bash
sudo rm /etc/sudoers.d/meteoedge-connectivity-watchdog
```

## Troubleshooting

### Issue: "reboot command FAILED" in the journal

**Cause**: the one-time sudoers step (Step 1) was never applied, or the
sudoers file has a syntax error / wrong permissions.

**Check**:

```bash
sudo -l -U p0k5
sudo visudo -c
```

**Fix**: redo Step 1.

### Issue: watchdog rebooted during a brief, since-resolved blip

**Cause**: `CONNECTIVITY_WATCHDOG_FAILURE_THRESHOLD` was set too low, or the
timer interval was changed without re-tuning the threshold (the threshold is
in units of *checks*, not minutes).

**Check**: `journalctl -u meteoedge-connectivity-watchdog.service` for the
consecutive-failure counts leading up to the reboot.

**Fix**: raise `CONNECTIVITY_WATCHDOG_FAILURE_THRESHOLD` (set in
`/home/p0k5/MeteoEdge/.env`, picked up via `EnvironmentFile=` in the service
unit) and restart the timer.

### Issue: state file shows a stale non-zero `consecutive_failures` after a long-healthy period

**Cause**: should not happen (a single successful check always resets to 0);
if seen, file a bug -- it points at `evaluate()` not being reached on a
passing check.

**Fix**: delete the state file to force a fresh start (safe -- it's just a
counter):

```bash
rm /home/p0k5/MeteoEdge/data/connectivity_watchdog_state.json
```

## References

- `scripts/connectivity_watchdog.py`: the watchdog script
- `src/tests/test_connectivity_watchdog.py`: test suite
- `deploy/systemd/meteoedge-connectivity-watchdog.service`: systemd service unit
- `deploy/systemd/meteoedge-connectivity-watchdog.timer`: systemd timer unit
- Issue #1491: original outage report and requirements
- `docs/DEPLOY_SYSTEMD_LOG_ROTATION.md`: the runbook this document is modeled on
