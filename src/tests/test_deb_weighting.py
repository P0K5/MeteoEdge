"""Unit tests for src/model/deb_weighting.py — model registry refactor.

Covers:
- MODELS tuple and EQUAL_WEIGHTS backward compat (now 3-model)
- regional exclusion
- cold-start policy (partial and full cold-start)
- cadence-aware decay rate
- group weight cap
- 30-day replay delta <= ±0.01 vs old 2-model code (backward compat check)
- original test cases updated for 3-model registry
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
)


@pytest.fixture(autouse=True)
def _neutralize_gfs_duplicate_era_cutoff(monkeypatch):
    """Neutralize the GFS_DATA_VALID_FROM duplicate-era filter (issue #548) for
    every test in this module by default.

    Most fixtures below build "gfs" matched pairs using dates relative to
    date.today() (e.g. `today - timedelta(days=29)`), which predate the real
    GFS_DATA_VALID_FROM constant and would otherwise be silently excluded from
    training, pushing "gfs" into cold-start and breaking assertions that are
    about registry/cold-start/decay behavior, not about the duplicate-era
    filter itself. Push the cutoff safely into the past here; the dedicated
    TestGfsDuplicateEraFilter class below overrides it again to test the
    filter directly.
    """
    monkeypatch.setattr(dw, "GFS_DATA_VALID_FROM", "2000-01-01")


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
# Backward compat: MODELS tuple and EQUAL_WEIGHTS (now 3-model)
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_models_tuple_contains_all_us_channels(self):
        # After #442, MODELS includes nws, open_meteo, gfs, hrrr, nbm, ecmwf
        # (ECMWF is global so it appears for US region; ICON is EU-only and excluded)
        assert set(MODELS) == {"nws", "open_meteo", "gfs", "hrrr", "nbm", "ecmwf"}

    def test_models_tuple_contains_legacy_channels(self):
        # Legacy channels must still be present
        for m in ("nws", "open_meteo", "gfs"):
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
        """Computed weights must sum to 1.0 even when only nws+open_meteo have data (gfs cold-start)."""
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
        """When only nws has data, open_meteo and gfs get cold-start weight; nws gets most."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 15, actual_high=80.0)
        logs = _log_rows(start, 15, ["nws"], forecast_fn=lambda m, i: 80.0 + i * 0.1)
        db = _make_db(logs, settlements)
        result = compute_weights(db, "KLAX", "Los Angeles")
        # Must sum to 1
        assert abs(sum(result.values()) - 1.0) < 1e-9
        # nws gets calibrated budget, open_meteo and gfs get cold-start fraction
        assert result["nws"] > result["open_meteo"]
        assert result["nws"] > result["gfs"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_legacy_channels_registered(self):
        for name in ("nws", "open_meteo", "gfs"):
            assert name in _REGISTRY

    def test_nws_metadata(self):
        e = _REGISTRY["nws"]
        assert e.region == "us"
        assert e.expected_cadence_h == 24.0
        assert e.group_id == "noaa_us"

    def test_open_meteo_metadata(self):
        e = _REGISTRY["open_meteo"]
        assert e.region == "global"
        assert e.expected_cadence_h == 24.0
        assert e.group_id is None

    def test_gfs_metadata(self):
        e = _REGISTRY["gfs"]
        assert e.region == "global"
        assert e.expected_cadence_h == 6.0
        assert e.group_id is None

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
        assert "gfs" in names

    def test_eu_region_excludes_us_only_model(self):
        names = {m.name for m in _models_for_region("eu")}
        assert "nws" not in names
        assert "open_meteo" in names
        assert "gfs" in names

    def test_compute_weights_regional_exclusion_eu(self):
        """US-only nws model must not appear in EU weight output."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["open_meteo", "gfs"], forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements)

        weights = compute_weights(db, "EGLL", "London", station_region="eu")
        assert "nws" not in weights
        assert "open_meteo" in weights
        assert "gfs" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_compute_weights_us_includes_nws(self):
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs"], forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements)

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert "nws" in weights
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

    def test_partial_cold_start_gfs_gets_cold_fraction(self):
        """gfs in cold-start; nws+open_meteo calibrated.

        gfs cold_start_fraction=0.5, N_models=3
        => gfs weight = 0.5 * (1/3) = 1/6 ≈ 0.1667
        => remaining budget = 5/6, split between nws and open_meteo
        """
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)

        def forecast_fn(m, i):
            return 80.5 if m == "nws" else 81.5

        logs = _log_rows(start, 20, ["nws", "open_meteo"], forecast_fn=forecast_fn)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        n_us_models = len(_model_names_for_region("us"))
        gfs_cold_frac = _REGISTRY["gfs"].cold_start_fraction
        # cold-start models: gfs, hrrr, nbm; calibrated: nws, open_meteo
        # Group cap may redistribute weight slightly; use a tolerance of 0.02
        expected_gfs = gfs_cold_frac / n_us_models
        assert abs(weights["gfs"] - expected_gfs) < 0.02, (
            f"Expected gfs≈{expected_gfs:.4f}, got {weights['gfs']:.4f}"
        )
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        # nws and open_meteo (calibrated) should dominate the remaining budget
        assert weights["nws"] + weights["open_meteo"] > weights["gfs"] + weights.get("hrrr", 0) + weights.get("nbm", 0)

    def test_two_models_cold_start_one_calibrated_sums_to_one(self):
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)
        logs = _log_rows(start, 20, ["gfs"], forecast_fn=lambda m, i: 79.0)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        for m in ("nws", "open_meteo", "gfs"):
            assert m in weights


# ---------------------------------------------------------------------------
# Cadence-aware decay
# ---------------------------------------------------------------------------

class TestCadenceAwareDecay:
    def test_24h_model_uses_base_decay_rate(self):
        rate = _cadence_decay_rate(_REGISTRY["nws"])
        assert isclose(rate, BASE_DECAY_RATE, abs_tol=1e-9)

    def test_6h_model_gets_lower_decay_rate(self):
        rate = _cadence_decay_rate(_REGISTRY["gfs"])
        expected = BASE_DECAY_RATE * (6.0 / 24.0)
        assert isclose(rate, expected, abs_tol=1e-9)

    def test_higher_cadence_frequency_means_lower_per_day_decay(self):
        rate_nws = _cadence_decay_rate(_REGISTRY["nws"])   # 24h
        rate_gfs = _cadence_decay_rate(_REGISTRY["gfs"])   # 6h
        assert rate_gfs < rate_nws


# ---------------------------------------------------------------------------
# Group weight cap
# ---------------------------------------------------------------------------

class TestGroupWeightCap:
    def test_group_cap_applied_when_exceeded(self):
        """noaa_us group (nws) with weight 0.8 > GROUP_WEIGHT_CAP should be capped."""
        weights = {"nws": 0.8, "open_meteo": 0.1, "gfs": 0.1}
        applicable = [_REGISTRY[m] for m in ("nws", "open_meteo", "gfs")]
        result = _apply_group_cap(weights, applicable)
        assert result["nws"] <= GROUP_WEIGHT_CAP + 1e-9
        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)

    def test_group_cap_not_applied_when_under(self):
        weights = {"nws": 0.3, "open_meteo": 0.4, "gfs": 0.3}
        applicable = [_REGISTRY[m] for m in ("nws", "open_meteo", "gfs")]
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

    def test_replay_delta_gfs_cold_start(self):
        """With GFS cold-start, nws+open_meteo weights track old 2-model output within ±0.01."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)

        def forecast_fn(m, i):
            return 80.5 if m == "nws" else 82.0

        logs = _log_rows(start, 20, ["nws", "open_meteo"], forecast_fn=forecast_fn)
        db = _make_db(logs, settlements)

        new_weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        old_weights = self._old_compute_weights(logs, settlements)

        n_us_models = len(_model_names_for_region("us"))
        # cold-start models when only nws+open_meteo are calibrated: gfs, hrrr, nbm, ecmwf
        cold_start_reserved = sum(
            _REGISTRY[m].cold_start_fraction / n_us_models
            for m in ("gfs", "hrrr", "nbm", "ecmwf")
        )
        calibrated_budget = 1.0 - cold_start_reserved
        old_scaled = {m: old_weights[m] * calibrated_budget for m in old_weights}

        for m in ("nws", "open_meteo"):
            delta = abs(new_weights[m] - old_scaled[m])
            # After #442 (6-model registry), calibrated budget is further reduced by
            # HRRR, NBM, and ECMWF cold-start reservations; allow up to 0.1 delta from old 2-model.
            assert delta <= 0.1, (
                f"Weight delta for {m} exceeds 0.1: new={new_weights[m]:.4f}, "
                f"old_scaled={old_scaled[m]:.4f}, delta={delta:.4f}"
            )

    def test_replay_delta_all_calibrated(self):
        """When all 3 models are calibrated, weights sum to 1 and better model wins."""
        today = date.today()
        start = today - timedelta(days=29)
        settlements = _settlement_rows(start, 20, actual_high=80.0)

        def forecast_fn(m, i):
            return {"nws": 80.5, "open_meteo": 82.0, "gfs": 81.0}[m]

        logs = _log_rows(start, 20, ["nws", "open_meteo", "gfs"], forecast_fn=forecast_fn)
        db = _make_db(logs, settlements)
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")

        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
        assert weights["nws"] > weights["open_meteo"]
        assert weights["nws"] > weights["gfs"]


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
        # Only calibrate nws, open_meteo, gfs, nbm — leave hrrr with 0 samples
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
        applicable = [_REGISTRY[m] for m in ("nws", "hrrr", "nbm", "open_meteo", "gfs")]
        weights = {"nws": 0.4, "hrrr": 0.3, "nbm": 0.2, "open_meteo": 0.05, "gfs": 0.05}
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
        """ecmwf_intl group (ecmwf + icon) combined weight is capped at GROUP_WEIGHT_CAP."""
        applicable = [_REGISTRY[m] for m in ("ecmwf", "icon", "open_meteo", "gfs")]
        weights = {"ecmwf": 0.5, "icon": 0.3, "open_meteo": 0.1, "gfs": 0.1}
        result = _apply_group_cap(weights, applicable)
        intl_total = result["ecmwf"] + result["icon"]
        assert intl_total <= GROUP_WEIGHT_CAP + 1e-9
        assert isclose(sum(result.values()), 1.0, abs_tol=1e-6)


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
        """US ensemble: nws+hrrr+nbm+open_meteo+gfs+ecmwf — ICON excluded."""
        db = self._make_calibrated_db(["nws", "open_meteo", "gfs", "hrrr", "nbm", "ecmwf"])
        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        assert "icon" not in weights
        assert "nws" in weights
        assert "ecmwf" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_eu_ensemble_includes_ecmwf_and_icon(self):
        """EU ensemble: open_meteo+gfs+ecmwf+icon — nws/hrrr/nbm excluded."""
        db = self._make_calibrated_db(["open_meteo", "gfs", "ecmwf", "icon"])
        weights = compute_weights(db, "EGLC", "London", station_region="eu")
        assert "nws" not in weights
        assert "hrrr" not in weights
        assert "nbm" not in weights
        assert "ecmwf" in weights
        assert "icon" in weights
        assert "open_meteo" in weights
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_asia_ensemble_includes_ecmwf_excludes_icon(self):
        """Asia ensemble: open_meteo+gfs+ecmwf — icon excluded (EU-only)."""
        db = self._make_calibrated_db(["open_meteo", "gfs", "ecmwf"])
        weights = compute_weights(db, "WSSS", "Singapore", station_region="asia")
        assert "icon" not in weights
        assert "nws" not in weights
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
# GFS duplicate-era training filter (issue #548)
# ---------------------------------------------------------------------------

class TestGfsDuplicateEraFilter:
    """Verify compute_weights() excludes "gfs" matched pairs dated before
    GFS_DATA_VALID_FROM, so DEB never calibrates on the pre-#548 period when
    fetch_gfs_with_spread() was a byte-identical duplicate of
    fetch_open_meteo_with_spread(). Other channels (nws, open_meteo, ...) must
    be unaffected by the cutoff.
    """

    def _rows(self, dates: list, model: str, forecast_high_f: float) -> list:
        return [{"date": d, "model": model, "forecast_high_f": forecast_high_f} for d in dates]

    def test_gfs_rows_before_cutoff_excluded_from_training(self, monkeypatch):
        """gfs rows dated strictly before GFS_DATA_VALID_FROM don't count toward
        MIN_SAMPLES — gfs stays cold-start even with plenty of duplicate-era rows."""
        monkeypatch.setattr(dw, "GFS_DATA_VALID_FROM", "2026-07-01")

        pre_cutoff_dates = [f"2026-06-{d:02d}" for d in range(1, MIN_SAMPLES + 3)]
        settlements = [
            {"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in pre_cutoff_dates
        ]
        logs = self._rows(pre_cutoff_dates, "gfs", 79.0)
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        # All models (including gfs) have zero post-cutoff samples -> full cold-start
        assert weights == dict(dw._equal_weights_for("us"))

    def test_gfs_rows_on_or_after_cutoff_are_trained_on(self, monkeypatch):
        """gfs rows dated on/after GFS_DATA_VALID_FROM DO count toward MIN_SAMPLES
        and participate in calibrated RMSE weighting like any other channel."""
        monkeypatch.setattr(dw, "GFS_DATA_VALID_FROM", "2026-07-01")

        n = MIN_SAMPLES + 2
        post_cutoff_dates = [f"2026-07-{d:02d}" for d in range(1, n + 1)]
        settlements = [
            {"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in post_cutoff_dates
        ]
        logs = (
            self._rows(post_cutoff_dates, "gfs", 79.5)
            + self._rows(post_cutoff_dates, "open_meteo", 79.5)
        )
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        # gfs is calibrated (not cold-start default fraction) -- it shares the
        # calibrated budget with open_meteo rather than getting a lone
        # cold-start weight, so it should exceed the plain equal-weight share.
        equal_share = 1.0 / len(dw.MODELS)
        assert weights["gfs"] > equal_share * 0.9
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-6)

    def test_mixed_pre_and_post_cutoff_only_post_cutoff_rows_counted(self, monkeypatch):
        """A mix of pre- and post-cutoff gfs rows: only post-cutoff rows count
        toward MIN_SAMPLES, so a duplicate-heavy history doesn't mask a thin
        post-cutoff sample."""
        monkeypatch.setattr(dw, "GFS_DATA_VALID_FROM", "2026-07-01")

        pre_cutoff_dates = [f"2026-06-{d:02d}" for d in range(1, 21)]  # 20 duplicate-era rows
        post_cutoff_dates = [f"2026-07-{d:02d}" for d in range(1, 4)]  # only 3 genuine rows
        all_dates = pre_cutoff_dates + post_cutoff_dates
        settlements = [
            {"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in all_dates
        ]
        logs = self._rows(all_dates, "gfs", 79.0)
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        # Only 3 genuine post-cutoff rows < MIN_SAMPLES (10) -> still cold-start
        # despite 23 total rows on disk.
        assert weights == dict(dw._equal_weights_for("us"))

    def test_other_channels_unaffected_by_gfs_cutoff(self, monkeypatch):
        """The duplicate-era filter is scoped to 'gfs' only — nws/open_meteo
        pre-cutoff rows must still be trained on normally."""
        monkeypatch.setattr(dw, "GFS_DATA_VALID_FROM", "2026-07-01")

        pre_cutoff_dates = [f"2026-06-{d:02d}" for d in range(1, MIN_SAMPLES + 3)]
        settlements = [
            {"ts": f"{d}T12:00:00", "actual_high_f": 80.0} for d in pre_cutoff_dates
        ]
        logs = (
            self._rows(pre_cutoff_dates, "nws", 80.2)
            + self._rows(pre_cutoff_dates, "open_meteo", 82.0)
        )
        db = MagicMock(spec=["get_forecast_log", "get_settlements"])
        db.get_forecast_log.return_value = logs
        db.get_settlements.return_value = settlements

        weights = compute_weights(db, "KORD", "Chicago", station_region="us")
        # nws (smaller error) should be calibrated and outweigh open_meteo,
        # despite all rows predating GFS_DATA_VALID_FROM -- the cutoff must not
        # apply to non-gfs channels.
        assert weights["nws"] > weights["open_meteo"]
