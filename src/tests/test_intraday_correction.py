"""Unit tests for src/model/intraday_correction.py."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.model.intraday_correction import (
    compute_correction,
    _interpolate_model_temp,
    _get_lat_lon,
    _get_station_region,
    _get_consensus_weights,
    _city_stations,
    _FALLBACK_WEIGHTS,
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
        # Consensus interpolates to ~75.9°F; use 83°F so delta (~7°F) is within
        # the 15°F plausibility gate and the decay=0 behavior can be tested.
        obs = _fresh_obs(temp_f=83.0)
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


class TestGetStationRegion:
    """Unit tests for the DEB region resolution used to pick the right DEB
    weight set (issue #572)."""

    def test_us_station_returns_us(self):
        # Chicago (KORD) is unit="F" in STATIONS.
        assert _get_station_region("Chicago") == "us"

    def test_eu_station_returns_eu(self):
        # London (EGLC) is unit="C" with an "E" ICAO prefix.
        assert _get_station_region("London") == "eu"

    def test_non_eu_international_station_returns_global(self):
        # Seoul (RKSI) is unit="C" with an "R" ICAO prefix (not EU).
        assert _get_station_region("Seoul") == "global"

    def test_unknown_city_returns_global(self):
        assert _get_station_region("UnknownCity_XYZ_99") == "global"


class TestGetConsensusWeights:
    """Unit tests for the DEB-weights basis resolution (issue #572).

    Covers: calibrated weights are used as-is; fallback to open_meteo-only
    when get_weights() is unavailable (raises or omits open_meteo).
    """

    def test_uses_live_calibrated_weights(self):
        """When DEB has calibrated weights, they are passed through unchanged."""
        db = MagicMock()
        calibrated = {"nws": 0.7, "open_meteo": 0.2, "gfs": 0.1}
        with patch(
            "src.model.intraday_correction.get_weights", return_value=calibrated
        ) as mock_get_weights:
            result = _get_consensus_weights("Chicago", db)

        assert result == calibrated
        mock_get_weights.assert_called_once_with(db, "Chicago", station_region="us")

    def test_passes_resolved_station_region(self):
        """station_region passed to get_weights() matches the city's DEB region."""
        db = MagicMock()
        with patch(
            "src.model.intraday_correction.get_weights",
            return_value={"open_meteo": 0.5, "ecmwf": 0.3, "icon": 0.2},
        ) as mock_get_weights:
            _get_consensus_weights("London", db)

        mock_get_weights.assert_called_once_with(db, "London", station_region="eu")

    def test_fallback_when_get_weights_raises(self):
        """get_weights() raising an exception falls back to the open_meteo-only basis."""
        db = MagicMock()
        with patch(
            "src.model.intraday_correction.get_weights",
            side_effect=RuntimeError("db unavailable"),
        ):
            result = _get_consensus_weights("Chicago", db)

        assert result == _FALLBACK_WEIGHTS

    def test_fallback_when_open_meteo_key_missing(self):
        """A weights dict without an 'open_meteo' key is treated as unavailable."""
        db = MagicMock()
        with patch(
            "src.model.intraday_correction.get_weights",
            return_value={"nws": 1.0},
        ):
            result = _get_consensus_weights("Chicago", db)

        assert result == _FALLBACK_WEIGHTS

    def test_deb_equal_weight_cold_start_is_not_a_fallback(self):
        """DEB's own equal-weight cold-start dict is a legitimate live basis —
        it is passed through as-is rather than overridden by the hardcoded
        fallback, since it reflects DEB's genuine current state."""
        db = MagicMock()
        equal_weights = {"nws": 1 / 3, "open_meteo": 1 / 3, "gfs": 1 / 3}
        with patch(
            "src.model.intraday_correction.get_weights", return_value=equal_weights
        ):
            result = _get_consensus_weights("Chicago", db)

        assert result == equal_weights


class TestComputeCorrectionBasisRegime:
    """Tests that compute_correction() feeds live DEB weights into
    build_consensus() and persists a basis snapshot for regime tagging
    (issue #572)."""

    def _patch_consensus(self, obs_dt: datetime) -> list:
        before_str = (obs_dt - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).isoformat()
        after_str = (obs_dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).isoformat()
        return [(before_str, 74.0), (after_str, 76.0)]

    def test_calibrated_weights_forwarded_to_build_consensus(self):
        """build_consensus() receives the live DEB weights, not a hardcoded basis."""
        db = MagicMock()
        obs = _fresh_obs(temp_f=77.0)
        state = _make_state(deb_mu_f=80.0)
        obs_dt = datetime.now(timezone.utc)
        consensus = self._patch_consensus(obs_dt)
        calibrated = {"nws": 0.1, "open_meteo": 0.6, "gfs": 0.3}

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=consensus) as mock_build,
            patch("src.model.intraday_correction.get_weights", return_value=calibrated),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=0.5),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            db.get_latest_observation.return_value = obs
            compute_correction("Tokyo", state, db)

        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs["weights"] == calibrated

    def test_basis_weights_snapshot_persisted_for_calibrated_regime(self):
        """The exact live weights used are persisted as a JSON snapshot so the
        residual layer can later segment regimes."""
        db = MagicMock()
        obs = _fresh_obs(temp_f=77.0)
        state = _make_state(deb_mu_f=80.0)
        obs_dt = datetime.now(timezone.utc)
        consensus = self._patch_consensus(obs_dt)
        calibrated = {"nws": 0.1, "open_meteo": 0.6, "gfs": 0.3}

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=consensus),
            patch("src.model.intraday_correction.get_weights", return_value=calibrated),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=0.5),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            db.get_latest_observation.return_value = obs
            compute_correction("Tokyo", state, db)

        kwargs = db.upsert_intraday_correction.call_args.kwargs
        assert json.loads(kwargs["basis_weights"]) == calibrated

    def test_basis_weights_snapshot_tags_fallback_regime(self):
        """When get_weights() is unavailable, the persisted basis snapshot is
        distinguishable from a calibrated-regime snapshot (fallback tag)."""
        db = MagicMock()
        obs = _fresh_obs(temp_f=77.0)
        state = _make_state(deb_mu_f=80.0)
        obs_dt = datetime.now(timezone.utc)
        consensus = self._patch_consensus(obs_dt)

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=consensus),
            patch(
                "src.model.intraday_correction.get_weights",
                side_effect=RuntimeError("db unavailable"),
            ),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=0.5),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            db.get_latest_observation.return_value = obs
            compute_correction("Tokyo", state, db)

        kwargs = db.upsert_intraday_correction.call_args.kwargs
        assert json.loads(kwargs["basis_weights"]) == _FALLBACK_WEIGHTS
        # Regime is distinguishable: fallback basis != a real calibrated basis.
        assert kwargs["basis_weights"] != json.dumps(
            {"nws": 0.1, "open_meteo": 0.6, "gfs": 0.3}, sort_keys=True
        )
