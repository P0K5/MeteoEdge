#!/usr/bin/env python3
"""Connectivity watchdog -- reboot the host after sustained outbound network loss.

Run every ~2 minutes via systemd timer (meteoedge-connectivity-watchdog.timer).
Checks real external reachability (TCP connect attempts, not just local link
state) against two independent targets. Treats a check as "down" only when
BOTH targets fail. Counts consecutive failures across invocations using a
small JSON state file, and reboots the host once the count reaches
FAILURE_THRESHOLD -- i.e. after a sustained outage, not a brief blip.

Background: issue #1491. The server (p0k5-macmini) had repeated outbound-only
outages (tailscaled/Telegram/cryptoedge all failing, no kernel crash trace)
that required a human to notice and manually restart the box. This closes
that gap for the "network flaps but OS is alive" failure mode. It does NOT
address the separate kernel-hang/Wi-Fi-driver-crash failure mode seen in the
same incident window -- no hardware watchdog is available on this host, so a
userspace process can't catch that case.

See docs/DEPLOY_CONNECTIVITY_WATCHDOG.md for deployment, including the
one-time, narrowly-scoped sudoers entry this script's reboot call requires.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# Ensure we're in the repo root (consistent with scripts/rotate_bot_log.py)
repo_root = Path(__file__).parent.parent
os.chdir(repo_root)
sys.path.insert(0, str(repo_root))

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars)
# ---------------------------------------------------------------------------

# Independent, reliable external targets (anycast DNS resolvers, port 53).
# A check only counts as "down" when ALL of these fail -- any single success
# means the host has real outbound reachability.
TARGETS: list[tuple[str, int]] = [
    ("1.1.1.1", 53),  # Cloudflare
    ("8.8.8.8", 53),  # Google
]

# Per-target connect timeout, seconds.
CHECK_TIMEOUT_SECONDS = float(os.getenv("CONNECTIVITY_WATCHDOG_TIMEOUT_SECONDS", "5"))

# Timer cadence is ~2 minutes (deploy/systemd/meteoedge-connectivity-watchdog.timer).
# 6 consecutive failures => ~12 minutes of sustained, total outbound loss
# before rebooting. Observed DERP-relay blips self-resolve within seconds to
# a couple of minutes, well under this window; outages that matter (the
# ~13 min and multi-day flapping ones in #1491) comfortably exceed it.
FAILURE_THRESHOLD = int(os.getenv("CONNECTIVITY_WATCHDOG_FAILURE_THRESHOLD", "6"))

STATE_FILE = Path(
    os.getenv(
        "CONNECTIVITY_WATCHDOG_STATE_FILE",
        str(repo_root / "data" / "connectivity_watchdog_state.json"),
    )
)

# The actual reboot command. Requires the narrowly-scoped sudoers entry
# documented in docs/DEPLOY_CONNECTIVITY_WATCHDOG.md:
#   p0k5 ALL=(root) NOPASSWD: /sbin/reboot
REBOOT_COMMAND = ["sudo", "/sbin/reboot"]

# Set to "1" to log the reboot decision without ever invoking REBOOT_COMMAND.
# Used for manual verification per the deploy runbook -- must never be set in
# the deployed timer/service unit.
DRY_RUN = os.getenv("CONNECTIVITY_WATCHDOG_DRY_RUN", "0") == "1"


# ---------------------------------------------------------------------------
# Reachability check
# ---------------------------------------------------------------------------

def probe_target(host: str, port: int, timeout: float) -> bool:
    """Attempt a TCP connection to (host, port). True = reachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_connectivity(
    targets: list[tuple[str, int]] | None = None,
    timeout: float | None = None,
    prober=probe_target,
) -> tuple[bool, dict[str, bool]]:
    """Check real external reachability against *targets*.

    Returns (is_up, results): is_up is True if ANY target is reachable
    (i.e. "down" requires ALL targets to fail); results maps "host:port" ->
    bool for per-target logging.
    """
    targets = targets if targets is not None else TARGETS
    timeout = timeout if timeout is not None else CHECK_TIMEOUT_SECONDS

    results: dict[str, bool] = {}
    for host, port in targets:
        results[f"{host}:{port}"] = prober(host, port, timeout)

    is_up = any(results.values())
    return is_up, results


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(state_file: Path = STATE_FILE) -> dict:
    """Load persisted watchdog state. Missing/corrupt state starts fresh."""
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("consecutive_failures"), int):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"consecutive_failures": 0}


def save_state(state: dict, state_file: Path = STATE_FILE) -> None:
    """Persist watchdog state atomically (write-then-rename)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    tmp.replace(state_file)


# ---------------------------------------------------------------------------
# Decision logic (pure -- no I/O, easy to unit test)
# ---------------------------------------------------------------------------

def evaluate(state: dict, is_up: bool, threshold: int = FAILURE_THRESHOLD) -> tuple[dict, bool]:
    """Given prior *state* and the latest check result, return (new_state, should_reboot).

    - A successful check always resets the counter to 0.
    - A failed check increments the counter by 1.
    - Reboot triggers once the counter reaches *threshold*. The counter is
      reset to 0 as part of that same decision so that if the reboot command
      fails, or the outage outlives one reboot cycle, the watchdog doesn't
      reboot-loop on every subsequent 2-minute check -- the next reboot still
      requires a fresh full *threshold* of consecutive failures.
    """
    consecutive_failures = state.get("consecutive_failures", 0)

    if is_up:
        return {"consecutive_failures": 0}, False

    consecutive_failures += 1
    should_reboot = consecutive_failures >= threshold
    new_state = {"consecutive_failures": 0 if should_reboot else consecutive_failures}
    return new_state, should_reboot


# ---------------------------------------------------------------------------
# Reboot action
# ---------------------------------------------------------------------------

def reboot(command: list[str] | None = None, dry_run: bool | None = None) -> None:
    """Invoke the reboot command. Never raises -- failures are logged loudly."""
    command = command if command is not None else REBOOT_COMMAND
    dry_run = dry_run if dry_run is not None else DRY_RUN

    if dry_run:
        print(
            f"[connectivity_watchdog] DRY_RUN set -- would reboot now via "
            f"{' '.join(command)}, skipping actual reboot"
        )
        return

    print(f"[connectivity_watchdog] invoking reboot: {' '.join(command)}")
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"[connectivity_watchdog] ERROR: reboot command FAILED: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_check() -> None:
    # Re-read module globals at call time (rather than relying on bound
    # default args) so tests can monkeypatch STATE_FILE/FAILURE_THRESHOLD.
    state = load_state(STATE_FILE)
    prior_failures = state.get("consecutive_failures", 0)

    is_up, results = check_connectivity()

    if is_up:
        print(f"[connectivity_watchdog] check PASSED {results}")
        if prior_failures:
            print(f"[connectivity_watchdog] resetting consecutive-failure counter (was {prior_failures})")
    else:
        print(f"[connectivity_watchdog] check FAILED (all targets unreachable) {results}", file=sys.stderr)

    new_state, should_reboot = evaluate(state, is_up, FAILURE_THRESHOLD)
    save_state(new_state, STATE_FILE)

    if not is_up:
        print(
            f"[connectivity_watchdog] consecutive_failures={prior_failures + 1} "
            f"(threshold={FAILURE_THRESHOLD})",
            file=sys.stderr,
        )

    if should_reboot:
        print(
            f"[connectivity_watchdog] sustained outage: {prior_failures + 1} consecutive failures "
            f">= threshold {FAILURE_THRESHOLD} -- REBOOTING at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            file=sys.stderr,
        )
        reboot()


def main() -> None:
    try:
        run_check()
    except Exception as e:  # noqa: BLE001 -- must never crash the timer silently
        print(f"[connectivity_watchdog] ERROR: unexpected failure: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
