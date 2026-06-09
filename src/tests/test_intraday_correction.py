"""Unit tests for src/model/intraday_correction.py."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.model.intraday_correction import (
    compute_correction,
    _interpolate_model_temp,
    _get_lat_lon,
    _city_stations,
)


def _make_state(deb_mu_f: float = 80.0) -> MagicMock:
    state = MagicMock()
    state.deb_mu_f = deb_mu_f
    state.station = "RJTT"
    return state


def _fresh_obs(temp_f: float = 75.0, source: str = "jma_ameidas") -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "temp_f": temp_f,
        "source": source,
        "station": "Tokyo",
    }


_CONSENSUS = [
    ("2024-06-15T08:00:00+00:00", 70.0),
    ("2024-06-15T09:00:00+00:00", 76.0),
    ("2024-06-15T10:00:00+00:00", 80.0),
]


class TestInterpolateModelTemp:
    """Unit tests for the interpolation helper."""

    def test_exact_match(self):
        t = datetime(2024, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
        result = _interpolate_model_temp(_CONSENSUS, t)
        assert result == pytest.approx(76.0)

    def test_midpoint_interpolation(self):
        t = datetime(2024, 6, 15, 8, 30, 0, tzinfo=timezone.utc)
        result = _interpolate_model_temp(_CONSENSUS, t)
        assert result == pytest.approx(73.0)

    def test_before_range_returns_first(self):
        t = datetime(2024, 6, 15, 7, 0, 0, tzinfo=timezone.utc)
        result = _interpolate_model_temp(_CONSENSUS, t)
        assert result == pytest.approx(70.0)

    def test_empty_consensus_returns_none(self):
        t = datetime(2024, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
        assert _interpolate_model_temp([], t) is None


class TestGetLatLon:
    def test_known_city(self):
        result = _get_lat_lon("Seoul")
        assert result is not None
        lat, lon = result
        assert 35 < lat < 40
        assert 125 < lon < 130

    def test_unknown_city_returns_none(self):
        assert _get_lat_lon("UnknownCity_XYZ_99") is None


class TestCityStations:
    def test_seoul_has_station(self):
        stations = _city_stations("Seoul")
        assert "RKSI" in stations

    def test_unknown_city_empty(self):
        assert _city_stations("UnknownCity_XYZ_99") == []


class TestComputeCorrection:
    def _patch_consensus(self, obs_dt: datetime) -> list:
        before_str = (obs_dt - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).isoformat()
        after_str = (obs_dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).isoformat()
        return [(before_str, 74.0), (after_str, 76.0)]

    def test_returns_float_on_valid_inputs(self):
        db = MagicMock()
        obs = _fresh_obs(temp_f=77.0)
        state = _make_state(deb_mu_f=80.0)
        obs_dt = datetime.now(timezone.utc)
        consensus = self._patch_consensus(obs_dt)

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=consensus),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=0.5),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            db.get_latest_observation.return_value = obs
            result = compute_correction("Tokyo", state, db)

        assert isinstance(result, float)

    def test_disabled_env_returns_none(self):
        db = MagicMock()
        state = _make_state(deb_mu_f=80.0)
        with patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "false"}):
            result = compute_correction("Tokyo", state, db)
        assert result is None

    def test_no_deb_mu_f_returns_none(self):
        db = MagicMock()
        state = _make_state()
        state.deb_mu_f = None
        with patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}):
            result = compute_correction("Tokyo", state, db)
        assert result is None

    def test_upsert_called_on_success(self):
        db = MagicMock()
        obs = _fresh_obs(temp_f=77.0)
        state = _make_state(deb_mu_f=80.0)
        obs_dt = datetime.now(timezone.utc)
        consensus = self._patch_consensus(obs_dt)

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=consensus),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=0.8),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            db.get_latest_observation.return_value = obs
            compute_correction("Tokyo", state, db)

        db.upsert_intraday_correction.assert_called_once()
        kwargs = db.upsert_intraday_correction.call_args.kwargs
        assert kwargs["city"] == "Tokyo"
        assert kwargs["decay_factor"] == pytest.approx(0.8)

    def test_decay_zero_returns_deb_mu_f(self):
        """When decay=0.0 (inside peak window), corrected_mu_f == deb_mu_f."""
        db = MagicMock()
        obs = _fresh_obs(temp_f=99.0)  # large deviation but decay=0
        state = _make_state(deb_mu_f=80.0)
        obs_dt = datetime.now(timezone.utc)
        consensus = self._patch_consensus(obs_dt)

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=consensus),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=0.0),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            db.get_latest_observation.return_value = obs
            result = compute_correction("Tokyo", state, db)

        assert result == pytest.approx(80.0)
