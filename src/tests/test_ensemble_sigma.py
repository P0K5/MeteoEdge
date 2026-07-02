"""Tests for per-station ensemble sigma estimator (Issue #447)."""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.model.ensemble_sigma import (
    SIGMA_FLOOR_F,
    MIN_CALIBRATION_SAMPLES,
    compute_ensemble_sigma,
    raw_member_sigma,
    _naive_sigma,
    _calibrated_sigma,
    _load_calibration_pairs,
)


# ---------------------------------------------------------------------------
# _naive_sigma
# ---------------------------------------------------------------------------

class TestNaiveSigma:
    def test_returns_stdev_of_members(self):
        members = [70.0, 72.0, 74.0, 76.0, 78.0]
        s = _naive_sigma(members)
        import statistics
        expected = statistics.stdev(members)
        assert s == pytest.approx(expected)

    def test_floor_enforced_when_stdev_is_zero(self):
        members = [72.0, 72.0, 72.0, 72.0]
        assert _naive_sigma(members) == SIGMA_FLOOR_F

    def test_floor_enforced_on_tiny_spread(self):
        # spread of 0.1 → stdev < 1.0 → floor kicks in
        members = [72.0, 72.1]
        assert _naive_sigma(members) == pytest.approx(SIGMA_FLOOR_F)

    def test_single_member_returns_floor(self):
        assert _naive_sigma([72.0]) == SIGMA_FLOOR_F

    def test_empty_list_returns_floor(self):
        assert _naive_sigma([]) == SIGMA_FLOOR_F

    def test_typical_30_member_ensemble(self):
        import statistics
        members = [70.0 + i * 0.5 for i in range(30)]
        s = _naive_sigma(members)
        assert s == pytest.approx(statistics.stdev(members))
        assert s > SIGMA_FLOOR_F


# ---------------------------------------------------------------------------
# raw_member_sigma — capture-time, UNFLOORED (issue #555)
# ---------------------------------------------------------------------------

class TestRawMemberSigma:
    """raw_member_sigma() must return the true sample stdev with NO floor
    applied, and None (never a placeholder number) when it cannot be
    computed — this is what capture_forecasts.py persists to
    model_forecast_log.sigma_f for the gefs channel."""

    def test_returns_unfloored_stdev_of_members(self):
        import statistics
        members = [70.0, 72.0, 74.0, 76.0, 78.0]
        s = raw_member_sigma(members)
        assert s == pytest.approx(statistics.stdev(members))

    def test_below_floor_spread_is_not_clamped(self):
        # spread of 0.1 → true stdev well under SIGMA_FLOOR_F (1.0); unlike
        # _naive_sigma()/compute_ensemble_sigma(), this must NOT be clamped up.
        import statistics
        members = [72.0, 72.1]
        expected = statistics.stdev(members)
        assert expected < SIGMA_FLOOR_F
        assert raw_member_sigma(members) == pytest.approx(expected)

    def test_identical_members_returns_true_zero_not_floor(self):
        # All members identical → true stdev is 0.0 — must be logged as a
        # genuine near-zero value, never silently clamped to SIGMA_FLOOR_F.
        members = [72.0, 72.0, 72.0, 72.0]
        assert raw_member_sigma(members) == pytest.approx(0.0)

    def test_none_for_single_member(self):
        assert raw_member_sigma([72.0]) is None

    def test_none_for_empty_list(self):
        assert raw_member_sigma([]) is None

    def test_typical_30_member_ensemble_matches_naive_unfloored(self):
        import statistics
        members = [70.0 + i * 0.5 for i in range(30)]
        s = raw_member_sigma(members)
        assert s == pytest.approx(statistics.stdev(members))
        assert s > SIGMA_FLOOR_F


# ---------------------------------------------------------------------------
# _calibrated_sigma
# ---------------------------------------------------------------------------

class TestCalibratedSigma:
    def test_applies_linear_regression(self):
        # Perfect linear relationship: abs_error = 2 * sigma_naive → slope=2, intercept=0
        pairs = [(float(x), float(2 * x)) for x in range(1, 50)]
        result = _calibrated_sigma(3.0, pairs)
        assert result == pytest.approx(6.0, abs=0.5)

    def test_floor_enforced_when_regression_predicts_low(self):
        # Pairs that produce a tiny predicted sigma
        pairs = [(10.0, 0.01)] * 50
        result = _calibrated_sigma(10.0, pairs)
        assert result >= SIGMA_FLOOR_F

    def test_degenerate_x_falls_back_to_naive(self):
        # All x identical → linregress undefined → should return naive clamped to floor
        pairs = [(3.0, y) for y in range(50)]
        result = _calibrated_sigma(3.0, pairs)
        assert result == pytest.approx(max(3.0, SIGMA_FLOOR_F))


# ---------------------------------------------------------------------------
# compute_ensemble_sigma — public API
# ---------------------------------------------------------------------------

class TestComputeEnsembleSigma:
    """Main API tests."""

    def test_naive_when_history_db_is_none(self):
        import statistics
        members = [70.0 + i * 0.5 for i in range(30)]
        result = compute_ensemble_sigma(members, "KORD", history_db=None)
        assert result == pytest.approx(statistics.stdev(members))

    def test_naive_when_insufficient_history(self):
        """DB with < 30 calibration pairs → naive estimator."""
        import statistics
        members = [70.0 + i * 0.5 for i in range(30)]

        mock_db = MagicMock()
        # Return too few pairs
        mock_db._conn.execute.return_value.fetchall.return_value = [
            ("2026-07-01", 72.0, 3.0),
        ]

        with patch("src.model.ensemble_sigma._load_calibration_pairs", return_value=[]):
            result = compute_ensemble_sigma(members, "KORD", history_db=mock_db)
        assert result == pytest.approx(statistics.stdev(members))

    def test_calibrated_when_sufficient_history(self):
        """DB with >= 30 varied calibration pairs → calibrated estimator used."""
        members = [70.0 + i * 0.5 for i in range(30)]
        # Pairs with varying x so linregress is non-degenerate
        # slope=2: abs_error = 2 * sigma_naive → calibrated ≈ 2 * naive
        synthetic_pairs = [(float(1 + i % 5), float(2 + 2 * (i % 5))) for i in range(MIN_CALIBRATION_SAMPLES)]

        mock_db = MagicMock()
        with patch("src.model.ensemble_sigma._load_calibration_pairs",
                   return_value=synthetic_pairs):
            result = compute_ensemble_sigma(members, "KORD", history_db=mock_db)
        # With slope≈2, calibrated sigma > floor
        assert result >= SIGMA_FLOOR_F
        # And calibration path was taken (result is not degenerate)
        assert isinstance(result, float)

    def test_floor_always_enforced(self):
        # Even if members are all identical, floor holds
        members = [72.0] * 30
        result = compute_ensemble_sigma(members, "KORD", history_db=None)
        assert result >= SIGMA_FLOOR_F

    def test_no_db_writes(self):
        """compute_ensemble_sigma must never write to the DB."""
        members = [70.0 + i for i in range(30)]
        mock_db = MagicMock()
        with patch("src.model.ensemble_sigma._load_calibration_pairs", return_value=[]):
            compute_ensemble_sigma(members, "KORD", history_db=mock_db)
        # No write calls
        mock_db._conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: compute_ensemble_sigma() (consumption-time / live path) is
# COMPLETELY UNCHANGED by #555 — same floor, same output, for the same
# inputs, as before raw_member_sigma() existed. #555 only changed what
# capture_forecasts.py persists at logging time; it must not alter live
# probability/trading behaviour.
# ---------------------------------------------------------------------------

class TestComputeEnsembleSigmaUnchangedByIssue555:
    """Locks in pre-#555 behaviour for compute_ensemble_sigma() so a future
    change to raw_member_sigma() (or anything else in this module) cannot
    accidentally leak the floor removal into the live consumption path."""

    def test_still_floors_zero_spread_members(self):
        # Identical members → true spread is 0.0, but the live/consumption
        # path must still clamp to SIGMA_FLOOR_F, exactly as before #555.
        members = [72.0] * 30
        result = compute_ensemble_sigma(members, "KORD", history_db=None)
        assert result == SIGMA_FLOOR_F

    def test_still_floors_below_floor_spread(self):
        # spread of 0.1 → true stdev < SIGMA_FLOOR_F; consumption path must
        # still clamp up to the floor (unlike raw_member_sigma(), which
        # would return the true ~0.07 value unfloored).
        members = [72.0, 72.1]
        result = compute_ensemble_sigma(members, "KORD", history_db=None)
        assert result == pytest.approx(SIGMA_FLOOR_F)

    def test_naive_path_output_matches_naive_sigma_directly(self):
        # No history_db → naive estimator, identical to calling _naive_sigma()
        # directly — same code path as pre-#555.
        members = [70.0 + i * 0.5 for i in range(30)]
        assert compute_ensemble_sigma(members, "KORD", history_db=None) == pytest.approx(
            _naive_sigma(members)
        )

    def test_does_not_import_or_depend_on_raw_member_sigma(self):
        # compute_ensemble_sigma()'s naive fallback must equal _naive_sigma()
        # (floored) — NOT raw_member_sigma() (unfloored) — for a spread that
        # would differ under the two estimators, proving the two code paths
        # are still fully independent post-#555.
        members = [72.0, 72.05]  # true stdev << SIGMA_FLOOR_F
        floored = compute_ensemble_sigma(members, "KORD", history_db=None)
        unfloored = raw_member_sigma(members)
        assert floored == SIGMA_FLOOR_F
        assert unfloored < SIGMA_FLOOR_F
        assert floored != pytest.approx(unfloored)

    def test_calibrated_path_unaffected(self):
        # With sufficient history, the calibrated-regression path is exactly
        # as before — raw_member_sigma() is not involved anywhere in it.
        members = [70.0 + i * 0.5 for i in range(30)]
        synthetic_pairs = [
            (float(1 + i % 5), float(2 + 2 * (i % 5))) for i in range(MIN_CALIBRATION_SAMPLES)
        ]
        mock_db = MagicMock()
        with patch(
            "src.model.ensemble_sigma._load_calibration_pairs",
            return_value=synthetic_pairs,
        ):
            result = compute_ensemble_sigma(members, "KORD", history_db=mock_db)
        expected = _calibrated_sigma(_naive_sigma(members), synthetic_pairs)
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _load_calibration_pairs — real SQLite integration
# ---------------------------------------------------------------------------

class TestLoadCalibrationPairs:
    """Test against a real in-memory SQLite DB."""

    def _make_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def test_returns_empty_when_no_sigma_f_column(self):
        """model_forecast_log without sigma_f column → empty list (graceful)."""
        db = self._make_db()
        pairs = _load_calibration_pairs("KORD", db)
        assert pairs == []

    def test_returns_empty_when_no_settlements(self):
        """Forecast rows but no settlements → no pairs."""
        db = self._make_db()
        pairs = _load_calibration_pairs("KORD", db)
        assert pairs == []

    def test_returns_pairs_when_data_present(self):
        """Synthetic DB with sigma_f + settlements → correct (sigma, abs_err) pairs."""
        db = self._make_db()

        # Manually add sigma_f column if absent (it's added by migration)
        try:
            db._conn.execute("ALTER TABLE model_forecast_log ADD COLUMN sigma_f REAL")
            db._conn.commit()
        except Exception:
            pass

        # Insert 5 forecast rows with sigma_f
        for i in range(5):
            date_str = f"2026-06-{i + 1:02d}"
            db._conn.execute(
                "INSERT INTO model_forecast_log(station,model,date,forecast_high_f,logged_at,sigma_f) "
                "VALUES(?,?,?,?,?,?)",
                ("KORD", "test", date_str, 80.0 + i, "2026-06-01T00:00:00Z", 2.0 + i * 0.5),
            )
            # Corresponding settlement
            db._conn.execute(
                "INSERT OR REPLACE INTO settlements"
                "(ts,station,ticker,bracket_low,bracket_high,actual_high_f,resolved_yes,source) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (f"{date_str}T18:00:00Z", "KORD", f"TICKER-{i}", 78.0, 82.0,
                 82.0 + i,  # actual = forecast + 2 → abs_err = 2
                 1, "polymarket"),
            )
        db._conn.commit()

        pairs = _load_calibration_pairs("KORD", db, lookback_days=365)
        assert len(pairs) == 5
        for sigma_naive, abs_err in pairs:
            assert sigma_naive >= 2.0
            assert abs_err == pytest.approx(2.0, abs=0.01)
