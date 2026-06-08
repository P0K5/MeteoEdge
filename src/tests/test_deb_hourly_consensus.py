"""Unit tests for src/model/deb_hourly_consensus.py — no API calls."""
from datetime import datetime, timezone
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


class TestDebConsensus:
    def test_build_consensus_returns_today_slots(self):
        """Only today's slots are returned; values are scaled by open_meteo weight."""
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

        # Values must be scaled by open_meteo weight (0.4)
        raw_temps = [60.0, 62.0, 80.0]
        for (t_str, t_val), raw in zip(result, raw_temps):
            assert isclose(t_val, raw * 0.4, abs_tol=1e-9), (
                f"Expected {raw * 0.4}, got {t_val}"
            )

    def test_build_consensus_none_on_no_data(self):
        """Returns None when _fetch_open_meteo_hourly returns None."""
        with patch("src.model.deb_hourly_consensus._fetch_open_meteo_hourly", return_value=None):
            result = build_consensus(40.0, -74.0, {"open_meteo": 0.4, "nws": 0.6})
        assert result is None

    def test_compute_deb_mu_f_weighted_blend(self):
        """weights nws=0.6, open_meteo=0.4; forecasts nws=82, om=78 -> 0.6*82 + 0.4*78 = 80.4."""
        weights = {"nws": 0.6, "open_meteo": 0.4}
        result = compute_deb_mu_f(forecast_nws=82.0, forecast_open_meteo=78.0, weights=weights)
        assert result is not None
        assert isclose(result, 80.4, abs_tol=1e-9), f"Expected 80.4, got {result}"

    def test_compute_deb_mu_f_none_on_missing_input(self):
        """Returns None when either forecast input is None."""
        weights = {"nws": 0.6, "open_meteo": 0.4}
        assert compute_deb_mu_f(None, 78.0, weights) is None
        assert compute_deb_mu_f(82.0, None, weights) is None
        assert compute_deb_mu_f(None, None, weights) is None
