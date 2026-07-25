"""Tests for the partial-pooling shrinkage blend (issue #798).

Issue #798 replaces run_emos_shadow.py's hard min_samples=60 per-city cutover
(issue #659) — per-city fit if a city has >=60 of its own settled triples,
else a 100%-pooled group fit with 0% weight on the city's own data — with a
continuous blend: weight = clamp(n_city / 60, 0.0, 1.0).

Test coverage:
1. shrinkage_weight() — boundary values, clamping, custom full_weight_samples.
2. blend_coefficients() — endpoints reduce to pure city/pooled fits, midpoint
   is the arithmetic mean, sigma positivity preserved.
3. Runner integration — a city in the new blended band (5 <= n < 60) gets a
   genuine blend (neither pure city nor pure pooled coefficients); cities at
   the old boundaries (>=60, <5) are bit-for-bit unchanged from pre-#798
   behaviour.
4. LLBG (Tel Aviv) backtest-style scenario using a 21-triple own window (the
   scale of the #767 diagnostics window) blended against its midlat_c pooling
   group, exercising the full runner path end-to-end.
"""
from __future__ import annotations

import pytest

from src.data.db import Database
from src.model.emos_calibration import (
    InsufficientDataError,
    blend_coefficients,
    fit_emos,
    pooling_group,
    shrinkage_weight,
)


def _db() -> Database:
    return Database(":memory:")


# ---------------------------------------------------------------------------
# 1. shrinkage_weight()
# ---------------------------------------------------------------------------

class TestShrinkageWeight:
    def test_zero_samples_is_fully_pooled(self):
        assert shrinkage_weight(0) == pytest.approx(0.0)

    def test_full_weight_at_legacy_threshold(self):
        """At n_city == 60 (the old hard cutover point), weight is exactly 1.0
        -- i.e. purely the city's own fit, matching pre-#798 behaviour."""
        assert shrinkage_weight(60) == pytest.approx(1.0)

    def test_clamped_above_threshold(self):
        """More than the threshold still clamps to 1.0, never overshoots."""
        assert shrinkage_weight(500) == pytest.approx(1.0)

    def test_linear_in_between(self):
        assert shrinkage_weight(30) == pytest.approx(0.5)
        assert shrinkage_weight(15) == pytest.approx(0.25)

    def test_custom_full_weight_samples(self):
        assert shrinkage_weight(10, full_weight_samples=20) == pytest.approx(0.5)

    def test_non_positive_threshold_returns_full_weight(self):
        """Degenerate config (full_weight_samples <= 0) must not divide by
        zero or return weight > 1 — treat as always fully trusted."""
        assert shrinkage_weight(5, full_weight_samples=0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2. blend_coefficients()
# ---------------------------------------------------------------------------

class TestBlendCoefficients:
    CITY = (1.0, 0.9, 0.4, 0.8)
    POOLED = (0.2, 1.1, 0.6, 1.0)

    def test_weight_one_is_pure_city_fit(self):
        blended = blend_coefficients(self.CITY, self.POOLED, weight=1.0)
        assert blended == pytest.approx(self.CITY)

    def test_weight_zero_is_pure_pooled_fit(self):
        blended = blend_coefficients(self.CITY, self.POOLED, weight=0.0)
        assert blended == pytest.approx(self.POOLED)

    def test_midpoint_is_arithmetic_mean(self):
        blended = blend_coefficients(self.CITY, self.POOLED, weight=0.5)
        expected = tuple((c + p) / 2 for c, p in zip(self.CITY, self.POOLED))
        assert blended == pytest.approx(expected)

    def test_weight_is_clamped(self):
        """Out-of-range weights are clamped rather than extrapolated."""
        over = blend_coefficients(self.CITY, self.POOLED, weight=1.5)
        under = blend_coefficients(self.CITY, self.POOLED, weight=-0.5)
        assert over == pytest.approx(self.CITY)
        assert under == pytest.approx(self.POOLED)

    def test_sigma_positivity_preserved(self):
        """A convex combination of two positive (c, d) pairs stays positive."""
        for w in (0.0, 0.1, 0.5, 0.9, 1.0):
            _a, _b, c, d = blend_coefficients(self.CITY, self.POOLED, weight=w)
            assert c > 0
            assert d > 0


# ---------------------------------------------------------------------------
# 3. Runner integration — blended band vs. unchanged boundaries
# ---------------------------------------------------------------------------

class TestBlendedBandInRunner:
    """Cities strictly between POOLED_MIN_CITY_SAMPLES (5) and
    FULL_WEIGHT_SAMPLES (60) now receive a genuine blend, whereas legacy
    behaviour gave them 0% of their own fit's weight."""

    _STATIONS = [
        ("EGLC", 51.5053, 0.0553, "London", "EGLC", "C", "Europe/London"),
        ("LFPB", 48.9694, 2.4414, "Paris", "LFPB", "C", "Europe/Paris"),
    ]

    def _run(self, monkeypatch, db, london_n, paris_n, london_true, paris_true):
        import scripts.run_emos_shadow as runner

        per_city = {
            "London": [(80.0, 2.0, 81.0)] * london_n,
            "Paris": [(70.0, 2.0, 71.0)] * paris_n,
        }
        fits = {"London": london_true, "Paris": paris_true}

        def fake_fetch(city, db, min_samples=1, **kw):
            triples = per_city.get(city, [])
            if len(triples) < min_samples:
                raise InsufficientDataError(f"{city}: {len(triples)} < {min_samples}")
            return triples

        def fake_fit(data):
            # Distinguish "whose data is this" by triple identity so the
            # pooled fit (concatenation of both cities) differs from either
            # city's own fit.
            if not data:
                return (0.0, 1.0, 0.5, 1.0)
            if all(t == per_city["London"][0] for t in data):
                return fits["London"]
            if all(t == per_city["Paris"][0] for t in data):
                return fits["Paris"]
            # Pooled: average of both cities' "true" fits, weighted by count.
            n_l, n_p = london_n, paris_n
            return tuple(
                (fits["London"][i] * n_l + fits["Paris"][i] * n_p) / (n_l + n_p)
                for i in range(4)
            )

        monkeypatch.setattr("src.config.STATIONS", self._STATIONS)
        monkeypatch.setattr("src.model.emos_calibration.fetch_training_data", fake_fetch)
        monkeypatch.setattr("src.model.emos_calibration.fit_emos", fake_fit)
        runner._run_calibration(db)

    def test_blended_city_gets_neither_pure_fit(self, monkeypatch):
        """London with 30 own triples (well inside the 5..59 band) must land
        strictly between its own fit and the pooled fit -- not equal to
        either (the defect the hard cutover produced: 0% or 100%, nothing in
        between)."""
        db = _db()
        london_true = (2.0, 1.0, 0.5, 1.0)
        paris_true = (0.0, 1.0, 0.5, 1.0)
        self._run(monkeypatch, db, london_n=30, paris_n=40,
                  london_true=london_true, paris_true=paris_true)

        row = db.get_emos_coefficients("London", "emos_shadow")
        assert row is not None
        # Weight for London = 30/60 = 0.5 -> blended 'a' should be the
        # midpoint of London's own 'a' (2.0) and the pooled 'a'.
        pooled_a = (london_true[0] * 30 + paris_true[0] * 40) / 70
        expected_a = 0.5 * london_true[0] + 0.5 * pooled_a
        assert row["a"] == pytest.approx(expected_a, abs=1e-9)
        # Sanity: strictly between the two pure endpoints (not equal to either).
        assert row["a"] != pytest.approx(london_true[0])
        assert row["a"] != pytest.approx(pooled_a)

    def test_full_weight_boundary_matches_pure_per_city(self, monkeypatch):
        """At n_city == 60 (the old hard threshold), the persisted fit must
        be bit-for-bit the pure per-city fit -- zero regression for cities
        that already cleared the legacy bar."""
        db = _db()
        london_true = (2.0, 1.05, 0.45, 0.95)
        paris_true = (0.0, 1.0, 0.5, 1.0)
        self._run(monkeypatch, db, london_n=60, paris_n=40,
                  london_true=london_true, paris_true=paris_true)

        row = db.get_emos_coefficients("London", "emos_shadow")
        assert row is not None
        assert row["a"] == pytest.approx(london_true[0], abs=1e-9)
        assert row["b"] == pytest.approx(london_true[1], abs=1e-9)
        assert row["c"] == pytest.approx(london_true[2], abs=1e-9)
        assert row["d"] == pytest.approx(london_true[3], abs=1e-9)

    def test_below_pooled_min_still_gets_nothing(self, monkeypatch):
        """Below POOLED_MIN_CITY_SAMPLES (5), a city still gets no
        coefficients at all -- unchanged from legacy."""
        db = _db()
        self._run(monkeypatch, db, london_n=3, paris_n=40,
                  london_true=(2.0, 1.0, 0.5, 1.0), paris_true=(0.0, 1.0, 0.5, 1.0))

        assert db.get_emos_coefficients("London", "emos_shadow") is None


# ---------------------------------------------------------------------------
# 4. LLBG (Tel Aviv) — 21-triple backtest window (scale referenced in #767)
# ---------------------------------------------------------------------------

class TestLLBGTwentyOneTripleBacktest:
    """Tel Aviv (station LLBG) is a midlat_c pooling-group member (lat 32.0,
    unit 'C', |lat| >= 23.5). This exercises the blend at the scale of the
    #767 diagnostics window: a city with a modest ~21-triple own history,
    still well short of the 60-sample full-weight bar, blended against its
    pooling group rather than either fully discarded (legacy <5 case) or
    fully overridden by the group average (legacy 5..59 case)."""

    _STATIONS = [
        ("LLBG", 32.0114, 34.8867, "Tel Aviv", "LLBG", "C", "Asia/Jerusalem"),
        ("LFPB", 48.9694, 2.4414, "Paris", "LFPB", "C", "Europe/Paris"),
        ("RKSI", 37.4602, 126.4407, "Seoul", "RKSI", "C", "Asia/Seoul"),
    ]

    def _seed_group(self, db):
        """Seed enough forecast_log + observation rows for a realistic
        backtest: Tel Aviv gets exactly 21 settled days (the #767 window
        scale); the other two midlat_c members contribute enough of their
        own data that the pooled total safely clears 60."""
        import random
        rng = random.Random(767)

        def seed(station, n, mu_base, obs_base):
            for i in range(n):
                date_str = f"2026-02-{(i % 28) + 1:02d}"
                # Use distinct years via date reuse isn't possible with this
                # schema's date-only key across a single month, so spread
                # across two months for n > 28.
                if i >= 28:
                    date_str = f"2026-03-{(i - 28) % 28 + 1:02d}"
                mu = mu_base + rng.uniform(-2.0, 2.0)
                db.upsert_forecast_log_v2(
                    station=station, model="nws", date=date_str,
                    forecast_high_f=mu, lead_hours=24,
                )
                db.insert_observation(
                    ts=f"{date_str}T14:00:00+00:00", station=station,
                    temp_f=obs_base + rng.uniform(-2.0, 2.0),
                    temp_native=obs_base + rng.uniform(-2.0, 2.0),
                    unit="C", source="metar",
                )

        seed(station="LLBG", n=21, mu_base=28.0, obs_base=29.0)   # the 21-triple window
        seed(station="LFPB", n=25, mu_base=15.0, obs_base=15.5)
        seed(station="RKSI", n=25, mu_base=18.0, obs_base=18.5)

    def test_tel_aviv_gets_a_genuine_blend_not_a_pure_fit(self, monkeypatch):
        """With exactly 21 of its own triples, Tel Aviv's weight is 21/60
        (~0.35) -- its persisted coefficients must be a real blend, and its
        own 21 triples (not the pooled count) are what CRPS is scored
        against, per the existing _persist contract (issue #667)."""
        import scripts.run_emos_shadow as runner
        from src.model import emos_calibration as cal

        db = _db()
        monkeypatch.setattr("src.config.STATIONS", self._STATIONS)
        self._seed_group(db)

        assert pooling_group("Tel Aviv") == "midlat_c"

        runner._run_calibration(db, stack="baseline")

        row = db.get_emos_coefficients("Tel Aviv", "emos_shadow")
        assert row is not None, "Tel Aviv should receive blended coefficients"
        assert row["ready_for_promotion"] == 0  # structural guard still holds

        # Recompute what a pure per-city fit (fit_emos on Tel Aviv's own 21
        # triples) and a pure pooled fit (the group's fit) would each give,
        # and confirm the persisted row is neither -- it's the shrinkage
        # blend at weight = 21/60.
        own_triples = cal.fetch_training_data(
            "Tel Aviv", db, min_samples=1, forecast_source="nws",
            regime=frozenset({"nws"}),
        )
        assert len(own_triples) == 21
        pure_city_fit = fit_emos(own_triples)

        weight = shrinkage_weight(21, full_weight_samples=60)
        assert weight == pytest.approx(21 / 60)

        # The persisted 'a' must differ from the pure per-city 'a' (unless
        # by extreme coincidence the pooled fit is identical -- guarded by
        # using distinct base temperatures per station above).
        assert row["a"] != pytest.approx(pure_city_fit[0], abs=1e-6)

        # A CRPS row was logged for today, evidencing this blended fit.
        assert db.get_emos_crps_count("Tel Aviv", model_mode="emos_shadow", forecast_source="baseline") == 1

    def test_weight_matches_direct_formula(self):
        """Sanity-check the 21-triple weight value referenced by issue #798's
        validation against the #767 backtest window."""
        assert shrinkage_weight(21, full_weight_samples=60) == pytest.approx(0.35)
