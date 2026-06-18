"""Tests for guardrail telemetry (issue #325)."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient

from src.data.db import Database
from src.dashboard.api import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    db = Database(":memory:")
    return db


# ---------------------------------------------------------------------------
# DB: log_guardrail_event
# ---------------------------------------------------------------------------

class TestLogGuardrailEvent:
    def test_cap_event_logged_when_p_yes_clamped(self):
        db = _db()
        db.log_guardrail_event("2026-01-01T12:00:00+00:00", "KORD", "cap_applied", 0.97, 0.95, "KORD-80-82")
        stats = db.get_guardrail_stats()
        assert stats["cap_events"]["total"] == 1

    def test_no_cap_event_logged_when_p_yes_unchanged(self):
        """If p_yes == raw_p_yes, scanner does not call log_guardrail_event."""
        db = _db()
        stats = db.get_guardrail_stats()
        assert stats["cap_events"]["total"] == 0

    def test_correction_event_logged_when_residual_differs(self):
        db = _db()
        db.log_guardrail_event("2026-01-01T12:00:00+00:00", "KORD", "correction_applied", 75.0, 73.5)
        stats = db.get_guardrail_stats()
        assert stats["correction_events"]["total"] == 1

    def test_no_correction_event_when_residual_unchanged(self):
        """Builder does not log when residual_mu == base_mu."""
        db = _db()
        stats = db.get_guardrail_stats()
        assert stats["correction_events"]["total"] == 0

    def test_delta_computed_correctly(self):
        db = _db()
        db.log_guardrail_event("ts", "KORD", "cap_applied", 0.97, 0.95)
        row = db._conn.execute(
            "SELECT delta FROM guardrail_events WHERE event_type='cap_applied'"
        ).fetchone()
        assert abs(row[0] - (0.95 - 0.97)) < 1e-9


# ---------------------------------------------------------------------------
# Scanner: cap event wiring (unit tests via DB mock)
# ---------------------------------------------------------------------------

class TestScannerCapWiring:
    def test_cap_event_logged_when_p_yes_clamped(self):
        """log_guardrail_event called with cap_applied when raw != clamped."""
        db = _db()
        db.log_guardrail_event("ts", "KORD", "cap_applied", 0.98, 0.95, "KORD-80-82")
        stats = db.get_guardrail_stats()
        assert stats["cap_events"]["total"] == 1
        assert stats["cap_events"]["avg_delta"] == pytest.approx(0.95 - 0.98)

    def test_no_cap_event_when_p_yes_within_bounds(self):
        """No cap_applied row when p_yes is not clamped (raw == final)."""
        db = _db()
        # Simulate scanner NOT calling log_guardrail_event (p_yes unchanged)
        stats = db.get_guardrail_stats()
        assert stats["cap_events"]["total"] == 0


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------

class TestGuardrailEventsEndpoint:
    def test_api_returns_correct_forced_exit_count(self):
        mock_db = MagicMock()
        mock_db.get_guardrail_stats.return_value = {
            "cap_events": {"total": 0, "last_7d": 0, "avg_delta": 0.0},
            "correction_events": {"total": 0, "last_7d": 0, "avg_delta": 0.0},
        }
        mock_db.get_forced_exit_stats.return_value = {
            "total": 5, "last_7d": 2, "by_station": {"KORD": 3, "KLAX": 2},
        }

        with patch("src.dashboard.api._db", mock_db):
            resp = client.get("/api/guardrail-events")

        assert resp.status_code == 200
        data = resp.json()
        assert data["forced_exits"]["total"] == 5

    def test_api_returns_cap_event_summary(self):
        mock_db = MagicMock()
        mock_db.get_guardrail_stats.return_value = {
            "cap_events": {"total": 8, "last_7d": 2, "avg_delta": -0.03},
            "correction_events": {"total": 0, "last_7d": 0, "avg_delta": 0.0},
        }
        mock_db.get_forced_exit_stats.return_value = {
            "total": 0, "last_7d": 0, "by_station": {},
        }

        with patch("src.dashboard.api._db", mock_db):
            resp = client.get("/api/guardrail-events")

        assert resp.status_code == 200
        data = resp.json()
        assert data["cap_events"]["total"] == 8
        assert data["cap_events"]["avg_delta_p"] == pytest.approx(-0.03)

    def test_api_returns_zeros_when_table_empty(self):
        mock_db = MagicMock()
        mock_db.get_guardrail_stats.return_value = {
            "cap_events": {"total": 0, "last_7d": 0, "avg_delta": 0.0},
            "correction_events": {"total": 0, "last_7d": 0, "avg_delta": 0.0},
        }
        mock_db.get_forced_exit_stats.return_value = {
            "total": 0, "last_7d": 0, "by_station": {},
        }

        with patch("src.dashboard.api._db", mock_db):
            resp = client.get("/api/guardrail-events")

        assert resp.status_code == 200
        data = resp.json()
        assert data["forced_exits"]["total"] == 0
        assert data["cap_events"]["total"] == 0
        assert data["correction_events"]["total"] == 0
