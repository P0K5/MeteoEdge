"""Unit tests for src/scripts/daily_health_report.py.

All DB queries and SMTP calls are mocked. Tests verify:
- build_report() produces output with all section headers
- Each section builder handles empty/missing data gracefully
- _build_verdict() returns the correct status for various conditions
- Formatting helpers (_fmt_pnl, _fmt_pct) work correctly
- _send_email() handles missing SMTP credentials
- --dry-run flag prints to stdout without sending email
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable
_HERE = Path(__file__).resolve().parent
if str(_HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[1]))

from src.scripts.daily_health_report import (  # noqa: E402
    _build_bot_health,
    _build_header,
    _build_m3_progress,
    _build_trading,
    _build_verdict,
    _fmt_pnl,
    _fmt_pct,
    _send_email,
    build_report,
    main,
)


# ---------------------------------------------------------------------------
# Mock Database helper
# ---------------------------------------------------------------------------

def _mock_db(rows_by_query: "dict[str, tuple] | None" = None) -> MagicMock:
    """Return a MagicMock Database whose _conn.execute() returns canned results.

    Keys in rows_by_query are SQL prefixes (first word is enough to match).
    Values are tuples that become fetchone() results.
    Multi-row queries: use _mock_db_multi() instead.
    """
    db = MagicMock()
    rows = rows_by_query or {}

    def _execute(sql, params=None):
        mock = MagicMock()
        sql_stripped = sql.strip()
        for prefix, result in rows.items():
            if sql_stripped.startswith(prefix):
                if isinstance(result, list):
                    # List of tuples → fetchall returns them, fetchone returns first
                    mock.fetchall.return_value = result
                    mock.fetchone.return_value = result[0] if result else (None,)
                else:
                    # Single tuple value
                    mock.fetchone.return_value = result
                    mock.fetchall.return_value = [result]
                return mock
        # Default: empty
        mock.fetchone.return_value = (None,)
        mock.fetchall.return_value = []
        return mock

    db._conn.execute = _execute
    db._conn.executescript = MagicMock()

    # get_emos_crps_count returns int
    db.get_emos_crps_count = MagicMock(return_value=5)
    return db


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

class TestFmtPnl:
    def test_positive(self):
        assert _fmt_pnl(12.34) == "+EUR12.34"

    def test_negative(self):
        assert _fmt_pnl(-5.00) == "-EUR5.00"

    def test_zero(self):
        assert _fmt_pnl(0.0) == "+EUR0.00"

    def test_none(self):
        assert _fmt_pnl(None) == "N/A"


class TestFmtPct:
    def test_half(self):
        assert _fmt_pct(0.5) == "50.0%"

    def test_quarter(self):
        assert _fmt_pct(0.255, 1) == "25.5%"

    def test_none(self):
        assert _fmt_pct(None) == "N/A"


# ---------------------------------------------------------------------------
# _build_header
# ---------------------------------------------------------------------------

class TestBuildHeader:
    def test_contains_date_and_separator(self):
        today = datetime(2026, 7, 30, 14, 0, 0, tzinfo=timezone.utc)
        result = _build_header(today)
        text = "\n".join(result)
        assert "2026-07-30" in text
        assert "MeteoEdge Daily Health" in text

    def test_returns_list_of_strings(self):
        result = _build_header(datetime.now(timezone.utc))
        assert isinstance(result, list)
        assert all(isinstance(line, str) for line in result)


# ---------------------------------------------------------------------------
# _build_bot_health (issue #914)
# ---------------------------------------------------------------------------

class TestBuildBotHealth:
    def test_returns_tuple_of_lines_and_flags(self):
        db = _mock_db()
        lines, flags = _build_bot_health(db, datetime.now(timezone.utc))
        assert isinstance(lines, list)
        assert isinstance(flags, dict)

    def test_poll_count_reads_poll_runs_not_scan_decisions(self):
        """Regression test for defect 1: scan_decisions is written only when
        brackets are evaluated (a handful of times a day), while the bot
        polls continuously (~every 5.5 min). Using scan_decisions produces a
        permanent false "5/288" alarm even though the bot is polling fine.
        This test fails against the pre-fix code, which counts
        `scan_decisions` and would report 5/288 here instead of 250/288.
        """
        import sqlite3
        from datetime import timedelta

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE poll_runs (id INTEGER PRIMARY KEY, poll_ts TEXT, mode TEXT)")
        conn.execute("CREATE TABLE scan_decisions (poll_ts TEXT)")

        today = datetime(2026, 7, 30, 14, 0, 0, tzinfo=timezone.utc)
        # 250 poll heartbeats in the last 24h (bot polling normally)...
        for i in range(250):
            ts = (today - timedelta(minutes=5 * i)).isoformat()
            conn.execute("INSERT INTO poll_runs(poll_ts, mode) VALUES (?, 'live')", (ts,))
        # ...but only 5 scan_decisions rows (brackets rarely evaluated).
        for i in range(5):
            ts = (today - timedelta(hours=4 * i)).isoformat()
            conn.execute("INSERT INTO scan_decisions(poll_ts) VALUES (?)", (ts,))
        conn.commit()

        db = MagicMock()
        db._conn = conn

        lines, flags = _build_bot_health(db, today)
        text = "\n".join(lines)

        assert "Polls 24h:   250/288" in text
        assert "5/288" not in text
        assert flags["poll_count_24h"] == 250
        assert "Evaluated (scan_decisions) 24h: 5" in text
        conn.close()

    def test_status_warn_on_poll_count_shortfall_even_if_last_poll_recent(self):
        """Regression test for defect 2: a bot that polled only a handful of
        times in 24h but happened to poll 3 minutes ago must not be reported
        [OK] Healthy -- the 24h shortfall must fold into the status."""
        import sqlite3
        from datetime import timedelta

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE poll_runs (id INTEGER PRIMARY KEY, poll_ts TEXT, mode TEXT)")
        conn.execute("CREATE TABLE scan_decisions (poll_ts TEXT)")

        today = datetime(2026, 7, 30, 14, 0, 0, tzinfo=timezone.utc)
        # Only 5 polls in 24h, but the most recent one is 3 minutes ago.
        poll_times = [
            today - timedelta(minutes=3),
            today - timedelta(hours=5),
            today - timedelta(hours=10),
            today - timedelta(hours=15),
            today - timedelta(hours=20),
        ]
        for ts in poll_times:
            conn.execute("INSERT INTO poll_runs(poll_ts, mode) VALUES (?, 'live')", (ts.isoformat(),))
        conn.commit()

        db = MagicMock()
        db._conn = conn

        lines, flags = _build_bot_health(db, today)
        text = "\n".join(lines)

        assert flags["status"] != "OK"
        assert "[OK] Healthy" not in text
        conn.close()

    def test_status_ok_when_poll_count_and_gap_both_healthy(self):
        import sqlite3
        from datetime import timedelta

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE poll_runs (id INTEGER PRIMARY KEY, poll_ts TEXT, mode TEXT)")
        conn.execute("CREATE TABLE scan_decisions (poll_ts TEXT)")

        # Use the real current time (not a fixed historical date) so the
        # most recent poll is genuinely "recent" relative to _utc_now().
        today = datetime.now(timezone.utc)
        for i in range(280):
            ts = (today - timedelta(minutes=5 * i)).isoformat()
            conn.execute("INSERT INTO poll_runs(poll_ts, mode) VALUES (?, 'live')", (ts,))
        conn.commit()

        db = MagicMock()
        db._conn = conn

        lines, flags = _build_bot_health(db, today)
        text = "\n".join(lines)

        assert flags["status"] == "OK"
        assert "[OK] Healthy" in text
        conn.close()


# ---------------------------------------------------------------------------
# _build_trading win-rate label (issue #914 defect 4)
# ---------------------------------------------------------------------------

class TestBuildTradingWinRateLabel:
    def test_win_rate_labeled_all_time_not_24h(self):
        """Regression test for defect 4: the win-rate query has no time
        filter (last N settled live trades all-time), but was previously
        labeled in a way that read as a 24h figure under the "last 24h UTC"
        heading. Fails against the pre-fix label "Win rate (20 settled):".
        """
        db = _mock_db({
            "SELECT COUNT(*), COALESCE(SUM(pnl)": (0, 0.0),
            "SELECT COUNT(*), COALESCE(SUM(shares * entry_price": (0, 0.0),
            "SELECT pnl FROM trades": [(1.0,)] * 12 + [(-1.0,)] * 8,
            "SELECT daily_pnl FROM risk_state": (None,),
        })
        result = _build_trading(db, datetime.now(timezone.utc))
        text = "\n".join(result)
        assert "Win rate (last 20 settled, all-time):" in text
        assert "Win rate (20 settled):" not in text


# ---------------------------------------------------------------------------
# _build_verdict
#
# Signature changed under issue #914 (defect 3): _build_verdict now takes
# `sections: list[tuple[name, lines, flags]]` instead of a flat list of
# rendered lines, so conditions are evaluated per-section instead of via
# substring matches over the whole report.
# ---------------------------------------------------------------------------

class TestBuildVerdict:
    def test_healthy_when_no_issues(self):
        sections = [("bot", ["Bot Pulse", "Status: [OK] Healthy"], {"status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[OK] Healthy" in text

    def test_warn_when_artifact_rate_high(self):
        sections = [("m3", ["M3 Progress", "p_yes=0.0 artifact:     17.5% [WARN]"], {})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "artifact rate high" in text

    def test_crit_when_bot_stale(self):
        sections = [
            ("bot", ["Bot Pulse", "Status: [STALE] 90 min since last poll"],
             {"status": "STALE", "detail": "90 min since last poll"}),
        ]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[CRIT]" in text
        assert "bot stale" in text

    def test_warn_for_kord_gap(self):
        sections = [("blockers", ["Open Blockers", "KORD GEFS capture gap -- [WARN] gap confirmed"], {})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "#897 KORD gap" in text

    def test_warn_for_no_gefs_data(self):
        sections = [("blockers", ["Open Blockers", "NO GEFS DATA"], {})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "#885 no GEFS data" in text

    def test_multiple_warnings_demoted(self):
        """Multiple WARN-level issues stay at WARN (not CRIT unless there's a CRIT trigger)."""
        sections = [
            ("m3", ["M3 Progress", "p_yes=0.0 artifact:     17.5% [WARN]"], {}),
            ("blockers", [
                "Open Blockers",
                "KORD GEFS capture gap -- [WARN] gap confirmed",
                "NO GEFS DATA",
            ], {}),
        ]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "artifact rate high" in text
        assert "#897 KORD gap" in text
        assert "#885 no GEFS data" in text

    def test_crit_with_warns_combined(self):
        """CRIT + WARN → CRIT verdict listing all issues."""
        sections = [
            ("bot", ["Bot Pulse", "Status: [STALE] 90 min since last poll"],
             {"status": "STALE", "detail": "90 min since last poll"}),
            ("m3", ["M3 Progress", "p_yes=0.0 artifact:     17.5% [WARN]"], {}),
        ]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[CRIT]" in text
        assert "bot stale" in text
        assert "artifact rate high" in text

    def test_unrelated_warn_not_attributed_to_artifact_rate(self):
        """Regression test for issue #914 defect 3.

        A [WARN] in the Bot Pulse section (e.g. the max-gap warning) must
        NOT be attributed to "artifact rate high" just because the phrase
        "p_yes=0.0 artifact:" appears somewhere else in the report. Before
        the fix, `_build_verdict` scanned the whole rendered report for
        "[WARN]" and, independently, for the artifact phrase -- so any WARN
        anywhere falsely triggered the artifact-rate verdict item even when
        the M3 section's own artifact line was [OK].
        """
        sections = [
            ("bot", [
                "Bot Pulse",
                "  [WARN] Max gap:    298 min (threshold: 20)",
                "  Status:      [WARN] only 5/288 polls in 24h",
            ], {"status": "WARN", "detail": "only 5/288 polls in 24h"}),
            ("m3", ["M3 Progress", "  p_yes=0.0 artifact:     2.0% [OK]"], {}),
        ]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "only 5/288 polls in 24h" in text
        assert "artifact rate high" not in text

    def test_bot_ok_status_not_polluted_by_other_section_crit_text(self):
        """A literal "[CRIT]" appearing in an unrelated section's text must
        not escalate the verdict to CRIT -- only the bot section's own
        structured status flag may do that (defect 3)."""
        sections = [
            ("bot", ["Bot Pulse", "Status: [OK] Healthy"], {"status": "OK"}),
            ("blockers", ["Open Blockers", "some historical note mentioning [CRIT] in passing"], {}),
        ]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[OK] Healthy" in text
        assert "bot stale" not in text


# ---------------------------------------------------------------------------
# build_report with mocked DB
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_returns_non_empty_string(self):
        db = _mock_db()
        result = build_report(db)
        assert isinstance(result, str)
        assert len(result) > 100

    def test_contains_all_section_headers(self):
        db = _mock_db()
        result = build_report(db)
        for header in [
            "Bot Pulse",
            "Trading (last 24h UTC)",
            "Data Pipeline (today UTC)",
            "Guardrails (last 24h)",
            "M3 Progress",
            "EMOS Status",
            "Open Blockers",
            "Verdict",
        ]:
            assert header in result, f"Missing section: {header}"

    def test_handles_empty_db_gracefully(self):
        """With no data at all, every section should produce output without crashing."""
        db = _mock_db()
        result = build_report(db)
        assert "MeteoEdge Daily Health" in result
        assert "Verdict" in result

    def test_handles_db_with_data(self):
        """With realistic data, sections should show actual numbers."""
        db = _mock_db({
            "SELECT MAX(poll_ts)": ("2026-07-30T13:50:00+00:00",),
            "SELECT COUNT(DISTINCT poll_ts)": (280,),
            "SELECT poll_ts FROM scan_decisions": [
                ("2026-07-30T13:45:00+00:00",),
                ("2026-07-30T13:50:00+00:00",),
            ],
            "SELECT COUNT(*), COALESCE(SUM(pnl)": (0, 0.0),
            "SELECT COUNT(*), COALESCE(SUM(shares * entry_price": (0, 0.0),
            "SELECT pnl FROM trades": [],
            "SELECT daily_pnl FROM risk_state": (None,),
            "SELECT COUNT(*) FROM observations": (1500,),
            # Specific GEFS query must precede the generic model_forecast_log one
            "SELECT COUNT(*) FROM model_forecast_log WHERE model='gefs'": (3445,),
            "SELECT COUNT(*) FROM model_forecast_log": (330,),
            "SELECT COUNT(*) FROM settlements": (28,),
            "SELECT COUNT(*) FROM guardrail_events": (12,),
            "SELECT COUNT(*) FROM trades WHERE close_reason": (0,),
            "SELECT COUNT(DISTINCT station || '-' || date)": (194,),
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts": (500,),
            "SELECT AVG(crps_score), COUNT(*) FROM emos_crps_log": (1.45, 27),
            "SELECT DISTINCT city FROM emos_calibration": [
                ("Chicago",), ("Atlanta",), ("Singapore",),
            ],
        })
        # Mock get_emos_crps_count per city
        db.get_emos_crps_count = MagicMock(return_value=5)
        result = build_report(db)
        assert "280/288" in result or "280" in result
        assert "1,500" in result
        assert "194/300" in result
        assert "Singapore" in result
        assert "3,445 GEFS rows" in result


# ---------------------------------------------------------------------------
# _send_email
# ---------------------------------------------------------------------------

class TestSendEmail:
    def test_returns_false_when_smtp_not_configured(self):
        with patch("src.scripts.daily_health_report.SMTP_USER", ""), \
             patch("src.scripts.daily_health_report.SMTP_PASS", ""), \
             patch("src.scripts.daily_health_report.ALERT_EMAIL_TO", ""):
            result = _send_email("Subject", "Body")
        assert result is False

    def test_returns_false_when_smtp_user_set_but_pass_empty(self):
        with patch("src.scripts.daily_health_report.SMTP_USER", "user@x.com"), \
             patch("src.scripts.daily_health_report.SMTP_PASS", ""), \
             patch("src.scripts.daily_health_report.ALERT_EMAIL_TO", "to@x.com"):
            result = _send_email("Subject", "Body")
        assert result is False

    def test_returns_false_when_alert_email_to_empty(self):
        with patch("src.scripts.daily_health_report.SMTP_USER", "user@x.com"), \
             patch("src.scripts.daily_health_report.SMTP_PASS", "pass"), \
             patch("src.scripts.daily_health_report.ALERT_EMAIL_TO", ""):
            result = _send_email("Subject", "Body")
        assert result is False

    def test_attempts_smtp_when_configured(self):
        with patch("src.scripts.daily_health_report.SMTP_USER", "user@x.com"), \
             patch("src.scripts.daily_health_report.SMTP_PASS", "pass"), \
             patch("src.scripts.daily_health_report.ALERT_EMAIL_TO", "to@x.com"), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_conn = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_conn
            result = _send_email("Subject", "Body")
        assert result is True
        mock_conn.send_message.assert_called_once()

    def test_returns_false_on_smtp_error(self):
        with patch("src.scripts.daily_health_report.SMTP_USER", "user@x.com"), \
             patch("src.scripts.daily_health_report.SMTP_PASS", "pass"), \
             patch("src.scripts.daily_health_report.ALERT_EMAIL_TO", "to@x.com"), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.side_effect = ConnectionRefusedError("refused")
            result = _send_email("Subject", "Body")
        assert result is False


# ---------------------------------------------------------------------------
# M3 Progress: Rail Metric (issue #910)
# ---------------------------------------------------------------------------

class TestBuildM3ProgressRailMetric:
    """Verify rail metric uses MODEL_PROB_CAP-derived thresholds (issue #910)."""

    def test_rail_metric_uses_model_prob_cap_thresholds(self):
        """With default MODEL_PROB_CAP=0.95, rail thresholds should be 0.05 and 0.95."""
        from src.config import MODEL_PROB_CAP

        # Mock: 1000 brackets, 624 are rail (62.4%)
        def mock_execute(sql, params=None):
            mock = MagicMock()
            if "capped_p_yes <= ? OR capped_p_yes >= ?" in sql:
                # This is the rail count query
                mock.fetchone.return_value = (624,)
            elif "raw_p_yes = 0.0" in sql:
                # This is the zero artifact query
                mock.fetchone.return_value = (0,)
            else:
                # This is the total brackets query
                mock.fetchone.return_value = (1000,)
            return mock

        db = MagicMock()
        db._conn.execute = mock_execute
        today = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
        result = _build_m3_progress(db, today)
        text = "\n".join(result)

        # Verify: output shows threshold range derived from MODEL_PROB_CAP
        expected_lower_pct = round((1.0 - MODEL_PROB_CAP) * 100, 1)
        expected_upper_pct = round(MODEL_PROB_CAP * 100, 1)
        assert f"Rail (0-{expected_lower_pct}% / {expected_upper_pct}%-100%)" in text
        # Verify rail metric shows exactly 62.4% and [WARN]
        assert "62.4%" in text
        assert "[WARN]" in text

    def test_rail_metric_low_concentration_ok(self):
        """With <20% concentration, rail should show [OK]."""
        # Mock: 1000 brackets, 150 are rail (15%)
        def mock_execute(sql, params=None):
            mock = MagicMock()
            if "capped_p_yes <= ? OR capped_p_yes >= ?" in sql:
                # Rail count query
                mock.fetchone.return_value = (150,)
            elif "raw_p_yes = 0.0" in sql:
                # Zero artifact query
                mock.fetchone.return_value = (0,)
            else:
                # Total brackets query
                mock.fetchone.return_value = (1000,)
            return mock

        db = MagicMock()
        db._conn.execute = mock_execute

        today = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
        result = _build_m3_progress(db, today)
        text = "\n".join(result)

        # Verify: 15% < 20% threshold, so should show [OK]
        assert "15.0%" in text
        # Rail line should have [OK], not just any [OK] in the section
        rail_line = [line for line in result if "Rail" in line][0]
        assert "[OK]" in rail_line

    def test_rail_metric_regression_capped_p_yes_at_floor(self):
        """Regression test: rows with capped_p_yes at clamp floor are counted as rail.

        This test exercises the actual SQL against real data. It fails with the old
        hardcoded thresholds (0.02/0.98) because they sit outside the MODEL_PROB_CAP
        clamp range (0.05/0.95), and passes with the current fix.
        """
        import sqlite3
        from src.config import MODEL_PROB_CAP
        from datetime import datetime, timezone, timedelta

        # Create in-memory database with scan_decisions table
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE scan_decisions (
                poll_ts TEXT NOT NULL,
                station TEXT NOT NULL,
                date TEXT NOT NULL,
                capped_p_yes REAL NOT NULL,
                raw_p_yes REAL NOT NULL
            )
        """)

        # Insert 1000 brackets total
        now_str = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc).isoformat()
        since_str = (datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc) - timedelta(hours=24)).isoformat()

        # Use the same rounding as _build_m3_progress to match the SQL thresholds
        clamp_floor = round(1.0 - MODEL_PROB_CAP, 10)

        # 600 brackets with capped_p_yes at or below the clamp floor
        for i in range(600):
            conn.execute(
                "INSERT INTO scan_decisions VALUES (?, ?, ?, ?, ?)",
                (now_str, f"STAT{i}", "2026-07-31", clamp_floor, 0.04),
            )

        # 400 brackets in the middle (not rail)
        for i in range(600, 1000):
            conn.execute(
                "INSERT INTO scan_decisions VALUES (?, ?, ?, ?, ?)",
                (now_str, f"STAT{i}", "2026-07-31", 0.50, 0.50),
            )

        conn.commit()

        # Create mock db with real connection
        db = MagicMock()
        db._conn = conn

        today = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
        result = _build_m3_progress(db, today)
        text = "\n".join(result)

        # With the fix, 600/1000 = 60.0% should be counted as rail
        assert "60.0%" in text
        # Should show [WARN] because 60% > 20% threshold
        rail_line = [line for line in result if "Rail" in line][0]
        assert "[WARN]" in rail_line

        conn.close()


# ---------------------------------------------------------------------------
# CLI: --dry-run
# ---------------------------------------------------------------------------

class TestMainDryRun:
    def test_dry_run_prints_and_returns_zero(self, capsys):
        """--dry-run prints report to stdout, returns 0, never touches SMTP."""
        with patch("src.data.db.Database") as mock_db_class:
            mock_db = _mock_db()
            mock_db_class.return_value = mock_db
            rc = main(["--dry-run"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "MeteoEdge Daily Health" in captured.out
        assert "Verdict" in captured.out
