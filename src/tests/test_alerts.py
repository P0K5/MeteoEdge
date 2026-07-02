"""Unit tests for src/monitoring/alerts.py.

All SMTP calls are mocked. Tests verify:
- Each of the 4 triggers fires correctly
- Deduplication blocks resends within 1 hour
- No crash when SMTP credentials are missing
- poll_once() integration: previous poll timestamp is passed to alert check
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from src.monitoring.alerts import (
    AlertManager,
    _ALERT_DAILY_WARNING,
    _ALERT_DAILY_STOP,
    _ALERT_WIN_RATE,
    _ALERT_POLL_MISSED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Trigger: daily loss warning
# ---------------------------------------------------------------------------

class TestDailyLossWarning:
    def test_fires_when_pnl_below_minus30(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(-31.0)
        mock_send.assert_called_once()
        subject = mock_send.call_args[0][0]
        assert "WARNING" in subject
        assert "-31.00" in subject

    def test_fires_exactly_at_minus31(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(-31.0)
        mock_send.assert_called_once()

    def test_does_not_fire_at_minus30_exactly(self):
        """Threshold is strictly less than -30."""
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(-30.0)
        mock_send.assert_not_called()

    def test_does_not_fire_for_positive_pnl(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(10.0)
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Trigger: daily loss stop
# ---------------------------------------------------------------------------

class TestDailyLossStop:
    def test_fires_when_pnl_below_minus50(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_stop(-51.0)
        mock_send.assert_called_once()
        subject = mock_send.call_args[0][0]
        assert "STOP" in subject
        assert "-51.00" in subject

    def test_does_not_fire_at_minus50_exactly(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_stop(-50.0)
        mock_send.assert_not_called()

    def test_does_not_fire_above_minus50(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_stop(-49.99)
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Trigger: win rate degradation
# ---------------------------------------------------------------------------

class TestWinRateDegradation:
    def test_fires_when_win_rate_below_50_pct(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_win_rate(0.49)
        mock_send.assert_called_once()
        subject = mock_send.call_args[0][0]
        assert "Win rate" in subject or "win rate" in subject.lower()

    def test_fires_at_zero_win_rate(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_win_rate(0.0)
        mock_send.assert_called_once()

    def test_does_not_fire_at_50_pct_exactly(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_win_rate(0.50)
        mock_send.assert_not_called()

    def test_does_not_fire_above_50_pct(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_win_rate(0.75)
        mock_send.assert_not_called()

    def test_subject_contains_percentage(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_win_rate(0.35)
        subject = mock_send.call_args[0][0]
        assert "35.0%" in subject


# ---------------------------------------------------------------------------
# Trigger: poll loop missed
# ---------------------------------------------------------------------------

class TestPollLoopMissed:
    def test_fires_when_last_poll_over_20_minutes_ago(self):
        mgr = AlertManager()
        old = _utcnow() - timedelta(minutes=25)
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_poll_missed(old)
        mock_send.assert_called_once()
        subject = mock_send.call_args[0][0]
        assert "Poll loop" in subject or "poll loop" in subject.lower()

    def test_does_not_fire_when_poll_is_recent(self):
        mgr = AlertManager()
        recent = _utcnow() - timedelta(minutes=10)
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_poll_missed(recent)
        mock_send.assert_not_called()

    def test_does_not_fire_when_last_poll_is_none(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_poll_missed(None)
        mock_send.assert_not_called()

    def test_handles_naive_datetime(self):
        """Naive datetime (no tzinfo) should be treated as UTC without crashing."""
        mgr = AlertManager()
        naive_old = datetime.utcnow() - timedelta(minutes=30)
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_poll_missed(naive_old)
        mock_send.assert_called_once()

    def test_subject_contains_minutes_ago(self):
        mgr = AlertManager()
        old = _utcnow() - timedelta(minutes=45)
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_poll_missed(old)
        subject = mock_send.call_args[0][0]
        assert "minutes ago" in subject


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_same_alert_not_sent_twice_within_one_hour(self):
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(-35.0)
            mgr._check_daily_warning(-35.0)
        mock_send.assert_called_once()

    def test_alert_resent_after_cooldown_expires(self):
        mgr = AlertManager()
        # Backdate the last-sent time by > 1 hour
        past = _utcnow() - timedelta(hours=2)
        mgr._last_sent[_ALERT_DAILY_WARNING] = past
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(-35.0)
        mock_send.assert_called_once()

    def test_different_alert_types_are_independent(self):
        """Sending a warning alert should not suppress the stop alert."""
        mgr = AlertManager()
        with patch.object(mgr, "_send") as mock_send:
            mgr._check_daily_warning(-35.0)  # fires warning
            mgr._check_daily_stop(-55.0)     # different key — should also fire
        assert mock_send.call_count == 2

    def test_is_suppressed_returns_true_within_window(self):
        mgr = AlertManager()
        mgr._last_sent[_ALERT_WIN_RATE] = _utcnow() - timedelta(minutes=30)
        assert mgr._is_suppressed(_ALERT_WIN_RATE) is True

    def test_is_suppressed_returns_false_after_window(self):
        mgr = AlertManager()
        mgr._last_sent[_ALERT_WIN_RATE] = _utcnow() - timedelta(hours=2)
        assert mgr._is_suppressed(_ALERT_WIN_RATE) is False

    def test_is_suppressed_returns_false_for_unseen_key(self):
        mgr = AlertManager()
        assert mgr._is_suppressed(_ALERT_POLL_MISSED) is False


# ---------------------------------------------------------------------------
# SMTP not configured
# ---------------------------------------------------------------------------

class TestSmtpNotConfigured:
    def test_no_crash_when_smtp_user_empty(self, caplog):
        """When SMTP credentials are absent, alerts are logged via the logging module."""
        import logging
        mgr = AlertManager()
        with caplog.at_level(logging.WARNING, logger="src.monitoring.alerts"):
            with patch("src.monitoring.alerts.ALERT_SMTP_USER", ""):
                with patch("src.monitoring.alerts.ALERT_SMTP_PASS", ""):
                    mgr._send("Test subject", "Test body")
        assert "SMTP not configured" in caplog.text

    def test_no_crash_when_smtp_pass_empty(self, caplog):
        import logging
        mgr = AlertManager()
        with caplog.at_level(logging.WARNING, logger="src.monitoring.alerts"):
            with patch("src.monitoring.alerts.ALERT_SMTP_USER", "user@example.com"):
                with patch("src.monitoring.alerts.ALERT_SMTP_PASS", ""):
                    mgr._send("Test subject", "Test body")
        assert "SMTP not configured" in caplog.text

    def test_smtp_error_logged_not_raised(self, caplog):
        """An SMTP connection failure must not propagate as an exception."""
        import logging
        mgr = AlertManager()
        with caplog.at_level(logging.ERROR, logger="src.monitoring.alerts"):
            with patch("src.monitoring.alerts.ALERT_SMTP_USER", "user@example.com"):
                with patch("src.monitoring.alerts.ALERT_SMTP_PASS", "pass"):
                    with patch("smtplib.SMTP") as mock_smtp:
                        mock_smtp.side_effect = ConnectionRefusedError("connection refused")
                        mgr._send("Test subject", "Test body")  # must not raise
        assert "failed to send email" in caplog.text


# ---------------------------------------------------------------------------
# Full check() integration
# ---------------------------------------------------------------------------

class TestCheckIntegration:
    def test_check_evaluates_all_four_triggers(self):
        mgr = AlertManager()
        old_poll = _utcnow() - timedelta(minutes=30)
        # Conditions that should trigger all four alerts
        with patch.object(mgr, "_send") as mock_send:
            mgr.check(daily_pnl=-55.0, win_rate_20=0.40, last_poll_time=old_poll)
        # daily_warning, daily_stop, win_rate, poll_missed = 4 calls
        assert mock_send.call_count == 4

    def test_check_fires_no_alerts_when_all_clear(self):
        mgr = AlertManager()
        recent_poll = _utcnow() - timedelta(minutes=5)
        with patch.object(mgr, "_send") as mock_send:
            mgr.check(daily_pnl=10.0, win_rate_20=0.70, last_poll_time=recent_poll)
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# poll_once integration: correct timestamp is passed to alert check
# ---------------------------------------------------------------------------

class TestPollOnceAlertIntegration:
    """Verify that poll_once() passes the PREVIOUS poll's timestamp to
    alert_manager.check(), not datetime.now().

    A bug existed where last_poll_time was set to datetime.now(timezone.utc)
    after every poll, making the poll-missed threshold unreachable.

    These tests mock unavailable heavy dependencies (dateutil, pytz, etc.) at
    the sys.modules level so that src.scripts.run can be imported in the test
    environment.
    """

    # Heavy modules that run.py pulls in transitively and that are not installed
    # in the test environment.
    _STUB_MODULES = [
        "dateutil", "dateutil.parser",
        "pytz",
        "src.data.metar", "src.data.nws", "src.data.open_meteo",
        "src.data.polymarket",
        "src.model.envelope",
        "src.risk.manager",
        "src.strategy.scanner",
        "src.config",
    ]

    @classmethod
    def _stub_sys_modules(cls):
        """Return a dict of module-name -> MagicMock stubs for missing imports."""
        stubs = {}
        for name in cls._STUB_MODULES:
            stubs[name] = MagicMock()
        # src.config needs specific constants used at module import time in run.py
        cfg = stubs["src.config"]
        cfg.STATIONS = []
        cfg.STATION_TZ = {}
        cfg.POLL_INTERVAL_SECONDS = 300
        cfg.LOG_DIR = MagicMock()
        cfg.CANDIDATES_CSV = MagicMock()
        cfg.SNAPSHOTS_JSONL = MagicMock()
        cfg.LIVE_TRADES_JSONL = MagicMock()
        cfg.RISK_DAILY_LOSS_LIMIT_EUR = 50.0
        cfg.RISK_MAX_OPEN_POSITIONS = 3
        cfg.RISK_DRAWDOWN_STOP_PCT = 0.20
        cfg.RISK_MIN_LIQUIDITY = 50
        cfg.STARTING_CAPITAL_EUR = 500.0
        cfg.POSITION_SIZE_EUR = 20.0
        cfg.POSITION_SIZE_WITH_FEES = 21.0
        # dateutil.parser.parse must return a real datetime so the alert threshold
        # comparison (minutes_ago > 20) works correctly.
        def _parse_iso(s, **kwargs):
            return datetime.fromisoformat(s)
        stubs["dateutil.parser"] = MagicMock()
        stubs["dateutil.parser"].parse = _parse_iso
        stubs["dateutil"] = MagicMock()
        stubs["dateutil"].parser = stubs["dateutil.parser"]
        return stubs

    def _make_mock_risk_manager(self):
        rm = MagicMock()
        rm._daily_pnl = 0.0
        rm.allow_trade.return_value = (False, "test")
        return rm

    def _import_run_with_stubs(self, stubs):
        """Import (or re-import) src.scripts.run with stub modules in place.

        Returns (run_module, original_modules) so the caller can hold stubs
        active during execution and restore them afterwards.
        """
        import sys
        # Save the ORIGINAL run module object so _restore_stubs can put the very
        # same object back. Other test modules (e.g. test_balance_check) import
        # `src.scripts.run` at collection time and call its `poll_once`; if we
        # replaced it with a freshly re-imported object, their patches (which
        # target sys.modules["src.scripts.run"]) would no longer affect the
        # poll_once they actually call, silently re-enabling real network I/O.
        original = {"src.scripts.run": sys.modules.get("src.scripts.run")}
        # Remove any cached version of the module so it re-executes with stubs
        sys.modules.pop("src.scripts.run", None)
        for name, stub in stubs.items():
            original[name] = sys.modules.get(name)
            sys.modules[name] = stub
        import src.scripts.run as run_module
        return run_module, original

    @staticmethod
    def _restore_stubs(original):
        """Restore sys.modules to the exact objects present before stubbing.

        Critically, this puts the ORIGINAL src.scripts.run object back (saved in
        _import_run_with_stubs) rather than re-importing a fresh one, so module
        identity is preserved for any other test that captured a reference to it.
        """
        import sys
        for name, orig in original.items():
            if orig is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig
        # Keep the package attribute (src.scripts.run) in sync with sys.modules.
        restored_run = original.get("src.scripts.run")
        if restored_run is not None:
            import src.scripts
            src.scripts.run = restored_run

    def test_poll_missed_alert_fires_when_previous_poll_was_old(self):
        """If the previous poll ran >20 min ago, the alert must fire."""
        import src.monitoring.dashboard as dashboard_module

        # Simulate a previous poll that happened 25 minutes ago
        old_ts = (_utcnow() - timedelta(minutes=25)).isoformat()

        stubs = self._stub_sys_modules()
        # Keep stubs active during the call so lazy imports inside poll_once resolve correctly
        run_module, original = self._import_run_with_stubs(stubs)
        try:
            alert_mgr = AlertManager()
            risk_mgr = self._make_mock_risk_manager()

            with patch.object(dashboard_module, "last_poll_ts", old_ts):
                with patch.object(run_module, "_build_weather", return_value={"KORD": MagicMock()}), \
                        patch.object(run_module, "build_weather_low_for_scanning", return_value={}):
                    with patch.object(run_module, "get_weather_markets", return_value=[]):
                        with patch.object(run_module, "scan_markets", return_value=([], [])):
                            with patch.object(dashboard_module, "_load_trades", return_value=[]):
                                with patch.object(dashboard_module, "_compute_win_rate", return_value=0.7):
                                    with patch.object(alert_mgr, "_send") as mock_send:
                                        run_module.poll_once(risk_mgr, live_trader=None, alert_manager=alert_mgr)
        finally:
            self._restore_stubs(original)

        # The poll-missed alert should have fired because prev_poll_ts was 25 min ago
        subjects = [c[0][0] for c in mock_send.call_args_list]
        assert any("poll" in s.lower() for s in subjects), (
            f"Expected poll-missed alert to fire, but _send was called with: {subjects}"
        )

    def test_poll_missed_alert_does_not_fire_when_previous_poll_was_recent(self):
        """If the previous poll ran <20 min ago, the poll-missed alert must not fire."""
        import src.monitoring.dashboard as dashboard_module

        # Simulate a previous poll that happened 5 minutes ago
        recent_ts = (_utcnow() - timedelta(minutes=5)).isoformat()

        stubs = self._stub_sys_modules()
        run_module, original = self._import_run_with_stubs(stubs)
        try:
            alert_mgr = AlertManager()
            risk_mgr = self._make_mock_risk_manager()

            with patch.object(dashboard_module, "last_poll_ts", recent_ts):
                with patch.object(run_module, "_build_weather", return_value={"KORD": MagicMock()}), \
                        patch.object(run_module, "build_weather_low_for_scanning", return_value={}):
                    with patch.object(run_module, "get_weather_markets", return_value=[]):
                        with patch.object(run_module, "scan_markets", return_value=([], [])):
                            with patch.object(dashboard_module, "_load_trades", return_value=[]):
                                with patch.object(dashboard_module, "_compute_win_rate", return_value=0.7):
                                    with patch.object(alert_mgr, "_send") as mock_send:
                                        run_module.poll_once(risk_mgr, live_trader=None, alert_manager=alert_mgr)
        finally:
            self._restore_stubs(original)

        subjects = [c[0][0] for c in mock_send.call_args_list]
        assert not any("poll" in s.lower() for s in subjects), (
            f"Expected no poll-missed alert, but _send was called with: {subjects}"
        )

    def test_no_poll_missed_alert_on_first_poll(self):
        """On the very first poll (last_poll_ts is None), the alert must not fire."""
        import src.monitoring.dashboard as dashboard_module

        stubs = self._stub_sys_modules()
        run_module, original = self._import_run_with_stubs(stubs)
        try:
            alert_mgr = AlertManager()
            risk_mgr = self._make_mock_risk_manager()

            with patch.object(dashboard_module, "last_poll_ts", None):
                with patch.object(run_module, "_build_weather", return_value={"KORD": MagicMock()}), \
                        patch.object(run_module, "build_weather_low_for_scanning", return_value={}):
                    with patch.object(run_module, "get_weather_markets", return_value=[]):
                        with patch.object(run_module, "scan_markets", return_value=([], [])):
                            with patch.object(dashboard_module, "_load_trades", return_value=[]):
                                with patch.object(dashboard_module, "_compute_win_rate", return_value=0.7):
                                    with patch.object(alert_mgr, "_send") as mock_send:
                                        run_module.poll_once(risk_mgr, live_trader=None, alert_manager=alert_mgr)
        finally:
            self._restore_stubs(original)

        subjects = [c[0][0] for c in mock_send.call_args_list]
        assert not any("poll" in s.lower() for s in subjects), (
            f"Expected no poll-missed alert on first poll, but _send was called with: {subjects}"
        )
