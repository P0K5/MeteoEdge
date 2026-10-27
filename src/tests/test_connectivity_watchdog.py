"""Tests for scripts/connectivity_watchdog.py.

Covers (per issue #1491):
- "down" requires ALL targets to fail; a single reachable target is "up"
- sustained all-targets-down for N consecutive checks triggers a reboot
- intermittent single-check failures interspersed with successes never
  reboot, and reset the counter on every success
- the state file round-trips across separate process invocations
- a reboot decision resets the in-memory/state counter so a persisting
  outage doesn't reboot-loop on every subsequent check
- reboot() never actually invokes a real reboot in tests
"""
from __future__ import annotations

import json

import pytest

from scripts import connectivity_watchdog as watchdog


# ---------------------------------------------------------------------------
# check_connectivity
# ---------------------------------------------------------------------------

class TestCheckConnectivity:
    def test_up_when_any_target_reachable(self):
        def prober(host, port, timeout):
            return host == "8.8.8.8"  # only the second target is reachable

        is_up, results = watchdog.check_connectivity(
            targets=[("1.1.1.1", 53), ("8.8.8.8", 53)], timeout=1.0, prober=prober
        )
        assert is_up is True
        assert results == {"1.1.1.1:53": False, "8.8.8.8:53": True}

    def test_down_only_when_all_targets_fail(self):
        def prober(host, port, timeout):
            return False

        is_up, results = watchdog.check_connectivity(
            targets=[("1.1.1.1", 53), ("8.8.8.8", 53)], timeout=1.0, prober=prober
        )
        assert is_up is False
        assert results == {"1.1.1.1:53": False, "8.8.8.8:53": False}

    def test_up_when_all_targets_reachable(self):
        def prober(host, port, timeout):
            return True

        is_up, _ = watchdog.check_connectivity(
            targets=[("1.1.1.1", 53), ("8.8.8.8", 53)], timeout=1.0, prober=prober
        )
        assert is_up is True

    def test_probe_target_returns_false_on_connection_error(self):
        # Port 0 / an unroutable-ish address should fail fast and return
        # False rather than raising -- exercises the real probe_target (not
        # mocked), but never touches the actual network targets.
        result = watchdog.probe_target("127.0.0.1", 1, timeout=0.2)
        assert result is False


# ---------------------------------------------------------------------------
# evaluate -- pure decision logic
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_success_resets_counter_from_zero(self):
        new_state, should_reboot = watchdog.evaluate({"consecutive_failures": 0}, is_up=True, threshold=6)
        assert new_state == {"consecutive_failures": 0}
        assert should_reboot is False

    def test_success_resets_counter_after_failures(self):
        new_state, should_reboot = watchdog.evaluate({"consecutive_failures": 4}, is_up=True, threshold=6)
        assert new_state == {"consecutive_failures": 0}
        assert should_reboot is False

    def test_failure_increments_counter(self):
        new_state, should_reboot = watchdog.evaluate({"consecutive_failures": 0}, is_up=False, threshold=6)
        assert new_state == {"consecutive_failures": 1}
        assert should_reboot is False

    def test_intermittent_failures_never_reach_threshold(self):
        """Single-check blips interspersed with successes must never reboot."""
        state = {"consecutive_failures": 0}
        sequence = [False, True, False, True, False, False, True]  # never 6 in a row
        for is_up in sequence:
            state, should_reboot = watchdog.evaluate(state, is_up=is_up, threshold=6)
            assert should_reboot is False
        assert state == {"consecutive_failures": 0}  # last check was a success

    def test_sustained_failure_reaches_threshold_and_reboots(self):
        state = {"consecutive_failures": 0}
        should_reboot = False
        for _ in range(6):
            state, should_reboot = watchdog.evaluate(state, is_up=False, threshold=6)
        assert should_reboot is True
        # Counter resets as part of the reboot decision itself.
        assert state == {"consecutive_failures": 0}

    def test_reboot_does_not_trigger_one_check_early(self):
        state = {"consecutive_failures": 0}
        should_reboot = False
        for _ in range(5):
            state, should_reboot = watchdog.evaluate(state, is_up=False, threshold=6)
        assert should_reboot is False
        assert state == {"consecutive_failures": 5}

    def test_counter_reset_after_reboot_prevents_immediate_re_reboot(self):
        """A persisting outage must not reboot on every single subsequent check."""
        state = {"consecutive_failures": 0}
        for _ in range(6):
            state, should_reboot = watchdog.evaluate(state, is_up=False, threshold=6)
        assert should_reboot is True
        assert state["consecutive_failures"] == 0

        # Outage continues -- next check must NOT reboot again immediately.
        state, should_reboot_again = watchdog.evaluate(state, is_up=False, threshold=6)
        assert should_reboot_again is False
        assert state["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# State persistence -- round trip across invocations
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_load_state_missing_file_starts_fresh(self, tmp_path):
        state_file = tmp_path / "state.json"
        assert not state_file.exists()
        state = watchdog.load_state(state_file)
        assert state == {"consecutive_failures": 0}

    def test_load_state_corrupt_file_starts_fresh(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not json{{{", encoding="utf-8")
        state = watchdog.load_state(state_file)
        assert state == {"consecutive_failures": 0}

    def test_save_then_load_round_trip(self, tmp_path):
        state_file = tmp_path / "subdir" / "state.json"
        watchdog.save_state({"consecutive_failures": 3}, state_file)
        assert state_file.exists()

        loaded = watchdog.load_state(state_file)
        assert loaded == {"consecutive_failures": 3}

    def test_round_trip_across_separate_invocations(self, tmp_path):
        """Simulates two separate timer-driven process runs sharing a state file."""
        state_file = tmp_path / "state.json"

        # "Run 1": a failure.
        state = watchdog.load_state(state_file)
        new_state, should_reboot = watchdog.evaluate(state, is_up=False, threshold=6)
        watchdog.save_state(new_state, state_file)
        assert should_reboot is False

        # "Run 2": a fresh process loads the same state file and sees the count.
        reloaded = watchdog.load_state(state_file)
        assert reloaded == {"consecutive_failures": 1}

        new_state2, should_reboot2 = watchdog.evaluate(reloaded, is_up=False, threshold=6)
        watchdog.save_state(new_state2, state_file)
        assert should_reboot2 is False
        assert watchdog.load_state(state_file) == {"consecutive_failures": 2}

    def test_save_state_overwrites_atomically_no_tmp_left_behind(self, tmp_path):
        state_file = tmp_path / "state.json"
        watchdog.save_state({"consecutive_failures": 1}, state_file)
        watchdog.save_state({"consecutive_failures": 2}, state_file)
        assert json.loads(state_file.read_text()) == {"consecutive_failures": 2}
        assert not state_file.with_suffix(".json.tmp").exists()


# ---------------------------------------------------------------------------
# reboot() -- must never actually invoke a real reboot in tests
# ---------------------------------------------------------------------------

class TestReboot:
    def test_dry_run_never_calls_subprocess(self, monkeypatch):
        calls = []
        monkeypatch.setattr(watchdog.subprocess, "run", lambda *a, **k: calls.append((a, k)))
        watchdog.reboot(command=["sudo", "/sbin/reboot"], dry_run=True)
        assert calls == []

    def test_non_dry_run_invokes_subprocess_with_expected_command(self, monkeypatch):
        calls = []
        monkeypatch.setattr(watchdog.subprocess, "run", lambda *a, **k: calls.append((a, k)))
        watchdog.reboot(command=["sudo", "/sbin/reboot"], dry_run=False)
        assert len(calls) == 1
        (args, kwargs) = calls[0]
        assert args[0] == ["sudo", "/sbin/reboot"]
        assert kwargs.get("check") is True

    def test_reboot_failure_is_logged_not_raised(self, monkeypatch, capsys):
        def raise_oserror(*a, **k):
            raise OSError("sudo: command not found")

        monkeypatch.setattr(watchdog.subprocess, "run", raise_oserror)
        watchdog.reboot(command=["sudo", "/sbin/reboot"], dry_run=False)  # must not raise
        captured = capsys.readouterr()
        assert "reboot command FAILED" in captured.err


# ---------------------------------------------------------------------------
# run_check -- end-to-end wiring with everything mocked
# ---------------------------------------------------------------------------

class TestRunCheckIntegration:
    def test_sustained_outage_triggers_reboot_across_runs(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(watchdog, "STATE_FILE", state_file)
        monkeypatch.setattr(watchdog, "FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(watchdog, "check_connectivity", lambda: (False, {"1.1.1.1:53": False, "8.8.8.8:53": False}))

        reboot_calls = []
        monkeypatch.setattr(watchdog, "reboot", lambda: reboot_calls.append(True))

        for _ in range(2):
            watchdog.run_check()
        assert reboot_calls == []  # not yet at threshold

        watchdog.run_check()
        assert reboot_calls == [True]  # third consecutive failure hits threshold=3

    def test_intermittent_blips_never_reboot_across_runs(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(watchdog, "STATE_FILE", state_file)
        monkeypatch.setattr(watchdog, "FAILURE_THRESHOLD", 3)

        reboot_calls = []
        monkeypatch.setattr(watchdog, "reboot", lambda: reboot_calls.append(True))

        results_sequence = [False, False, True, False, False, True, False]
        for is_up in results_sequence:
            monkeypatch.setattr(watchdog, "check_connectivity", lambda is_up=is_up: (is_up, {}))
            watchdog.run_check()

        assert reboot_calls == []
        assert watchdog.load_state(state_file) == {"consecutive_failures": 1}
