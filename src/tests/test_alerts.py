"""Unit tests for src/monitoring/alerts.py.

All SMTP calls are mocked. Tests verify:
- Each of the 4 triggers fires correctly
- Deduplication blocks resends within 1 hour
- No crash when SMTP credentials are missing
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
    def test_no_crash_when_smtp_user_empty(self, capsys):
        """When SMTP credentials are absent, alerts are logged to stderr."""
        mgr = AlertManager()
        with patch("src.monitoring.alerts.ALERT_SMTP_USER", ""):
            with patch("src.monitoring.alerts.ALERT_SMTP_PASS", ""):
                mgr._send("Test subject", "Test body")
        captured = capsys.readouterr()
        assert "SMTP not configured" in captured.err

    def test_no_crash_when_smtp_pass_empty(self, capsys):
        mgr = AlertManager()
        with patch("src.monitoring.alerts.ALERT_SMTP_USER", "user@example.com"):
            with patch("src.monitoring.alerts.ALERT_SMTP_PASS", ""):
                mgr._send("Test subject", "Test body")
        captured = capsys.readouterr()
        assert "SMTP not configured" in captured.err

    def test_smtp_error_logged_not_raised(self, capsys):
        """An SMTP connection failure must not propagate as an exception."""
        mgr = AlertManager()
        with patch("src.monitoring.alerts.ALERT_SMTP_USER", "user@example.com"):
            with patch("src.monitoring.alerts.ALERT_SMTP_PASS", "pass"):
                with patch("smtplib.SMTP") as mock_smtp:
                    mock_smtp.side_effect = ConnectionRefusedError("connection refused")
                    mgr._send("Test subject", "Test body")  # must not raise
        captured = capsys.readouterr()
        assert "failed to send email" in captured.err


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
