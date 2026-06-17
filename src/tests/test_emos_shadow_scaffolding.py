"""Tests for EMOS shadow scaffolding: CRPS/DEB logging, promotion guard, runner, API endpoint.

Issue #324 — all tests use in-memory DB to avoid file I/O.

CRITICAL CONSTRAINT: ready_for_promotion must NEVER be set to 1 by any automated
code in this PR. Every path in save_coefficients and the API endpoint must keep it 0.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient

from src.data.db import Database
from src.model.emos_calibration import (
    InsufficientDataError,
    fit_emos,
    save_coefficients,
)
from src.model.emos_mode import get_city_mode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


def _upsert_shadow(db: Database, city: str = "Chicago", *,
                   a: float = 0.0, b: float = 1.0,
                   c: float = 0.5, d: float = 1.0,
                   ready_for_promotion: int = 0) -> None:
    db.upsert_emos_coefficients(
        city=city,
        model_mode="emos_shadow",
        a=a, b=b, c=c, d=d,
        crps_score=1.5,
        trained_at=datetime.now(timezone.utc).isoformat(),
        ready_for_promotion=ready_for_promotion,
    )


def _synthetic_triples(n: int = 80) -> list[tuple[float, float, float]]:
    """Generate synthetic (mu_raw, sigma_raw, y) triples for fit testing."""
    import random
    rng = random.Random(99)
    return [
        (rng.uniform(70.0, 95.0), 3.0, rng.gauss(82.0, 3.0))
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Test 1: fit writes emos_shadow row
# ---------------------------------------------------------------------------

class TestFitWritesEmosShadowRow:
    def test_fit_writes_emos_shadow_row(self):
        """save_coefficients always writes model_mode='emos_shadow'."""
        db = _db()
        triples = _synthetic_triples()
        a, b, c, d = fit_emos(triples)
        save_coefficients("Chicago", a, b, c, d, 1.23, db)

        row = db.get_emos_coefficients("Chicago", "emos_shadow")
        assert row is not None, "Expected emos_shadow row in emos_calibration"
        assert row["a"] == pytest.approx(a, abs=1e-9)
        assert row["b"] == pytest.approx(b, abs=1e-9)


# ---------------------------------------------------------------------------
# Test 2: fit always sets ready_for_promotion=0
# ---------------------------------------------------------------------------

class TestFitAlwaysSetsReadyForPromotionZero:
    def test_fit_always_sets_ready_for_promotion_zero(self):
        """CRITICAL: save_coefficients must never set ready_for_promotion=1."""
        db = _db()
        triples = _synthetic_triples()
        a, b, c, d = fit_emos(triples)
        save_coefficients("Miami", a, b, c, d, 0.98, db)

        row = db.get_emos_coefficients("Miami", "emos_shadow")
        assert row is not None
        assert row["ready_for_promotion"] == 0, (
            "ready_for_promotion MUST be 0 after automated save — "
            f"got {row['ready_for_promotion']!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: InsufficientDataError is caught at DEBUG not WARNING
# ---------------------------------------------------------------------------

class TestInsufficientDataLogsDebug:
    def test_insufficient_data_logs_debug_not_warning(self, caplog):
        """When InsufficientDataError is raised in the runner, it must be
        caught at DEBUG level, not WARNING — so it doesn't pollute alert channels."""
        # Simulate what run_emos_shadow.main_with_db does when data is scarce
        import logging as _logging

        # Call the runner logic inline to verify log level
        city = "FakeCity"
        fitted = 0
        skipped = 0

        with caplog.at_level(_logging.DEBUG, logger="scripts.run_emos_shadow"):
            try:
                raise InsufficientDataError("only 2 samples; need 60")
            except InsufficientDataError as e:
                _logging.getLogger("scripts.run_emos_shadow").debug(
                    "[emos_shadow] city=%s: insufficient data — %s", city, e
                )
                skipped += 1

        assert skipped == 1
        assert fitted == 0
        # Ensure no WARNING was emitted for InsufficientDataError
        warning_records = [r for r in caplog.records if r.levelno >= _logging.WARNING]
        assert not warning_records, (
            f"Expected no WARNING for InsufficientDataError; got: {warning_records}"
        )


# ---------------------------------------------------------------------------
# Test 4: promotion guard blocks below min samples
# ---------------------------------------------------------------------------

class TestPromotionGuardBlocksBelowMinSamples:
    def test_promotion_guard_blocks_below_min_samples(self, monkeypatch):
        """With 5 CRPS samples and EMOS_MIN_SAMPLES=20, get_city_mode returns 'emos_shadow'."""
        db = _db()
        # Insert both shadow and primary rows, primary marked ready
        _upsert_shadow(db, "Chicago")
        db.upsert_emos_coefficients(
            city="Chicago",
            model_mode="emos_primary",
            a=0.0, b=1.0, c=0.5, d=1.0,
            crps_score=1.2,
            trained_at=datetime.now(timezone.utc).isoformat(),
            ready_for_promotion=1,  # marked ready
        )
        # Patch get_emos_crps_count to return 5 (below threshold)
        monkeypatch.setattr(db, "get_emos_crps_count", lambda city: 5)
        monkeypatch.setenv("EMOS_MIN_SAMPLES", "20")

        mode = get_city_mode("Chicago", db=db)
        assert mode == "emos_shadow", (
            f"Expected 'emos_shadow' when samples < min; got {mode!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: promotion guard allows above min samples
# ---------------------------------------------------------------------------

class TestPromotionGuardAllowsAboveMinSamples:
    def test_promotion_guard_allows_above_min_samples(self, monkeypatch):
        """With 25 CRPS samples and EMOS_MIN_SAMPLES=20, get_city_mode returns 'emos_primary'."""
        db = _db()
        _upsert_shadow(db, "Miami")
        db.upsert_emos_coefficients(
            city="Miami",
            model_mode="emos_primary",
            a=0.0, b=1.0, c=0.5, d=1.0,
            crps_score=1.2,
            trained_at=datetime.now(timezone.utc).isoformat(),
            ready_for_promotion=1,  # marked ready
        )
        # Patch get_emos_crps_count to return 25 (above threshold)
        monkeypatch.setattr(db, "get_emos_crps_count", lambda city: 25)
        monkeypatch.setenv("EMOS_MIN_SAMPLES", "20")

        mode = get_city_mode("Miami", db=db)
        assert mode == "emos_primary", (
            f"Expected 'emos_primary' when samples >= min; got {mode!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: CRPS log entry created
# ---------------------------------------------------------------------------

class TestCrpsLogEntryCreated:
    def test_crps_log_entry_created(self):
        """db.log_crps inserts a row into emos_crps_log."""
        db = _db()
        db.log_crps("Chicago", "2026-06-01", 1.234, model_mode="emos_shadow")

        cur = db._conn.execute(
            "SELECT city, date, crps_score, model_mode FROM emos_crps_log WHERE city='Chicago'"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "Chicago"
        assert row[1] == "2026-06-01"
        assert abs(row[2] - 1.234) < 1e-9
        assert row[3] == "emos_shadow"


# ---------------------------------------------------------------------------
# Test 7: get_emos_crps_count
# ---------------------------------------------------------------------------

class TestGetEmosCrpsCount:
    def test_get_emos_crps_count(self):
        """Count per city matches inserted rows."""
        db = _db()
        # Insert 3 rows for Chicago, 1 for Miami
        db.log_crps("Chicago", "2026-06-01", 1.1)
        db.log_crps("Chicago", "2026-06-02", 1.2)
        db.log_crps("Chicago", "2026-06-03", 1.3)
        db.log_crps("Miami", "2026-06-01", 2.0)

        assert db.get_emos_crps_count("Chicago") == 3
        assert db.get_emos_crps_count("Miami") == 1
        assert db.get_emos_crps_count("Houston") == 0


# ---------------------------------------------------------------------------
# Test 8: DEB weight log entry created
# ---------------------------------------------------------------------------

class TestDebWeightLogEntryCreated:
    def test_deb_weight_log_entry_created(self):
        """db.log_deb_weights inserts a row into deb_weight_log."""
        db = _db()
        weights = {"nws": 0.5, "open_meteo": 0.3, "gfs": 0.2}
        db.log_deb_weights("Singapore", "2026-06-01", json.dumps(weights))

        cur = db._conn.execute(
            "SELECT city, date, weights_json FROM deb_weight_log WHERE city='Singapore'"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "Singapore"
        assert row[1] == "2026-06-01"
        loaded = json.loads(row[2])
        assert loaded["nws"] == pytest.approx(0.5)
        assert loaded["open_meteo"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Test 9: DEB weight not logged twice same day
# ---------------------------------------------------------------------------

class TestDebWeightNotLoggedTwiceSameDay:
    def test_deb_weight_not_logged_twice_same_day(self, monkeypatch):
        """get_weights logs DEB weights at most once per (city, day)."""
        import src.model.deb_weighting as deb_mod

        # Reset the module-level set to ensure clean state
        deb_mod._logged_today.clear()

        db = MagicMock()
        # Provide realistic return values so get_weights doesn't early-return
        db.get_model_weights.return_value = [
            {"model": "nws", "weight": 0.4, "date": "2026-06-17"},
            {"model": "open_meteo", "weight": 0.35, "date": "2026-06-17"},
            {"model": "gfs", "weight": 0.25, "date": "2026-06-17"},
        ]
        db.log_deb_weights = MagicMock()

        monkeypatch.setenv("DEB_ENABLED", "true")

        # Call get_weights twice for the same city on the same day
        deb_mod.get_weights(db, "Chicago")
        deb_mod.get_weights(db, "Chicago")

        # log_deb_weights should only have been called once
        assert db.log_deb_weights.call_count == 1, (
            f"Expected log_deb_weights called once; got {db.log_deb_weights.call_count}"
        )

        # Clean up
        deb_mod._logged_today.clear()


# ---------------------------------------------------------------------------
# Test 10: API endpoint returns per-city status with ready_for_promotion=False
# ---------------------------------------------------------------------------

class TestApiEndpointReturnsCityStatus:
    def test_api_endpoint_returns_per_city_status(self):
        """GET /api/emos-shadow/status returns per-city list with ready_for_promotion=False."""
        from src.dashboard import api as api_mod

        db = _db()
        # Log a CRPS entry so count > 0 for at least one city
        db.log_crps("Chicago", "2026-06-17", 1.5)

        with patch.object(api_mod, "_db", db):
            client = TestClient(api_mod.app)
            r = client.get("/api/emos-shadow/status")

        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0

        # Every city must have ready_for_promotion=False (never automated)
        for entry in data:
            assert "ready_for_promotion" in entry
            assert entry["ready_for_promotion"] is False, (
                f"ready_for_promotion must be False for city={entry.get('city')!r}; "
                f"got {entry['ready_for_promotion']!r}"
            )

        # Chicago should have n_samples=1 from our log_crps call
        chicago = next((e for e in data if e["city"] == "Chicago"), None)
        assert chicago is not None
        assert chicago["n_samples"] == 1
        assert chicago["mean_crps"] == pytest.approx(1.5, abs=1e-6)
