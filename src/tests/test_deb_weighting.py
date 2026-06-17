"""Unit tests for src/model/deb_weighting.py — compute_weights and check_weight_quality.

Tests the Dynamic Error Balancing (DEB) weight computation module
with mocked database calls.
"""
from unittest.mock import MagicMock

import pytest

from src.model.deb_weighting import (
    EQUAL_WEIGHTS,
    MIN_SAMPLES,
    compute_weights,
    check_weight_quality,
)


class TestDebWeighting:
    def test_equal_weight_fallback_when_insufficient_data(self):
        """When an active model has fewer than MIN_SAMPLES paired entries, return EQUAL_WEIGHTS."""
        db = MagicMock()
        # Both models have rows but fewer than MIN_SAMPLES paired with actuals
        db.get_forecast_log.return_value = [
            {"date": "2024-01-01", "model": "nws", "forecast_high_f": 80.0},
            {"date": "2024-01-02", "model": "nws", "forecast_high_f": 81.0},
            {"date": "2024-01-03", "model": "nws", "forecast_high_f": 79.0},
            {"date": "2024-01-04", "model": "nws", "forecast_high_f": 82.0},
            {"date": "2024-01-05", "model": "nws", "forecast_high_f": 78.0},
            {"date": "2024-01-01", "model": "open_meteo", "forecast_high_f": 80.5},
            {"date": "2024-01-02", "model": "open_meteo", "forecast_high_f": 81.5},
            {"date": "2024-01-03", "model": "open_meteo", "forecast_high_f": 79.5},
            {"date": "2024-01-04", "model": "open_meteo", "forecast_high_f": 82.5},
            {"date": "2024-01-05", "model": "open_meteo", "forecast_high_f": 78.5},
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
        assert all(v > 0 for m, v in rmse.items() if m != "gfs")

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

    def test_phantom_model_excluded_sole_active_gets_full_weight(self):
        """Phantom-NWS scenario: NWS has zero log rows, open_meteo has enough samples.

        The phantom model (nws) must be excluded and the sole active model
        (open_meteo) must receive weight=1.0 after renormalisation.
        This is the core fix for issue #306.
        """
        db = MagicMock()
        # Only open_meteo has log rows — NWS is absent (international station)
        db.get_forecast_log.return_value = [
            {"date": f"2024-01-{i:02d}", "model": "open_meteo", "forecast_high_f": 30.0 + i * 0.1}
            for i in range(1, 16)
        ]
        db.get_settlements.return_value = [
            {"ts": f"2024-01-{i:02d}T12:00:00", "actual_high_f": 30.2 + i * 0.05}
            for i in range(1, 16)
        ]

        weights, rmse = compute_weights(db, "WSSS", "Singapore")

        # Phantom NWS must carry zero weight
        assert weights["nws"] == 0.0
        # Sole active model must absorb all weight
        assert abs(weights["open_meteo"] - 1.0) < 1e-9
        # open_meteo RMSE should be non-zero (computed from real errors)
        assert rmse["open_meteo"] > 0.0

    def test_phantom_model_both_absent_falls_back_to_equal_weights(self):
        """When no model has any log rows, fall back to EQUAL_WEIGHTS."""
        db = MagicMock()
        db.get_forecast_log.return_value = []
        db.get_settlements.return_value = []

        weights, rmse = compute_weights(db, "WSSS", "Singapore")
        assert weights == EQUAL_WEIGHTS
        assert all(v == 0.0 for v in rmse.values())

    def test_phantom_nws_insufficient_samples_for_open_meteo_falls_back(self):
        """NWS is phantom; open_meteo has rows but below MIN_SAMPLES → EQUAL_WEIGHTS fallback."""
        db = MagicMock()
        # open_meteo has rows but fewer than MIN_SAMPLES; nws has zero rows
        db.get_forecast_log.return_value = [
            {"date": f"2024-01-{i:02d}", "model": "open_meteo", "forecast_high_f": 30.0 + i * 0.1}
            for i in range(1, 6)  # only 5 rows, below MIN_SAMPLES=10
        ]
        db.get_settlements.return_value = [
            {"ts": f"2024-01-{i:02d}T12:00:00", "actual_high_f": 30.2 + i * 0.05}
            for i in range(1, 6)
        ]

        weights, rmse = compute_weights(db, "WSSS", "Singapore")
        assert weights == EQUAL_WEIGHTS
        assert all(v == 0.0 for v in rmse.values())

    def test_weights_keys_always_include_all_models(self):
        """Returned weights dict must always contain keys for all MODELS (even at 0.0)."""
        db = MagicMock()
        db.get_forecast_log.return_value = [
            {"date": f"2024-01-{i:02d}", "model": "open_meteo", "forecast_high_f": 30.0 + i * 0.1}
            for i in range(1, 16)
        ]
        db.get_settlements.return_value = [
            {"ts": f"2024-01-{i:02d}T12:00:00", "actual_high_f": 30.2 + i * 0.05}
            for i in range(1, 16)
        ]

        weights, rmse = compute_weights(db, "WSSS", "Singapore")
        from src.model.deb_weighting import MODELS
        for m in MODELS:
            assert m in weights, f"Model {m} missing from weights dict"


class TestCheckWeightQuality:
    def test_no_violations_when_model_has_rows(self):
        """No violations when every model carrying weight has log rows."""
        db = MagicMock()
        db.get_model_weights.return_value = [
            {"model": "nws", "weight": 0.6, "date": "2024-01-15"},
            {"model": "open_meteo", "weight": 0.4, "date": "2024-01-15"},
        ]
        db.get_forecast_log.return_value = [
            {"date": "2024-01-10", "model": "nws", "forecast_high_f": 80.0},
            {"date": "2024-01-10", "model": "open_meteo", "forecast_high_f": 81.0},
        ]

        violations = check_weight_quality(db, "KORD", "Chicago")
        assert violations == []

    def test_violation_when_phantom_nws_carries_weight(self):
        """Violation detected when nws carries weight but has zero log rows."""
        db = MagicMock()
        db.get_model_weights.return_value = [
            {"model": "nws", "weight": 0.5, "date": "2024-01-15"},
            {"model": "open_meteo", "weight": 0.5, "date": "2024-01-15"},
        ]
        # NWS has no log rows for this station
        db.get_forecast_log.return_value = [
            {"date": "2024-01-10", "model": "open_meteo", "forecast_high_f": 30.0},
        ]

        violations = check_weight_quality(db, "WSSS", "Singapore")
        assert len(violations) == 1
        assert "nws" in violations[0]
        assert "Singapore" in violations[0]

    def test_no_violations_when_no_weights_exist(self):
        """No violations when there are no model_weights rows."""
        db = MagicMock()
        db.get_model_weights.return_value = []

        violations = check_weight_quality(db, "WSSS", "Singapore")
        assert violations == []

    def test_no_violation_when_phantom_carries_zero_weight(self):
        """A model with zero weight and zero log rows is not a violation."""
        db = MagicMock()
        db.get_model_weights.return_value = [
            {"model": "nws", "weight": 0.0, "date": "2024-01-15"},
            {"model": "open_meteo", "weight": 1.0, "date": "2024-01-15"},
        ]
        db.get_forecast_log.return_value = [
            {"date": "2024-01-10", "model": "open_meteo", "forecast_high_f": 30.0},
        ]

        violations = check_weight_quality(db, "WSSS", "Singapore")
        assert violations == []
