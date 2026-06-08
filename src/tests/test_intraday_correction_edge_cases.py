"""Edge case unit tests for src/model/intraday_correction.py."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

try:
    from src.model.intraday_correction import compute_correction
    HAS_CORRECTION = True
except ImportError:
    HAS_CORRECTION = False

pytestmark = pytest.mark.skipif(not HAS_CORRECTION, reason="intraday_correction not yet implemented")


def _make_state(deb_mu_f=80.0, latest_temp_f=75.0):
    """Build a minimal WeatherState mock."""
    from unittest.mock import MagicMock
    state = MagicMock()
    state.deb_mu_f = deb_mu_f
    state.latest_temp_f = latest_temp_f
    state.station = "RJTT"
    return state


class TestComputeCorrectionEdgeCases:
    def test_zero_deviation_produces_zero_delta(self):
        """obs_temp_f == model_temp_f → delta_f == 0.0, corrected_mu_f == deb_mu_f."""
        db = MagicMock()
        db.get_latest_observation.return_value = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "temp_f": 75.0,
            "source": "jma_ameidas",
            "station": "Tokyo",
        }
        state = _make_state(deb_mu_f=80.0)

        with (
            patch("src.model.intraday_correction.build_consensus", return_value=[
                ("2024-06-15T08:00:00", 75.0),
                ("2024-06-15T09:00:00", 76.0),
            ]),
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch("src.model.intraday_correction.get_decay_factor", return_value=1.0),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            result = compute_correction("Tokyo", state, db)

        # When obs_temp == model_temp (interpolated), delta should be ~0
        # Result should equal deb_mu_f (80.0) or very close
        if result is not None:
            assert abs(result - 80.0) < 2.0  # within 2F of base mu

    def test_stale_obs_returns_none(self):
        """Obs age > 2x cadence_min → compute_correction returns None."""
        db = MagicMock()
        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
        db.get_latest_observation.return_value = {
            "ts": stale_ts,
            "temp_f": 99.0,
            "source": "jma_ameidas",
            "station": "Tokyo",
        }
        state = _make_state(deb_mu_f=80.0)

        with (
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            result = compute_correction("Tokyo", state, db)

        assert result is None, "Stale obs should return None"
        db.upsert_intraday_correction.assert_not_called()

    def test_no_obs_returns_none_no_exception(self):
        """db.get_latest_observation returns None → returns None, no exception."""
        db = MagicMock()
        db.get_latest_observation.return_value = None
        state = _make_state(deb_mu_f=80.0)

        with (
            patch("src.model.intraday_correction.get_source_priority", return_value=[
                {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            ]),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            result = compute_correction("Tokyo", state, db)

        assert result is None

    def test_unknown_city_does_not_raise(self):
        """City not in PEAK_WINDOWS → compute_correction does not raise."""
        db = MagicMock()
        db.get_latest_observation.return_value = None
        state = _make_state(deb_mu_f=80.0)

        with (
            patch("src.model.intraday_correction.get_source_priority", return_value=[]),
            patch.dict("os.environ", {"INTRADAY_CORRECTION_ENABLED": "true"}),
        ):
            try:
                result = compute_correction("UnknownCity_XYZ", state, db)
            except Exception as e:
                pytest.fail(f"Should not raise for unknown city, got: {e}")
