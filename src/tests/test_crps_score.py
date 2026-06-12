"""Unit tests for src/model/crps_score.py."""
from math import isclose

import pytest

from src.model.crps_score import crps_gaussian, mean_crps


class TestCrpsGaussian:
    def test_empty_list_returns_none(self):
        """mean_crps([]) is None."""
        result = mean_crps([])
        assert result is None

    def test_single_sample(self):
        """mean_crps([(10, 1, 12)]) == crps_gaussian(10, 1, 12)."""
        single = crps_gaussian(10, 1, 12)
        result = mean_crps([(10, 1, 12)])
        assert isclose(result, single, abs_tol=1e-10)

    def test_symmetry_around_mean(self):
        """crps_gaussian(10, 2, 13) ≈ crps_gaussian(10, 2, 7) (within 1e-10)."""
        above = crps_gaussian(10, 2, 13)
        below = crps_gaussian(10, 2, 7)
        assert isclose(above, below, abs_tol=1e-10), (
            f"Symmetry failed: above_mean={above:.10f}, below_mean={below:.10f}"
        )

    def test_deterministic_limit_sigma_zero(self):
        """crps_gaussian(10, 0, 12) == 2.0 (|10-12|)."""
        result = crps_gaussian(10, 0, 12)
        assert result == 2.0

    def test_deterministic_exact_zero(self):
        """crps_gaussian(5, 0, 5) == 0.0."""
        result = crps_gaussian(5, 0, 5)
        assert result == 0.0

    def test_deterministic_negative_sigma(self):
        """Negative sigma treated as deterministic."""
        result = crps_gaussian(10, -1, 12)
        assert result == 2.0

    def test_multi_sample_mean(self):
        """mean_crps([(m,s,y1),(m,s,y2)]) == (crps_gaussian(m,s,y1) + crps_gaussian(m,s,y2)) / 2."""
        crps1 = crps_gaussian(10, 2, 12)
        crps2 = crps_gaussian(10, 2, 15)
        expected = (crps1 + crps2) / 2
        result = mean_crps([(10, 2, 12), (10, 2, 15)])
        assert isclose(result, expected, abs_tol=1e-10)

    def test_positive_crps(self):
        """crps_gaussian(10, 2, 15) > 0."""
        result = crps_gaussian(10, 2, 15)
        assert result > 0.0

    def test_crps_zero_when_observation_at_mean(self):
        """When observation equals mean (z=0), CRPS = σ * (2*φ(0) - 1/√π)."""
        result = crps_gaussian(10, 2, 10)
        # When z=0, CRPS = σ * (0 + 2*φ(0) - 1/√π)
        # φ(0) = 1/√(2π) ≈ 0.3989, so 2*φ(0) ≈ 0.7979
        # 1/√π ≈ 0.5642
        # CRPS = 2 * (0.7979 - 0.5642) ≈ 0.4674
        assert isclose(result, 0.4674, abs_tol=0.001), f"Got {result}"

    def test_standard_normal_known_value(self):
        """Test with known parameters: mu=0, sigma=1."""
        result = crps_gaussian(0, 1, 1)
        # For z=1: CRPS = 1 * [1 * (2*Φ(1) - 1) + 2*φ(1) - 1/√π]
        # Φ(1) ≈ 0.8413, φ(1) ≈ 0.2420, 1/√π ≈ 0.5642
        # CRPS ≈ 0.6826 + 0.4840 - 0.5642 ≈ 0.6024
        assert isclose(result, 0.6024, abs_tol=0.001), f"Got {result}"

    def test_large_sigma(self):
        """Large sigma produces larger CRPS."""
        result_small = crps_gaussian(0, 1, 0)
        result_large = crps_gaussian(0, 10, 0)
        assert result_large > result_small

    def test_observation_far_from_mean(self):
        """Observation far from mean has large CRPS."""
        near = crps_gaussian(10, 1, 10.1)
        far = crps_gaussian(10, 1, 20)
        assert far > near

    def test_multiple_samples_averaging(self):
        """mean_crps correctly averages over multiple (mu, sigma, y) tuples."""
        forecasts = [
            (10, 1, 12),
            (20, 2, 18),
            (30, 3, 31),
        ]
        manual = sum(crps_gaussian(mu, sigma, y) for mu, sigma, y in forecasts) / 3
        result = mean_crps(forecasts)
        assert isclose(result, manual, abs_tol=1e-10)

    def test_all_same_values(self):
        """Three identical forecasts give same result as single forecast."""
        single = crps_gaussian(15, 2, 17)
        triple = mean_crps([(15, 2, 17), (15, 2, 17), (15, 2, 17)])
        assert isclose(triple, single, abs_tol=1e-10)
