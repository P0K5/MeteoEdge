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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable
_HERE = Path(__file__).resolve().parent
if str(_HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[1]))

from src.scripts.daily_health_report import (
    HIGH_RAIL_WARN_RATIO,  # noqa: E402
    M3_CLEAN_DATA_CLOCK_START,  # noqa: E402
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
        conn.execute("CREATE TABLE scan_decisions (poll_ts TEXT, station TEXT, date TEXT, raw_p_yes REAL, capped_p_yes REAL, yes_ask INTEGER, no_ask INTEGER)")

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
        assert "Brackets live (scan_decisions, upserted): 5" in text
        conn.close()

    def test_status_warn_on_poll_count_shortfall_even_if_last_poll_recent(self):
        """Regression test for defect 2: a bot that polled only a handful of
        times in 24h but happened to poll 3 minutes ago must not be reported
        [OK] Healthy -- the 24h shortfall must fold into the status."""
        import sqlite3
        from datetime import timedelta

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE poll_runs (id INTEGER PRIMARY KEY, poll_ts TEXT, mode TEXT)")
        conn.execute("CREATE TABLE scan_decisions (poll_ts TEXT, station TEXT, date TEXT, raw_p_yes REAL, capped_p_yes REAL, yes_ask INTEGER, no_ask INTEGER)")

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
        conn.execute("CREATE TABLE scan_decisions (poll_ts TEXT, station TEXT, date TEXT, raw_p_yes REAL, capped_p_yes REAL, yes_ask INTEGER, no_ask INTEGER)")

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
        sections = [("m3", ["M3 Progress", "Interior-zero gaps: 5 of 20 ladders (25.0%) [WARN]"],
                     {"mass_status": "OK", "rail_status": "OK", "gaps_status": "WARN"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "artifact rate high" in text

    def test_m3_high_rail_warns_gaps_ok_names_the_rail_not_artifact_rate(self):
        """Issue #972: a high-rail WARN must be named as a rail/sharpness
        problem, not folded into "artifact rate high" just because it is a
        WARN somewhere in the M3 section."""
        sections = [("m3", ["M3 Progress", "High rail ... [WARN]", "Interior-zero gaps: 0 of 20 (0.0%) [OK]"],
                     {"mass_status": "OK", "rail_status": "WARN", "gaps_status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "rail" in text.lower()
        assert "artifact rate high" not in text

    def test_m3_gaps_warn_rail_ok_names_the_gaps(self):
        sections = [("m3", ["M3 Progress", "High rail ... [OK]", "Interior-zero gaps: 5 of 20 (25.0%) [WARN]"],
                     {"mass_status": "OK", "rail_status": "OK", "gaps_status": "WARN"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "artifact rate high" in text
        assert "rail" not in text.lower()

    def test_m3_both_rail_and_gaps_warn_produce_both_labels(self):
        sections = [("m3", ["M3 Progress", "High rail ... [WARN]", "Interior-zero gaps: 5 of 20 (25.0%) [WARN]"],
                     {"mass_status": "OK", "rail_status": "WARN", "gaps_status": "WARN"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "artifact rate high" in text
        assert "rail" in text.lower()

    def test_m3_healthy_section_produces_no_m3_label(self):
        sections = [("m3", ["M3 Progress", "everything [OK]"],
                     {"mass_status": "OK", "rail_status": "OK", "gaps_status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[OK] Healthy" in text
        assert "artifact rate high" not in text
        assert "rail" not in text.lower()
        assert "mass" not in text.lower()

    def test_m3_ladder_mass_warn_names_mass_not_artifact_rate(self):
        """Issue #972 required test: a ladder-mass WARN must name the mass
        problem, not "artifact rate high"."""
        sections = [("m3", ["M3 Progress", "Ladder mass: 0.80 mean ... [WARN]",
                             "Interior-zero gaps: 0 of 20 (0.0%) [OK]"],
                     {"mass_status": "WARN", "rail_status": "OK", "gaps_status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "mass" in text.lower()
        assert "artifact rate high" not in text

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
            ("m3", ["M3 Progress", "Interior-zero gaps: 5 of 20 ladders (25.0%) [WARN]"],
             {"mass_status": "OK", "rail_status": "OK", "gaps_status": "WARN"}),
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
            ("m3", ["M3 Progress", "Interior-zero gaps: 5 of 20 ladders (25.0%) [WARN]"],
             {"mass_status": "OK", "rail_status": "OK", "gaps_status": "WARN"}),
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
        "Interior-zero gaps:" appears somewhere else in the report. Before
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
            ("m3", ["M3 Progress", "  Interior-zero gaps: 0 of 20 ladders (0.0%) [OK]"],
             {"mass_status": "OK", "rail_status": "OK", "gaps_status": "OK"}),
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

    def test_all_m3_flags_unknown_yields_unknown_verdict(self):
        """All three m3 flags UNKNOWN → verdict is NOT [OK] Healthy, and names all three."""
        sections = [("m3", ["M3 Progress", "(no ladders in 24h -- cannot assess)", "(no brackets in 24h -- cannot assess)"],
                     {"mass_status": "UNKNOWN", "rail_status": "UNKNOWN", "gaps_status": "UNKNOWN"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[UNKNOWN]" in text
        assert "[OK] Healthy" not in text
        assert "M3 ladder mass not assessed" in text
        assert "M3 high rail not assessed" in text
        assert "M3 gaps not assessed" in text

    def test_one_unknown_others_ok_yields_unknown_verdict(self):
        """One UNKNOWN, others OK → [UNKNOWN] Cannot fully assess, naming only the unassessed one."""
        sections = [("m3", ["M3 Progress", "Ladder mass: 1.00 mean [OK]", "High rail ... [OK]", "Interior-zero gaps: 0 of 20 (0.0%) [OK]"],
                     {"mass_status": "UNKNOWN", "rail_status": "OK", "gaps_status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[UNKNOWN]" in text
        assert "[OK] Healthy" not in text
        assert "M3 ladder mass not assessed" in text
        assert "M3 high rail not assessed" not in text
        assert "M3 gaps not assessed" not in text

    def test_one_warn_one_unknown_warn_wins_both_labels_appear(self):
        """One WARN + one UNKNOWN → [WARN] tag wins, and BOTH labels appear."""
        sections = [("m3", ["M3 Progress", "Ladder mass: 0.80 mean [WARN]", "High rail: (no ceiling -- cannot assess)", "Interior-zero gaps: 0 of 20 (0.0%) [OK]"],
                     {"mass_status": "WARN", "rail_status": "UNKNOWN", "gaps_status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[WARN]" in text
        assert "[OK] Healthy" not in text
        assert "M3 ladder mass leaking" in text
        assert "M3 high rail not assessed" in text
        assert "M3 gaps not assessed" not in text

    def test_all_m3_ok_still_yields_ok_verdict(self):
        """All three OK → still [OK] Healthy (regression guard)."""
        sections = [("m3", ["M3 Progress", "Ladder mass: 1.00 mean [OK]", "High rail ... [OK]", "Interior-zero gaps: 0 of 20 (0.0%) [OK]"],
                     {"mass_status": "OK", "rail_status": "OK", "gaps_status": "OK"})]
        result = _build_verdict(sections)
        text = "\n".join(result)
        assert "[OK] Healthy" in text
        assert "[UNKNOWN]" not in text
        assert "not assessed" not in text

    def test_query_error_yields_partial_data_distinct_from_empty_window(self):
        """Query-error path yields 'partial data only' and is distinguishable from empty-window path."""
        # Query-error path: has "unavailable" in the text
        sections_error = [("m3", ["M3 Progress", "(unavailable -- query error)"], {"mass_status": "UNKNOWN", "rail_status": "UNKNOWN", "gaps_status": "UNKNOWN"})]
        result_error = _build_verdict(sections_error)
        text_error = "\n".join(result_error)
        assert "[WARN]" in text_error  # "partial data only" is a WARN
        assert "partial data only" in text_error

        # Empty-window path: no "unavailable", just UNKNOWN flags
        sections_empty = [("m3", ["M3 Progress", "(no ladders in 24h -- cannot assess)"], {"mass_status": "UNKNOWN", "rail_status": "UNKNOWN", "gaps_status": "UNKNOWN"})]
        result_empty = _build_verdict(sections_empty)
        text_empty = "\n".join(result_empty)
        assert "[UNKNOWN]" in text_empty  # Only UNKNOWN flags, no query error
        assert "partial data only" not in text_empty
        assert "cannot assess" in text_empty or "not assessed" in text_empty


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
            # Explicit: the poll count comes from poll_runs (#914). This used
            # to be unset, so poll_count was None and _build_bot_health threw
            # inside its try -- the "280" the assertion below looks for was
            # actually matching the Evaluated line by coincidence.
            "SELECT COUNT(*) FROM poll_runs": (280,),
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
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts": (500,),
            "SELECT AVG(crps_score), COUNT(*) FROM emos_crps_log": (1.45, 27),
            "SELECT DISTINCT city FROM emos_calibration": [
                ("Chicago",), ("Atlanta",), ("Singapore",),
            ],
        })
        # Mock get_emos_crps_count per city
        db.get_emos_crps_count = MagicMock(return_value=5)
        # Station-days come from bracket_evals now, not scan_decisions -- the
        # gate's own source. 3 stations x 2 settlement dates = 6 station-days.
        evals = []
        for st in ("KATL", "KORD", "KDEN"):
            for settle in ("2026-08-06", "2026-08-07"):
                for j in range(11):
                    evals.append({
                        "station": st, "ts": "2026-08-07T12:00:00+00:00",
                        "end_date": settle, "is_next_day_flag": 0,
                        "p_yes_raw": 1 / 11,
                        "bracket_low": -50.0 if j == 0 else 80.0 + (j - 1) * 2.0,
                        "bracket_high": 200.0 if j == 10 else 80.0 + j * 2.0,
                        "yes_ask": 30, "no_ask": 70,
                    })
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=evals):
            result = build_report(db)
        assert "280/288" in result or "280" in result
        assert "1,500" in result
        assert "6/300" in result
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
# M3 Progress: Rail / artifact leading indicators (issue #969)
# ---------------------------------------------------------------------------

class TestBuildM3ProgressLeadingIndicators:
    """Rail and interior-zero-gap indicators read `bracket_evals` and agree
    with `post_fix_model_health` (issue #969).

    Replaces the old `TestBuildM3ProgressRailMetric`, which pinned the
    `scan_decisions`-based behaviour this issue removes: a query against the
    FULL bracket ladder thresholded with `rail_pct < 20` / `zero_pct < 10`,
    Pass-1's GATE-SELECTED baselines. On a full ladder -- mostly
    far-out-of-the-money brackets that legitimately sit near a rail -- that
    threshold can essentially never clear (an 11-bracket ladder has at most
    one bracket that can resolve YES), so it fired `[WARN]` unconditionally.
    """

    @staticmethod
    def _ladder_rows(n_stations, n_brackets=11, high_rail_stations=0,
                     high_rail_p=0.99, filler_p=0.30, day=None,
                     station_prefix="ST"):
        """`n_stations` station-day ladders of `n_brackets` brackets each, one
        poll snapshot. The first `high_rail_stations` ladders each get exactly
        one bracket at `high_rail_p` (the true daily high) with the rest at
        0.02 (comfortably a low rail, i.e. low-rail-high-by-construction); the
        remaining stations get every bracket at `filler_p` -- no rail at all,
        matching how a full ladder legitimately looks when the model isn't
        overconfident.
        """
        day = day or M3_CLEAN_DATA_CLOCK_START
        rows = []
        for i in range(n_stations):
            station = f"{station_prefix}{i}"
            for j in range(n_brackets):
                if i < high_rail_stations:
                    p = high_rail_p if j == 0 else 0.02
                else:
                    p = filler_p
                rows.append({
                    "station": station,
                    "ts": f"{day}T12:00:00+00:00",
                    "end_date": day,
                    "ticker": f"{station}-{j}",
                    "bracket_low": float(j),
                    "bracket_high": float(j + 1),
                    "p_yes_raw": p,
                })
        return rows

    def _text(self, rows, today=None):
        today = today or datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)
        db = MagicMock()
        with patch("src.scripts.bss_market_vs_model_report.load_bracket_eval_rows",
                   return_value=rows):
            lines, _flags = _build_m3_progress(db, today)
            return "\n".join(lines)

    def _flags(self, rows, today=None):
        today = today or datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)
        db = MagicMock()
        with patch("src.scripts.bss_market_vs_model_report.load_bracket_eval_rows",
                   return_value=rows):
            _lines, flags = _build_m3_progress(db, today)
            return flags

    def _rail_line(self, text):
        return [line for line in text.splitlines() if "High rail" in line][0]

    def _artifact_line(self, text):
        return [line for line in text.splitlines() if "Interior-zero gaps" in line][0]

    def test_no_high_rail_brackets_reads_healthy(self):
        """Every ladder sits in the middle -- high rail 0% of a non-zero
        ceiling -- must read [OK], the AC's 'low rail high, high rail near
        zero' case (no bracket here is even at the low rail, which is a
        stronger version of the same claim: nothing here should trip a
        near-zero high-rail share into [WARN])."""
        rows = self._ladder_rows(n_stations=20, high_rail_stations=0)
        text = self._text(rows)
        assert "[OK]" in self._rail_line(text)
        assert "0.0%" in self._rail_line(text)

    def test_high_rail_at_structural_ceiling_warns(self):
        """Every ladder's one bracket at 0.99 -- high rail share == exactly
        1 / ladder_size, i.e. AT the structural ceiling -- must read [WARN]."""
        rows = self._ladder_rows(n_stations=20, high_rail_stations=20)
        text = self._text(rows)
        rail_line = self._rail_line(text)
        assert "[WARN]" in rail_line
        assert "100%" in rail_line

    def test_interior_zero_gap_warns(self):
        """One ladder with a zero bracket flanked by non-zero brackets on
        both sides, in sorted bracket order -- the #820 failure shape -- must
        read [WARN], even while every other ladder is clean."""
        rows = self._ladder_rows(n_stations=20, high_rail_stations=0)
        gap_ladder = [
            {"station": "GAP", "ts": f"{M3_CLEAN_DATA_CLOCK_START}T12:00:00+00:00",
             "end_date": M3_CLEAN_DATA_CLOCK_START, "ticker": f"GAP-{j}",
             "bracket_low": float(j), "bracket_high": float(j + 1), "p_yes_raw": p}
            for j, p in enumerate([0.1, 0.2, 0.0, 0.2, 0.1])
        ]
        text = self._text(rows + gap_ladder)
        artifact_line = self._artifact_line(text)
        assert "[WARN]" in artifact_line
        assert "1 of" in artifact_line

    def test_no_interior_zero_gaps_reads_healthy(self):
        rows = self._ladder_rows(n_stations=20, high_rail_stations=0)
        text = self._text(rows)
        artifact_line = self._artifact_line(text)
        assert "[OK]" in artifact_line
        assert "0 of" in artifact_line

    def test_row_polled_before_clock_but_settling_after_it_is_excluded(self):
        """The clean-data clock filters on POLL time, not settlement date
        (#941 / #969 review). A ladder polled the day before the clock
        starts, settling ON the clock-start date, is exactly the pre-#920
        contamination class the clock exists to exclude -- filtering on
        `end_date` instead would let it through and read healthy."""
        before_clock = "2026-08-05"
        assert before_clock < M3_CLEAN_DATA_CLOCK_START
        contaminated_high_rail = [
            {"station": "PRECLOCK", "ts": f"{before_clock}T12:00:00+00:00",
             "end_date": M3_CLEAN_DATA_CLOCK_START, "ticker": f"PRECLOCK-{j}",
             "bracket_low": float(j), "bracket_high": float(j + 1),
             "p_yes_raw": 0.99 if j == 0 else 0.02}
            for j in range(11)
        ]
        text = self._text(contaminated_high_rail)
        # Excluded entirely -> no brackets survive the clock filter.
        assert "(no brackets in 24h -- cannot assess)" in text

    def test_agrees_with_post_fix_model_health(self):
        """Regression test (issue #969): the health report and
        `post_fix_model_health` must read the SAME [OK]/[WARN] verdict on the
        same fixture window, computed via the same shared functions, so they
        cannot silently drift apart again."""
        from src.scripts.post_fix_model_health import (
            interior_zero_violations, rail_concentration,
            structural_high_rail_ceiling)

        for rows, expect_warn in [
            (self._ladder_rows(n_stations=20, high_rail_stations=0), False),
            (self._ladder_rows(n_stations=20, high_rail_stations=20), True),
        ]:
            text = self._text(rows)
            report_warns = "[WARN]" in self._rail_line(text)

            mapped = [{**r, "settlement_date": r["end_date"], "poll_ts": r["ts"]}
                      for r in rows]
            rails = rail_concentration(mapped)
            ceiling = structural_high_rail_ceiling(mapped)
            gate_ratio = rails["high_rail_share"] / ceiling
            gate_warns = gate_ratio >= HIGH_RAIL_WARN_RATIO

            assert report_warns == gate_warns == expect_warn, (
                f"health report ({report_warns}) and post_fix_model_health "
                f"({gate_warns}) disagree on the same fixture window"
            )

    def test_no_brackets_is_not_reported_as_healthy(self):
        text = self._text([])
        assert "Rail / artifact: (no brackets in 24h -- cannot assess)" in text
        assert "0.0% [OK]" not in text


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


class TestLadderMassIndicator:
    """The check whose absence cost two weeks.

    #917 (°F ladders at half width, sum 0.53) and #920 (truncation without
    renormalisation, sum 0.80) both reached production and survived weeks.
    Both were visible in one number from the day they landed.

    Reads ``bracket_evals``, not ``scan_decisions`` -- see ``_ladder_mass``.
    Three attempts on the upsert table failed structurally: it keeps only each
    market's last write, so a ladder frozen at an old poll cannot be recovered
    by any grouping.
    """

    @staticmethod
    def _rows(ladders, day="2026-08-07", station_prefix="ST", open_top=True,
              yes_ask=30, no_ask=70, settle=None):
        """ladders: list of (total, n_brackets). One ladder per station.

        Production-shaped: brackets span ``[-50, 200]``, because a ladder
        without open-ended tails is *coverage-limited* and its deficit is
        deliberately excused -- a fixture lacking them tests nothing. Pass
        ``open_top=False`` for the coverage-limited case (the RCSS ladder).
        """
        out = []
        for i, (total, n) in enumerate(ladders):
            top = 200.0 if open_top else 80.0 + (n - 1) * 2.0
            edges = [-50.0] + [80.0 + k * 2.0 for k in range(n - 1)] + [top]
            for j in range(n):
                out.append({
                    "station": f"{station_prefix}{i}",
                    "ts": f"{day}T12:00:00+00:00",
                    "end_date": settle or day,
                    "is_next_day_flag": 0,
                    "p_yes_raw": total / n,
                    "bracket_low": edges[j],
                    "bracket_high": edges[j + 1],
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                })
        return out

    def _text(self, rows, today=None, since=None):
        today = today or datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)
        db = MagicMock()
        db._conn = sqlite3.connect(":memory:")
        db._conn.execute(
            "CREATE TABLE scan_decisions (poll_ts TEXT, station TEXT, date TEXT, "
            "raw_p_yes REAL, capped_p_yes REAL, yes_ask INTEGER, no_ask INTEGER)"
        )
        with patch("src.scripts.bss_market_vs_model_report.load_bracket_eval_rows",
                   return_value=rows):
            lines, _flags = _build_m3_progress(db, today)
            return "\n".join(lines)

    def test_a_conserving_ladder_reads_ok(self):
        text = self._text(self._rows([(1.00, 11)]))
        assert "1.000 mean, 1.000 worst (0/1 deficient) [OK]" in text

    def test_the_920_signature_warns(self):
        """0.80 -- what production showed while #920 was live."""
        text = self._text(self._rows([(0.80, 11)]))
        assert "[WARN]" in text and "leaking mass" in text

    def test_the_917_signature_warns(self):
        """0.53 -- °F ladders integrated at half width."""
        text = self._text(self._rows([(0.53, 11)]))
        assert "[WARN]" in text

    def test_worst_ladder_is_reported_not_just_the_mean(self):
        """A defect confined to one station is exactly what a mean hides."""
        text = self._text(self._rows([(1.00, 11), (1.00, 11), (0.20, 11)]))
        assert "0.200 worst" in text
        assert "[WARN]" in text

    def test_same_day_and_next_day_ladders_are_not_pooled(self):
        """Pooling two distributions manufactures a mass excess that is not
        in the data -- and #921 is an excess investigation."""
        rows = self._rows([(1.00, 11)])
        rows += [dict(r, is_next_day_flag=1) for r in rows]
        text = self._text(rows)
        assert "1.000 mean, 1.000 worst (0/2 deficient) [OK]" in text

    def test_ladders_polled_before_the_clock_are_excluded(self):
        """Pre-#920 ladders came from a different model; including them would
        warn forever. Production had exactly this -- 08-05 ladders at 0.22-0.49
        sitting beside clean 08-06 ones."""
        rows = self._rows([(0.46, 11)], day="2026-08-05")
        rows += self._rows([(1.00, 11)], day=M3_CLEAN_DATA_CLOCK_START,
                           station_prefix="CLEAN")
        text = self._text(rows, today=datetime(2026, 8, 6, 14, 0, 0,
                                               tzinfo=timezone.utc))
        assert "1.000 mean, 1.000 worst (0/1 deficient) [OK]" in text

    def test_a_part_listed_ladder_is_not_scored(self):
        """Too few brackets to mean anything -- not evidence of leaking mass."""
        text = self._text(self._rows([(0.30, 3)]))
        assert "cannot assess" in text

    def test_no_ladders_is_not_reported_as_healthy(self):
        """Absent evidence must not be indistinguishable from a clean ladder --
        for the mass check OR for the rail/artifact rates below it, which used
        to divide by `or 1` and render an empty day as a flawless '0.0% [OK]'."""
        text = self._text([])
        assert "Ladder mass:   (no ladders in 24h -- cannot assess)" in text
        assert "Rail / artifact: (no brackets in 24h -- cannot assess)" in text
        assert "0.0% [OK]" not in text

    def test_a_met_bar_with_dirty_ladders_is_refused(self):
        """The 2026-08-04 failure in one assertion: '362/300 (121%)' printed
        against a window that was entirely contaminated. A count is only
        meaningful once the probabilities behind it conserve mass.

        Both halves now come from bracket_evals -- the gate's own source -- so
        one set of rows supplies the count and the mass together, and they
        cannot disagree the way scan_decisions and bracket_evals did.
        """
        now = datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)
        # 30 stations x 11 settlement dates = 330 station-days, past the bar,
        # every ladder leaking (0.80 -- what production showed under #920).
        leaking = []
        for d in range(11):
            leaking += self._rows([(0.80, 11)] * 30, day="2026-08-07",
                                  settle=f"2026-08-{6 + d:02d}")
        text = self._text(leaking, today=now)
        assert "/300" in text and "330/300" in text      # bar reported met
        assert "leaking mass" in text                    # but mass is not clean
        assert "the gate must NOT run" in text

    def test_station_days_count_from_the_clock_constant(self):
        """The clock has moved twice (#917, #920). Rows before it are not
        merely old -- they were produced by a different model."""
        rows = (self._rows([(1.0, 11)], day="2026-07-25", station_prefix="OLD")
                + self._rows([(1.0, 11)], day=M3_CLEAN_DATA_CLOCK_START,
                             station_prefix="NEW"))
        text = self._text(rows)
        assert "1/300" in text
        assert M3_CLEAN_DATA_CLOCK_START in text

    def test_scoreable_excludes_what_the_gate_excludes(self):
        """The bar counts SCOREABLE station-days. A station-day whose only rows
        are zero-artifact or rail-clipped contributes nothing to the gate, and
        must not inflate the count (#932)."""
        day = M3_CLEAN_DATA_CLOCK_START
        rows = (
            # #820 artifact: p_yes_raw == 0.0 -> excluded
            self._rows([(0.0, 11)], day=day, station_prefix="ZERO")
            # rail-clipped market price -> excluded
            + self._rows([(1.0, 11)], day=day, station_prefix="RAIL",
                         yes_ask=1, no_ask=99)
            # scoreable
            + self._rows([(1.0, 11)], day=day, station_prefix="GOOD")
        )
        text = self._text(rows)
        assert "1/300" in text


class TestHealthReportAgreesWithTheGate:
    """The health report, the diagnostic and the gate must count one thing.

    They have silently diverged twice. #943 moved the mass check to
    ``bracket_evals`` and left the station-day count on ``scan_decisions``,
    eight lines apart in the same function. Then the mass check gained no
    coverage-limited exemption while the diagnostic did, so on 2026-08-07 the
    email warned "probabilities are leaking mass -- station-days below are NOT
    clean" about a ladder the diagnostic had correctly excused.

    Both were invisible to every other test here, because each tool was only
    ever tested against itself.
    """

    def _rows(self, station, settle, total=1.0, n=11, open_top=True,
              yes_ask=30, no_ask=70, day="2026-08-07"):
        top = 200.0 if open_top else 80.0 + (n - 1) * 2.0
        edges = [-50.0] + [80.0 + k * 2.0 for k in range(n - 1)] + [top]
        return [{"station": station, "ts": f"{day}T12:00:00+00:00",
                 "end_date": settle, "is_next_day_flag": 0,
                 "p_yes_raw": total / n,
                 "bracket_low": edges[j], "bracket_high": edges[j + 1],
                 "yes_ask": yes_ask, "no_ask": no_ask} for j in range(n)]

    def test_station_days_match_the_gates_own_definition(self):
        """`bss_market_vs_model_report` counts distinct (station,
        settlement_date) over the whole window. Anything else -- notably a
        per-poll-day count summed across days -- double-counts a settlement
        date polled as both next-day and same-day."""
        from src.scripts.daily_health_report import _scoreable_station_days
        rows = []
        for st in ("A", "B"):
            for settle in ("2026-08-06", "2026-08-07", "2026-08-08"):
                # same pair seen at two different poll times
                rows += self._rows(st, settle, day="2026-08-06")
                rows += self._rows(st, settle, day="2026-08-07")
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=rows):
            n = _scoreable_station_days("2026-08-06")
        gate_definition = len({(r["station"], r["end_date"]) for r in rows})
        assert n == gate_definition == 6, (
            "a pair polled twice must count once, not twice")

    def test_the_mass_exemption_matches_the_diagnostic(self):
        """One ladder, judged by both tools, must get the same answer."""
        from src.scripts.daily_health_report import _ladder_mass
        from src.scripts.m3_window_diagnostics import (
            deficient_ladders, group_ladders)
        # The RCSS shape: short, no open top -> excusable by both.
        rows = (self._rows("GOOD", "2026-08-07")
                + self._rows("RCSS", "2026-08-08", total=0.813, n=10,
                             open_top=False))
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=rows):
            mass = _ladder_mass("2026-08-06")

        diag_excused = [lad for lad in deficient_ladders(group_ladders(rows))
                        if lad["coverage_limited"]]
        assert mass["n_deficient"] == 0, "health report must not flag it"
        assert mass["n_excused"] == len(diag_excused) == 1, (
            "both tools must excuse the same ladder")

    def test_a_real_defect_is_flagged_by_BOTH(self):
        """The other side of the boundary -- agreement must not mean silence."""
        from src.scripts.daily_health_report import _ladder_mass
        from src.scripts.m3_window_diagnostics import (
            deficient_ladders, group_ladders)
        # #920 shape: open at both ends, still short.
        rows = self._rows("KORD", "2026-08-07", total=0.80)
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=rows):
            mass = _ladder_mass("2026-08-06")
        diag_bad = [lad for lad in deficient_ladders(group_ladders(rows))
                    if not lad["coverage_limited"]]
        assert mass["n_deficient"] == len(diag_bad) == 1
        assert mass["n_excused"] == 0

    def test_accrual_is_marginal_not_average(self):
        """The opening day opens TWO settlement dates -- it polls same-day and
        next-day together -- so ``total / elapsed`` is inflated by it forever
        and only decays toward the truth.

        Measured in production: 08-06 contributed 56 station-days, 08-07 and
        08-08 contributed 28 each. The report read ~42/day then ~38/day while
        the real increment was 28 both times, understating the wait to 300 by
        three days. Erring early is the bad direction -- it invites running the
        gate before it can decide anything.

        This fixture reproduces that shape: two poll days covering three
        settlement dates. Average = 84/2 = 42. Marginal = 28.
        """
        rows = []
        for st in range(28):
            # poll day 1 prices today and tomorrow; poll day 2 the same
            rows += self._rows(f"ST{st}", "2026-08-06", day="2026-08-06")
            rows += self._rows(f"ST{st}", "2026-08-07", day="2026-08-06")
            rows += self._rows(f"ST{st}", "2026-08-07", day="2026-08-07")
            rows += self._rows(f"ST{st}", "2026-08-08", day="2026-08-07")
        today = datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)
        db = MagicMock()
        db._conn = sqlite3.connect(":memory:")
        db._conn.execute(
            "CREATE TABLE scan_decisions (poll_ts TEXT, station TEXT, "
            "date TEXT, raw_p_yes REAL, capped_p_yes REAL, yes_ask INTEGER, "
            "no_ask INTEGER)")
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=rows):
            lines, _flags = _build_m3_progress(db, today)
            text = "\n".join(lines)
        assert "84/300" in text
        assert "~28/day" in text, f"averaged instead of marginal: {text!r}"
        assert "~42/day" not in text
        # (300-84)/28 = 7.7 -> 8 whole days. Rounding DOWN would promise the
        # bar a day before it is met, which is the same optimism in miniature.
        assert "+ 8d" in text, text

    def test_marginal_rate_is_the_widest_settlement_date(self):
        """A fully-covered settlement date holds one station-day per
        contributing station, so it IS the daily increment -- and a partially
        collected newest date cannot drag it down."""
        from src.scripts.daily_health_report import _marginal_accrual
        pairs = {(f"ST{i}", "2026-08-06") for i in range(28)}
        pairs |= {(f"ST{i}", "2026-08-07") for i in range(28)}
        pairs |= {("ST0", "2026-08-08")}          # today, barely started
        assert _marginal_accrual(pairs) == 28
        assert _marginal_accrual(set()) == 0


class TestDedupBeforeExclude:
    """The gate de-duplicates and THEN excludes, so a bracket-day is judged on
    the model's final word about it. Counting the other way round -- exclude
    every row, then take distinct pairs -- keeps any bracket-day that was ever
    contested at any poll during the day.

    On 2026-08-10 that read 173/300 against a true 111: over half the reported
    progress was bracket-days the gate would drop. This is the third correction
    to this counter, and the only one of the three that no test would have
    caught, because every earlier test used a single poll per bracket.
    """

    def _row(self, ts, p, station="KATL", settle="2026-08-07", ticker="0xA",
             yes_ask=30, no_ask=70):
        return {"station": station, "ts": ts, "end_date": settle,
                "ticker": ticker, "is_next_day_flag": 0, "p_yes_raw": p,
                "bracket_low": 80.0, "bracket_high": 82.0,
                "yes_ask": yes_ask, "no_ask": no_ask}

    def _count(self, rows, since="2026-08-06"):
        from src.scripts.daily_health_report import _scoreable_pairs
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=rows):
            return len(_scoreable_pairs(since))

    def test_contested_early_but_zero_at_the_final_poll_does_NOT_count(self):
        """The exact case that inflated the count. The gate drops this
        bracket-day; so must we."""
        rows = [self._row("2026-08-07T09:00:00+00:00", 0.25),   # contested
                self._row("2026-08-07T15:00:00+00:00", 0.0)]    # final: zero
        assert self._count(rows) == 0

    def test_zero_early_but_contested_at_the_final_poll_DOES_count(self):
        """The mirror image -- excluding on any poll would lose a bracket-day
        the gate will score."""
        rows = [self._row("2026-08-07T09:00:00+00:00", 0.0),
                self._row("2026-08-07T15:00:00+00:00", 0.25)]
        assert self._count(rows) == 1

    def test_rail_at_the_final_poll_also_drops_the_bracket_day(self):
        """Both exclusions are applied post-de-duplication, not just the
        zero one."""
        rows = [self._row("2026-08-07T09:00:00+00:00", 0.25),
                self._row("2026-08-07T15:00:00+00:00", 0.25, yes_ask=1,
                          no_ask=99)]
        assert self._count(rows) == 0

    def test_a_station_day_survives_if_ANY_of_its_brackets_does(self):
        """De-duplication is per BRACKET; the station-day enters the scored
        population if any of its brackets' final polls survive."""
        rows = [self._row("2026-08-07T15:00:00+00:00", 0.0, ticker="0xA"),
                self._row("2026-08-07T15:00:00+00:00", 0.25, ticker="0xB")]
        assert self._count(rows) == 1

    def test_different_settlement_dates_dedup_apart(self):
        """Same station, same ticker, same poll -- but today's ladder and
        tomorrow's are different bracket-days and must not collapse."""
        rows = [self._row("2026-08-07T15:00:00+00:00", 0.25,
                          settle="2026-08-07"),
                self._row("2026-08-07T15:00:00+00:00", 0.25,
                          settle="2026-08-08")]
        assert self._count(rows) == 2

    def test_the_count_is_labelled_as_a_pre_resolution_upper_bound(self):
        """The gate scores only RESOLVED station-days -- 81 against 111 on
        2026-08-10. An unqualified number here reads as progress it isn't."""
        rows = [self._row("2026-08-07T15:00:00+00:00", 0.25)]
        today = datetime(2026, 8, 7, 14, 0, 0, tzinfo=timezone.utc)
        db = MagicMock()
        db._conn = sqlite3.connect(":memory:")
        db._conn.execute(
            "CREATE TABLE scan_decisions (poll_ts TEXT, station TEXT, "
            "date TEXT, raw_p_yes REAL, capped_p_yes REAL, yes_ask INTEGER, "
            "no_ask INTEGER)")
        with patch("src.scripts.bss_market_vs_model_report."
                   "load_bracket_eval_rows", return_value=rows):
            lines, _flags = _build_m3_progress(db, today)
            text = "\n".join(lines)
        assert "upper bound" in text
        assert "resolved" in text
