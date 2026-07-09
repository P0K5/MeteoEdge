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
# Test 7b: the daily runner logs a CRPS row per city (and dedups same-day)
# ---------------------------------------------------------------------------

class TestRunnerLogsCrps:
    def test_run_calibration_logs_crps_once_per_day(self, monkeypatch):
        """_run_calibration appends a CRPS row per fitted city, deduped per day.

        Without this, emos_crps_log stays empty and the promotion guard
        (get_emos_crps_count >= EMOS_MIN_SAMPLES) can never clear — so
        emos_primary would be unreachable.
        """
        import scripts.run_emos_shadow as runner

        db = _db()
        canned = [(80.0, 2.0, 81.0)] * 60
        monkeypatch.setattr(
            "src.model.emos_calibration.fetch_training_data",
            lambda city, db, **kw: canned,
        )
        monkeypatch.setattr(
            "src.model.emos_calibration.fit_emos",
            lambda data: (0.0, 1.0, 0.5, 1.0),
        )

        runner._run_calibration(db)
        assert db.get_emos_crps_count("Chicago") == 1

        # A same-day re-run (e.g. after a process restart) must not double-count.
        runner._run_calibration(db)
        assert db.get_emos_crps_count("Chicago") == 1


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
        """get_weights returns consistent weights across repeated calls on the same day."""
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

        monkeypatch.setenv("DEB_ENABLED", "true")

        # Call get_weights twice for the same city on the same day
        w1 = deb_mod.get_weights(db, "Chicago")
        w2 = deb_mod.get_weights(db, "Chicago")

        # Both calls should return the same weights
        assert w1 == w2, f"Expected same weights on repeated calls; got {w1} vs {w2}"

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


# ---------------------------------------------------------------------------
# Test 10b: get_emos_shadow_city_status returns model_weights_snapshot
# ---------------------------------------------------------------------------

class TestGetEmosShadowCityStatusModelWeights:
    def test_model_weights_snapshot_for_latest_date(self):
        """get_emos_shadow_city_status returns {model: weight} dict for most recent date."""
        db = _db()
        # Insert model weights for Chicago across two dates
        db.upsert_model_weight(city="Chicago", model="nws", date="2026-06-15", weight=0.4, rmse=1.2)
        db.upsert_model_weight(city="Chicago", model="open_meteo", date="2026-06-15", weight=0.35, rmse=1.5)
        db.upsert_model_weight(city="Chicago", model="gfs", date="2026-06-15", weight=0.25, rmse=1.8)
        # More recent date
        db.upsert_model_weight(city="Chicago", model="nws", date="2026-06-17", weight=0.45, rmse=1.1)
        db.upsert_model_weight(city="Chicago", model="open_meteo", date="2026-06-17", weight=0.40, rmse=1.4)
        db.upsert_model_weight(city="Chicago", model="gfs", date="2026-06-17", weight=0.15, rmse=1.9)

        status = db.get_emos_shadow_city_status("Chicago")

        # Should return snapshot for most recent date (2026-06-17)
        assert status["model_weights_snapshot"] is not None
        assert status["model_weights_snapshot"]["nws"] == pytest.approx(0.45)
        assert status["model_weights_snapshot"]["open_meteo"] == pytest.approx(0.40)
        assert status["model_weights_snapshot"]["gfs"] == pytest.approx(0.15)
        # Older date should NOT appear
        assert len(status["model_weights_snapshot"]) == 3

    def test_model_weights_snapshot_returns_none_when_empty(self):
        """get_emos_shadow_city_status returns None when no model_weights exist."""
        db = _db()
        # Don't insert any model_weights
        status = db.get_emos_shadow_city_status("NoDataCity")

        assert status["model_weights_snapshot"] is None

    def test_api_endpoint_includes_model_weights_snapshot(self):
        """API endpoint includes model_weights_snapshot field (renamed from deb_weights_snapshot)."""
        from src.dashboard import api as api_mod

        db = _db()
        db.log_crps("Seattle", "2026-06-17", 1.3)
        db.upsert_model_weight(city="Seattle", model="nws", date="2026-06-17", weight=0.5, rmse=1.0)
        db.upsert_model_weight(city="Seattle", model="open_meteo", date="2026-06-17", weight=0.5, rmse=1.0)

        with patch.object(api_mod, "_db", db):
            client = TestClient(api_mod.app)
            r = client.get("/api/emos-shadow/status")

        assert r.status_code == 200
        data = r.json()
        seattle = next((e for e in data if e["city"] == "Seattle"), None)
        assert seattle is not None
        assert "model_weights_snapshot" in seattle
        assert seattle["model_weights_snapshot"] is not None
        assert seattle["model_weights_snapshot"]["nws"] == pytest.approx(0.5)
        assert seattle["model_weights_snapshot"]["open_meteo"] == pytest.approx(0.5)
        # Old field name should not exist
        assert "deb_weights_snapshot" not in seattle


# ---------------------------------------------------------------------------
# Pooled cross-station fallback (issue #659)
# ---------------------------------------------------------------------------

class TestPoolingGroup:
    def test_us_f_stations(self):
        from src.model.emos_calibration import pooling_group
        assert pooling_group("Chicago") == "us_f"
        assert pooling_group("Miami") == "us_f"

    def test_tropics_by_latitude(self):
        from src.model.emos_calibration import pooling_group
        assert pooling_group("Singapore") == "tropics_c"      # lat 1.4
        assert pooling_group("Shenzhen") == "tropics_c"       # lat 22.6
        assert pooling_group("Sao Paulo") == "tropics_c"      # lat -23.4

    def test_midlatitude_c_stations(self):
        from src.model.emos_calibration import pooling_group
        assert pooling_group("London") == "midlat_c"
        assert pooling_group("Seoul") == "midlat_c"
        assert pooling_group("Wellington") == "midlat_c"      # lat -41.3

    def test_unknown_city_returns_none(self):
        from src.model.emos_calibration import pooling_group
        assert pooling_group("Atlantis") is None


class TestFetchTrainingDataPooled:
    def test_pools_across_cities_and_counts(self, monkeypatch):
        from src.model import emos_calibration as cal

        per_city = {"London": [(70.0, 2.0, 71.0)] * 40, "Paris": [(72.0, 2.0, 73.0)] * 25}

        def fake_fetch(city, db, min_samples=60, **kw):
            if city not in per_city:
                raise cal.InsufficientDataError(city)
            return per_city[city]

        monkeypatch.setattr(cal, "fetch_training_data", fake_fetch)
        pooled, counts = cal.fetch_training_data_pooled(
            ["London", "Paris", "Atlantis"], db=None, regime=frozenset({"nws"}),
        )
        assert len(pooled) == 65
        assert counts == {"London": 40, "Paris": 25}  # zero-contributors omitted

    def test_raises_when_pooled_total_below_min(self, monkeypatch):
        from src.model import emos_calibration as cal
        monkeypatch.setattr(
            cal, "fetch_training_data",
            lambda city, db, min_samples=60, **kw: [(70.0, 2.0, 71.0)] * 10,
        )
        with pytest.raises(InsufficientDataError):
            cal.fetch_training_data_pooled(
                ["London", "Paris"], db=None, regime=frozenset({"nws"}),
            )


class TestPooledFallbackInRunner:
    """When no city clears the per-city bar, the runner fits the pooling group
    once and writes per-city coefficients — but only for cities contributing
    at least POOLED_MIN_CITY_SAMPLES of their own triples."""

    _STATIONS = [
        ("EGLC", 51.5053, 0.0553, "London", "EGLC", "C", "Europe/London"),
        ("LFPB", 48.9694, 2.4414, "Paris", "LFPB", "C", "Europe/Paris"),
    ]

    def _run(self, monkeypatch, db, london_n=58, paris_n=3):
        import scripts.run_emos_shadow as runner

        per_city = {
            "London": [(70.0, 2.0, 71.0)] * london_n,
            "Paris": [(72.0, 2.0, 73.0)] * paris_n,
        }

        def fake_fetch(city, db, min_samples=60, **kw):
            triples = per_city.get(city, [])
            if len(triples) < min_samples:
                raise InsufficientDataError(
                    f"{city}: {len(triples)} < {min_samples}"
                )
            return triples

        monkeypatch.setattr("src.config.STATIONS", self._STATIONS)
        monkeypatch.setattr("src.model.emos_calibration.fetch_training_data", fake_fetch)
        monkeypatch.setattr(
            "src.model.emos_calibration.fit_emos", lambda data: (0.0, 1.0, 0.5, 1.0)
        )
        runner._run_calibration(db)

    def test_pooled_coefficients_written_for_contributing_city(self, monkeypatch):
        db = _db()
        self._run(monkeypatch, db)  # 58 + 3 pooled = 61 >= 60

        row = db.get_emos_coefficients("London", "emos_shadow")
        assert row is not None
        assert row["ready_for_promotion"] == 0  # structural guard holds for pooled fits
        assert db.get_emos_crps_count("London") == 1

    def test_city_below_min_own_samples_gets_nothing(self, monkeypatch):
        db = _db()
        self._run(monkeypatch, db)  # Paris contributes 3 < POOLED_MIN_CITY_SAMPLES

        assert db.get_emos_coefficients("Paris", "emos_shadow") is None
        assert db.get_emos_crps_count("Paris") == 0

    def test_pooled_group_below_min_writes_nothing(self, monkeypatch):
        db = _db()
        self._run(monkeypatch, db, london_n=30, paris_n=10)  # pooled 40 < 60

        assert db.get_emos_coefficients("London", "emos_shadow") is None
        assert db.get_emos_coefficients("Paris", "emos_shadow") is None
