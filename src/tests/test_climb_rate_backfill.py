"""Tests for --from-db climb-rate backfill: DB method and computation logic.

Issue #587 regression suite: the previous version of these tests re-implemented
the binning math inline instead of calling compute_from_db(), so two production
bugs (daily high computed per hour-cell → within-hour spread instead of climb;
binning by UTC hour while the consumer indexes by local hour) shipped with
green tests. Everything below drives the REAL compute_from_db() against a real
on-disk Database with a known diurnal profile, for one UTC-negative station
(KORD, America/Chicago) and one UTC+9 station (RJTT, Asia/Tokyo) whose local
afternoon straddles the UTC date boundary.
"""
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Ensure project root is importable from the test runner's perspective
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.data.db import Database
from scripts.build_climb_lookup import compute_from_db, MIN_DAYS_PER_CELL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(path: str) -> Database:
    return Database(path)


def _insert_obs(db: Database, station: str, ts: str, temp_f: float) -> None:
    db.insert_observation(
        ts=ts,
        station=station,
        temp_f=temp_f,
        temp_native=temp_f,
        unit="F",
        source="metar",
    )


# Deterministic diurnal profile in LOCAL time: rises 1°F/h to a 75.0°F peak at
# 15:00 local, then falls 1°F/h. Remaining climb from local hour h is therefore
# exactly (75.0 - profile[h]) for h <= 15 and 0.0 after the peak.
def _profile(local_hour: int) -> float:
    if local_hour <= 15:
        return 60.0 + local_hour
    return 75.0 - (local_hour - 15)


def _seed_diurnal_days(
    db: Database, station: str, tz: str, n_days: int,
    month: int = 6, year: int = 2026, naive_ts: bool = False,
    flat: "float | None" = None,
) -> None:
    """Insert hourly observations following _profile for n_days local dates.

    Timestamps are stored in UTC (aware ISO by default; naive when naive_ts).
    When flat is set, every hour gets that constant temperature instead.
    """
    tzinfo = ZoneInfo(tz)
    for day in range(1, n_days + 1):
        for hour in range(24):
            local_dt = datetime(year, month, day, hour, 0, tzinfo=tzinfo)
            utc_dt = local_dt.astimezone(timezone.utc)
            ts = utc_dt.replace(tzinfo=None).isoformat() if naive_ts else utc_dt.isoformat()
            temp = flat if flat is not None else _profile(hour)
            _insert_obs(db, station, ts, temp)


def _synthetic_baseline(value: float = 99.0) -> "dict[int, dict[int, float]]":
    return {m: {h: value for h in range(24)} for m in range(1, 13)}


def _run(db_path: str, stations_subset: "list[str]", baseline_value: float = 99.0):
    """Run the real compute_from_db with an existing synthetic baseline."""
    existing = {icao: _synthetic_baseline(baseline_value) for icao in stations_subset}
    return compute_from_db(db_path, existing)


# ---------------------------------------------------------------------------
# 1. DB method returns raw timestamps (issue #587: no UTC truncation)
# ---------------------------------------------------------------------------

class TestGetHourlyObsForClimbSchema:
    def test_returns_ts_and_temp_f(self, tmp_path):
        db = _db(str(tmp_path / "t.db"))
        _insert_obs(db, "KORD", "2026-06-15T14:00:00+00:00", 82.0)
        rows = db.get_hourly_obs_for_climb("KORD")
        assert len(rows) == 1
        assert rows[0]["ts"] == "2026-06-15T14:00:00+00:00"
        assert rows[0]["temp_f"] == pytest.approx(82.0)
        assert isinstance(rows[0]["temp_f"], float)

    def test_empty_when_no_obs(self, tmp_path):
        db = _db(str(tmp_path / "t.db"))
        assert db.get_hourly_obs_for_climb("ZZZZ") == []

    def test_filters_by_station(self, tmp_path):
        db = _db(str(tmp_path / "t.db"))
        _insert_obs(db, "KORD", "2026-06-15T14:00:00+00:00", 82.0)
        _insert_obs(db, "KMIA", "2026-06-15T14:00:00+00:00", 91.0)
        rows = db.get_hourly_obs_for_climb("KORD")
        assert len(rows) == 1
        assert rows[0]["temp_f"] == pytest.approx(82.0)

    def test_ordered_by_ts(self, tmp_path):
        db = _db(str(tmp_path / "t.db"))
        _insert_obs(db, "KORD", "2026-06-16T10:00:00+00:00", 75.0)
        _insert_obs(db, "KORD", "2026-06-15T08:00:00+00:00", 70.0)
        rows = db.get_hourly_obs_for_climb("KORD")
        assert rows[0]["ts"].startswith("2026-06-15")
        assert rows[1]["ts"].startswith("2026-06-16")


# ---------------------------------------------------------------------------
# 1b. compute_from_db canonical-feed routing (issue #669): high-cadence city
# feeds must be folded in, not just the METAR-keyed ICAO rows.
# ---------------------------------------------------------------------------

class TestComputeFromDbCanonicalFeedRouting:
    """RKSI/RKPK have a 24/7 AMOS feed persisted under the city name ("Seoul",
    "Busan"), independent of STATION_ACTIVE_HOURS. Before #669,
    compute_from_db() only ever called get_hourly_obs_for_climb(icao) -- the
    ICAO-keyed METAR rows -- and silently ignored that denser, always-on city
    feed, exactly the same class of bug #558 already fixed for
    get_daily_obs_high(). compute_from_db() must now union every DB key
    config.get_canonical_station_feeds(icao) returns."""

    def test_seoul_amos_feed_is_folded_into_rksi_table(self, tmp_path):
        """A city-keyed (AMOS) observation for "Seoul" must count toward RKSI's
        climb table even though it's never stored under the ICAO code "RKSI"."""
        path = str(tmp_path / "seoul.db")
        db = _db(path)
        # RKSI's active window starts at local 11:00 (see STATION_ACTIVE_HOURS),
        # so only afternoon/evening METAR is ever persisted under "RKSI" in
        # production. Simulate that gap: seed METAR for hours 11-23 only under
        # "RKSI", but seed the FULL 24h diurnal profile under "Seoul" (AMOS,
        # which runs 24/7 regardless of active hours).
        tzinfo = ZoneInfo("Asia/Seoul")
        for day in range(1, 13):
            for hour in range(24):
                local_dt = datetime(2026, 6, day, hour, 0, tzinfo=tzinfo)
                utc_dt = local_dt.astimezone(timezone.utc)
                ts = utc_dt.isoformat()
                temp = _profile(hour)
                db.insert_observation(
                    ts=ts, station="Seoul", temp_f=temp, temp_native=temp,
                    unit="C", source="amos", cadence_min=15, is_official=1,
                )
                if hour >= 11:
                    _insert_obs(db, "RKSI", ts, temp)

        lookup, sources = _run(path, ["RKSI"])

        # Without the Seoul-keyed union, hour-6 (before RKSI's 11:00 active
        # window) would have zero DB observations and stay synthetic (99.0).
        # With the union, the AMOS-sourced "Seoul" rows cover hour 6 too.
        assert lookup["RKSI"][6][6] == pytest.approx(75.0 - _profile(6), abs=0.01)
        assert "DB observations" in sources["RKSI"]

    def test_metar_only_station_is_unaffected(self, tmp_path):
        """KORD has no city-keyed high-cadence feed -- behaviour is unchanged,
        and an unrelated city-keyed row for a different station must not leak in."""
        path = str(tmp_path / "kord.db")
        db = _db(path)
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=12)
        # Unrelated city-keyed row (Singapore/MSS) must not affect KORD's table.
        db.insert_observation(
            ts="2026-06-15T14:00:00+00:00", station="Singapore", temp_f=900.0,
            temp_native=482.0, unit="C", source="mss", cadence_min=1, is_official=1,
        )

        lookup, _ = _run(path, ["KORD"])

        june = lookup["KORD"][6]
        assert june[6] == pytest.approx(75.0 - _profile(6), abs=0.01)  # unaffected by Singapore row


# ---------------------------------------------------------------------------
# 2. compute_from_db recovers a known diurnal curve (both #587 bugs regress here)
# ---------------------------------------------------------------------------

class TestComputeFromDbRecoversKnownCurve:
    """The builder must recover remaining-climb = peak - profile[hour], binned
    by LOCAL hour. The old per-hour-cell daily high yields 0.0 everywhere
    (one obs per hour per day → no within-hour spread), and old UTC binning
    shifts RJTT cells by 9 hours — either bug fails these assertions."""

    def test_us_station_local_binning(self, tmp_path):
        path = str(tmp_path / "us.db")
        db = _db(path)
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=12)
        lookup, sources = _run(path, ["KORD"])

        june = lookup["KORD"][6]
        assert june[6] == pytest.approx(75.0 - _profile(6), abs=0.01)   # 9.0
        assert june[15] == pytest.approx(0.0, abs=0.01)                 # at the peak
        assert june[20] == pytest.approx(5.0, abs=0.01)
        assert "DB observations" in sources["KORD"]

    def test_utc_plus_9_station_crosses_utc_date_boundary(self, tmp_path):
        """Tokyo's local morning is the previous UTC date — local-date daily
        highs and local-hour binning must both survive the boundary."""
        path = str(tmp_path / "jp.db")
        db = _db(path)
        _seed_diurnal_days(db, "RJTT", "Asia/Tokyo", n_days=12, naive_ts=True)
        lookup, _ = _run(path, ["RJTT"])

        june = lookup["RJTT"][6]
        assert june[6] == pytest.approx(9.0, abs=0.01)
        assert june[15] == pytest.approx(0.0, abs=0.01)
        assert june[20] == pytest.approx(5.0, abs=0.01)

    def test_months_without_data_keep_synthetic(self, tmp_path):
        path = str(tmp_path / "us.db")
        db = _db(path)
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=12)
        lookup, _ = _run(path, ["KORD"], baseline_value=99.0)
        # No observations in January → entire month stays synthetic
        assert all(v == 99.0 for v in lookup["KORD"][1].values())


# ---------------------------------------------------------------------------
# 3. Sparse-data gate
# ---------------------------------------------------------------------------

class TestSparseDataGate:
    def test_below_threshold_keeps_synthetic(self, tmp_path):
        path = str(tmp_path / "sparse.db")
        db = _db(path)
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=MIN_DAYS_PER_CELL - 1)
        lookup, _ = _run(path, ["KORD"], baseline_value=99.0)
        assert all(v == 99.0 for v in lookup["KORD"][6].values())

    def test_at_threshold_is_db_derived(self, tmp_path):
        path = str(tmp_path / "atgate.db")
        db = _db(path)
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=MIN_DAYS_PER_CELL)
        lookup, _ = _run(path, ["KORD"], baseline_value=99.0)
        assert lookup["KORD"][6][6] == pytest.approx(9.0, abs=0.01)


# ---------------------------------------------------------------------------
# 4. Plausibility guard (issue #587 acceptance criterion)
# ---------------------------------------------------------------------------

class TestPlausibilityGuard:
    def test_cratered_month_triggers_warning(self, tmp_path, caplog):
        """A DB-derived June that collapses vs synthetic May/July must warn —
        the exact signature of the bug this suite guards against."""
        path = str(tmp_path / "flat.db")
        db = _db(path)
        # Flat temperature all day → every delta is 0.0 → June hour-6 = 0.0,
        # while the synthetic baseline for May/July hour-6 stays at 99.0.
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=12, flat=70.0)
        with caplog.at_level(logging.WARNING):
            _run(path, ["KORD"], baseline_value=99.0)
        assert any("PLAUSIBILITY" in r.getMessage() for r in caplog.records)

    def test_healthy_curve_does_not_warn(self, tmp_path, caplog):
        path = str(tmp_path / "ok.db")
        db = _db(path)
        _seed_diurnal_days(db, "KORD", "America/Chicago", n_days=12)
        with caplog.at_level(logging.WARNING):
            _run(path, ["KORD"], baseline_value=12.0)  # neighbours ~same scale
        assert not any("PLAUSIBILITY" in r.getMessage() for r in caplog.records)
