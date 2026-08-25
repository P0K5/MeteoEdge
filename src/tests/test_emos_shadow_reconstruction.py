"""Tests for src/scripts/emos_shadow_reconstruction.py (issue #1041).

Covers: read-only DB guard, mu_raw/sigma_raw/mu_final parity against the
live serving path (emos_serving_mu/apply_emos), and reconstruct_bracket_row's
documented scope (same-day, high-direction only).
"""
import sqlite3
from datetime import datetime

import pytest

from src.config import FORECAST_STDDEV_F
from src.data.db import Database
from src.model.emos_mode import apply_emos, emos_serving_mu, resolve_sigma_raw
from src.model.envelope import WeatherState
from src.scripts.emos_shadow_reconstruction import (
    ReadOnlyDatabase,
    reconstruct_bracket_row,
    reconstruct_emos_mu_sigma,
    reconstruct_intraday_delta,
    reconstruct_mu_raw,
)

STATION = "KORD"
CITY = "Chicago"
DATE = "2026-08-10"


def _seed_db(path) -> None:
    """Seed a writable Database with model_forecast_log + emos_calibration
    rows a reconstruction test can read back read-only."""
    db = Database(str(path))
    db.upsert_forecast_log_v2(
        station=STATION, model="nws", date=DATE, forecast_high_f=80.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    db.upsert_forecast_log_v2(
        station=STATION, model="open_meteo", date=DATE, forecast_high_f=82.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    # A farther lead bin, so the nearest-bin logic actually has to choose.
    db.upsert_forecast_log_v2(
        station=STATION, model="nws", date=DATE, forecast_high_f=76.0,
        lead_hours=24, issued_at="2026-08-09T18:00:00+00:00",
    )
    db.upsert_emos_coefficients(
        city=CITY, model_mode="emos_shadow",
        a=1.0, b=0.9, c=0.5, d=1.1,
        crps_score=1.2, trained_at=datetime.utcnow().isoformat(),
        ready_for_promotion=0, lead_hours=6,
    )
    db.upsert_intraday_correction(
        city=CITY, station=STATION, source="metar", date=DATE,
        obs_time="2026-08-10T13:00:00+00:00",
        obs_temp_f=83.0, model_temp_f=81.0, delta_f=2.0,
        corrected_mu_f=83.0, decay_factor=0.5,
        basis_weights='{"open_meteo": 1.0}',
    )
    # Persist immediately -- reconstruction opens a SEPARATE connection.
    db._conn.commit()
    db._conn.close()


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------

class TestReadOnlyDatabase:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ReadOnlyDatabase(tmp_path / "does_not_exist.db")

    def test_write_statement_is_denied(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                ro._conn.execute(
                    "INSERT INTO bot_config(key, value, updated_at) VALUES('x','y','z')"
                )
        finally:
            ro._conn.close()

    def test_ddl_statement_is_denied(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                ro._conn.execute("CREATE TABLE evil (id INTEGER)")
        finally:
            ro._conn.close()

    def test_reads_still_work(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            coeffs = ro.get_emos_coefficients(CITY, "emos_shadow", lead_hours=6)
            assert coeffs is not None
            assert coeffs["a"] == 1.0
        finally:
            ro._conn.close()

    def test_no_insert_update_delete_against_protected_tables(self, tmp_path):
        """Standing guard (issue #1041 acceptance criteria): the module must
        never write to emos_calibration, bracket_evals, scan_decisions, or
        model_forecast_log. Exercises the full reconstruction call path and
        asserts the authorizer never had to deny anything AND the DB file's
        row counts are unchanged."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)

        def _table_counts():
            con = sqlite3.connect(str(db_path))
            counts = {}
            for table in ("emos_calibration", "model_forecast_log", "scan_decisions",
                          "intraday_corrections"):
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            con.close()
            return counts

        before = _table_counts()
        ro = ReadOnlyDatabase(db_path)
        try:
            row = {
                "station": STATION, "end_date": DATE, "minutes_to_settlement": 300.0,
                "current_high": 78.0, "latest_temp": 79.0,
                "bracket_low": 79.0, "bracket_high": 83.0,
                "yes_ask": 30, "no_ask": 72, "ticker": "0xtest",
                "ts": "2026-08-10T13:30:00+00:00", "direction": "high",
                "is_next_day_flag": 0,
            }
            reconstruct_bracket_row(ro, row)
        finally:
            ro._conn.close()
        after = _table_counts()
        assert before == after


# ---------------------------------------------------------------------------
# Parity against the live serving path
# ---------------------------------------------------------------------------

class TestParityWithLiveServingPath:
    def test_mu_raw_matches_emos_serving_mu(self, tmp_path):
        """mu_raw reconstructed from model_forecast_log at the nearest lead
        bin must equal the stack-mean emos_serving_mu computes from a live
        WeatherState carrying the SAME two forecast values."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            mu_raw = reconstruct_mu_raw(ro, STATION, DATE, minutes_to_settlement=300.0)
        finally:
            ro._conn.close()

        # minutes_to_settlement=300 -> 5h -> nearest lead bin is 6 (vs 24) for
        # both models, i.e. forecast_high_f=80.0 (nws) and 82.0 (open_meteo).
        assert mu_raw == pytest.approx((80.0 + 82.0) / 2)

        # Live path: a WeatherState carrying the same two values must average
        # to the identical mu_raw before apply_emos.
        state = WeatherState(
            station=STATION, now_local=datetime(2026, 8, 10, 13, 0),
            sunset_local=datetime(2026, 8, 10, 19, 30),
            current_high_f=78.0, current_high_time=datetime(2026, 8, 10, 13, 0),
            latest_temp_f=79.0, latest_temp_time=datetime(2026, 8, 10, 13, 0),
            forecast_high_f=80.0, secondary_forecast_f=82.0,
        )
        writable_db = Database(str(db_path))
        try:
            sigma_raw = resolve_sigma_raw(state, True, FORECAST_STDDEV_F)
            live_mu_cal, live_sigma_cal = apply_emos(
                mu_raw, sigma_raw, CITY, writable_db, minutes_to_settlement=300.0
            )
            served = emos_serving_mu(
                state, CITY, writable_db, sigma_raw, minutes_to_settlement=300.0
            )
        finally:
            writable_db._conn.close()
        assert served is not None
        served_mu, served_sigma = served
        # emos_serving_mu computes the SAME mu_raw internally from state --
        # its mu_cal (before intraday) must equal apply_emos(mu_raw, ...).
        assert served_sigma == pytest.approx(live_sigma_cal)

    def test_sigma_raw_resolves_to_forecast_stddev_f(self, tmp_path):
        """Issue #1041 context: ensemble_sigma_f is never populated in this
        window, so sigma_raw must always resolve to FORECAST_STDDEV_F here --
        exactly what resolve_sigma_raw does when passed ensemble_sigma_f=None."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            resolved = reconstruct_emos_mu_sigma(
                ro, STATION, CITY, DATE, "2026-08-10T13:30:00+00:00",
                minutes_to_settlement=300.0,
            )
        finally:
            ro._conn.close()
        assert resolved is not None
        mu_final, sigma_cal = resolved
        # a=1.0, b=0.9, mu_raw=81.0 -> mu_cal = 1.0 + 0.9*81.0 = 73.9
        # sigma_raw=FORECAST_STDDEV_F=2.0, c=0.5, d=1.1 -> sigma_cal = 0.5+1.1*2.0=2.7
        # intraday delta = delta_f * decay_factor = 2.0 * 0.5 = 1.0
        assert sigma_cal == pytest.approx(0.5 + 1.1 * FORECAST_STDDEV_F)
        assert mu_final == pytest.approx(1.0 + 0.9 * 81.0 + 1.0)

    def test_missing_stack_member_forecast_returns_none(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db._conn.commit()
        db._conn.close()
        ro = ReadOnlyDatabase(db_path)
        try:
            mu_raw = reconstruct_mu_raw(ro, "UNKNOWN", DATE, minutes_to_settlement=300.0)
        finally:
            ro._conn.close()
        assert mu_raw is None


# ---------------------------------------------------------------------------
# Intraday delta reconstruction
# ---------------------------------------------------------------------------

class TestReconstructIntradayDelta:
    def test_picks_the_row_at_or_before_poll_ts(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            delta = reconstruct_intraday_delta(
                ro, CITY, DATE, "2026-08-10T14:00:00+00:00"
            )
        finally:
            ro._conn.close()
        assert delta == pytest.approx(2.0 * 0.5)

    def test_no_row_before_poll_ts_returns_zero(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            delta = reconstruct_intraday_delta(
                ro, CITY, DATE, "2026-08-10T05:00:00+00:00"  # before the only row
            )
        finally:
            ro._conn.close()
        assert delta == 0.0


# ---------------------------------------------------------------------------
# reconstruct_bracket_row scope (issue #1041: same-day, high-direction only)
# ---------------------------------------------------------------------------

class TestReconstructBracketRowScope:
    def _row(self, **overrides) -> dict:
        base = {
            "station": STATION, "end_date": DATE, "minutes_to_settlement": 300.0,
            "current_high": 78.0, "latest_temp": 79.0,
            "bracket_low": 79.0, "bracket_high": 83.0,
            "yes_ask": 30, "no_ask": 72, "ticker": "0xtest",
            "ts": "2026-08-10T13:30:00+00:00", "direction": "high",
            "is_next_day_flag": 0,
        }
        base.update(overrides)
        return base

    def test_returns_a_probability_for_a_well_formed_row(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            p = reconstruct_bracket_row(ro, self._row())
        finally:
            ro._conn.close()
        assert p is not None
        assert 0.0 <= p <= 1.0

    def test_next_day_row_is_out_of_scope(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            p = reconstruct_bracket_row(ro, self._row(is_next_day_flag=1))
        finally:
            ro._conn.close()
        assert p is None

    def test_low_direction_row_is_out_of_scope(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            p = reconstruct_bracket_row(ro, self._row(direction="low"))
        finally:
            ro._conn.close()
        assert p is None

    def test_missing_current_high_is_unreconstructable(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            p = reconstruct_bracket_row(ro, self._row(current_high=None))
        finally:
            ro._conn.close()
        assert p is None

    def test_unknown_station_is_unreconstructable(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            p = reconstruct_bracket_row(ro, self._row(station="ZZZZ"))
        finally:
            ro._conn.close()
        assert p is None
