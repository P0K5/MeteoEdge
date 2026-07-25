"""Tests for EMOS sigma-source retraining (issue #449).

Covers:
- fetch_training_data() sigma_source="fixed" vs "ensemble" (default)
- fetch_training_data() rejects an unknown sigma_source
- NaN/inf triples are dropped rather than corrupting the fit
- save_coefficients() threads sigma_source/lead_hours through to the DB layer
- check_ready_for_promotion() sigma_source filter
- upsert/get_emos_coefficients: sigma_source='ensemble' never overwrites the
  legacy sigma_source='fixed' row (same non-overwrite pattern as #659's
  forecast_source column)
- Migration: an emos_calibration table with the pre-#449 UNIQUE constraint
  (city, model_mode, forecast_source) gets widened to include sigma_source
  and lead_hours without losing existing rows.
"""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
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

def _make_db_with_rows(forecast_rows, obs_highs):
    db = MagicMock()
    db.get_forecast_log_by_lead.return_value = forecast_rows
    db.get_daily_obs_high.side_effect = lambda station, date_str: obs_highs.get(date_str)
    return db


def _forecast_row(date_str, forecast_high_f, model="nws", sigma_f=None):
    return {
        "date": date_str,
        "forecast_high_f": forecast_high_f,
        "model": model,
        "sigma_f": sigma_f,
    }


def _real_db():
    from src.data.db import Database
    tmp = tempfile.mktemp(suffix=".db")
    return Database(path=tmp), tmp


def _cleanup(db, path):
    try:
        db._conn.close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass  # Windows may still hold the file open; not worth failing the test over


# ---------------------------------------------------------------------------
# fetch_training_data — sigma_source axis
# ---------------------------------------------------------------------------

class TestFetchTrainingDataSigmaSource:
    def test_unknown_sigma_source_raises_value_error(self):
        db = _make_db_with_rows([], {})
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            with pytest.raises(ValueError, match="sigma_source must be one of"):
                fetch_training_data(
                    "Chicago", db, min_samples=1, forecast_source="nws",
                    sigma_source="bogus",
                )

    def test_default_sigma_source_is_ensemble_uses_persisted_sigma_f(self):
        """Default (unparametrised) call keeps pre-#449 behaviour: prefer sigma_f."""
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-05-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=4.2))
            obs[d] = 67.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data("Chicago", db, min_samples=30, forecast_source="nws")
        for _, sigma_f, _ in data:
            assert sigma_f == pytest.approx(4.2)

    def test_explicit_ensemble_matches_default(self):
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-06-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=3.1))
            obs[d] = 67.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data(
                "Chicago", db, min_samples=30, forecast_source="nws", sigma_source="ensemble",
            )
        for _, sigma_f, _ in data:
            assert sigma_f == pytest.approx(3.1)

    def test_fixed_ignores_persisted_sigma_f(self):
        """sigma_source='fixed' always uses FORECAST_STDDEV_F, even when sigma_f is populated."""
        from src.config import FORECAST_STDDEV_F
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-07-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=9.9))  # deliberately far from default
            obs[d] = 67.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data(
                "Chicago", db, min_samples=30, forecast_source="nws", sigma_source="fixed",
            )
        for _, sigma_f, _ in data:
            assert sigma_f == pytest.approx(float(FORECAST_STDDEV_F))
            assert sigma_f != pytest.approx(9.9)

    def test_fixed_and_ensemble_produce_different_training_data(self):
        """Same raw rows, different sigma_source -> different sigma_f in the triples."""
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-08-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=6.0))
            obs[d] = 67.0
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            fixed_data = fetch_training_data(
                "Chicago", db, min_samples=30, forecast_source="nws", sigma_source="fixed",
            )
            ensemble_data = fetch_training_data(
                "Chicago", db, min_samples=30, forecast_source="nws", sigma_source="ensemble",
            )
        assert fixed_data[0][1] != ensemble_data[0][1]
        assert ensemble_data[0][1] == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# fetch_training_data — NaN / inf handling (issue #449)
# ---------------------------------------------------------------------------

class TestFetchTrainingDataNaNHandling:
    def test_nan_mu_row_is_dropped(self):
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-09-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=3.0))
            obs[d] = 67.0
        # Corrupt one date's forecast_high_f with NaN
        rows[0] = _forecast_row("2025-09-01", float("nan"), model="nws", sigma_f=3.0)
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data("Chicago", db, min_samples=1, forecast_source="nws")
        assert len(data) == 29
        assert all(not math.isnan(mu) for mu, _, _ in data)

    def test_inf_sigma_row_is_dropped(self):
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-10-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=3.0))
            obs[d] = 67.0
        rows[5] = _forecast_row("2025-10-06", 65.0, model="nws", sigma_f=float("inf"))
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            data = fetch_training_data("Chicago", db, min_samples=1, forecast_source="nws")
        assert len(data) == 29
        assert all(not math.isinf(sigma) for _, sigma, _ in data)

    def test_nan_rows_count_toward_min_samples_shortfall(self):
        """Dropped NaN triples reduce the effective sample count for the min_samples gate."""
        rows, obs = [], {}
        for i in range(30):
            d = f"2025-11-{i + 1:02d}"
            rows.append(_forecast_row(d, 65.0, model="nws", sigma_f=3.0))
            obs[d] = 67.0
        # Corrupt 5 of the 30 rows -> only 25 clean triples remain
        for i in range(5):
            rows[i] = _forecast_row(f"2025-11-{i + 1:02d}", float("nan"), model="nws", sigma_f=3.0)
        db = _make_db_with_rows(rows, obs)
        with patch("src.config.STATIONS", [("KORD", 0, 0, "Chicago", "KORD", "F", "US/Central")]):
            with pytest.raises(InsufficientDataError, match="25"):
                fetch_training_data("Chicago", db, min_samples=30, forecast_source="nws")


# ---------------------------------------------------------------------------
# save_coefficients — sigma_source / lead_hours pass-through
# ---------------------------------------------------------------------------

class TestSaveCoefficientsSigmaSource:
    def test_default_sigma_source_and_lead_hours_passed_through(self):
        db = MagicMock()
        save_coefficients("Chicago", 0.1, 1.0, 0.5, 1.0, 2.5, db)
        call_kwargs = db.upsert_emos_coefficients.call_args[1]
        assert call_kwargs["sigma_source"] is None  # resolves via the DB layer's active config
        assert call_kwargs["lead_hours"] == 24

    def test_explicit_sigma_source_and_lead_hours_passed_through(self):
        db = MagicMock()
        save_coefficients(
            "Chicago", 0.2, 0.9, 0.6, 1.1, 2.3, db,
            forecast_source="nws", sigma_source="ensemble", lead_hours=6,
        )
        call_kwargs = db.upsert_emos_coefficients.call_args[1]
        assert call_kwargs["sigma_source"] == "ensemble"
        assert call_kwargs["lead_hours"] == 6


# ---------------------------------------------------------------------------
# check_ready_for_promotion — sigma_source filter
# ---------------------------------------------------------------------------

class TestCheckReadyForPromotionSigmaSource:
    def _rows(self, entries):
        return [
            {"city": c, "forecast_source": s, "sigma_source": sg, "ready_for_promotion": p}
            for c, s, sg, p in entries
        ]

    def test_no_sigma_source_filter_matches_pre_449_behaviour(self):
        """sigma_source=None (default) does not filter -- identical to pre-#449."""
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", "fixed", 1),
            ("Denver", "hrrr_nbm", "ensemble", 1),
        ])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Denver"]) is True

    def test_sigma_source_filter_excludes_wrong_track(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", "fixed", 1),
            ("Denver", "hrrr_nbm", "fixed", 1),
        ])
        assert check_ready_for_promotion(
            db, "hrrr_nbm", ["Chicago", "Denver"], sigma_source="ensemble",
        ) is False

    def test_sigma_source_filter_matches_correct_track(self):
        db = MagicMock()
        db.get_all_emos_calibration.return_value = self._rows([
            ("Chicago", "hrrr_nbm", "ensemble", 1),
            ("Denver", "hrrr_nbm", "ensemble", 1),
        ])
        assert check_ready_for_promotion(
            db, "hrrr_nbm", ["Chicago", "Denver"], sigma_source="ensemble",
        ) is True


# ---------------------------------------------------------------------------
# upsert/get_emos_coefficients — sigma_source non-overwrite guarantee (real DB)
# ---------------------------------------------------------------------------

class TestUpsertEmosCoefficientsSigmaSource:
    def test_ensemble_retrain_does_not_overwrite_fixed_legacy_row(self):
        db, path = _real_db()
        try:
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.0, b=1.0, c=0.5, d=1.0,
                forecast_source="nws_open_meteo", sigma_source="fixed",
            )
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow",
                a=0.5, b=0.9, c=0.6, d=1.2,
                forecast_source="nws_open_meteo", sigma_source="ensemble",
            )
            rows = db.get_all_emos_calibration()
            assert len(rows) == 2
            sources = {r["sigma_source"] for r in rows}
            assert sources == {"fixed", "ensemble"}
            fixed_row = db.get_emos_coefficients("Chicago", "emos_shadow", "nws_open_meteo", "fixed")
            ensemble_row = db.get_emos_coefficients("Chicago", "emos_shadow", "nws_open_meteo", "ensemble")
            assert fixed_row["a"] == pytest.approx(0.0)
            assert ensemble_row["a"] == pytest.approx(0.5)
        finally:
            _cleanup(db, path)

    def test_default_sigma_source_resolves_to_ensemble(self):
        """Issue #799: with no USE_ENSEMBLE_SIGMA config row, the active resolver
        defaults to 'ensemble' -- CONFIG_DEFAULTS["USE_ENSEMBLE_SIGMA"] flipped
        to True as part of switching ensemble sigma on."""
        db, path = _real_db()
        try:
            db.upsert_emos_coefficients(
                city="Miami", model_mode="emos_shadow", a=1.0, b=1.0, c=0.5, d=1.0,
            )
            rows = db.get_all_emos_calibration()
            assert rows[0]["sigma_source"] == "ensemble"
        finally:
            _cleanup(db, path)

    def test_use_ensemble_sigma_false_resolves_to_fixed(self):
        """USE_ENSEMBLE_SIGMA='false' in bot_config resolves the legacy 'fixed' track."""
        db, path = _real_db()
        try:
            db.set_config("USE_ENSEMBLE_SIGMA", "false")
            db.upsert_emos_coefficients(
                city="Miami", model_mode="emos_shadow", a=1.0, b=1.0, c=0.5, d=1.0,
            )
            rows = db.get_all_emos_calibration()
            assert rows[0]["sigma_source"] == "fixed"
        finally:
            _cleanup(db, path)

    def test_config_driven_active_sigma_source(self):
        """USE_ENSEMBLE_SIGMA (issue #799) -- not the now-legacy EMOS_SIGMA_SOURCE
        key -- is what changes the default sigma_source resolution."""
        db, path = _real_db()
        try:
            db.set_config("USE_ENSEMBLE_SIGMA", "true")
            db.upsert_emos_coefficients(
                city="Seattle", model_mode="emos_shadow", a=1.0, b=1.0, c=0.5, d=1.0,
            )
            rows = db.get_all_emos_calibration()
            assert rows[0]["sigma_source"] == "ensemble"
        finally:
            _cleanup(db, path)

    def test_emos_sigma_source_key_is_now_a_noop(self):
        """Issue #799: setting the legacy EMOS_SIGMA_SOURCE key alone must NOT
        change the resolved sigma_source -- only USE_ENSEMBLE_SIGMA does. This
        is the regression guard for the exact decoupling this issue closes."""
        db, path = _real_db()
        try:
            db.set_config("USE_ENSEMBLE_SIGMA", "false")
            db.set_config("EMOS_SIGMA_SOURCE", "ensemble")  # legacy key, ignored
            db.upsert_emos_coefficients(
                city="Denver", model_mode="emos_shadow", a=1.0, b=1.0, c=0.5, d=1.0,
            )
            rows = db.get_all_emos_calibration()
            assert rows[0]["sigma_source"] == "fixed"
        finally:
            _cleanup(db, path)

    def test_lead_hours_stored_independently(self):
        db, path = _real_db()
        try:
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow", a=1.0, b=1.0, c=0.5, d=1.0,
                forecast_source="nws", sigma_source="fixed", lead_hours=6,
            )
            db.upsert_emos_coefficients(
                city="Chicago", model_mode="emos_shadow", a=2.0, b=1.0, c=0.5, d=1.0,
                forecast_source="nws", sigma_source="fixed", lead_hours=24,
            )
            rows = db.get_all_emos_calibration()
            assert len(rows) == 2
            by_lead = db.get_emos_coefficients_by_lead("Chicago", "emos_shadow", "nws", "fixed")
            assert set(by_lead.keys()) == {6, 24}
            assert by_lead[6]["a"] == pytest.approx(1.0)
            assert by_lead[24]["a"] == pytest.approx(2.0)
        finally:
            _cleanup(db, path)


# ---------------------------------------------------------------------------
# Migration: pre-#449 emos_calibration (3-col UNIQUE) gets widened
# ---------------------------------------------------------------------------

class TestMigrationWidensUniqueConstraint:
    def test_old_3col_unique_db_migrates_and_preserves_rows(self):
        """A DB with the pre-#449 UNIQUE(city, model_mode, forecast_source) schema
        gets rebuilt to include sigma_source/lead_hours without losing data, and a
        sigma_source='ensemble' upsert afterward does not collide with the
        migrated legacy row (which lands at sigma_source='fixed')."""
        tmp = tempfile.mktemp(suffix=".db")
        try:
            conn = sqlite3.connect(tmp)
            conn.execute("""
                CREATE TABLE emos_calibration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    city TEXT NOT NULL,
                    model_mode TEXT NOT NULL,
                    forecast_source TEXT NOT NULL DEFAULT 'nws_open_meteo',
                    a REAL NOT NULL, b REAL NOT NULL,
                    c REAL NOT NULL, d REAL NOT NULL,
                    crps_score REAL,
                    ready_for_promotion INTEGER DEFAULT 0,
                    trained_at TEXT,
                    UNIQUE(city, model_mode, forecast_source)
                )
            """)
            conn.execute(
                "INSERT INTO emos_calibration (city, model_mode, forecast_source, a, b, c, d) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("Miami", "emos_primary", "hrrr_nbm", 0.1, 1.0, 0.5, 1.0),
            )
            conn.commit()
            conn.close()

            from src.data.db import Database
            db = Database(path=tmp)
            try:
                rows = db.get_all_emos_calibration()
                assert len(rows) == 1
                assert rows[0]["sigma_source"] == "fixed"
                assert rows[0]["lead_hours"] == 24

                # An ensemble retrain for the same (city, mode, forecast_source)
                # must land as an independent row, not replace the migrated one.
                db.upsert_emos_coefficients(
                    city="Miami", model_mode="emos_primary", forecast_source="hrrr_nbm",
                    a=0.9, b=0.7, c=0.3, d=1.3, sigma_source="ensemble",
                )
                rows = db.get_all_emos_calibration()
                assert len(rows) == 2
                legacy = db.get_emos_coefficients("Miami", "emos_primary", "hrrr_nbm", "fixed")
                new = db.get_emos_coefficients("Miami", "emos_primary", "hrrr_nbm", "ensemble")
                assert legacy["a"] == pytest.approx(0.1)
                assert new["a"] == pytest.approx(0.9)
            finally:
                _cleanup(db, tmp)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def test_old_2col_unique_db_migrates(self):
        """The oldest schema (pre-#659, city+model_mode only) also migrates cleanly
        straight to the #449/#665 schema in one pass."""
        tmp = tempfile.mktemp(suffix=".db")
        try:
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

            from src.data.db import Database
            db = Database(path=tmp)
            try:
                rows = db.get_all_emos_calibration()
                assert len(rows) == 1
                assert rows[0]["forecast_source"] == "nws_open_meteo"
                assert rows[0]["sigma_source"] == "fixed"
                assert rows[0]["lead_hours"] == 24
            finally:
                _cleanup(db, tmp)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
