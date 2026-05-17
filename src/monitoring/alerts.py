"""Email alert system for MeteoEdge.

Fires email alerts via smtplib when risk thresholds are crossed.
All four alert triggers are implemented with per-type deduplication
(one alert per type per hour). SMTP credentials are read from environment
variables; if unconfigured, alerts are logged to stderr instead of crashing.

Configuration (environment variables):
    SMTP_HOST   — SMTP server hostname (default: smtp.gmail.com)
    SMTP_PORT   — SMTP server port (default: 587)
    SMTP_USER   — SMTP username / sender address
    SMTP_PASS   — SMTP password or app-specific password

Usage in run.py:
    from src.monitoring.alerts import AlertManager
    alert_manager = AlertManager()
    # After each poll cycle:
    alert_manager.check(
        daily_pnl=risk_manager._daily_pnl,
        win_rate_20=computed_win_rate_20,
        last_poll_time=last_poll_datetime,
    )

Standalone test (sends a test email when run directly):
    python -m src.monitoring.alerts
"""
from __future__ import annotations

import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional

# ---------------------------------------------------------------------------
# Email configuration
# ---------------------------------------------------------------------------

ALERT_EMAIL_TO = "andre.freixo.santos@gmail.com"
ALERT_SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
ALERT_SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
ALERT_SMTP_USER = os.getenv("SMTP_USER", "")
ALERT_SMTP_PASS = os.getenv("SMTP_PASS", "")

# Alert type keys used for deduplication
_ALERT_DAILY_WARNING = "daily_loss_warning"
_ALERT_DAILY_STOP = "daily_loss_stop"
_ALERT_WIN_RATE = "win_rate_degradation"
_ALERT_POLL_MISSED = "poll_loop_missed"

# Deduplication cooldown: do not resend the same alert within this window.
_DEDUP_HOURS = 1


# ---------------------------------------------------------------------------
# AlertManager
# ---------------------------------------------------------------------------

class AlertManager:
    """Fires and deduplicates email alerts for MeteoEdge risk events.

    All state is in-memory; deduplication resets on process restart.
    """

    def __init__(self) -> None:
        # Maps alert_key -> last sent UTC datetime
        self._last_sent: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check(
        self,
        daily_pnl: float,
        win_rate_20: float,
        last_poll_time: Optional[datetime],
    ) -> None:
        """Evaluate all alert conditions and fire emails where needed.

        Args:
            daily_pnl: Cumulative PnL for today in EUR (negative = loss).
            win_rate_20: Win rate over the last 20 trades as a float 0.0–1.0.
            last_poll_time: UTC datetime of the most recent completed poll,
                or None if no poll has completed yet.
        """
        self._check_daily_warning(daily_pnl)
        self._check_daily_stop(daily_pnl)
        self._check_win_rate(win_rate_20)
        self._check_poll_missed(last_poll_time)

    # ------------------------------------------------------------------
    # Alert conditions
    # ------------------------------------------------------------------

    def _check_daily_warning(self, daily_pnl: float) -> None:
        """Fire when daily PnL drops below -€30."""
        if daily_pnl < -30.0:
            subject = f"WARNING: Daily loss approaching limit (€{daily_pnl:.2f} today)"
            body = (
                f"MeteoEdge daily PnL is €{daily_pnl:.2f}.\n"
                "Loss is approaching the daily limit of -€50. Review open positions."
            )
            self._fire(_ALERT_DAILY_WARNING, subject, body)

    def _check_daily_stop(self, daily_pnl: float) -> None:
        """Fire when daily PnL drops below -€50 (trading halt threshold)."""
        if daily_pnl < -50.0:
            subject = f"STOP: Daily loss limit hit (€{daily_pnl:.2f} today) — trading halted"
            body = (
                f"MeteoEdge daily PnL is €{daily_pnl:.2f}.\n"
                "The daily loss limit of -€50 has been breached. The risk manager "
                "will block all new trades until midnight UTC."
            )
            self._fire(_ALERT_DAILY_STOP, subject, body)

    def _check_win_rate(self, win_rate_20: float) -> None:
        """Fire when win rate over last 20 trades falls below 50%."""
        if win_rate_20 < 0.50:
            pct = win_rate_20 * 100
            subject = f"ALERT: Win rate degraded to {pct:.1f}% over last 20 trades"
            body = (
                f"MeteoEdge win rate over the last 20 trades is {pct:.1f}%.\n"
                "This is below the 50% warning threshold. Review the prediction model."
            )
            self._fire(_ALERT_WIN_RATE, subject, body)

    def _check_poll_missed(self, last_poll_time: Optional[datetime]) -> None:
        """Fire when the last poll was more than 20 minutes ago."""
        if last_poll_time is None:
            return
        now = datetime.now(timezone.utc)
        # Ensure timezone-aware comparison
        if last_poll_time.tzinfo is None:
            last_poll_time = last_poll_time.replace(tzinfo=timezone.utc)
        minutes_ago = (now - last_poll_time).total_seconds() / 60
        if minutes_ago > 20:
            subject = f"ALERT: Poll loop may be down — last poll was {minutes_ago:.0f} minutes ago"
            body = (
                f"MeteoEdge has not completed a poll in {minutes_ago:.0f} minutes.\n"
                f"Last successful poll: {last_poll_time.isoformat()}\n"
                "Check that the polling process is still running."
            )
            self._fire(_ALERT_POLL_MISSED, subject, body)

    # ------------------------------------------------------------------
    # Delivery and deduplication
    # ------------------------------------------------------------------

    def _fire(self, alert_key: str, subject: str, body: str) -> None:
        """Send an alert if it has not been sent within the dedup window."""
        if self._is_suppressed(alert_key):
            return
        self._send(subject, body)
        self._last_sent[alert_key] = datetime.now(timezone.utc)

    def _is_suppressed(self, alert_key: str) -> bool:
        """Return True if the alert was already sent within the last hour."""
        last = self._last_sent.get(alert_key)
        if last is None:
            return False
        elapsed = datetime.now(timezone.utc) - last
        return elapsed < timedelta(hours=_DEDUP_HOURS)

    def _send(self, subject: str, body: str) -> None:
        """Deliver an email via SMTP, or log to stderr if SMTP is not configured."""
        if not ALERT_SMTP_USER or not ALERT_SMTP_PASS:
            print(
                f"[alerts] SMTP not configured — alert suppressed to stderr:\n"
                f"  Subject: {subject}",
                file=sys.stderr,
            )
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = ALERT_SMTP_USER
        msg["To"] = ALERT_EMAIL_TO
        msg.set_content(body)

        try:
            with smtplib.SMTP(ALERT_SMTP_HOST, ALERT_SMTP_PORT, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(ALERT_SMTP_USER, ALERT_SMTP_PASS)
                smtp.send_message(msg)
            print(f"[alerts] sent: {subject}")
        except Exception as e:
            print(
                f"[alerts] failed to send email: {e}\n"
                f"  Subject: {subject}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

def test_alert() -> None:
    """Send a test email to verify SMTP configuration.

    Run with: python -m src.monitoring.alerts
    """
    manager = AlertManager()
    subject = "MeteoEdge test alert"
    body = (
        "This is a test alert from MeteoEdge.\n"
        "If you received this, SMTP is configured correctly."
    )
    manager._send(subject, body)
    print("[alerts] test_alert() completed")


if __name__ == "__main__":
    test_alert()
