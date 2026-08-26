"""Tests for src/scripts/sigma_lever_reconstruction_report.py (issue #1048).

Mirrors test_emos_shadow_reconstruction.py's DB-fixture pattern: seed a
writable Database, then reconstruct read-only against it via a SEPARATE
ReadOnlyDatabase connection.
"""
from datetime import datetime

import pytest

from src.data.db import Database
from src.model.ensemble_sigma import SIGMA_FLOOR_F
from src.scripts.emos_shadow_reconstruction import ReadOnlyDatabase
from src.scripts.sigma_lever_reconstruction_report import (
    DAWN_COHORT_STATIONS,
    DAWN_MIN_MINUTES_TO_SETTLEMENT,
    VARIANT_BASELINE,
    VARIANT_CALIBRATED,
    VARIANT_NAIVE_FLOOR,
    is_dawn_cohort_row,
    reconstruct_bracket_row_variants,
    reconstruct_deb_mu_raw,
    reconstruct_mu_legacy,
    reconstruct_sigma_variants,
)

STATION = "KORD"
CITY = "Chicago"
DATE = "2026-08-10"


def _seed_db(path, *, with_gefs: bool = True) -> None:
    db = Database(str(path))
    db.upsert_forecast_log_v2(
        station=STATION, model="nws", date=DATE, forecast_high_f=80.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    db.upsert_forecast_log_v2(
        station=STATION, model="open_meteo", date=DATE, forecast_high_f=82.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    if with_gefs:
        db.upsert_forecast_log_v2(
            station=STATION, model="gefs", date=DATE, forecast_high_f=81.0,
            lead_hours=6, issued_at="2026-08-10T12:00:00+00:00", sigma_f=0.6,
        )
    db.upsert_intraday_correction(
        city=CITY, station=STATION, source="metar", date=DATE,
        obs_time="2026-08-10T13:00:00+00:00",
        obs_temp_f=83.0, model_temp_f=81.0, delta_f=2.0,
        corrected_mu_f=83.0, decay_factor=0.5,
        basis_weights='{"open_meteo": 1.0}',
    )
    db._conn.commit()
    db._conn.close()


class TestReconstructDebMuRaw:
    def test_equal_weight_blend_of_nws_and_open_meteo(self, tmp_path):
        """DEB_ENABLED is off by default -> get_weights returns equal weights
        for the region -> the blend is a plain average of nws/open_meteo
        (gfs excluded from the DEB registry per issue #761)."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu_raw = reconstruct_deb_mu_raw(ro, STATION, CITY, DATE, minutes_to_settlement=360.0)
        finally:
            ro._conn.close()
        assert mu_raw == pytest.approx((80.0 + 82.0) / 2)

    def test_returns_none_when_no_forecast_logged(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db._conn.commit()
        db._conn.close()
        ro = ReadOnlyDatabase(db_path)
        try:
            assert reconstruct_deb_mu_raw(ro, STATION, CITY, DATE, 360.0) is None
        finally:
            ro._conn.close()


class TestReconstructMuLegacy:
    def test_adds_persisted_intraday_delta_to_deb_blend(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_mu_legacy(
                ro, STATION, CITY, DATE, poll_ts="2026-08-10T13:30:00+00:00",
                minutes_to_settlement=360.0,
            )
        finally:
            ro._conn.close()
        # deb_mu_raw = 81.0 (equal-weight nws/open_meteo average) + delta_f*decay_factor = 2.0*0.5=1.0
        assert mu == pytest.approx(81.0 + 1.0)

    def test_returns_none_when_deb_blend_unreconstructable(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db._conn.commit()
        db._conn.close()
        ro = ReadOnlyDatabase(db_path)
        try:
            assert reconstruct_mu_legacy(
                ro, STATION, CITY, DATE, "2026-08-10T13:30:00+00:00", 360.0
            ) is None
        finally:
            ro._conn.close()


class TestReconstructSigmaVariants:
    def test_naive_floor_and_calibrated_fallback_without_history(self, tmp_path):
        """With fewer than MIN_CALIBRATION_SAMPLES historical pairs, the
        calibrated variant falls back to the same value as naive_floor."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            variants = reconstruct_sigma_variants(ro, STATION, DATE, minutes_to_settlement=360.0)
        finally:
            ro._conn.close()
        assert variants is not None
        assert variants["raw_member_sigma"] == pytest.approx(0.6)
        assert variants["naive_floor"] == pytest.approx(max(0.6, SIGMA_FLOOR_F))
        assert variants["calibrated"] == variants["naive_floor"]

    def test_returns_none_when_no_gefs_log(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_gefs=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            assert reconstruct_sigma_variants(ro, STATION, DATE, 360.0) is None
        finally:
            ro._conn.close()


class TestReconstructBracketRowVariants:
    def _row(self, **overrides):
        row = {
            "station": STATION, "end_date": DATE, "minutes_to_settlement": 360.0,
            "current_high": 78.0, "latest_temp": 79.0,
            "bracket_low": 79.0, "bracket_high": 83.0,
            "yes_ask": 30, "no_ask": 72, "ticker": "0xtest",
            "ts": "2026-08-10T13:30:00+00:00", "direction": "high",
            "is_next_day_flag": 0,
        }
        row.update(overrides)
        return row

    def test_all_three_variants_present_when_gefs_available(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_gefs=True)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_variants(ro, self._row())
        finally:
            ro._conn.close()
        assert result is not None
        assert VARIANT_BASELINE in result
        assert VARIANT_NAIVE_FLOOR in result
        assert VARIANT_CALIBRATED in result
        for p in result.values():
            assert 0.0 <= p <= 1.0

    def test_only_baseline_present_when_no_gefs_log(self, tmp_path):
        """Issue #1048 acceptance criteria: a missing gefs log drops
        naive_floor/calibrated but NOT the baseline variant."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_gefs=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_variants(ro, self._row())
        finally:
            ro._conn.close()
        assert result is not None
        assert VARIANT_BASELINE in result
        assert VARIANT_NAIVE_FLOOR not in result
        assert VARIANT_CALIBRATED not in result

    def test_none_when_mu_unreconstructable(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db._conn.commit()
        db._conn.close()
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_variants(ro, self._row())
        finally:
            ro._conn.close()
        assert result is None

    def test_none_for_next_day_row(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_variants(
                ro, self._row(is_next_day_flag=1)
            )
        finally:
            ro._conn.close()
        assert result is None

    def test_none_for_low_direction_row(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_variants(ro, self._row(direction="low"))
        finally:
            ro._conn.close()
        assert result is None


class TestDawnCohortProxy:
    def test_matching_station_and_far_lead_time_is_dawn(self):
        station = next(iter(DAWN_COHORT_STATIONS))
        row = {"station": station, "minutes_to_settlement": DAWN_MIN_MINUTES_TO_SETTLEMENT + 1}
        assert is_dawn_cohort_row(row) is True

    def test_non_dawn_station_is_not_dawn(self):
        row = {"station": "KMIA", "minutes_to_settlement": 10000.0}
        assert is_dawn_cohort_row(row) is False

    def test_dawn_station_close_to_settlement_is_not_dawn(self):
        station = next(iter(DAWN_COHORT_STATIONS))
        row = {"station": station, "minutes_to_settlement": 10.0}
        assert is_dawn_cohort_row(row) is False

    def test_missing_minutes_is_not_dawn(self):
        station = next(iter(DAWN_COHORT_STATIONS))
        row = {"station": station, "minutes_to_settlement": None}
        assert is_dawn_cohort_row(row) is False
