"""Issue #1238: a "database is locked" error inside one station's intraday
correction must not abort the whole scan.

Before this fix, `compute_correction()`'s write
(`db.upsert_intraday_correction`) could raise `sqlite3.OperationalError:
database is locked`, which propagated unhandled through `_build_one_station()`
and `build_weather_for_scanning()`'s per-station loop, aborting the scan for
every station after the unlucky one. `src/data/db.py`'s
`upsert_intraday_correction` now retries and swallows that specific error
internally (see `src/tests/test_db.py::TestUpsertIntradayCorrectionLockHandling`),
and `_build_one_station()` additionally catches it as defense-in-depth for the
OTHER db calls inside `compute_correction()`'s chain (e.g.
`refresh_weights()`'s `upsert_model_weight`). These tests cover that second,
builder-level layer directly.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.data.db import Database

KORD = ("KORD", 41.9742, -87.9073, "Chicago", "KORD", "F", "America/Chicago")
KMIA = ("KMIA", 25.7953, -80.2901, "Miami", "KMIA", "F", "America/New_York")

_FAKE_METAR = [{"temp": 30.0, "reportTime": "2026-06-24T02:00:00+00:00"}]


def _db() -> Database:
    return Database(":memory:")


def _patch_build_deps(*, compute_correction_patch):
    """Same dependency set as test_weather_builder_stack.py's
    `_patch_build_deps`, parameterised on the one function this module cares
    about (`compute_correction`) so each test can control its behaviour."""
    return [
        patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR),
        patch(
            "src.weather.builder.compute_daily_high",
            return_value=(86.0, datetime(2026, 6, 24, 18, 0, tzinfo=timezone.utc)),
        ),
        patch("src.weather.builder.fetch_nws_forecast_high", return_value=80.0),
        patch("src.weather.builder.fetch_secondary_forecast", return_value=84.0),
        patch("src.weather.builder.fetch_gfs_forecast_high", return_value=82.0),
        patch("src.weather.builder.fetch_hourly_temp_now", return_value=None),
        patch("src.weather.builder.compute_deb_mu_f", return_value=82.0),
        patch(
            "src.weather.builder.now_local",
            return_value=datetime(2026, 6, 24, 13, 0, tzinfo=timezone.utc),
        ),
        patch(
            "src.weather.builder.sunset_local",
            return_value=datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc),
        ),
        patch("src.weather.builder.get_canonical_station_feeds", side_effect=lambda s: [s]),
        patch("src.weather.builder.get_source_priority", return_value=[]),
        patch("src.weather.builder.refresh_weights"),
        patch(
            "src.weather.builder.get_weights",
            return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0},
        ),
        compute_correction_patch,
        patch("src.weather.builder.apply_residual_correction", side_effect=lambda city, mu, db: (mu, {})),
    ]


class TestBuildOneStationSurvivesLockedCorrectionWrite:
    def test_locked_correction_degrades_gracefully_instead_of_raising(self):
        """compute_correction() raising 'database is locked' must not raise
        out of _build_one_station() -- the station still builds, just without
        an intraday correction for this cycle."""
        from src.weather.builder import _build_one_station

        db = _db()
        patches = _patch_build_deps(
            compute_correction_patch=patch(
                "src.weather.builder.compute_correction",
                side_effect=sqlite3.OperationalError("database is locked"),
            )
        )
        for p in patches:
            p.start()
        try:
            state = _build_one_station(
                "KORD", 41.9742, -87.9073, "Chicago", unit="F", db=db
            )
        finally:
            for p in patches:
                p.stop()

        assert state is not None, (
            "a locked correction write must degrade this station, not drop it"
        )
        assert state.corrected_mu_f is None, (
            "no correction should be applied this cycle -- falls back to deb_mu_f"
        )

    def test_other_operational_errors_still_propagate(self):
        """Only 'database is locked' is caught -- any other OperationalError
        (e.g. a genuine schema problem) must still raise, not be silently
        swallowed as a degraded station."""
        from src.weather.builder import _build_one_station

        db = _db()
        patches = _patch_build_deps(
            compute_correction_patch=patch(
                "src.weather.builder.compute_correction",
                side_effect=sqlite3.OperationalError("no such table: bogus"),
            )
        )
        for p in patches:
            p.start()
        try:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                _build_one_station("KORD", 41.9742, -87.9073, "Chicago", unit="F", db=db)
        finally:
            for p in patches:
                p.stop()


class TestBuildWeatherForScanningContinuesPastOneLockedStation:
    """The actual acceptance criterion: a station whose correction write fails
    permanently does not abort the remaining stations -- the per-station loop
    in build_weather_for_scanning() completes."""

    def test_one_locked_station_does_not_abort_the_scan(self):
        from src.weather.builder import build_weather_for_scanning

        db = _db()

        def _flaky_compute_correction(city, state, db):
            if city == "Chicago":
                raise sqlite3.OperationalError("database is locked")
            return 82.0  # KMIA/Miami succeeds normally

        patches = _patch_build_deps(
            compute_correction_patch=patch(
                "src.weather.builder.compute_correction",
                side_effect=_flaky_compute_correction,
            )
        )
        patches.append(patch("src.weather.builder.STATIONS", [KORD, KMIA]))
        patches.append(
            patch("src.weather.builder.STATION_ACTIVE_HOURS", {"KORD": (0, 24), "KMIA": (0, 24)})
        )
        for p in patches:
            p.start()
        try:
            health_out: list = []
            weather = build_weather_for_scanning(
                stations=[KORD, KMIA], db=db, health_out=health_out
            )
        finally:
            for p in patches:
                p.stop()

        # Both stations present: KORD degraded (no correction), KMIA unaffected.
        assert set(weather.keys()) == {"KORD", "KMIA"}, (
            f"expected both stations to survive the scan, got {list(weather.keys())}"
        )
        assert weather["KORD"].corrected_mu_f is None
        assert weather["KMIA"].corrected_mu_f == pytest.approx(82.0)
