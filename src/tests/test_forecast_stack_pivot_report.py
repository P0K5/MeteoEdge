"""Tests for src/scripts/forecast_stack_pivot_report.py (issue #1057).

Mirrors test_sigma_lever_reconstruction_report.py's DB-fixture pattern: seed
a writable Database, then reconstruct read-only against it via a SEPARATE
ReadOnlyDatabase connection.
"""
import pytest

from src.data.db import Database
from src.model.ensemble_sigma import SIGMA_FLOOR_F
from src.scripts.emos_shadow_reconstruction import ReadOnlyDatabase
from src.scripts.forecast_stack_pivot_report import (
    VARIANT_ECMWF_ICON,
    VARIANT_HRRR_NBM,
    VARIANT_LEGACY_BASELINE,
    VARIANT_NBM_ALONE,
    reconstruct_bracket_row_mus,
    reconstruct_nbm_alone_raw,
    reconstruct_stack_mu_raw,
    select_best_mu_variant,
)
from src.config import FORECAST_STACK_MODELS

STATION = "KORD"
CITY = "Chicago"
DATE = "2026-08-10"


def _seed_db(path, *, with_hrrr_nbm: bool = True, with_ecmwf_icon: bool = True,
             with_gefs: bool = True) -> None:
    db = Database(str(path))
    db.upsert_forecast_log_v2(
        station=STATION, model="nws", date=DATE, forecast_high_f=80.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    db.upsert_forecast_log_v2(
        station=STATION, model="open_meteo", date=DATE, forecast_high_f=82.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    if with_hrrr_nbm:
        db.upsert_forecast_log_v2(
            station=STATION, model="hrrr", date=DATE, forecast_high_f=84.0,
            lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
        )
        db.upsert_forecast_log_v2(
            station=STATION, model="nbm", date=DATE, forecast_high_f=86.0,
            lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
        )
    if with_ecmwf_icon:
        db.upsert_forecast_log_v2(
            station=STATION, model="ecmwf", date=DATE, forecast_high_f=79.0,
            lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
        )
        db.upsert_forecast_log_v2(
            station=STATION, model="icon", date=DATE, forecast_high_f=81.0,
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


class TestReconstructStackMuRaw:
    def test_hrrr_nbm_stack_equal_weight_mean(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_stack_mu_raw(
                ro, STATION, DATE, minutes_to_settlement=360.0,
                stack_models=FORECAST_STACK_MODELS["hrrr_nbm"],
            )
        finally:
            ro._conn.close()
        # {nws, open_meteo, hrrr, nbm} = {80, 82, 84, 86} -> mean 83.0
        assert mu == pytest.approx((80.0 + 82.0 + 84.0 + 86.0) / 4)

    def test_intl_ecmwf_icon_stack_equal_weight_mean(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_stack_mu_raw(
                ro, STATION, DATE, minutes_to_settlement=360.0,
                stack_models=FORECAST_STACK_MODELS["intl_ecmwf_icon"],
            )
        finally:
            ro._conn.close()
        # {nws, open_meteo, ecmwf, icon} = {80, 82, 79, 81} -> mean 80.5
        assert mu == pytest.approx((80.0 + 82.0 + 79.0 + 81.0) / 4)

    def test_returns_none_when_no_stack_member_logged(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_hrrr_nbm=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_stack_mu_raw(
                ro, STATION, DATE, minutes_to_settlement=360.0,
                stack_models=frozenset({"hrrr", "nbm"}),
            )
        finally:
            ro._conn.close()
        assert mu is None

    def test_partial_stack_still_reconstructs_from_available_members(self, tmp_path):
        """A row missing one hypothetical-stack member still yields a mean
        of whichever members ARE logged -- the equal-weight construction
        does not require every member present."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_hrrr_nbm=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_stack_mu_raw(
                ro, STATION, DATE, minutes_to_settlement=360.0,
                stack_models=FORECAST_STACK_MODELS["hrrr_nbm"],
            )
        finally:
            ro._conn.close()
        # Only nws/open_meteo logged -> mean of those two.
        assert mu == pytest.approx((80.0 + 82.0) / 2)


class TestReconstructNbmAloneRaw:
    def test_returns_raw_nbm_value_unblended(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_nbm_alone_raw(ro, STATION, DATE, minutes_to_settlement=360.0)
        finally:
            ro._conn.close()
        assert mu == pytest.approx(86.0)

    def test_returns_none_when_no_nbm_logged(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_hrrr_nbm=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu = reconstruct_nbm_alone_raw(ro, STATION, DATE, minutes_to_settlement=360.0)
        finally:
            ro._conn.close()
        assert mu is None


class TestReconstructBracketRowMus:
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

    def test_all_four_mu_sources_and_sigma_variants_present(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_mus(ro, self._row())
        finally:
            ro._conn.close()
        assert result is not None
        mus = result["mus"]
        assert VARIANT_LEGACY_BASELINE in mus
        assert VARIANT_HRRR_NBM in mus
        assert VARIANT_ECMWF_ICON in mus
        assert VARIANT_NBM_ALONE in mus
        assert result["sigma_variants"] is not None
        assert result["sigma_variants"]["naive_floor"] == pytest.approx(max(0.6, SIGMA_FLOOR_F))

    def test_missing_nbm_drops_only_hrrr_nbm_and_nbm_alone_variants(self, tmp_path):
        """Issue #1057 acceptance criteria: a row missing inputs for one
        variant is dropped from that variant only."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_hrrr_nbm=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_mus(ro, self._row())
        finally:
            ro._conn.close()
        assert result is not None
        mus = result["mus"]
        assert VARIANT_LEGACY_BASELINE in mus
        assert VARIANT_ECMWF_ICON in mus
        assert VARIANT_NBM_ALONE not in mus
        # hrrr_nbm still reconstructs from the surviving nws/open_meteo members.
        assert VARIANT_HRRR_NBM in mus

    def test_no_gefs_log_drops_sigma_variants_but_not_mus(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path, with_gefs=False)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_mus(ro, self._row())
        finally:
            ro._conn.close()
        assert result is not None
        assert result["sigma_variants"] is None
        assert VARIANT_LEGACY_BASELINE in result["mus"]

    def test_none_when_no_mu_source_reconstructable(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db._conn.commit()
        db._conn.close()
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_mus(ro, self._row())
        finally:
            ro._conn.close()
        assert result is None

    def test_none_for_next_day_row(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_mus(ro, self._row(is_next_day_flag=1))
        finally:
            ro._conn.close()
        assert result is None

    def test_none_for_low_direction_row(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            result = reconstruct_bracket_row_mus(ro, self._row(direction="low"))
        finally:
            ro._conn.close()
        assert result is None


class TestSelectBestMuVariant:
    def test_picks_highest_bss(self):
        assert select_best_mu_variant({
            VARIANT_HRRR_NBM: -0.20,
            VARIANT_ECMWF_ICON: -0.05,
            VARIANT_NBM_ALONE: -0.30,
        }) == VARIANT_ECMWF_ICON

    def test_picks_positive_over_negative(self):
        assert select_best_mu_variant({
            VARIANT_HRRR_NBM: -0.01,
            VARIANT_ECMWF_ICON: 0.02,
            VARIANT_NBM_ALONE: None,
        }) == VARIANT_ECMWF_ICON

    def test_ignores_none_values(self):
        assert select_best_mu_variant({
            VARIANT_HRRR_NBM: None,
            VARIANT_ECMWF_ICON: -0.10,
            VARIANT_NBM_ALONE: None,
        }) == VARIANT_ECMWF_ICON

    def test_returns_none_when_all_none(self):
        assert select_best_mu_variant({
            VARIANT_HRRR_NBM: None,
            VARIANT_ECMWF_ICON: None,
            VARIANT_NBM_ALONE: None,
        }) is None

    def test_tie_broken_by_fixed_stack_order(self):
        """Equal BSS -- STACK_VARIANTS order (hrrr_nbm, intl_ecmwf_icon, nbm)
        decides, deterministically."""
        assert select_best_mu_variant({
            VARIANT_HRRR_NBM: -0.10,
            VARIANT_ECMWF_ICON: -0.10,
            VARIANT_NBM_ALONE: -0.10,
        }) == VARIANT_HRRR_NBM
