"""Unit tests for src/model/deb_weighting.py — compute_weights function.

Tests the Dynamic Error Balancing (DEB) weight computation module
with mocked database calls.
"""
from unittest.mock import MagicMock

import pytest

from src.model.deb_weighting import (
    EQUAL_WEIGHTS,
    MIN_SAMPLES,
    compute_weights,
)


class TestDebWeighting:
    def test_equal_weight_fallback_when_insufficient_data(self):
        """When a model has fewer than MIN_SAMPLES entries, return EQUAL_WEIGHTS."""
        db = MagicMock()
        db.get_forecast_log.return_value = [
            {"date": "2024-01-01", "model": "nws", "forecast_high_f": 80.0},
            {"date": "2024-01-02", "model": "nws", "forecast_high_f": 81.0},
            {"date": "2024-01-03", "model": "nws", "forecast_high_f": 79.0},
            {"date": "2024-01-04", "model": "nws", "forecast_high_f": 82.0},
            {"date": "2024-01-05", "model": "nws", "forecast_high_f": 78.0},
        ]
        db.get_settlements.return_value = [
            {"ts": "2024-01-01T12:00:00", "actual_high_f": 80.1},
            {"ts": "2024-01-02T12:00:00", "actual_high_f": 81.2},
            {"ts": "2024-01-03T12:00:00", "actual_high_f": 79.3},
            {"ts": "2024-01-04T12:00:00", "actual_high_f": 82.1},
            {"ts": "2024-01-05T12:00:00", "actual_high_f": 78.2},
        ]

        weights, rmse = compute_weights(db, "WSSS", "Singapore")
        assert weights == EQUAL_WEIGHTS
        assert all(v == 0.0 for v in rmse.values())

    def test_weights_sum_to_one(self):
        """Computed weights must sum to exactly 1.0."""
        db = MagicMock()
        db.get_forecast_log.return_value = [
            {"date": f"2024-01-{i:02d}", "model": "nws", "forecast_high_f": 80.0 + i * 0.1}
            for i in range(1, 16)
        ] + [
            {"date": f"2024-01-{i:02d}", "model": "open_meteo", "forecast_high_f": 80.5 + i * 0.1}
            for i in range(1, 16)
        ]
        db.get_settlements.return_value = [
            {"ts": f"2024-01-{i:02d}T12:00:00", "actual_high_f": 80.2 + i * 0.05}
            for i in range(1, 16)
        ]

        weights, rmse = compute_weights(db, "KNYC", "New York")
        assert abs(sum(weights.values()) - 1.0) < 1e-9
        assert all(v > 0 for v in rmse.values())

    def test_lower_rmse_model_gets_higher_weight(self):
        """Model with lower RMSE (smaller errors) should get higher weight."""
        db = MagicMock()
        db.get_forecast_log.return_value = (
            [
                {"date": f"2024-01-{i:02d}", "model": "nws", "forecast_high_f": 80.1}
                for i in range(1, 16)
            ]
            + [
                {"date": f"2024-01-{i:02d}", "model": "open_meteo", "forecast_high_f": 85.0}
                for i in range(1, 16)
            ]
        )
        db.get_settlements.return_value = [
            {"ts": f"2024-01-{i:02d}T12:00:00", "actual_high_f": 80.0}
            for i in range(1, 16)
        ]

        weights, rmse = compute_weights(db, "KORD", "Chicago")
        assert weights["nws"] > weights["open_meteo"]
        assert rmse["nws"] < rmse["open_meteo"]

    def test_single_model_gets_weight_one(self):
        """When only one model has forecast rows (others are phantom), it receives
        weight=1.0 and the phantom models receive 0.0.

        This is the correct behaviour after the phantom-model guard was introduced:
        models with zero rows in the trailing window are excluded from the blend
        entirely and the remaining model(s) are renormalised to sum to 1.0.
        """
        db = MagicMock()
        db.get_forecast_log.return_value = [
            {"date": f"2024-01-{i:02d}", "model": "nws", "forecast_high_f": 80.0 + i * 0.1}
            for i in range(1, 16)
        ]
        db.get_settlements.return_value = [
            {"ts": f"2024-01-{i:02d}T12:00:00", "actual_high_f": 80.2 + i * 0.05}
            for i in range(1, 16)
        ]

        weights, rmse = compute_weights(db, "KLAX", "Los Angeles")
        # NWS is the only active model; phantom models (open_meteo, gfs) get 0.0.
        assert weights["nws"] == 1.0
        assert weights["open_meteo"] == 0.0
        assert weights["gfs"] == 0.0
        # Total must still sum to 1.0
        assert abs(sum(weights.values()) - 1.0) < 1e-9
        # RMSE dict only has the active model key(s); no entry for phantom models.
        assert "nws" in rmse
        assert rmse["nws"] > 0.0
