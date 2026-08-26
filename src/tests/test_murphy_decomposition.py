"""Tests for src/model/murphy_decomposition.py (issue #1048)."""
import pytest

from src.model.murphy_decomposition import murphy_decomposition


class TestMurphyDecomposition:
    def test_empty_samples(self):
        decomp = murphy_decomposition([])
        assert decomp["n"] == 0
        assert decomp["reliability"] is None
        assert decomp["bs_decomposed"] is None

    def test_perfectly_calibrated_forecaster_has_near_zero_reliability(self):
        """Two buckets, each with a constant forecast that exactly matches its
        own observed frequency -- the synthetic case the issue asks for.

        Bucket A: p=0.10 for 10 samples, 1 True  -> obs_freq = 0.10 = mean_pred.
        Bucket B: p=0.80 for 10 samples, 8 True  -> obs_freq = 0.80 = mean_pred.
        Reliability must be exactly 0 (both buckets' mean forecast equals
        their own observed frequency by construction). Resolution must be
        > 0 (the two buckets have different observed frequencies from the
        0.45 base rate), so this is not a degenerate single-bucket case.
        """
        bucket_a = [(0.10, True)] + [(0.10, False)] * 9
        bucket_b = [(0.80, True)] * 8 + [(0.80, False)] * 2
        samples = bucket_a + bucket_b

        decomp = murphy_decomposition(samples)

        assert decomp["n"] == 20
        # mean_pred is a float SUM over 10 identical 0.10/0.80 literals divided
        # by n_k, not the literal itself -- it lands within float rounding of
        # obs_freq, not bit-identical to it, so reliability is near-zero
        # (~1e-33) rather than exactly 0.0. Assert the tolerance, not equality.
        assert decomp["reliability"] == pytest.approx(0.0, abs=1e-9)
        assert decomp["resolution"] > 0.0
        assert decomp["base_rate"] == 0.45
        assert decomp["n_buckets_used"] == 2
        # No intra-bucket forecast variance (every sample in a bucket carries
        # the identical p) -- the binned decomposition is exact here, so it
        # must match the true Brier score, not just approximate it.
        assert decomp["bs_decomposed"] == pytest.approx(decomp["bs_actual"], abs=1e-9)

    def test_uncalibrated_forecaster_has_positive_reliability(self):
        """A forecaster that is confidently wrong in one bucket (p=0.90 but
        the bucket resolves YES only 10% of the time) must show reliability
        clearly above zero."""
        bucket = [(0.90, True)] + [(0.90, False)] * 9
        decomp = murphy_decomposition(bucket)

        assert decomp["n"] == 10
        assert decomp["reliability"] > 0.05
        # Single bucket -- no discrimination possible, resolution must be 0.
        assert decomp["resolution"] == 0.0

    def test_decomposition_identity_holds(self):
        """BS = Reliability - Resolution + Uncertainty must hold to float
        precision for any input, not just the constant-forecast cases above."""
        samples = [
            (0.05, False), (0.15, False), (0.15, True), (0.40, True),
            (0.40, False), (0.60, True), (0.85, True), (0.85, True),
            (0.85, False), (0.95, True),
        ]
        decomp = murphy_decomposition(samples)
        recomputed = decomp["reliability"] - decomp["resolution"] + decomp["uncertainty"]
        assert abs(recomputed - decomp["bs_decomposed"]) < 1e-12

    def test_custom_edges_are_respected(self):
        samples = [(0.3, True), (0.3, False), (0.7, True)]
        decomp = murphy_decomposition(samples, edges=[0.0, 0.5, 1.001])
        assert decomp["n_buckets_used"] == 2
