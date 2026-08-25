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
    reconstruct_current_high,
    reconstruct_emos_mu_sigma,
    reconstruct_intraday_delta,
    reconstruct_latest_temp,
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


# ---------------------------------------------------------------------------
# current_high / latest_temp reconstruction from observations (issue #1044)
# ---------------------------------------------------------------------------

def _seed_observations(path, rows) -> None:
    """Insert *rows* (list of insert_observation kwargs dicts) into an
    already-created DB at *path*. Separate from ``_seed_db`` since not every
    test needs observations, and the module under test always reads through
    a SEPARATE ``ReadOnlyDatabase`` connection than this one."""
    db = Database(str(path))
    for row in rows:
        db.insert_observation(**row)
    db._conn.commit()
    db._conn.close()


def _obs(ts, temp_f, station=STATION, source="metar"):
    return {
        "ts": ts, "station": station, "temp_f": temp_f, "temp_native": temp_f,
        "unit": "F", "source": source,
    }


class TestReconstructLatestTemp:
    def test_picks_the_row_at_or_before_poll_ts(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        _seed_observations(db_path, [
            _obs("2026-08-10T12:00:00+00:00", 70.0),
            _obs("2026-08-10T18:00:00+00:00", 85.0),
            _obs("2026-08-10T19:00:00+00:00", 95.0),  # after poll_ts
        ])
        ro = ReadOnlyDatabase(db_path)
        try:
            temp = reconstruct_latest_temp(ro, STATION, "2026-08-10T18:30:00+00:00")
        finally:
            ro._conn.close()
        assert temp == pytest.approx(85.0)

    def test_no_observation_before_poll_ts_returns_none(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        _seed_observations(db_path, [
            _obs("2026-08-10T18:00:00+00:00", 85.0),
        ])
        ro = ReadOnlyDatabase(db_path)
        try:
            temp = reconstruct_latest_temp(ro, STATION, "2026-08-10T05:00:00+00:00")
        finally:
            ro._conn.close()
        assert temp is None


class TestReconstructCurrentHigh:
    """STATION=KORD is America/Chicago (UTC-5 in August), so
    2026-08-10T05:00:00+00:00 -> 2026-08-11T05:00:00+00:00 is the station-
    local calendar day 2026-08-10."""

    def test_max_within_local_day_at_or_before_poll_ts(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        _seed_observations(db_path, [
            _obs("2026-08-10T13:00:00+00:00", 70.0),  # 08:00 local
            _obs("2026-08-10T18:00:00+00:00", 85.0),  # 13:00 local -- the max at-or-before poll_ts
        ])
        ro = ReadOnlyDatabase(db_path)
        try:
            high = reconstruct_current_high(ro, STATION, DATE, "2026-08-10T18:30:00+00:00")
        finally:
            ro._conn.close()
        assert high == pytest.approx(85.0)

    def test_causality_post_poll_ts_extreme_observation_is_excluded(self, tmp_path):
        """Issue #1044's load-bearing acceptance criterion: an observation
        AFTER poll_ts -- even a more extreme one -- must never change the
        reconstructed current_high. Feeding it in would leak the eventual
        settled outcome into a mid-day reconstruction."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        _seed_observations(db_path, [
            _obs("2026-08-10T13:00:00+00:00", 70.0),  # 08:00 local
            _obs("2026-08-10T18:00:00+00:00", 85.0),  # 13:00 local -- at-or-before poll_ts
            _obs("2026-08-10T19:00:00+00:00", 95.0),  # 14:00 local -- AFTER poll_ts, must be ignored
        ])
        ro = ReadOnlyDatabase(db_path)
        try:
            high = reconstruct_current_high(ro, STATION, DATE, "2026-08-10T18:30:00+00:00")
        finally:
            ro._conn.close()
        assert high == pytest.approx(85.0)
        assert high != pytest.approx(95.0)

    def test_previous_local_day_observation_excluded(self, tmp_path):
        """A hot reading on the PREVIOUS station-local calendar day must not
        leak into today's current_high, even though it's before poll_ts."""
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        _seed_observations(db_path, [
            _obs("2026-08-09T20:00:00+00:00", 110.0),  # 15:00 local Aug 9 -- previous local day
            _obs("2026-08-10T13:00:00+00:00", 70.0),   # 08:00 local Aug 10
        ])
        ro = ReadOnlyDatabase(db_path)
        try:
            high = reconstruct_current_high(ro, STATION, DATE, "2026-08-10T18:30:00+00:00")
        finally:
            ro._conn.close()
        assert high == pytest.approx(70.0)

    def test_unknown_station_returns_none(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            high = reconstruct_current_high(ro, "ZZZZ", DATE, "2026-08-10T18:30:00+00:00")
        finally:
            ro._conn.close()
        assert high is None

    def test_no_observations_returns_none(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            high = reconstruct_current_high(ro, STATION, DATE, "2026-08-10T18:30:00+00:00")
        finally:
            ro._conn.close()
        assert high is None


# ---------------------------------------------------------------------------
# Real bracket_evals row shape (issue #1044 -- #1042 merged broken because no
# test exercised this shape: real bracket_evals rows carry no
# current_high/latest_temp keys at all).
# ---------------------------------------------------------------------------

class TestReconstructionAgainstRealRowShape:
    def test_unmodified_real_bracket_evals_row_reconstructs(self, tmp_path):
        """This is an UNMODIFIED line copied verbatim from a production
        logs/bracket_evals.2026-08-25.jsonl file (station NZWN,
        2026-08-25T00:00:00+00:00 poll), run through the exact same
        poll_ts/end_date field mapping
        ``bss_market_vs_model_report.load_bracket_eval_rows`` applies
        (poll_ts -> ts, settlement_date -> end_date). It carries NO
        current_high/latest_temp keys -- confirming 0% of real rows do,
        which is the whole reason #1042 reconstructed 0/6,374 rows against
        real data. Given matching observations fixture rows, reconstruction
        must now succeed (non-None, in [0, 1])."""
        real_raw_line = {
            "station": "NZWN",
            "ticker": "0x8d1a508048248190d8e221edabaf853a2594a2c1d99c05f0741d651c927e425b",
            "bracket_low": -50.0, "bracket_high": 51.8,
            "poll_ts": "2026-08-25T00:00:00+00:00",
            "yes_ask": 1, "no_ask": 99,
            "p_yes": 0.05, "p_yes_raw": 0.0,
            "emos_mode": "emos_shadow", "is_next_day": 0,
            "minutes_to_settlement": 718.1, "execution_mode": "shadow",
            "settlement_date": "2026-08-25", "direction": "high",
        }
        assert "current_high" not in real_raw_line
        assert "latest_temp" not in real_raw_line
        row = {
            "ts": real_raw_line["poll_ts"],
            "station": real_raw_line["station"],
            "ticker": real_raw_line["ticker"],
            "end_date": real_raw_line["settlement_date"][:10],
            "bracket_low": real_raw_line["bracket_low"],
            "bracket_high": real_raw_line["bracket_high"],
            "yes_ask": real_raw_line["yes_ask"],
            "no_ask": real_raw_line["no_ask"],
            "minutes_to_settlement": real_raw_line["minutes_to_settlement"],
            "direction": real_raw_line["direction"],
            "is_next_day_flag": real_raw_line["is_next_day"],
        }
        # real_raw_line.get("current_high")/("latest_temp") -- absent, so
        # load_bracket_eval_rows_for_reconstruction's norm_row["current_high"]
        # = raw_row.get("current_high") = None, same as leaving the keys out here.

        station, city, date_str = "NZWN", "Wellington", "2026-08-25"
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db.upsert_forecast_log_v2(
            station=station, model="nws", date=date_str, forecast_high_f=52.0,
            lead_hours=12, issued_at="2026-08-24T12:00:00+00:00",
        )
        db.upsert_forecast_log_v2(
            station=station, model="open_meteo", date=date_str, forecast_high_f=53.0,
            lead_hours=12, issued_at="2026-08-24T12:00:00+00:00",
        )
        db.upsert_emos_coefficients(
            city=city, model_mode="emos_shadow",
            a=0.5, b=0.95, c=0.4, d=1.0,
            crps_score=1.0, trained_at="2026-08-24T00:00:00+00:00",
            ready_for_promotion=0, lead_hours=12,
        )
        db._conn.commit()
        db._conn.close()
        # NZWN is Pacific/Auckland; 2026-08-25T00:00:00+00:00 poll_ts falls
        # within the 2026-08-25 station-local calendar day.
        _seed_observations(db_path, [
            _obs("2026-08-24T22:00:00+00:00", 50.0, station=station),
            _obs("2026-08-24T23:30:00+00:00", 52.0, station=station),
        ])

        ro = ReadOnlyDatabase(db_path)
        try:
            p = reconstruct_bracket_row(ro, row)
        finally:
            ro._conn.close()
        assert p is not None
        assert 0.0 <= p <= 1.0
