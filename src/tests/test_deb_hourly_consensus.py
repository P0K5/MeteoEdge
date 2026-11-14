"""Unit tests for src/model/deb_hourly_consensus.py — no API calls."""
import contextlib
from datetime import datetime, timedelta, timezone
from math import isclose
from unittest.mock import patch

import pytest

from src.model.deb_hourly_consensus import build_consensus, compute_deb_mu_f


def _make_hourly_data(today_str: str) -> dict:
    """Build a minimal Open-Meteo-shaped hourly payload for testing."""
    return {
        "hourly": {
            "time": [
                f"{today_str}T00:00",
                f"{today_str}T01:00",
                f"{today_str}T12:00",
                "2000-01-01T00:00",   # a past day — must be filtered out
            ],
            "temperature_2m": [60.0, 62.0, 80.0, 55.0],
        }
    }


def _make_hourly_data_tz(utc_offset_seconds, hours_offset_list, temps, now_utc=None):
    """Build a mock Open-Meteo ``timezone=auto`` payload for a non-UTC station.

    Mirrors ``test_open_meteo.py``'s helper of the same name: ``hourly.time[]``
    entries are naive strings in the station's *local* time, and
    ``utc_offset_seconds`` (top-level) is the station's current UTC offset.
    ``hours_offset_list`` is relative to "now" expressed in that local time.
    ``now_utc`` defaults to the real current instant; pass a fixed value for
    deterministic UTC-day-boundary tests.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    local_now = (now_utc + timedelta(seconds=utc_offset_seconds)).replace(
        minute=0, second=0, microsecond=0
    )
    times = [(local_now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in hours_offset_list]
    return {
        "utc_offset_seconds": utc_offset_seconds,
        "hourly": {"time": times, "temperature_2m": temps},
    }


class _FrozenDatetime(datetime):
    """Pins ``datetime.now()`` for deterministic UTC-day-boundary tests."""

    _fixed: "datetime | None" = None

    @classmethod
    def now(cls, tz=None):
        assert cls._fixed is not None, "set _FrozenDatetime._fixed before use"
        return cls._fixed.astimezone(tz) if tz else cls._fixed


@contextlib.contextmanager
def _frozen_consensus_now(fixed: datetime):
    """Freeze ``src.model.deb_hourly_consensus``'s ``datetime.now(timezone.utc)`` reads."""
    _FrozenDatetime._fixed = fixed
    try:
        with patch("src.model.deb_hourly_consensus.datetime", _FrozenDatetime):
            yield
    finally:
        _FrozenDatetime._fixed = None


class TestDebConsensus:
    def test_build_consensus_returns_today_slots(self):
        """Only slots within the rolling window of "now" are returned; values
        are renormalized over hourly-capable sources (om_scale=1.0 here)."""
        today = datetime.now(timezone.utc).date().isoformat()
        mock_data = _make_hourly_data(today)
        weights = {"open_meteo": 0.4, "nws": 0.6}

        with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=mock_data):
            result = build_consensus(40.0, -74.0, weights)

        assert result is not None
        # Only today's 3 slots should be present (the "2000-01-01" slot is excluded)
        assert len(result) == 3
        time_strs = [r[0] for r in result]
        for t in time_strs:
            assert t[:10] == today, f"Non-today slot leaked through: {t}"

        # Values must be returned as-is (no weight scaling applied)
        raw_temps = [60.0, 62.0, 80.0]
        for (t_str, t_val), raw in zip(result, raw_temps):
            assert isclose(t_val, raw, abs_tol=1e-9), (
                f"Expected {raw}, got {t_val}"
            )

    @pytest.mark.parametrize("weights,label", [
        ({"nws": 0.5, "open_meteo": 0.5}, "baseline_us"),
        ({"open_meteo": 1.0}, "intl_om_only"),
        ({"nws": 0.15, "open_meteo": 0.25, "gfs": 0.15, "hrrr": 0.15, "nbm": 0.15, "ecmwf": 0.10, "icon": 0.05}, "full_7model"),
        ({"open_meteo": 0.4, "nws": 0.0}, "om_only_zero_nws"),
    ])
    def test_build_consensus_scale_invariant(self, weights, label):
        """Same input temps must produce same consensus output regardless of weight dict.

        Covers all four FORECAST_STACK shapes so a future stack promotion cannot
        silently re-break intraday corrections (prerequisite for #437).
        """
        today = datetime.now(timezone.utc).date().isoformat()
        mock_data = _make_hourly_data(today)
        raw_temps = [60.0, 62.0, 80.0]

        with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=mock_data):
            result = build_consensus(40.0, -74.0, weights)

        assert result is not None, f"[{label}] got None"
        assert len(result) == 3, f"[{label}] wrong slot count"
        for (t_str, t_val), raw in zip(result, raw_temps):
            assert isclose(t_val, raw, abs_tol=1e-9), (
                f"[{label}] weight scaling leaked into hourly path: "
                f"expected {raw}, got {t_val}. weights={weights}"
            )

    def test_build_consensus_none_on_no_data(self):
        """Returns None when _fetch_open_meteo_hourly returns None."""
        with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=None):
            result = build_consensus(40.0, -74.0, {"open_meteo": 0.4, "nws": 0.6})
        assert result is None

    def test_west_of_utc_station_converts_naive_local_to_true_utc(self):
        """Regression for issue #1525 (west-of-UTC, mirrors #810/#818's pattern).

        build_consensus() must convert Open-Meteo's naive-local timestamps to
        true UTC instants (local - offset = UTC) before returning them, not
        stamp them as UTC directly — otherwise the "current hour" slot is
        actually |offset| hours away from the real current instant.
        """
        data = _make_hourly_data_tz(-18000, [-1, 0, 1], [75.0, 77.0, 80.0])  # UTC-5 (Chicago)
        with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=data):
            result = build_consensus(41.98, -87.9, {"open_meteo": 1.0})

        assert result is not None
        # _make_hourly_data_tz floors local-now to the top of the hour, and
        # the offset is a whole number of hours, so converting back to UTC
        # must land exactly on the true current UTC hour.
        expected_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        by_val = {t_val: datetime.fromisoformat(t_str) for t_str, t_val in result}
        # The naive-local "current hour" slot (77.0) must map to the true
        # current UTC hour — not ~5h off, which the naive-as-UTC bug would
        # produce for a UTC-5 station.
        assert by_val[77.0] == expected_hour

    def test_east_of_utc_station_converts_naive_local_to_true_utc(self):
        """Regression for issue #1525 (east-of-UTC case, mirrors #810/#818's pattern)."""
        data = _make_hourly_data_tz(32400, [-1, 0, 1], [75.0, 77.0, 80.0])  # UTC+9 (Tokyo/Seoul)
        with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=data):
            result = build_consensus(35.68, 139.77, {"open_meteo": 1.0})

        assert result is not None
        expected_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        by_val = {t_val: datetime.fromisoformat(t_str) for t_str, t_val in result}
        assert by_val[77.0] == expected_hour

    def test_rolling_window_retains_slots_old_date_filter_would_drop(self):
        """Regression for issue #1525's second bug: comparing a local-labelled
        date string against "today in UTC" silently drops valid near-boundary
        hours for stations far from UTC. The fix is a rolling
        ``±_CONSENSUS_WINDOW_HOURS``-of-now-UTC window instead.

        Frozen UTC now is late in the UTC day; for a UTC+9 station the local
        date has already rolled over to the next day, so under the old
        ``t_str[:10] == today(UTC)`` filter every slot here (all
        local-date "tomorrow" relative to UTC) would have been dropped and
        build_consensus() would have returned None.
        """
        frozen_now = datetime(2026, 11, 14, 23, 30, 0, tzinfo=timezone.utc)
        utc_offset_seconds = 9 * 3600  # UTC+9 (Tokyo/Seoul)
        data = _make_hourly_data_tz(
            utc_offset_seconds, [-1, 0, 1], [75.0, 77.0, 80.0], now_utc=frozen_now
        )
        # Sanity-check the premise: every slot's local date differs from the
        # frozen UTC date — exactly what the old date-string filter compared.
        local_dates = {t[:10] for t in data["hourly"]["time"]}
        assert local_dates == {"2026-11-15"}
        assert frozen_now.date().isoformat() == "2026-11-14"

        with _frozen_consensus_now(frozen_now):
            with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=data):
                result = build_consensus(35.68, 139.77, {"open_meteo": 1.0})

        assert result is not None, (
            "rolling window should retain near-UTC-boundary local-tomorrow slots"
        )
        vals = {t_val for _, t_val in result}
        assert vals == {75.0, 77.0, 80.0}

    def test_compute_deb_mu_f_weighted_blend(self):
        """weights nws=0.6, open_meteo=0.4; forecasts nws=82, om=78 -> 0.6*82 + 0.4*78 = 80.4."""
        weights = {"nws": 0.6, "open_meteo": 0.4}
        result = compute_deb_mu_f(forecast_nws=82.0, forecast_open_meteo=78.0, weights=weights)
        assert result is not None
        assert isclose(result, 80.4, abs_tol=1e-9), f"Expected 80.4, got {result}"

    def test_compute_deb_mu_f_single_source_fallback(self):
        """Returns the available source at full weight when the other is None."""
        weights = {"nws": 0.6, "open_meteo": 0.4}
        # Non-US station: no NWS — use Open-Meteo directly
        assert compute_deb_mu_f(None, 78.0, weights) == 78.0
        # Open-Meteo unavailable — use NWS directly
        assert compute_deb_mu_f(82.0, None, weights) == 82.0

    def test_compute_deb_mu_f_none_on_both_missing(self):
        """Returns None only when both inputs are None."""
        weights = {"nws": 0.6, "open_meteo": 0.4}
        assert compute_deb_mu_f(None, None, weights) is None
