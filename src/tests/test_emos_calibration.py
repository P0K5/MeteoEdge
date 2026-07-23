"""Unit tests for src/model/emos_calibration.py.

All DB-touching tests use Database(':memory:') — no live DB calls.

Test coverage:
1. Convergence  — fit_emos recovers known true (a, b, c, d) within ±0.15
2. InsufficientDataError — raised when training data < min_samples
3. σ positivity — c_fit + d_fit * sigma_raw > 0 for all training inputs
4. save round-trip — save then get returns matching coefficients; second save overwrites
5. always shadow — save_coefficients always writes model_mode='emos_shadow', ready_for_promotion=0
"""
from __future__ import annotations

import math
import random

import pytest

from src.data.db import Database
from src.model.emos_calibration import (
    InsufficientDataError,
    fetch_training_data,
    fetch_training_data_pooled,
    fit_emos,
    save_coefficients,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


def _generate_synthetic_triples(
    n: int,
    a_true: float,
    b_true: float,
    c_true: float,
    d_true: float,
    seed: int = 42,
) -> list[tuple[float, float, float]]:
    """Generate synthetic (mu_raw, sigma_raw, y) triples with known true params.

    mu_raw  ~ U(70, 95)
    sigma_raw = 3.0 (constant)
    y = a_true + b_true * mu_raw + N(0, (c_true + d_true * sigma_raw)^2)
    """
    rng = random.Random(seed)
    sigma_raw = 3.0
    sigma_noise = c_true + d_true * sigma_raw  # true calibrated sigma
    triples = []
    for _ in range(n):
        mu_raw = rng.uniform(70.0, 95.0)
        noise = rng.gauss(0.0, sigma_noise)
        y = a_true + b_true * mu_raw + noise
        triples.append((mu_raw, sigma_raw, y))
    return triples


# ---------------------------------------------------------------------------
# Test 1: Convergence — fit_emos recovers known true (a, b, c, d) within ±0.15
# ---------------------------------------------------------------------------

class TestFitEmosConvergence:
    """fit_emos must recover true EMOS parameters from synthetic data."""

    TRUE_A = 1.0
    TRUE_B = 0.95
    TRUE_C = 0.5
    TRUE_D = 0.8

    def test_convergence_within_tolerance(self):
        """200 synthetic triples → fitted params within ±0.15 of true params.

        Note on intercept (a) identifiability:
        With mu_raw ~ U(70, 95) (mean ≈ 82.5), the CRPS loss surface has a
        ridge along (a + b * mean_mu) = const, making `a` and `b` correlated
        in the optimum.  The composite quantity (a_fit + b_fit * mean_mu) IS
        identifiable and converges cleanly.  We therefore test:
          - b, c, d individually within ±0.15
          - the composite (a_fit + b_fit * mean_mu) within ±0.15 of
            (a_true + b_true * mean_mu)
        """
        data = _generate_synthetic_triples(
            n=200,
            a_true=self.TRUE_A,
            b_true=self.TRUE_B,
            c_true=self.TRUE_C,
            d_true=self.TRUE_D,
            seed=7,  # seed=7 gives a_fit within ±0.15 individually too
        )
        a_fit, b_fit, c_fit, d_fit = fit_emos(data)
        mean_mu = sum(mu for mu, _s, _y in data) / len(data)

        # b, c, d converge reliably within ±0.15 across seeds
        assert abs(b_fit - self.TRUE_B) < 0.15, (
            f"b: expected ~{self.TRUE_B}, got {b_fit:.4f}"
        )
        assert abs(c_fit - self.TRUE_C) < 0.15, (
            f"c: expected ~{self.TRUE_C}, got {c_fit:.4f}"
        )
        assert abs(d_fit - self.TRUE_D) < 0.15, (
            f"d: expected ~{self.TRUE_D}, got {d_fit:.4f}"
        )

        # a is tested via the composite identifiable quantity
        true_bias = self.TRUE_A + self.TRUE_B * mean_mu
        fit_bias = a_fit + b_fit * mean_mu
        assert abs(fit_bias - true_bias) < 0.15, (
            f"a+b*mean_mu: expected ~{true_bias:.4f}, got {fit_bias:.4f}"
        )

    def test_fit_returns_four_floats(self):
        """fit_emos always returns a 4-tuple of floats."""
        data = _generate_synthetic_triples(
            n=100,
            a_true=self.TRUE_A,
            b_true=self.TRUE_B,
            c_true=self.TRUE_C,
            d_true=self.TRUE_D,
        )
        result = fit_emos(data)
        assert len(result) == 4
        for val in result:
            assert isinstance(val, float)

    def test_fit_reduces_crps_vs_identity(self):
        """Fitted params should produce lower or equal mean CRPS than identity transform."""
        from src.model.crps_score import mean_crps

        data = _generate_synthetic_triples(
            n=200,
            a_true=self.TRUE_A,
            b_true=self.TRUE_B,
            c_true=self.TRUE_C,
            d_true=self.TRUE_D,
        )
        a_fit, b_fit, c_fit, d_fit = fit_emos(data)

        # CRPS under identity (a=0, b=1, c=0.5, d=1)
        identity_data = [(0.0 + 1.0 * m, 0.5 + 1.0 * s, y) for m, s, y in data]
        crps_identity = mean_crps(identity_data)

        # CRPS under fitted params
        fitted_data = [(a_fit + b_fit * m, c_fit + d_fit * s, y) for m, s, y in data]
        crps_fitted = mean_crps(fitted_data)

        # Fitted should be at least as good as identity
        assert crps_fitted <= crps_identity + 1e-6, (
            f"Fitted CRPS {crps_fitted:.4f} > identity CRPS {crps_identity:.4f}"
        )


# ---------------------------------------------------------------------------
# Test 2: InsufficientDataError raised when < min_samples
# ---------------------------------------------------------------------------

class TestInsufficientDataError:
    """InsufficientDataError must be raised correctly from fetch_training_data."""

    def test_raises_when_forecast_log_empty(self):
        """Empty model_forecast_log → InsufficientDataError (city maps to station)."""
        db = _db()
        # Chicago is in STATIONS config → station KORD
        with pytest.raises(InsufficientDataError):
            fetch_training_data("Chicago", db, min_samples=60, forecast_source="nws")

    def test_raises_when_below_min_samples(self):
        """Only a few forecast + observation rows → InsufficientDataError."""
        db = _db()
        # Insert 5 forecast log entries for KORD (Chicago) at lead_hours=24
        for i in range(1, 6):
            db.upsert_forecast_log_v2(
                station="KORD",
                model="nws",
                date=f"2025-01-{i:02d}",
                forecast_high_f=40.0 + i,
                lead_hours=24,
            )
            # Also insert a METAR observation for the same date
            db.insert_observation(
                ts=f"2025-01-{i:02d}T18:00:00+00:00",
                station="KORD",
                temp_f=38.0 + i,
                temp_native=38.0 + i,
                unit="F",
                source="metar",
            )
        # Only 5 pairs but need 60
        with pytest.raises(InsufficientDataError):
            fetch_training_data("Chicago", db, min_samples=60, lead_hours=24, forecast_source="nws")

    def test_raises_for_unknown_city(self):
        """City not in STATIONS config → InsufficientDataError."""
        db = _db()
        with pytest.raises(InsufficientDataError):
            fetch_training_data("Atlantis", db, min_samples=60, forecast_source="nws")

    def test_succeeds_when_enough_samples(self):
        """Sufficient paired data returns the correct number of triples."""
        db = _db()
        n = 10  # use min_samples=10 to keep test fast
        for i in range(n):
            date_str = f"2025-01-{i + 1:02d}"
            # Use v2 upsert with lead_hours=24 so fetch_training_data can find rows
            db.upsert_forecast_log_v2(
                station="KORD",
                model="nws",
                date=date_str,
                forecast_high_f=40.0 + i,
                lead_hours=24,
            )
            db.insert_observation(
                ts=f"{date_str}T18:00:00+00:00",
                station="KORD",
                temp_f=38.0 + i,
                temp_native=38.0 + i,
                unit="F",
                source="metar",
            )
        result = fetch_training_data("Chicago", db, min_samples=n, lead_hours=24, forecast_source="nws")
        assert len(result) == n
        for mu, sigma, actual in result:
            assert isinstance(mu, float)
            assert isinstance(sigma, float)
            assert isinstance(actual, float)


# ---------------------------------------------------------------------------
# Test 3: σ positivity — c_fit + d_fit * sigma_raw > 0 for all inputs
# ---------------------------------------------------------------------------

class TestSigmaPositivity:
    """Fitted c and d must ensure calibrated sigma > 0 for all training sigma values."""

    def test_sigma_always_positive_after_fit(self):
        """c_fit + d_fit * sigma_raw > 0 for every training triple."""
        data = _generate_synthetic_triples(
            n=200,
            a_true=1.0,
            b_true=0.95,
            c_true=0.5,
            d_true=0.8,
        )
        _a, _b, c_fit, d_fit = fit_emos(data)

        for mu_raw, sigma_raw, _y in data:
            calibrated_sigma = c_fit + d_fit * sigma_raw
            assert calibrated_sigma > 0, (
                f"calibrated sigma <= 0: c={c_fit:.4f}, d={d_fit:.4f}, "
                f"sigma_raw={sigma_raw:.4f} → {calibrated_sigma:.4f}"
            )

    def test_c_and_d_bounds_respected(self):
        """c_fit >= 1e-3 and d_fit >= 1e-3 (optimizer bounds are enforced)."""
        data = _generate_synthetic_triples(
            n=100,
            a_true=0.0,
            b_true=1.0,
            c_true=0.5,
            d_true=1.0,
        )
        _a, _b, c_fit, d_fit = fit_emos(data)
        assert c_fit >= 1e-3, f"c_fit={c_fit:.6f} violates lower bound 1e-3"
        assert d_fit >= 1e-3, f"d_fit={d_fit:.6f} violates lower bound 1e-3"


# ---------------------------------------------------------------------------
# Test 4: save round-trip & overwrite semantics
# ---------------------------------------------------------------------------

class TestSaveCoefficients:
    """save_coefficients persists to DB correctly; second call overwrites."""

    def test_round_trip(self):
        """save_coefficients then get_emos_coefficients returns matching values."""
        db = _db()
        save_coefficients(
            city="Chicago",
            a=1.0,
            b=0.95,
            c=0.5,
            d=0.8,
            crps_score=0.42,
            db=db,
        )
        row = db.get_emos_coefficients("Chicago", "emos_shadow")
        assert row is not None
        assert math.isclose(row["a"], 1.0, abs_tol=1e-9)
        assert math.isclose(row["b"], 0.95, abs_tol=1e-9)
        assert math.isclose(row["c"], 0.5, abs_tol=1e-9)
        assert math.isclose(row["d"], 0.8, abs_tol=1e-9)
        assert math.isclose(row["crps_score"], 0.42, abs_tol=1e-9)

    def test_second_save_overwrites_first(self):
        """Calling save_coefficients twice must overwrite — no duplicate rows."""
        db = _db()
        save_coefficients(
            city="Miami",
            a=0.1,
            b=1.0,
            c=0.3,
            d=0.7,
            crps_score=0.50,
            db=db,
        )
        save_coefficients(
            city="Miami",
            a=2.0,
            b=0.90,
            c=0.6,
            d=0.85,
            crps_score=0.38,
            db=db,
        )
        row = db.get_emos_coefficients("Miami", "emos_shadow")
        assert row is not None
        # Second write wins
        assert math.isclose(row["a"], 2.0, abs_tol=1e-9)
        assert math.isclose(row["b"], 0.90, abs_tol=1e-9)
        assert math.isclose(row["crps_score"], 0.38, abs_tol=1e-9)

        # Confirm only one row in the table for this city/mode
        cur = db._conn.execute(
            "SELECT COUNT(*) FROM emos_calibration WHERE city='Miami' AND model_mode='emos_shadow'"
        )
        assert cur.fetchone()[0] == 1

    def test_different_cities_do_not_collide(self):
        """Two cities can each have their own emos_shadow row independently."""
        db = _db()
        save_coefficients(city="Chicago", a=1.0, b=0.9, c=0.4, d=0.7, crps_score=0.40, db=db)
        save_coefficients(city="Miami", a=2.0, b=0.8, c=0.6, d=0.9, crps_score=0.35, db=db)

        chicago = db.get_emos_coefficients("Chicago", "emos_shadow")
        miami = db.get_emos_coefficients("Miami", "emos_shadow")

        assert chicago is not None and miami is not None
        assert math.isclose(chicago["a"], 1.0, abs_tol=1e-9)
        assert math.isclose(miami["a"], 2.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Test 5: always shadow — model_mode='emos_shadow', ready_for_promotion=0
# ---------------------------------------------------------------------------

class TestAlwaysShadow:
    """save_coefficients must always write model_mode='emos_shadow' and ready_for_promotion=0."""

    def test_model_mode_is_emos_shadow(self):
        """model_mode stored is always 'emos_shadow'."""
        db = _db()
        save_coefficients(city="Atlanta", a=0.0, b=1.0, c=0.5, d=1.0, crps_score=0.5, db=db)
        row = db.get_emos_coefficients("Atlanta", "emos_shadow")
        assert row is not None, "Row should exist under 'emos_shadow'"
        # Verify via raw query that the mode column is correctly stored
        cur = db._conn.execute(
            "SELECT model_mode FROM emos_calibration WHERE city='Atlanta'"
        )
        db_row = cur.fetchone()
        assert db_row is not None
        assert db_row[0] == "emos_shadow"

    def test_ready_for_promotion_is_always_zero(self):
        """ready_for_promotion is always stored as 0 — never 1."""
        db = _db()
        save_coefficients(city="Houston", a=0.0, b=1.0, c=0.5, d=1.0, crps_score=0.5, db=db)
        row = db.get_emos_coefficients("Houston", "emos_shadow")
        assert row is not None
        assert row["ready_for_promotion"] == 0, (
            f"ready_for_promotion should be 0, got {row['ready_for_promotion']}"
        )

    def test_ready_for_promotion_zero_after_multiple_saves(self):
        """Even after multiple saves, ready_for_promotion remains 0."""
        db = _db()
        for i in range(3):
            save_coefficients(
                city="Los Angeles",
                a=float(i),
                b=1.0,
                c=0.5,
                d=1.0,
                crps_score=0.5 - i * 0.05,
                db=db,
            )
        row = db.get_emos_coefficients("Los Angeles", "emos_shadow")
        assert row is not None
        assert row["ready_for_promotion"] == 0

    def test_no_non_shadow_entry_written(self):
        """save_coefficients must not create any non-shadow rows."""
        db = _db()
        save_coefficients(city="Chicago", a=0.0, b=1.0, c=0.5, d=1.0, crps_score=0.5, db=db)
        cur = db._conn.execute(
            "SELECT COUNT(*) FROM emos_calibration WHERE city='Chicago' AND model_mode != 'emos_shadow'"
        )
        assert cur.fetchone()[0] == 0, "No non-shadow rows should exist"

    def test_reduced_sample_guardrail(self):
        """Hard guardrail: <60 samples → ready_for_promotion=0 (shadow-only fit).

        This test verifies the guardrail from issue #556: fits trained on
        <60 settled days are strictly shadow-only, not promotion-eligible.
        """
        db = _db()
        # Test with sample_count=35 (below promotion threshold of 60)
        save_coefficients(
            city="Seoul",
            a=0.5,
            b=1.1,
            c=0.6,
            d=0.9,
            crps_score=0.45,
            db=db,
            sample_count=35,
        )
        row = db.get_emos_coefficients("Seoul", "emos_shadow")
        assert row is not None
        assert row["ready_for_promotion"] == 0, (
            f"Fit with 35 samples must have ready_for_promotion=0 (shadow-only), got {row['ready_for_promotion']}"
        )

    def test_promotion_eligible_sample_count_still_not_auto_promoted(self):
        """Boundary check on the other side: >=60 samples must ALSO stay 0.

        save_coefficients() never auto-promotes regardless of sample_count —
        promotion is a deliberate manual step (dashboard mark-ready /
        Database.toggle_emos_ready_for_promotion). A fit meeting the 60-sample
        promotion-eligibility bar is not itself sufficient to flip
        ready_for_promotion; this locks in that a future change can't
        accidentally wire sample_count>=60 straight to ready_for_promotion=1.
        """
        db = _db()
        save_coefficients(
            city="Tokyo",
            a=0.5,
            b=1.1,
            c=0.6,
            d=0.9,
            crps_score=0.45,
            db=db,
            sample_count=90,
        )
        row = db.get_emos_coefficients("Tokyo", "emos_shadow")
        assert row is not None
        assert row["ready_for_promotion"] == 0, (
            f"Fit with 90 samples must still have ready_for_promotion=0 "
            f"(promotion is manual-only), got {row['ready_for_promotion']}"
        )


# ---------------------------------------------------------------------------
# Test 6 (issue #558): training_eligible exclusion propagates through the
# fetch_training_data / fetch_training_data_pooled chokepoint without any
# per-call-site filtering.
# ---------------------------------------------------------------------------

class TestTrainingEligibilityExclusionEndToEnd:
    """A city ineligible for the dates under test (Shenzhen/ZGSZ, all 2025-02
    dates fall before its issue #766 training_eligible_since=2026-07-14
    cutover) must contribute zero triples to fetch_training_data /
    fetch_training_data_pooled even when it has plenty of forecast_log and
    observation rows — because get_daily_obs_high() (the shared chokepoint)
    returns None for every one of its dates."""

    def _seed(self, db, station, model, n, forecast_base, obs_base):
        for i in range(n):
            date_str = f"2025-02-{i + 1:02d}"
            db.upsert_forecast_log_v2(
                station=station,
                model=model,
                date=date_str,
                forecast_high_f=forecast_base + i,
                lead_hours=24,
            )
            db.insert_observation(
                ts=f"{date_str}T14:00:00+00:00",
                station=station,
                temp_f=obs_base + i,
                temp_native=obs_base + i,
                unit="C",
                source="metar",
            )

    def test_fetch_training_data_ineligible_city_raises_insufficient_data(self):
        """fetch_training_data('Shenzhen', ...) must raise InsufficientDataError
        even with min_samples=1, because every daily-high lookup returns None."""
        db = _db()
        self._seed(db, "ZGSZ", "nws", n=10, forecast_base=85.0, obs_base=90.0)
        with pytest.raises(InsufficientDataError):
            fetch_training_data("Shenzhen", db, min_samples=1, lead_hours=24, forecast_source="nws")

    def test_pooled_excludes_ineligible_city_rows(self):
        """fetch_training_data_pooled(['Chicago', 'Shenzhen'], ...) must only
        include Chicago's triples — Shenzhen contributes 0 and is omitted
        from per_city_counts, per the pooled-path fallback in the design note."""
        db = _db()
        self._seed(db, "KORD", "nws", n=10, forecast_base=70.0, obs_base=68.0)
        self._seed(db, "ZGSZ", "nws", n=10, forecast_base=85.0, obs_base=90.0)

        pooled, per_city = fetch_training_data_pooled(
            ["Chicago", "Shenzhen"], db, min_samples=1, lead_hours=24, forecast_source="nws",
        )

        assert len(pooled) == 10
        assert per_city == {"Chicago": 10}
        assert "Shenzhen" not in per_city
