"""Tests for EMOS retrain tooling extensions (issue #463).

Covers:
- fetch_training_data() with forecast_source filter
- save_coefficients() with forecast_source parameter
- check_ready_for_promotion() gate function
- upsert_emos_coefficients() backward-compat default
- Migration: DB without forecast_source column gets it added
"""
from __future__ import annotations

import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, patch

import pytest

from src.model.emos_calibration import (
    InsufficientDataError,
    check_ready_for_promotion,
    fetch_training_data,
    fit_emos,
    save_coefficients,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_with_rows(
    forecast_rows: list[dict],
    obs_highs: dict[str, float | None],
    emos_rows: list[dict] | None = None,
):
    """Build a mock DB returning given forecast_rows and obs_highs."""
    db = MagicMock()
    db.get_forecast_log_by_lead.return_value = forecast_rows
    db.get_daily_obs_high.side_effect = lambda station, date_str: obs_highs.get(date_str)
    if emos_rows is not None:
        db.get_all_emos_calibration.return_value = emos_rows
    return db


def _forecast_row(date_str: str, forecast_high_f: float, model: str = "nws", sigma_f: float | None = None) -> dict:
    return {
        "date": date_str,
        "forecast_high_f": forecast_high_f,
        "model": model,
        "sigma_f": sigma_f,
    }


def _make_30_training_pairs():
    """30 varied forecast+obs pairs."""
    rows = []
    obs = {}
    for i in range(30):
        d = f"2025-01-{i+1:02d}"
        rows.append(_forecast_row(d, 60.0 + i * 0.5, model="nws"))
        obs[d] = 62.0 + i * 0.5
    return rows, obs


# ---------------------------------------------------------------------------
# fetch_training_data — forecast_source filter
# ---------------------------------------------------------------------------

class TestFetchTrainingDataForecastSource:
    def test_no_filter_includes_all_models(self):
        rows, obs = _make_30_training_pairs()
        # Add a second-model row on an existing date (day 1)
        rows.append(_forecast_row("2025-01-01", 61.0, model="hrrr_nbm"))
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data("Chicago", db, min_samples=30)
        assert len(data) == 30  # one per date

    def test_filter_by_forecast_source_excludes_other_models(self):
        rows = []
        obs = {}
        for i in range(30):
            d = f"2025-02-{i+1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws"))
            rows.append(_forecast_row(d, 66.0, model="hrrr_nbm"))
            obs[d] = 67.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data("Chicago", db, min_samples=30, forecast_source="hrrr_nbm")
        # All 30 dates should have hrrr_nbm rows; mu_f should be 66.0 (not average of nws+hrrr)
        assert len(data) == 30
        for mu_f, _, _ in data:
            assert mu_f == pytest.approx(66.0)

    def test_filter_raises_when_no_matching_source(self):
        rows, obs = _make_30_training_pairs()
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            with pytest.raises(InsufficientDataError, match="model_forecast_log is empty"):
                fetch_training_data("Chicago", db, min_samples=1, forecast_source="nonexistent_source")

    def test_filter_raises_insufficient_when_partial_match(self):
        # Only 5 rows match forecast_source, need 30
        rows = []
        obs = {}
        for i in range(5):
            d = f"2025-03-{i+1:02d}"
            rows.append(_forecast_row(d, 70.0, model="hrrr_nbm"))
            obs[d] = 72.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            with pytest.raises(InsufficientDataError, match="5"):
                fetch_training_data("Chicago", db, min_samples=30, forecast_source="hrrr_nbm")

    def test_sigma_f_from_row_used_when_available(self):
        rows = []
        obs = {}
        for i in range(30):
            d = f"2025-04-{i+1:02d}"
            rows.append(_forecast_row(d, 68.0, model="hrrr_nbm", sigma_f=3.5))
            obs[d] = 70.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data("Chicago", db, min_samples=30, forecast_source="hrrr_nbm")
        for _, sigma_f, _ in data:
            assert sigma_f == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# save_coefficients — forecast_source parameter
# ---------------------------------------------------------------------------

class TestSaveCoefficients:
    def test_saves_with_default_source(self):
        db = MagicMock()
        save_coefficients("Chicago", 0.1, 1.0, 0.5, 1.0, 2.5, db)
        call_kwargs = db.upsert_emos_coefficients.call_args[1]
        assert call_kwargs["forecast_source"] == "nws_open_meteo"
        assert call_kwargs["model_mode"] == "emos_shadow"
        assert call_kwargs["ready_for_promotion"] == 0

    def test_saves_with_explicit_source(self):
        db = MagicMock()
        save_coefficients("Chicago", 0.2, 0.9, 0.6, 1.1, 2.3, db, forecast_source="hrrr_nbm")
        call_kwargs = db.upsert_emos_coefficients.call_args[1]
        assert call_kwargs["forecast_source"] == "hrrr_nbm"

    def test_never_sets_ready_for_promotion(self):
        db = MagicMock()
        save_coefficients("Seattle", 0.0, 1.0, 0.5, 1.0, 3.0, db, forecast_source="hrrr_nbm")
        call_kwargs = db.upsert_emos_coefficients.call_args[1]
        assert call_kwargs["ready_for_promotion"] == 0

    def test_city_and_coefficients_passed_correctly(self):
        db = MagicMock()
        save_coefficients("Phoenix", 1.5, 0.95, 0.8, 1.2, 1.8, db, forecast_source="nws_open_meteo")
        call_kwargs = db.upsert_emos_coefficients.call_args[1]
        assert call_kwargs["city"] == "Phoenix"
        assert call_kwargs["a"] == pytest.approx(1.5)
        assert call_kwargs["b"] == pytest.approx(0.95)
        assert call_kwargs["c"] == pytest.approx(0.8)
        assert call_kwargs["d"] == pytest.approx(1.2)
        assert call_kwargs["crps_score"] == pytest.approx(1.8)


# ---------------------------------------------------------------------------
# check_ready_for_promotion
# ---------------------------------------------------------------------------

class TestCheckReadyForPromotion:
    def _rows(self, entries):
        """Build emos_calibration rows from (city, source, promoted) tuples."""
        return [
            {"city": c, "forecast_source": s, "ready_for_promotion": p}
            for c, s, p in entries
        ]

    def test_all_cities_promoted_returns_true(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", 1),
            ("Denver", "hrrr_nbm", 1),
        ])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Denver"]) is True

    def test_one_city_not_promoted_returns_false(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", 1),
            ("Denver", "hrrr_nbm", 0),
        ])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Denver"]) is False

    def test_city_missing_entirely_returns_false(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", 1),
        ])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Denver"]) is False

    def test_wrong_forecast_source_not_counted(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "nws_open_meteo", 1),
            ("Denver", "nws_open_meteo", 1),
        ])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Denver"]) is False

    def test_empty_cities_returns_false(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = []
        assert check_ready_for_promotion(db, "hrrr_nbm", []) is False

    def test_single_city_promoted(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", 1),
        ])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago"]) is True


# ---------------------------------------------------------------------------
# upsert_emos_coefficients — backward-compat via real SQLite DB
# ---------------------------------------------------------------------------

class TestUpsertEmosCoefficientsForecastSource:
    def _make_db(self):
        from src.data.db import Database
        tmp = tempfile.mktemp(suffix=".db")
        return Database(path=tmp), tmp

    def test_default_forecast_source_stored(self):
        db, path = self._make_db()
        try:
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.0, b=1.0, c=0.5, d=1.0,
                crps_score=2.0, trained_at="2025-01-01T00:00:00Z",
            )
            rows = db.get_all_emos_calibration()
            assert len(rows) == 1
            assert rows[0]["forecast_source"] == "nws_open_meteo"
        finally:
            os.unlink(path)

    def test_explicit_forecast_source_stored(self):
        db, path = self._make_db()
        try:
            db.upsert_emos_coefficients(
                city="Denver", model_mode="emos_shadow",
                a=0.1, b=0.9, c=0.6, d=1.1,
                forecast_source="hrrr_nbm",
            )
            rows = db.get_all_emos_calibration()
            assert len(rows) == 1
            assert rows[0]["forecast_source"] == "hrrr_nbm"
            assert rows[0]["city"] == "Denver"
        finally:
            os.unlink(path)

    def test_two_sources_for_same_city_independent(self):
        """Different forecast_source values coexist for the same (city, model_mode)."""
        db, path = self._make_db()
        try:
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.0, b=1.0, c=0.5, d=1.0, forecast_source="nws_open_meteo",
            )
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.2, b=0.95, c=0.6, d=1.1, forecast_source="hrrr_nbm",
            )
            rows = db.get_all_emos_calibration()
            assert len(rows) == 2
            sources = {r["forecast_source"] for r in rows}
            assert sources == {"nws_open_meteo", "hrrr_nbm"}
        finally:
            os.unlink(path)

    def test_upsert_replaces_same_key(self):
        """Second upsert for same (city, model_mode, forecast_source) updates the row."""
        db, path = self._make_db()
        try:
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.0, b=1.0, c=0.5, d=1.0, forecast_source="hrrr_nbm",
            )
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.5, b=0.8, c=0.4, d=1.2, forecast_source="hrrr_nbm",
            )
            rows = db.get_all_emos_calibration()
            assert len(rows) == 1
            assert rows[0]["a"] == pytest.approx(0.5)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Migration: existing DB without forecast_source gets it added
# ---------------------------------------------------------------------------

class TestMigrationAddsforecastSource:
    def test_old_db_gets_forecast_source_column(self):
        """A DB created without forecast_source column is migrated on open."""
        tmp = tempfile.mktemp(suffix=".db")
        try:
            # Create old-style DB manually (without forecast_source)
            conn = sqlite3.connect(tmp)
            conn.execute("""
                CREATE TABLE emos_calibration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    city TEXT NOT NULL,
                    model_mode TEXT NOT NULL,
                    a REAL NOT NULL, b REAL NOT NULL,
                    c REAL NOT NULL, d REAL NOT NULL,
                    crps_score REAL,
                    ready_for_promotion INTEGER DEFAULT 0,
                    trained_at TEXT,
                    UNIQUE(city, model_mode)
                )
            """)
            conn.execute(
                "INSERT INTO emos_calibration (city, model_mode, a, b, c, d) VALUES (?, ?, ?, ?, ?, ?)",
                ("Chicago", "emos_shadow", 0.1, 1.0, 0.5, 1.0),
            )
            conn.commit()
            conn.close()

            # Open via Database — migration should add forecast_source
            from src.data.db import Database
            db = Database(path=tmp)
            rows = db.get_all_emos_calibration()
            assert len(rows) == 1
            assert "forecast_source" in rows[0]
            assert rows[0]["forecast_source"] == "nws_open_meteo"
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# fit_emos — basic smoke test
# ---------------------------------------------------------------------------

class TestFitEmos:
    def test_fit_produces_valid_coefficients(self):
        """fit_emos should return (a, b, c, d) with c > 0 and d > 0."""
        data = [(60.0 + i, 3.0, 62.0 + i) for i in range(30)]
        a, b, c, d = fit_emos(data)
        assert isinstance(a, float)
        assert isinstance(b, float)
        assert c > 0
        assert d > 0

    def test_identity_transform_on_perfect_data(self):
        """With perfect forecast data, b ≈ 1 and a ≈ 0 (mean is already correct)."""
        data = [(float(t), 3.0, float(t)) for t in range(60, 90)]
        a, b, c, d = fit_emos(data)
        assert b == pytest.approx(1.0, abs=0.3)
