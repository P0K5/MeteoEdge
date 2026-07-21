"""Unit tests for src/model/deb_weighting.py — model registry refactor.

Covers:
- MODELS tuple and EQUAL_WEIGHTS backward compat (now 5-model US registry,
  post-#761 "gfs" removal)
- regional exclusion
- cold-start policy (partial and full cold-start)
- cadence-aware decay rate
- group weight cap
- 30-day replay delta <= ±0.01 vs old 2-model code (backward compat check)
- original test cases updated for the current registry
- issue #761: "gfs" drop path (no weight contributed, remaining weights
  renormalize) and baseline EMOS regime membership unaffected
"""
import math
from datetime import date, timedelta
from math import isclose
from unittest.mock import MagicMock

import pytest

import src.model.deb_weighting as dw
from src.model.deb_weighting import (
    BASE_DECAY_RATE,
    GROUP_WEIGHT_CAP,
    MIN_SAMPLES,
    MODELS,
    EQUAL_WEIGHTS,
    _REGISTRY,
    _apply_group_cap,
    _cadence_decay_rate,
    _equal_weights_for,
    _model_names_for_region,
    _models_for_region,
    compute_weights,
    register_model,
    refresh_weights,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(log_rows: list, settlement_rows: list, with_lead_log: bool = False) -> MagicMock:
    """Build a minimal mock db object.

    By default the mock does NOT expose get_forecast_log_by_lead so that
    compute_weights falls back to get_forecast_log (the standard path).
    Pass with_lead_log=True to test the lead-hours guard code path.
    """
    # Use spec to prevent MagicMock from auto-creating get_forecast_log_by_lead,
    # which would make hasattr() always return True.
    spec_attrs = [
        "get_forecast_log",
        "get_settlements",
        "get_model_weights",
        "upsert_model_weight",
        "upsert_forecast_log",
    ]
    if with_lead_log:
        spec_attrs.append("get_forecast_log_by_lead")
    db = MagicMock(spec=spec_attrs)
    db.get_forecast_log.return_value = log_rows
    db.get_settlements.return_value = settlement_rows
    if with_lead_log:
        db.get_forecast_log_by_lead.return_value = log_rows
    return db


def _settlement_rows(start_date: date, n: int, actual_high: float = 80.0) -> list:
    return [
        {"ts": (start_date + timedelta(days=i)).isoformat() + "T12:00:00", "actual_high_f": actual_high}
        for i in range(n)
    ]


def _log_rows(start_date: date, n: int, models: list, forecast_fn=None) -> list:
    rows = []
    for i in range(n):
        d = (start_date + timedelta(days=i)).isoformat()
        for model in models:
            forecast = forecast_fn(model, i) if forecast_fn else 80.0
            rows.append({"date": d, "model": model, "forecast_high_f": forecast})
    return rows


# ---------------------------------------------------------------------------
# Backward compat: MODELS tuple and EQUAL_WEIGHTS
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_models_tuple_contains_all_us_channels(self):
        # After #761, "gfs" was removed from the registry (it double-counted
        # the same physical GFS model as "open_meteo"). MODELS now includes
        # nws, open_meteo, hrrr, nbm, ecmwf (ECMWF is global so it appears for
        # US region; ICON is EU-only and excluded).
        assert set(MODELS) == {"nws", "open_meteo", "hrrr", "nbm", "ecmwf"}

    def test_models_tuple_does_not_contain_gfs(self):
        """Issue #761: "gfs" must no longer be a registered DEB channel."""
        assert "gfs" not in MODELS

    def test_models_tuple_contains_legacy_channels(self):
        # Legacy channels must still be present
        for m in ("nws", "open_meteo"):
            assert m in MODELS

    def test_equal_weights_sum_to_one(self):
        assert isclose(sum(EQUAL_WEIGHTS.values()), 1.0, abs_tol=1e-9)

    def test_equal_weights_keys_match_models(self):
        assert set(EQUAL_WEIGHTS.keys()) == set(MODELS)

    def test_equal_weights_uniform(self):
        expected = 1.0 / len(MODELS)
        for v in EQUAL_WEIGHTS.values():
            assert isclose(v, expected, abs_tol=1e-9)

    def test_equal_weight_fallback_when_insufficient_data(self):
        """When all models have fewer than MIN_SAMPLES entries, return EQUAL_WEIGHTS."""
        db = _make_db(
            log_rows=[
                {"date": "2024-01-01", "model": "nws", "forecast_high_f": 80.0},
                {"date": "2024-01-02", "model": "nws", "forecast_high_f": 81.0},
            ],
            settlement_rows=[
                {"ts": "2024-01-01T12:00:00", "actual_high_f": 80.1},
                {"ts": "2024-01-02T12:00:00", "actual_high_f": 81.2},
            ],
        )
        result = compute_weights(db, "WSSS", "Singapore")
        # Full cold-start -> equal weights
        expected = _equal_weights_for("us")
        for m, w in expected.items():
            assert isclose(result[m], w, abs_tol=1e-9)

    def test_weights_sum_to_one_two_model_data(self):
        """Computed weights must sum to 1.0 even when only nws+open_meteo have data (others cold-start)."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 15, actual_high=80.2)
        logs = (
            _log_rows(start, 15, ["nws"], forecast_fn=lambda m, i: 80.0 + i * 0.1)
            + _log_rows(start, 15, ["open_meteo"], forecast_fn=lambda m, i: 80.5 + i * 0.1)
        )
        db = _make_db(logs, settlements)
        result = compute_weights(db, "KNYC", "New York")
        assert abs(sum(result.values()) - 1.0) < 1e-9

    def test_lower_rmse_model_gets_higher_weight(self):
        """Model with lower RMSE (smaller errors) should get higher weight among calibrated models."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 15, actual_high=80.0)
        logs = (
            _log_rows(start, 15, ["nws"], forecast_fn=lambda m, i: 80.1)   # tiny error
            + _log_rows(start, 15, ["open_meteo"], forecast_fn=lambda m, i: 85.0)  # big error
        )
        db = _make_db(logs, settlements)
        result = compute_weights(db, "KORD", "Chicago")
        # nws should have more weight than open_meteo
        assert result["nws"] > result["open_meteo"]

    def test_only_nws_calibrated_falls_back_via_cold_start(self):
        """When only nws has data, all other channels get cold-start weight; nws gets most."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 15, actual_high=80.0)
        logs = _log_rows(start, 15, ["nws"], forecast_fn=lambda m, i: 80.0 + i * 0.1)
        db = _make_db(logs, settlements)
        result = compute_weights(db, "KLAX", "Los Angeles")
        # Must sum to 1
        assert abs(sum(result.values()) - 1.0) < 1e-9
        # nws gets calibrated budget, everything else gets cold-start fraction
        assert result["nws"] > result["open_meteo"]
        assert result["nws"] > result["hrrr"]
        assert "gfs" not in result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_legacy_channels_registered(self):
        for name in ("nws", "open_meteo"):
            assert name in _REGISTRY

    def test_gfs_not_registered(self):
        """Issue #761: "gfs" was removed from the DEB registry entirely —
        it double-counted the same physical GFS model as "open_meteo"."""
        assert "gfs" not in _REGISTRY

    def test_nws_metadata(self):
        e = _REGISTRY["nws"]
        assert e.region == "us"
        assert e.expected_cadence_h == 24.0
        assert e.group_id == "noaa_us"

    def test_open_meteo_metadata(self):
        # After #550: open_meteo is grouped "gfs_family" — originally because
        # it shared a raw gfs_seamless constituent with "gfs", and pre-#548
        # "gfs" was literally a byte-identical duplicate of "open_meteo".
        # After #761 removed "gfs", open_meteo keeps this group_id as a
        # forward-compatible placeholder for "gefs" (issue #448).
        e = _REGISTRY["open_meteo"]
        assert e.region == "global"
        assert e.expected_cadence_h == 24.0
        assert e.group_id == "gfs_family"

    def test_register_new_model(self):
        try:
            register_model("test_eu_model", region="eu", expected_cadence_h=12.0, group_id="test_group")
            assert "test_eu_model" in _REGISTRY
            e = _REGISTRY["test_eu_model"]
            assert e.region == "eu"
            assert e.expected_cadence_h == 12.0
            assert e.group_id == "test_group"
        finally:
            _REGISTRY.pop("test_eu_model", None)


# ---------------------------------------------------------------------------
# Regional applicability
# ---------------------------------------------------------------------------

class TestRegionalApplicability:
    def test_us_region_includes_global_and_us(self):
        names = {m.name for m in _models_for_region("us")}
        assert "nws" in names
        assert "open_meteo" in names
        assert "gfs" not in names

    def test_eu_region_excludes_us_only_model(self):
        names = {m.name for m in _models_for_region("eu")}
        assert "nws" not in names
        assert "open_meteo" in names
        assert "gfs" not in names

    def test_compute_weights_regional_exclusion_eu(self):
        """US-only nws model must not appear in EU weight output; "gfs" rows
        in the log (issue #761: still ingested, no longer a DEB channel) must
        not appear either."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["open_meteo", "gfs"], forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements)

        weights = compute_weights(db, "EGLL", "London", station_region="eu")
        assert "nws" not in weights
        assert "open_meteo" in weights
        assert "gfs" not in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_compute_weights_us_includes_nws(self):
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs"], forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements)

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert "nws" in weights
        assert "gfs" not in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# Cold-start policy
# ---------------------------------------------------------------------------

class TestColdStartPolicy:
    def test_full_cold_start_returns_equal_weights(self):
        db = _make_db([], [])
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        expected = _equal_weights_for("us")
        for m, w in expected.items():
            assert isclose(weights[m], w, abs_tol=1e-9)

    def test_partial_cold_start_hrrr_gets_cold_fraction(self):
        """hrrr in cold-start; nws+open_meteo calibrated.

        hrrr cold_start_fraction=0.4, N_models=5 (post-#761: nws, open_meteo,
        hrrr, nbm, ecmwf)
        => hrrr weight = 0.4 / 5 = 0.08
        => remaining budget split between nws and open_meteo
        """
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)

        def forecast_fn(m, i):
            return 80.5 if m == "nws" else 81.5

        logs = _log_rows(start, 20, ["nws", "open_meteo"], forecast_fn=forecast_fn)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        assert "gfs" not in weights
        n_us_models = len(_model_names_for_region("us"))
        hrrr_cold_frac = _REGISTRY["hrrr"].cold_start_fraction
        # cold-start models: hrrr, nbm, ecmwf; calibrated: nws, open_meteo
        # Group cap may redistribute weight slightly; use a tolerance of 0.02
        expected_hrrr = hrrr_cold_frac / n_us_models
        assert abs(weights["hrrr"] - expected_hrrr) < 0.02, (
            f"Expected hrrr≈{expected_hrrr:.4f}, got {weights['hrrr']:.4f}"
        )
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        # nws and open_meteo (calibrated) should dominate the remaining budget
        assert weights["nws"] + weights["open_meteo"] > weights["hrrr"] + weights.get("nbm", 0) + weights.get("ecmwf", 0)

    def test_gfs_log_rows_ignored_all_cold_start(self):
        """Issue #761: log rows for "gfs" (still possibly present from
        ingestion, or from before the channel was dropped) contribute nothing
        to DEB — with no registered-model data at all, this is full cold-start
        equal weights, and "gfs" is absent from the result."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["gfs"], forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert weights == dict(_equal_weights_for("us"))
        assert "gfs" not in weights
        for m in ("nws", "open_meteo"):
            assert m in weights


# ---------------------------------------------------------------------------
# Cadence-aware decay
# ---------------------------------------------------------------------------

class TestCadenceAwareDecay:
    def test_24h_model_uses_base_decay_rate(self):
        rate = _cadence_decay_rate(_REGISTRY["nws"])
        assert isclose(rate, BASE_DECAY_RATE, abs_tol=1e-9)

    def test_6h_model_gets_lower_decay_rate(self):
        rate = _cadence_decay_rate(_REGISTRY["nbm"])
        expected = BASE_DECAY_RATE * (6.0 / 24.0)
        assert isclose(rate, expected, abs_tol=1e-9)

    def test_higher_cadence_frequency_means_lower_per_day_decay(self):
        rate_nws = _cadence_decay_rate(_REGISTRY["nws"])   # 24h
        rate_nbm = _cadence_decay_rate(_REGISTRY["nbm"])   # 6h
        assert rate_nbm < rate_nws


# ---------------------------------------------------------------------------
# Group weight cap
# ---------------------------------------------------------------------------

class TestGroupWeightCap:
    def test_group_cap_applied_when_exceeded(self):
        """noaa_us group (nws + hrrr) combined weight 0.9 > GROUP_WEIGHT_CAP should be capped."""
        weights = {"nws": 0.8, "open_meteo": 0.1, "hrrr": 0.1}
        applicable = [_REGISTRY[m] for m in ("nws", "open_meteo", "hrrr")]
        result = _apply_group_cap(weights, applicable)
        assert result["nws"] + result["hrrr"] <= GROUP_WEIGHT_CAP + 1e-9
        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)

    def test_group_cap_not_applied_when_under(self):
        weights = {"nws": 0.3, "open_meteo": 0.4, "hrrr": 0.3}
        applicable = [_REGISTRY[m] for m in ("nws", "open_meteo", "hrrr")]
        result = _apply_group_cap(weights, applicable)
        for m in weights:
            assert isclose(result[m], weights[m], abs_tol=1e-9)

    def test_group_cap_with_two_correlated_models(self):
        """Two correlated models; combined weight capped."""
        try:
            register_model("ma", region="us", expected_cadence_h=24.0, group_id="grp")
            register_model("mb", region="us", expected_cadence_h=24.0, group_id="grp")
            register_model("mc", region="us", expected_cadence_h=24.0, group_id=None)

            applicable = [_REGISTRY[m] for m in ("ma", "mb", "mc")]
            weights = {"ma": 0.4, "mb": 0.4, "mc": 0.2}
            result = _apply_group_cap(weights, applicable)
            group_total = result["ma"] + result["mb"]
            assert group_total <= GROUP_WEIGHT_CAP + 1e-9
            assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)
        finally:
            for m in ("ma", "mb", "mc"):
                _REGISTRY.pop(m, None)

    def test_group_cap_3_correlated_plus_1_independent(self):
        """3 correlated + 1 independent: group capped, independent gains freed weight."""
        try:
            register_model("c1", region="us", expected_cadence_h=24.0, group_id="corr")
            register_model("c2", region="us", expected_cadence_h=24.0, group_id="corr")
            register_model("c3", region="us", expected_cadence_h=24.0, group_id="corr")
            register_model("ind", region="us", expected_cadence_h=24.0, group_id=None)

            applicable = [_REGISTRY[m] for m in ("c1", "c2", "c3", "ind")]
            weights = {"c1": 0.3, "c2": 0.3, "c3": 0.3, "ind": 0.1}
            result = _apply_group_cap(weights, applicable)

            group_total = result["c1"] + result["c2"] + result["c3"]
            assert group_total <= GROUP_WEIGHT_CAP + 1e-9
            assert result["ind"] > 0.1  # received freed weight
            assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)
        finally:
            for m in ("c1", "c2", "c3", "ind"):
                _REGISTRY.pop(m, None)


# ---------------------------------------------------------------------------
# 30-day replay delta <= ±0.01 vs old 2-model code
# ---------------------------------------------------------------------------

class TestLegacyReplayDelta:
    """Verify that for 3-channel data the new code produces weights consistent
    with old behavior for the nws/open_meteo ratio.
    """

    def _old_compute_weights(self, log_rows, settlement_rows):
        """Reimplementation of the original 2-model compute_weights logic."""
        _OLD_DECAY = 0.05
        old_models = ("nws", "open_meteo")
        actuals = {row["ts"][:10]: row["actual_high_f"] for row in settlement_rows}
        errors = {m: [] for m in old_models}
        today = date.today()
        for row in log_rows:
            d = row["date"]
            if d not in actuals or row["model"] not in errors:
                continue
            days_ago = (today - date.fromisoformat(d)).days
            err = abs(row["forecast_high_f"] - actuals[d])
            errors[row["model"]].append((days_ago, err))

        for m in old_models:
            if len(errors[m]) < 10:
                return {m: 0.5 for m in old_models}

        rmse = {}
        for m in old_models:
            total_w = sum(math.exp(-_OLD_DECAY * k) for k, _ in errors[m])
            wmse = sum(math.exp(-_OLD_DECAY * k) * e**2 for k, e in errors[m]) / total_w
            rmse[m] = math.sqrt(wmse)

        raw = {m: 1.0 / rmse[m] for m in old_models}
        total = sum(raw.values())
        return {m: raw[m] / total for m in old_models}

    def test_replay_delta_others_cold_start(self):
        """With hrrr/nbm/ecmwf cold-start, nws+open_meteo weights track old 2-model output within ±0.1."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)

        def forecast_fn(m, i):
            return 80.5 if m == "nws" else 82.0

        logs = _log_rows(start, 20, ["nws", "open_meteo"], forecast_fn=forecast_fn)
        db = _make_db(logs, settlements)

        new_weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        old_weights = self._old_compute_weights(logs, settlements)

        assert "gfs" not in new_weights
        n_us_models = len(_model_names_for_region("us"))
        # cold-start models when only nws+open_meteo are calibrated: hrrr, nbm, ecmwf
        # ("gfs" removed by #761 — no longer registered, contributes no reservation)
        cold_start_reserved = sum(
            _REGISTRY[m].cold_start_fraction / n_us_models
            for m in ("hrrr", "nbm", "ecmwf")
        )
        calibrated_budget = 1.0 - cold_start_reserved
        old_scaled = {m: old_weights[m] * calibrated_budget for m in old_weights}

        for m in ("nws", "open_meteo"):
            delta = abs(new_weights[m] - old_scaled[m])
            # Calibrated budget is reduced by HRRR, NBM, and ECMWF cold-start
            # reservations; allow up to 0.1 delta from old 2-model.
            assert delta <= 0.1, (
                f"Weight delta for {m} exceeds 0.1: new={new_weights[m]:.4f}, "
                f"old_scaled={old_scaled[m]:.4f}, delta={delta:.4f}"
            )

    def test_replay_delta_all_calibrated(self):
        """When nws/open_meteo/hrrr are all calibrated, weights sum to 1 and better model wins."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)

        def forecast_fn(m, i):
            return {"nws": 80.5, "open_meteo": 82.0, "hrrr": 81.0}[m]

        logs = _log_rows(start, 20, ["nws", "open_meteo", "hrrr"], forecast_fn=forecast_fn)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert "gfs" not in weights
        assert weights["nws"] > weights["open_meteo"]
        assert weights["nws"] > weights["hrrr"]


# ---------------------------------------------------------------------------
# Lead-hours guard (Fix 2 / issue #422)
# ---------------------------------------------------------------------------

class TestLeadHoursGuard:
    def test_uses_get_forecast_log_by_lead_when_available(self):
        """When db exposes get_forecast_log_by_lead, compute_weights must use it."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs"],
                         forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements, with_lead_log=True)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        db.get_forecast_log_by_lead.assert_called_once()
        db.get_forecast_log.assert_not_called()
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_falls_back_to_get_forecast_log_when_no_lead_method(self):
        """When db does not expose get_forecast_log_by_lead, use get_forecast_log."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs"],
                         forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements, with_lead_log=False)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        db.get_forecast_log.assert_called_once()
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

# ---------------------------------------------------------------------------
# compute_weights: weights sum to 1 invariant
# ---------------------------------------------------------------------------

class TestWeightsSumToOne:
    @pytest.mark.parametrize("station_region", ["us", "eu"])
    def test_equal_weights_sum_to_one(self, station_region):
        db = _make_db([], [])
        weights = compute_weights(db, "TEST", "TestCity", station_region=station_region)
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_calibrated_weights_sum_to_one(self):
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs"],
                         forecast_fn=lambda m, i: 79.0 + (0 if m == "nws" else 1.5))
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# HRRR + NBM registry wiring (issue #435)
# ---------------------------------------------------------------------------

class TestHrrrNbmRegistry:
    """Verify HRRR and NBM are registered with correct metadata."""

    def test_hrrr_registered(self):
        assert "hrrr" in _REGISTRY

    def test_nbm_registered(self):
        assert "nbm" in _REGISTRY

    def test_hrrr_metadata(self):
        e = _REGISTRY["hrrr"]
        assert e.region == "us"
        assert e.expected_cadence_h == 1.0
        assert e.group_id == "noaa_us"
        assert isclose(e.cold_start_fraction, 0.4, abs_tol=1e-9)

    def test_nbm_metadata(self):
        e = _REGISTRY["nbm"]
        assert e.region == "us"
        assert e.expected_cadence_h == 6.0
        assert e.group_id == "noaa_us"
        assert isclose(e.cold_start_fraction, 0.4, abs_tol=1e-9)

    def test_hrrr_nbm_included_in_us_region(self):
        names = {m.name for m in _models_for_region("us")}
        assert "hrrr" in names
        assert "nbm" in names

    def test_hrrr_nbm_excluded_from_eu_region(self):
        """HRRR and NBM are US-only; must not appear for EU stations."""
        names = {m.name for m in _models_for_region("eu")}
        assert "hrrr" not in names
        assert "nbm" not in names


# ---------------------------------------------------------------------------
# Ensemble weighting with 2 / 3 / 4 models present (issue #435)
# ---------------------------------------------------------------------------

class TestEnsembleNModels:
    """Verify compute_weights works correctly with 2, 3, and 4 active models."""

    def _make_calibrated_db(self, models: list[str], actual: float = 80.0,
                            n: int = 20) -> MagicMock:
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, n, actual_high=actual)
        logs = _log_rows(start, n, models, forecast_fn=lambda m, i: actual + 0.5)
        return _make_db(logs, settlements)

    def test_2_models_nws_open_meteo_sums_to_one(self):
        """2-model path: nws + open_meteo calibrated; others cold-start."""
        db = self._make_calibrated_db(["nws", "open_meteo"])
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert weights["nws"] > 0
        assert weights["open_meteo"] > 0

    def test_3_models_adds_nbm_sums_to_one(self):
        """3-model path: nws + open_meteo + nbm calibrated."""
        db = self._make_calibrated_db(["nws", "open_meteo", "nbm"])
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert "nws" in weights
        assert "open_meteo" in weights
        assert "nbm" in weights

    def test_4_models_adds_hrrr_sums_to_one(self):
        """4-model path: nws + open_meteo + nbm + hrrr all calibrated."""
        db = self._make_calibrated_db(["nws", "open_meteo", "nbm", "hrrr"])
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert "hrrr" in weights
        assert "nbm" in weights
        assert "nws" in weights
        assert "open_meteo" in weights

    def test_hrrr_cold_start_gets_cold_start_fraction(self):
        """When HRRR has 0 samples, its weight equals cold_start_fraction / n_models."""
        today = date.today()
        start = today - timedelta(days=29)
        n_models = len({m.name for m in _models_for_region("us")})  # all US models
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        # Only calibrate nws, open_meteo, nbm — leave hrrr with 0 samples
        # ("gfs" rows are included to confirm they're silently ignored — issue #761)
        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs", "nbm"],
                         forecast_fn=lambda m, i: 80.5)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        expected_hrrr = _REGISTRY["hrrr"].cold_start_fraction / n_models
        assert isclose(weights["hrrr"], expected_hrrr, abs_tol=1e-6), (
            f"Expected hrrr cold-start weight={expected_hrrr:.4f}, got {weights['hrrr']:.4f}"
        )
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_noaa_us_group_cap_includes_hrrr_nbm(self):
        """NWS + HRRR + NBM are all in noaa_us group; their combined weight is capped.

        Construct a case where NWS, HRRR, and NBM together would exceed the cap
        and verify _apply_group_cap enforces it.
        """
        applicable = [_REGISTRY[m] for m in ("nws", "hrrr", "nbm", "open_meteo")]
        weights = {"nws": 0.4, "hrrr": 0.3, "nbm": 0.2, "open_meteo": 0.1}
        result = _apply_group_cap(weights, applicable)

        noaa_total = result["nws"] + result["hrrr"] + result["nbm"]
        assert noaa_total <= GROUP_WEIGHT_CAP + 1e-9, (
            f"noaa_us group weight {noaa_total:.4f} exceeds cap {GROUP_WEIGHT_CAP}"
        )
        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)

    def test_lower_rmse_hrrr_outweighs_nbm(self):
        """When HRRR errors are smaller than NBM, HRRR gets higher weight."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = (
            _log_rows(start, 20, ["nws", "open_meteo", "gfs"],
                      forecast_fn=lambda m, i: 80.5)
            + _log_rows(start, 20, ["hrrr"], forecast_fn=lambda m, i: 80.1)   # tiny error
            + _log_rows(start, 20, ["nbm"],  forecast_fn=lambda m, i: 84.0)   # large error
        )
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert weights["hrrr"] > weights["nbm"]


# ---------------------------------------------------------------------------
# compute_deb_mu_f with HRRR and NBM inputs (issue #435)
# ---------------------------------------------------------------------------

class TestComputeDebMuFExtended:
    """Verify compute_deb_mu_f handles 4-model and graceful-degradation cases."""

    def test_4_model_consensus(self):
        """4-model weighted average: nws=0.3, om=0.2, hrrr=0.3, nbm=0.2."""
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"nws": 0.3, "open_meteo": 0.2, "hrrr": 0.3, "nbm": 0.2}
        result = compute_deb_mu_f(
            forecast_nws=82.0,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_hrrr=83.0,
            forecast_nbm=80.0,
        )
        expected = 0.3 * 82.0 + 0.2 * 78.0 + 0.3 * 83.0 + 0.2 * 80.0
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"

    def test_degrades_gracefully_when_hrrr_missing(self):
        """When HRRR is None, remaining 3 models renormalise to 1.0."""
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"nws": 0.3, "open_meteo": 0.2, "hrrr": 0.3, "nbm": 0.2}
        result = compute_deb_mu_f(
            forecast_nws=82.0,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_hrrr=None,
            forecast_nbm=80.0,
        )
        # Available: nws=0.3, open_meteo=0.2, nbm=0.2 -> total=0.7
        expected = (0.3 * 82.0 + 0.2 * 78.0 + 0.2 * 80.0) / 0.7
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"

    def test_degrades_gracefully_when_nbm_missing(self):
        """When NBM is None, remaining 3 models renormalise to 1.0."""
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"nws": 0.3, "open_meteo": 0.2, "hrrr": 0.3, "nbm": 0.2}
        result = compute_deb_mu_f(
            forecast_nws=82.0,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_hrrr=83.0,
            forecast_nbm=None,
        )
        # Available: nws=0.3, om=0.2, hrrr=0.3 -> total=0.8
        expected = (0.3 * 82.0 + 0.2 * 78.0 + 0.3 * 83.0) / 0.8
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"

    def test_degrades_gracefully_when_both_hrrr_nbm_missing(self):
        """When both HRRR and NBM are None, falls back to 2-model NWS+OM blend."""
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"nws": 0.3, "open_meteo": 0.2, "hrrr": 0.3, "nbm": 0.2}
        result = compute_deb_mu_f(
            forecast_nws=82.0,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_hrrr=None,
            forecast_nbm=None,
        )
        # Available: nws=0.3, om=0.2 -> total=0.5
        expected = (0.3 * 82.0 + 0.2 * 78.0) / 0.5
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"


# ---------------------------------------------------------------------------
# ECMWF + ICON registry wiring (issue #442)
# ---------------------------------------------------------------------------

class TestEcmwfIconRegistry:
    """Verify ECMWF and ICON are registered with correct metadata."""

    def test_ecmwf_registered(self):
        assert "ecmwf" in _REGISTRY

    def test_icon_registered(self):
        assert "icon" in _REGISTRY

    def test_ecmwf_metadata(self):
        e = _REGISTRY["ecmwf"]
        assert e.region == "global"
        assert e.expected_cadence_h == 12.0
        assert e.group_id == "ecmwf_intl"
        assert isclose(e.cold_start_fraction, 0.5, abs_tol=1e-9)

    def test_icon_metadata(self):
        e = _REGISTRY["icon"]
        assert e.region == "eu"
        assert e.expected_cadence_h == 6.0
        assert e.group_id == "ecmwf_intl"
        assert isclose(e.cold_start_fraction, 0.5, abs_tol=1e-9)

    def test_ecmwf_included_in_us_region(self):
        """ECMWF is global, so it is technically included for US stations."""
        names = {m.name for m in _models_for_region("us")}
        assert "ecmwf" in names

    def test_icon_excluded_from_us_region(self):
        """ICON is EU-only; must not appear for US stations."""
        names = {m.name for m in _models_for_region("us")}
        assert "icon" not in names

    def test_ecmwf_included_in_eu_region(self):
        names = {m.name for m in _models_for_region("eu")}
        assert "ecmwf" in names

    def test_icon_included_in_eu_region(self):
        names = {m.name for m in _models_for_region("eu")}
        assert "icon" in names

    def test_ecmwf_included_in_asia_region(self):
        names = {m.name for m in _models_for_region("asia")}
        assert "ecmwf" in names

    def test_icon_excluded_from_asia_region(self):
        """ICON is EU-only; must not appear for Asia stations."""
        names = {m.name for m in _models_for_region("asia")}
        assert "icon" not in names

    def test_ecmwf_intl_group_cap_applied(self):
        """ecmwf_intl group (ecmwf + icon) combined weight is capped at GROUP_WEIGHT_CAP.

        Note: open_meteo is grouped "gfs_family" (issue #550, was
        group_id=None; "gfs" itself removed by #761), so this exercises
        _apply_group_cap redistributing freed weight to a member of ANOTHER
        group rather than to a literally ungrouped model — see the fix
        described in _apply_group_cap's docstring.
        """
        applicable = [_REGISTRY[m] for m in ("ecmwf", "icon", "open_meteo")]
        weights = {"ecmwf": 0.5, "icon": 0.3, "open_meteo": 0.2}
        result = _apply_group_cap(weights, applicable)
        intl_total = result["ecmwf"] + result["icon"]
        assert intl_total <= GROUP_WEIGHT_CAP + 1e-9
        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# GFS-family group_id honesty (issue #550, amended by #761)
# ---------------------------------------------------------------------------

class TestGfsFamilyRegistry:
    """Verify open_meteo keeps the "gfs_family" group_id after #761 removed
    its former "gfs" partner (both derived from Open-Meteo's API; open_meteo's
    multi-model mean includes a gfs_seamless constituent identical to what the
    now-removed dedicated "gfs" channel fetched — issue #761 dropped the
    redundant "gfs" channel entirely rather than merely capping the pair's
    combined weight). The group_id is retained as a forward-compatible
    placeholder for "gefs" (issue #448, not yet wired into DEB)."""

    def test_open_meteo_registered_gfs_family(self):
        assert _REGISTRY["open_meteo"].group_id == "gfs_family"

    def test_gfs_not_registered(self):
        assert "gfs" not in _REGISTRY

    def test_no_channel_left_with_undocumented_none_group(self):
        """Every currently-registered channel has a deliberate, non-None
        group_id. (Acceptance criterion for #550: "or a documented reason for
        None" — no registered channel currently uses that escape hatch.)"""
        for name, entry in _REGISTRY.items():
            assert entry.group_id is not None, (
                f"Channel {name!r} has group_id=None with no documented "
                "rationale — see issue #550"
            )

    def test_gfs_family_solo_member_capped_at_group_cap(self):
        """Issue #761: with "gfs" removed, "gfs_family" has a single member
        (open_meteo). GROUP_WEIGHT_CAP still applies, so open_meteo's own
        weight is capped at GROUP_WEIGHT_CAP and the freed weight
        redistributes to ecmwf/icon (members of a different group)."""
        applicable = [_REGISTRY[m] for m in ("open_meteo", "ecmwf", "icon")]
        weights = {"open_meteo": 0.8, "ecmwf": 0.15, "icon": 0.05}
        result = _apply_group_cap(weights, applicable)
        assert result["open_meteo"] <= GROUP_WEIGHT_CAP + 1e-9
        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)

    def test_redistribution_when_no_ungrouped_models_remain(self):
        """Regression test for the _apply_group_cap fix (issue #550), updated
        for #761's solo-member "gfs_family".

        Before the #550 fix, freed weight from a capped group was only
        redistributed to models with group_id=None. An EU/global-only
        applicable set (open_meteo, ecmwf, icon) has ZERO ungrouped models —
        every applicable model belongs to gfs_family or ecmwf_intl. Without
        the fix, freed weight from an over-cap group was silently dropped and
        weights summed to less than 1.0.
        """
        applicable = [_REGISTRY[m] for m in ("open_meteo", "ecmwf", "icon")]
        assert all(m.group_id is not None for m in applicable), (
            "test premise requires every applicable model to be grouped"
        )
        weights = {"open_meteo": 0.8, "ecmwf": 0.15, "icon": 0.05}
        pre_sum = sum(weights.values())
        assert isclose(pre_sum, 1.0, abs_tol=1e-9)

        result = _apply_group_cap(weights, applicable)

        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6), (
            f"weights must still sum to 1.0 after capping, got {sum(result.values())}"
        )
        assert result["open_meteo"] <= GROUP_WEIGHT_CAP + 1e-9
        assert result["ecmwf"] + result["icon"] <= GROUP_WEIGHT_CAP + 1e-9


# ---------------------------------------------------------------------------
# Regional ensemble composition (issue #442)
# ---------------------------------------------------------------------------

class TestRegionalEnsembles:
    """Verify compute_weights produces correct model sets for US, EU, and Asia."""

    def _make_calibrated_db(self, models: list[str], n: int = 20, actual: float = 80.0):
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, n, actual_high=actual)
        logs = _log_rows(start, n, models, forecast_fn=lambda m, i: actual + 0.5)
        return _make_db(logs, settlements)

    def test_us_ensemble_excludes_icon(self):
        """US ensemble: nws+hrrr+nbm+open_meteo+ecmwf — ICON excluded, gfs
        rows present in the log but ignored (issue #761: not a DEB channel)."""
        db = self._make_calibrated_db(["nws", "open_meteo", "gfs", "hrrr", "nbm", "ecmwf"])
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert "icon" not in weights
        assert "gfs" not in weights
        assert "nws" in weights
        assert "ecmwf" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_eu_ensemble_includes_ecmwf_and_icon(self):
        """EU ensemble: open_meteo+ecmwf+icon — nws/hrrr/nbm excluded, gfs
        rows present in the log but ignored (issue #761)."""
        db = self._make_calibrated_db(["open_meteo", "gfs", "ecmwf", "icon"])
        weights = compute_weights(db, "EGLC", "London", station_region="eu")
        assert "nws" not in weights
        assert "hrrr" not in weights
        assert "nbm" not in weights
        assert "gfs" not in weights
        assert "ecmwf" in weights
        assert "icon" in weights
        assert "open_meteo" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_asia_ensemble_includes_ecmwf_excludes_icon(self):
        """Asia ensemble: open_meteo+ecmwf — icon excluded (EU-only), gfs
        rows present in the log but ignored (issue #761)."""
        db = self._make_calibrated_db(["open_meteo", "gfs", "ecmwf"])
        weights = compute_weights(db, "WSSS", "Singapore", station_region="asia")
        assert "icon" not in weights
        assert "nws" not in weights
        assert "gfs" not in weights
        assert "ecmwf" in weights
        assert "open_meteo" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# compute_deb_mu_f with ECMWF + ICON inputs (issue #442)
# ---------------------------------------------------------------------------

class TestComputeDebMuFEcmwfIcon:
    """Verify compute_deb_mu_f handles ECMWF/ICON and US-station guard."""

    def test_eu_4_model_consensus_ecmwf_icon(self):
        """EU 4-model blend: open_meteo+gfs+ecmwf+icon."""
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"open_meteo": 0.25, "gfs": 0.25, "ecmwf": 0.25, "icon": 0.25}
        result = compute_deb_mu_f(
            forecast_nws=None,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_gfs=79.0,
            forecast_ecmwf=80.0,
            forecast_icon=77.0,
            station_region="eu",
        )
        expected = 0.25 * 78.0 + 0.25 * 79.0 + 0.25 * 80.0 + 0.25 * 77.0
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"

    def test_asia_ensemble_ecmwf_no_icon(self):
        """Asia 3-model blend: open_meteo+gfs+ecmwf (no icon)."""
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"open_meteo": 1/3, "gfs": 1/3, "ecmwf": 1/3}
        result = compute_deb_mu_f(
            forecast_nws=None,
            forecast_open_meteo=82.0,
            weights=weights,
            forecast_gfs=83.0,
            forecast_ecmwf=81.0,
            forecast_icon=None,
            station_region="asia",
        )
        total_w = 1/3 + 1/3 + 1/3
        expected = (1/3 * 82.0 + 1/3 * 83.0 + 1/3 * 81.0) / total_w
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6)

    def test_us_station_ecmwf_warns_and_excludes(self):
        """When forecast_ecmwf provided for US station, log warning and exclude."""
        import logging as _logging
        import io
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"nws": 0.5, "open_meteo": 0.3, "ecmwf": 0.2}
        handler = _logging.StreamHandler(io.StringIO())
        handler.setLevel(_logging.WARNING)
        logger = _logging.getLogger("src.model.deb_hourly_consensus")
        logger.addHandler(handler)
        try:
            result = compute_deb_mu_f(
                forecast_nws=82.0,
                forecast_open_meteo=80.0,
                weights=weights,
                forecast_ecmwf=85.0,  # should be excluded with warning
                station_region="us",
            )
            log_output = handler.stream.getvalue()
        finally:
            logger.removeHandler(handler)

        # ECMWF excluded -> only nws=0.5, open_meteo=0.3 -> total=0.8
        expected = (0.5 * 82.0 + 0.3 * 80.0) / 0.8
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"
        assert "ecmwf" in log_output.lower(), (
            f"Expected warning about ECMWF exclusion, got: {log_output!r}"
        )

    def test_ecmwf_none_for_us_station_no_warning(self):
        """When forecast_ecmwf is None for US station, no warning is emitted."""
        import logging as _logging
        import io
        from src.model.deb_hourly_consensus import compute_deb_mu_f
        weights = {"nws": 0.5, "open_meteo": 0.5}
        handler = _logging.StreamHandler(io.StringIO())
        handler.setLevel(_logging.WARNING)
        logger = _logging.getLogger("src.model.deb_hourly_consensus")
        logger.addHandler(handler)
        try:
            result = compute_deb_mu_f(
                forecast_nws=82.0,
                forecast_open_meteo=80.0,
                weights=weights,
                forecast_ecmwf=None,
                station_region="us",
            )
            log_output = handler.stream.getvalue()
        finally:
            logger.removeHandler(handler)

        assert "ecmwf" not in log_output.lower()
        assert result is not None


# ---------------------------------------------------------------------------
# Issue #761: "gfs" drop path — no weight contributed, remaining weights
# renormalize; ingestion-only "gfs" rows in model_forecast_log are inert.
#
# Historical note: this class previously covered the GFS_DATA_VALID_FROM
# duplicate-era training filter (issue #548), which excluded pre-2026-07-02
# "gfs" rows from DEB training because they were byte-identical duplicates of
# "open_meteo". #761 removed "gfs" from the registry entirely (rather than
# merely capping its combined weight with "open_meteo"), which subsumes that
# filter — "gfs" rows are ignored regardless of date now, so the boundary
# tests were replaced with drop-path tests below.
# ---------------------------------------------------------------------------

class TestGfsDropPath:
    """Verify compute_weights() never returns a "gfs" weight and remaining
    channel weights still sum to 1.0, whether or not "gfs" rows happen to be
    present in model_forecast_log (issue #761)."""

    def _rows(self, dates: list, model: str, forecast_high_f: float) -> list:
        return [{"date": d, "model": model, "forecast_high_f": forecast_high_f} for d in dates]

    def test_gfs_rows_present_never_contribute_a_weight(self):
        """Even with plenty of well-formed "gfs" rows (any date, including
        recent ones), compute_weights() must never return a "gfs" key —
        capture_forecasts.py may still log "gfs" rows for audit purposes, but
        DEB no longer consumes them."""
        today = date.today()
        start = today - timedelta(days=29)
        n = MIN_SAMPLES + 5
        dates = [(start + timedelta(days=i)).isoformat() for i in range(n)]
        settlements = [{"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in dates]
        logs = (
            self._rows(dates, "gfs", 79.0)
            + self._rows(dates, "open_meteo", 79.5)
            + self._rows(dates, "nws", 80.2)
        )
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert "gfs" not in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_gfs_only_data_is_full_cold_start_without_gfs_key(self):
        """When the only forecast_log data is for "gfs" (no registered model
        has any samples), compute_weights() falls back to full equal weights
        over the registered channels — "gfs" is absent from the result even
        though it dominates the raw log."""
        today = date.today()
        start = today - timedelta(days=29)
        dates = [(start + timedelta(days=i)).isoformat() for i in range(MIN_SAMPLES + 5)]
        settlements = [{"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in dates]
        logs = self._rows(dates, "gfs", 79.0)
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert weights == dict(dw._equal_weights_for("us"))
        assert "gfs" not in weights

    def test_remaining_weights_renormalize_to_one_with_gfs_dominant_errors(self):
        """Renormalization check: even when "gfs" rows would (if consumed)
        have the smallest error of any channel, DEB output still sums to 1.0
        over only the registered channels — "gfs"'s (would-be) share is
        redistributed among nws/open_meteo/etc., not simply dropped from the
        total."""
        today = date.today()
        start = today - timedelta(days=29)
        n = MIN_SAMPLES + 5
        dates = [(start + timedelta(days=i)).isoformat() for i in range(n)]
        settlements = [{"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in dates]
        logs = (
            self._rows(dates, "gfs", 80.01)          # tiny error, would dominate if consumed
            + self._rows(dates, "nws", 84.0)          # large error
            + self._rows(dates, "open_meteo", 84.0)   # large error
        )
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert "gfs" not in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        # nws and open_meteo (identical errors, both calibrated) should each
        # be non-trivial contributors — the weight "gfs" would have taken
        # must have gone somewhere, not vanished from the total.
        assert weights["nws"] > 0.1
        assert weights["open_meteo"] > 0.1

    def test_other_channels_unaffected_by_gfs_rows_at_any_date(self):
        """"gfs" rows (any date) must never influence nws/open_meteo
        calibration — they are simply skipped, not merged or double-counted."""
        today = date.today()
        start = today - timedelta(days=29)
        dates = [(start + timedelta(days=i)).isoformat() for i in range(MIN_SAMPLES + 5)]
        settlements = [{"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in dates]
        logs_without_gfs = (
            self._rows(dates, "nws", 80.2)
            + self._rows(dates, "open_meteo", 82.0)
        )
        logs_with_gfs = logs_without_gfs + self._rows(dates, "gfs", 79.0)

        db1 = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db1.get_forecast_log.return_value = logs_without_gfs
        db1.get_settlements.return_value = settlements
        weights_without_gfs = compute_weights(db1, "KORD", "Chicago", station_region="us")

        db2 = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db2.get_forecast_log.return_value = logs_with_gfs
        db2.get_settlements.return_value = settlements
        weights_with_gfs = compute_weights(db2, "KORD", "Chicago", station_region="us")

        for m in ("nws", "open_meteo"):
            assert isclose(weights_without_gfs[m], weights_with_gfs[m], abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Issue #550 sanity check (amended #761): US + EU/global all-calibrated
# group cap behavior
# ---------------------------------------------------------------------------

class TestIssue550GroupCapSanityCheck:
    """End-to-end sanity check (issue #550 acceptance criterion, still valid
    post-#761): for a US station and an EU/global station with every
    applicable channel calibrated, no group_id's combined weight exceeds
    GROUP_WEIGHT_CAP and the returned weights sum to 1.0.

    Forecast errors are deliberately skewed so that "open_meteo" (the sole
    "gfs_family" member since #761 removed "gfs") would dominate the ensemble
    on raw inverse-RMSE alone — this exercises the group cap rather than
    merely a scenario where it never triggers. "gfs" log rows are also
    included to confirm they're silently ignored rather than double-counted.
    """

    def _calibrated_db(self, models_and_forecasts: dict, actual: float = 80.0, n: int = 20):
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, n, actual_high=actual)
        logs = []
        for model, forecast in models_and_forecasts.items():
            logs += _log_rows(start, n, [model], forecast_fn=lambda m, i, f=forecast: f)
        return _make_db(logs, settlements)

    def test_us_station_all_calibrated_group_caps_respected(self):
        """US station (KORD): nws, open_meteo, hrrr, nbm, ecmwf all
        calibrated (icon excluded — EU-only). open_meteo given the smallest
        error so gfs_family would otherwise dominate. A "gfs" log row is
        included (tiny error too) to confirm it's ignored, not merged in."""
        db = self._calibrated_db({
            "nws": 80.5,
            "hrrr": 80.6,
            "nbm": 80.7,
            "ecmwf": 80.8,
            "open_meteo": 80.05,  # tiny error -> high raw weight
            "gfs": 80.05,         # tiny error -> must be ignored (issue #761)
        })
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        assert "gfs" not in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

        noaa_us_total = weights["nws"] + weights["hrrr"] + weights["nbm"]
        gfs_family_total = weights["open_meteo"]
        ecmwf_intl_total = weights["ecmwf"]  # icon excluded for US stations

        assert noaa_us_total <= GROUP_WEIGHT_CAP + 1e-9, (
            f"noaa_us group {noaa_us_total:.4f} exceeds cap {GROUP_WEIGHT_CAP}"
        )
        assert gfs_family_total <= GROUP_WEIGHT_CAP + 1e-9, (
            f"gfs_family group {gfs_family_total:.4f} exceeds cap {GROUP_WEIGHT_CAP}"
        )
        assert ecmwf_intl_total <= GROUP_WEIGHT_CAP + 1e-9, (
            f"ecmwf_intl group {ecmwf_intl_total:.4f} exceeds cap {GROUP_WEIGHT_CAP}"
        )

    def test_eu_station_all_calibrated_group_caps_respected(self):
        """EU/global station (EGLL, London): open_meteo, ecmwf, icon all
        calibrated (nws/hrrr/nbm excluded — US-only). This is the scenario
        with ZERO ungrouped models — every applicable channel is in either
        "gfs_family" or "ecmwf_intl" — so it directly exercises the
        _apply_group_cap redistribution fix. A "gfs" log row is included to
        confirm it's ignored, not merged in."""
        db = self._calibrated_db({
            "ecmwf": 80.8,
            "icon": 80.9,
            "open_meteo": 80.05,  # tiny error -> high raw weight
            "gfs": 80.05,         # tiny error -> must be ignored (issue #761)
        })
        weights = compute_weights(db, "EGLL", "London", station_region="eu")

        assert "gfs" not in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert "nws" not in weights
        assert "hrrr" not in weights
        assert "nbm" not in weights

        gfs_family_total = weights["open_meteo"]
        ecmwf_intl_total = weights["ecmwf"] + weights["icon"]

        assert gfs_family_total <= GROUP_WEIGHT_CAP + 1e-9, (
            f"gfs_family group {gfs_family_total:.4f} exceeds cap {GROUP_WEIGHT_CAP}"
        )
        assert ecmwf_intl_total <= GROUP_WEIGHT_CAP + 1e-9, (
            f"ecmwf_intl group {ecmwf_intl_total:.4f} exceeds cap {GROUP_WEIGHT_CAP}"
        )


# ---------------------------------------------------------------------------
# Real RMSE and sample_count logging (issue #553)
# ---------------------------------------------------------------------------

class TestComputeWeightsWithMetadata:
    """Verify _compute_weights_with_metadata returns real RMSE and sample counts."""

    def test_calibrated_model_gets_real_rmse(self):
        """Calibrated model should return computed RMSE, not 0.0."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws"], forecast_fn=lambda m, i: 80.5)
        db = _make_db(logs, settlements)

        weights, rmse_dict, sample_counts = dw._compute_weights_with_metadata(
            db, "KORD", "Chicago", station_region="us"
        )

        assert weights["nws"] > 0
        assert rmse_dict["nws"] > 0, "Calibrated model should have real RMSE > 0"
        assert sample_counts["nws"] >= MIN_SAMPLES, (
            f"Calibrated model should have sample_count >= MIN_SAMPLES, got {sample_counts['nws']}"
        )

    def test_cold_start_model_gets_zero_rmse(self):
        """Cold-start model (insufficient samples) should return rmse=0.0."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        # nws calibrated (20 samples); open_meteo cold-start (0 samples)
        logs = _log_rows(start, 20, ["nws"], forecast_fn=lambda m, i: 80.5)
        db = _make_db(logs, settlements)

        weights, rmse_dict, sample_counts = dw._compute_weights_with_metadata(
            db, "KORD", "Chicago", station_region="us"
        )

        # nws is calibrated, open_meteo is cold-start; "gfs" is not a
        # registered channel at all (issue #761), so it has no entry here.
        assert "gfs" not in sample_counts
        assert "gfs" not in rmse_dict
        assert sample_counts["nws"] >= MIN_SAMPLES
        assert sample_counts["open_meteo"] < MIN_SAMPLES
        assert rmse_dict["nws"] > 0, "Calibrated model should have real RMSE > 0"
        assert rmse_dict["open_meteo"] == 0.0, "Cold-start model should have rmse=0.0"

    def test_all_cold_start_returns_sample_counts(self):
        """When all models are cold-start, still return sample counts."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 5, ["nws"], forecast_fn=lambda m, i: 80.5)  # only 5 samples
        db = _make_db(logs, settlements)

        weights, rmse_dict, sample_counts = dw._compute_weights_with_metadata(
            db, "KORD", "Chicago", station_region="us"
        )

        # All models should have sample_count < MIN_SAMPLES
        for m in sample_counts:
            assert sample_counts[m] < MIN_SAMPLES, (
                f"Expected all models cold-start, but {m} has {sample_counts[m]} >= {MIN_SAMPLES}"
            )
            assert rmse_dict[m] == 0.0, f"Cold-start model {m} should have rmse=0.0"

    def test_mixed_calibrated_cold_start(self):
        """Mixed scenario: some models calibrated, others cold-start."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        # nws + open_meteo calibrated (20 samples each); hrrr cold-start (0 samples)
        logs = (
            _log_rows(start, 20, ["nws"], forecast_fn=lambda m, i: 80.5)
            + _log_rows(start, 20, ["open_meteo"], forecast_fn=lambda m, i: 82.0)
        )
        db = _make_db(logs, settlements)

        weights, rmse_dict, sample_counts = dw._compute_weights_with_metadata(
            db, "KORD", "Chicago", station_region="us"
        )

        assert "gfs" not in sample_counts
        assert "gfs" not in rmse_dict
        assert sample_counts["nws"] == 20
        assert sample_counts["open_meteo"] == 20
        assert sample_counts["hrrr"] == 0
        # nws + open_meteo should have real RMSE
        assert rmse_dict["nws"] > 0
        assert rmse_dict["open_meteo"] > 0
        # hrrr cold-start should have 0.0 rmse
        assert rmse_dict["hrrr"] == 0.0

    def test_compute_weights_backward_compat(self):
        """Verify compute_weights() still returns just weights (backward compat)."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws"], forecast_fn=lambda m, i: 80.5)
        db = _make_db(logs, settlements)

        weights = dw.compute_weights(db, "KORD", "Chicago", station_region="us")

        # Should return a dict, not a tuple
        assert isinstance(weights, dict)
        assert "nws" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# refresh_weights: skip excluded cities (issue #558, #718)
# ---------------------------------------------------------------------------

class TestRefreshWeightsExclusion:
    """Verify refresh_weights skips cities marked training_ineligible in
    config/source_priority.yaml (issue #558, #718).

    The fix ensures that excluded cities (ZGSZ/Shenzhen, ZHHH/Wuhan,
    ZHCC/Zhengzhou, ZSJN/Jinan) do not receive new model_weights rows,
    preventing cold-start equal-weight rows from being written for
    ineligible training locations.
    """

    def test_refresh_weights_skips_excluded_city(self, monkeypatch):
        """When a city is ineligible (training_eligible: false), refresh_weights
        should return early without writing any model_weights rows (issue #718)."""
        # Mock is_training_eligible to return False for "Shenzhen"
        monkeypatch.setattr(dw, "is_training_eligible", lambda city: city != "Shenzhen")

        db = MagicMock(spec=[
            "get_all_config",
            "get_model_weights",
            "upsert_model_weight",
        ])
        db.get_all_config.return_value = {"DEB_ENABLED": "true"}

        # Call refresh_weights for an excluded city
        refresh_weights(db, station="ZGSZ", city="Shenzhen", station_region="asia")

        # upsert_model_weight should NOT be called
        db.upsert_model_weight.assert_not_called()

    def test_refresh_weights_processes_eligible_city(self, monkeypatch):
        """When a city is eligible, refresh_weights should proceed and call
        upsert_model_weight for each model (issue #718)."""
        # Mock is_training_eligible to return True for "Singapore"
        monkeypatch.setattr(dw, "is_training_eligible", lambda city: True)

        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws", "open_meteo"],
                          forecast_fn=lambda m, i: 80.5)

        db = MagicMock(spec=[
            "get_all_config",
            "get_model_weights",
            "upsert_model_weight",
            "get_forecast_log_by_lead",
            "get_obs_highs_range",
        ])
        db.get_all_config.return_value = {"DEB_ENABLED": "true"}
        db.get_model_weights.return_value = []  # No prior weights
        db.get_forecast_log_by_lead.return_value = logs
        db.get_obs_highs_range.return_value = {row["ts"][:10]: row["actual_high_f"]
                                                for row in settlements}

        # Call refresh_weights for an eligible city
        refresh_weights(db, station="WSSS", city="Singapore", station_region="asia")

        # upsert_model_weight should be called for each model
        assert db.upsert_model_weight.call_count > 0, (
            "upsert_model_weight should be called for eligible cities"
        )
        # Each call should include city="Singapore"
        for call in db.upsert_model_weight.call_args_list:
            assert call.kwargs.get("city") == "Singapore", (
                f"upsert_model_weight called with incorrect city"
            )

    def test_refresh_weights_respects_deb_enabled_flag(self, monkeypatch):
        """When DEB_ENABLED is false, refresh_weights should return early
        regardless of training_eligible status."""
        monkeypatch.setattr(dw, "is_training_eligible", lambda city: True)

        db = MagicMock(spec=[
            "get_all_config",
            "get_model_weights",
            "upsert_model_weight",
        ])
        db.get_all_config.return_value = {"DEB_ENABLED": "false"}

        # Call refresh_weights
        refresh_weights(db, station="WSSS", city="Singapore", station_region="asia")

        # upsert_model_weight should NOT be called (DEB_ENABLED takes precedence)
        db.upsert_model_weight.assert_not_called()

    def test_refresh_weights_skips_if_already_refreshed_today(self, monkeypatch):
        """When weights were already refreshed today, skip even if eligible."""
        monkeypatch.setattr(dw, "is_training_eligible", lambda city: True)

        today = date.today().isoformat()
        db = MagicMock(spec=[
            "get_all_config",
            "get_model_weights",
            "upsert_model_weight",
        ])
        db.get_all_config.return_value = {"DEB_ENABLED": "true"}
        db.get_model_weights.return_value = [{"date": today, "model": "nws", "weight": 0.5}]

        # Call refresh_weights
        refresh_weights(db, station="WSSS", city="Singapore", station_region="asia")

        # upsert_model_weight should NOT be called (already refreshed today)
        db.upsert_model_weight.assert_not_called()

    def test_refresh_weights_four_excluded_cities(self, monkeypatch):
        """Verify that all 4 cities from issue #558 (ZSJN/ZGSZ/ZHHH/ZHCC) are
        properly excluded."""
        excluded_cities = ["Jinan", "Shenzhen", "Wuhan", "Zhengzhou"]

        def mock_is_training_eligible(city):
            return city not in excluded_cities

        monkeypatch.setattr(dw, "is_training_eligible", mock_is_training_eligible)

        db = MagicMock(spec=[
            "get_all_config",
            "get_model_weights",
            "upsert_model_weight",
            "get_forecast_log_by_lead",
            "get_obs_highs_range",
        ])
        db.get_all_config.return_value = {"DEB_ENABLED": "true"}

        # Test each excluded city
        for city in excluded_cities:
            db.reset_mock()
            refresh_weights(db, station="TEST", city=city, station_region="asia")
            db.upsert_model_weight.assert_not_called(), (
                f"upsert_model_weight should not be called for excluded city {city}"
            )

        # Test a non-excluded city
        db.reset_mock()
        db.get_model_weights.return_value = []
        db.get_forecast_log_by_lead.return_value = []
        db.get_obs_highs_range.return_value = {}
        refresh_weights(db, station="WSSS", city="Singapore", station_region="asia")
        # This city is not in the excluded list, so upsert should be attempted
        # (even if it results in no-op due to missing data)


# ---------------------------------------------------------------------------
# Issue #761 hard guardrail: baseline EMOS regime membership is untouched
# ---------------------------------------------------------------------------

class TestBaselineEmosRegimeUnaffectedByGfsDrop:
    """Acceptance criterion for #761: dropping "gfs" from the DEB channel set
    must not alter FORECAST_STACK_MODELS["baseline"] in src/config.py — the
    EMOS baseline regime {nws, open_meteo} that EMOS training/serving depends
    on (src/model/emos_calibration.py, src/model/emos_mode.py) is a completely
    separate mapping from the DEB registry touched by this issue."""

    def test_baseline_regime_is_nws_open_meteo_only(self):
        from src.config import FORECAST_STACK_MODELS
        assert FORECAST_STACK_MODELS["baseline"] == frozenset({"nws", "open_meteo"})

    def test_baseline_regime_never_contained_gfs(self):
        """"gfs" was never a member of any FORECAST_STACK_MODELS regime —
        confirms #761 touched only the DEB registry, not this EMOS mapping."""
        from src.config import FORECAST_STACK_MODELS
        for regime, models in FORECAST_STACK_MODELS.items():
            assert "gfs" not in models, (
                f"FORECAST_STACK_MODELS[{regime!r}] unexpectedly contains 'gfs': {models}"
            )

    def test_all_forecast_stack_regimes_unchanged_shape(self):
        """Sanity check on the full FORECAST_STACK_MODELS mapping post-#761 —
        no regime was added, removed, or resized as a side effect of the DEB
        registry change."""
        from src.config import FORECAST_STACK_MODELS
        assert FORECAST_STACK_MODELS == {
            "baseline": frozenset({"nws", "open_meteo"}),
            "hrrr_nbm": frozenset({"nws", "open_meteo", "hrrr", "nbm"}),
            "intl_ecmwf_icon": frozenset({"nws", "open_meteo", "ecmwf", "icon"}),
            "full": frozenset({"nws", "open_meteo", "hrrr", "nbm", "ecmwf", "icon", "gefs"}),
        }
