"""Unit tests for the pure computation core of
src/scripts/ensemble_sigma_calibration_backtest.py (issue #450).

Covers:
- calibrate_triples: EMOS (a, b, c, d) transform applied to raw triples.
- bracket_reliability_pairs: synthetic bracket decomposition -> (p, hit) pairs.
- sharpness_stats: mean/median calibrated sigma.
- fit_city_track: partial-pooling blend thresholds (per-city / blended /
  excluded), against a fake DB double -- no real sqlite file needed.
- build_report: recommendation gate (promote/hold) on canned aggregate stats.

No network, no real DB file -- fit_city_track tests use an in-memory fake
Database that fakes fetch_training_data's dependencies via monkeypatching the
model_forecast_log-backed methods it calls.
"""
import math

import pytest

from src.scripts.ensemble_sigma_calibration_backtest import (
    bracket_reliability_pairs,
    build_report,
    calibrate_triples,
    fit_city_track,
    mean_crps_for_triples,
    run_backtest,
    sharpness_stats,
)


class TestCalibrateTriples:
    def test_identity_transform(self):
        raw = [(70.0, 2.0, 71.0)]
        out = calibrate_triples(raw, (0.0, 1.0, 0.0, 1.0))
        assert out == [(70.0, 2.0, 71.0)]

    def test_shift_and_scale(self):
        raw = [(70.0, 2.0, 71.0)]
        # mu_cal = 1 + 2*70 = 141 ; sigma_cal = 0.5 + 0.5*2 = 1.5
        out = calibrate_triples(raw, (1.0, 2.0, 0.5, 0.5))
        assert out == [(141.0, 1.5, 71.0)]

    def test_empty(self):
        assert calibrate_triples([], (0.0, 1.0, 0.0, 1.0)) == []


class TestBracketReliabilityPairs:
    def test_actual_at_mean_scores_highest_probability_bracket_as_hit(self):
        # mu=70, sigma=1, actual=70.5 -> the [70, 71) bracket (centred under
        # the peak) should carry ~the largest probability mass of any
        # 1-degree bracket and be marked a hit. [69, 70) is its mirror-image
        # neighbour and scores within float noise of the same mass, so allow
        # a small tolerance rather than a strict >=.
        pairs = bracket_reliability_pairs([(70.0, 1.0, 70.5)])
        hit_pairs = [(p, w) for p, w in pairs if w]
        assert len(hit_pairs) == 1
        p_hit = hit_pairs[0][0]
        miss_ps = [p for p, w in pairs if not w]
        assert all(p_hit >= p - 1e-9 for p in miss_ps)

    def test_probabilities_for_one_triple_sum_near_one(self):
        # Wide span (8 sigma total) should capture ~all probability mass.
        pairs = bracket_reliability_pairs([(70.0, 1.0, 70.5)], span_sigmas=4.0)
        total_p = sum(p for p, _ in pairs)
        assert total_p == pytest.approx(1.0, abs=0.01)

    def test_nonpositive_sigma_skipped(self):
        assert bracket_reliability_pairs([(70.0, 0.0, 70.0)]) == []
        assert bracket_reliability_pairs([(70.0, -1.0, 70.0)]) == []

    def test_nan_inf_skipped(self):
        assert bracket_reliability_pairs([(float("nan"), 1.0, 70.0)]) == []
        assert bracket_reliability_pairs([(70.0, 1.0, float("inf"))]) == []

    def test_bracket_width_controls_bin_count(self):
        narrow = bracket_reliability_pairs([(70.0, 1.0, 70.0)], bracket_width=1.0, span_sigmas=2.0)
        wide = bracket_reliability_pairs([(70.0, 1.0, 70.0)], bracket_width=2.0, span_sigmas=2.0)
        assert len(narrow) > len(wide)

    def test_exactly_one_hit_per_triple(self):
        pairs = bracket_reliability_pairs([(70.0, 1.0, 72.3)], span_sigmas=4.0)
        hits = [w for _, w in pairs if w]
        assert len(hits) == 1


class TestSharpnessStats:
    def test_mean_and_median(self):
        triples = [(0, 1.0, 0), (0, 2.0, 0), (0, 3.0, 0)]
        stats = sharpness_stats(triples)
        assert stats["n"] == 3
        assert stats["mean_sigma"] == pytest.approx(2.0)
        assert stats["median_sigma"] == pytest.approx(2.0)

    def test_even_count_median(self):
        triples = [(0, 1.0, 0), (0, 2.0, 0), (0, 3.0, 0), (0, 4.0, 0)]
        stats = sharpness_stats(triples)
        assert stats["median_sigma"] == pytest.approx(2.5)

    def test_empty(self):
        stats = sharpness_stats([])
        assert stats == {"n": 0, "mean_sigma": None, "median_sigma": None}


class TestMeanCrpsForTriples:
    def test_perfect_forecast_low_crps(self):
        # Very tight sigma centred exactly on the actual -> near-zero CRPS.
        triples = [(70.0, 0.01, 70.0)]
        crps = mean_crps_for_triples(triples)
        assert crps < 0.01

    def test_empty_returns_none(self):
        assert mean_crps_for_triples([]) is None


# ---------------------------------------------------------------------------
# fit_city_track — partial-pooling thresholds against a fake DB
# ---------------------------------------------------------------------------

class _FakeDB:
    """Fakes just enough of the Database surface fetch_training_data needs.

    Rows are keyed by (station, date); get_daily_obs_high and
    get_forecast_log_by_lead read straight from the in-memory dict so no
    sqlite file is needed.
    """

    def __init__(self, rows_by_station):
        self._rows = rows_by_station  # {station: [row dicts]}
        self._obs = {}  # {(station, date): actual_high_f}
        for station, rows in rows_by_station.items():
            for r in rows:
                self._obs[(station, r["date"])] = r["_actual"]

    def get_forecast_log_by_lead(self, station, since_date, lead_hours):
        return [
            {k: v for k, v in r.items() if k != "_actual"}
            for r in self._rows.get(station, [])
            if r.get("lead_hours", 24) == lead_hours
        ]

    def get_daily_obs_high(self, station, d):
        return self._obs.get((station, d))


def _row(date, model="nws", forecast_high_f=70.0, sigma_f=2.0, actual=71.0, station="KORD"):
    return {
        "date": date, "model": model, "forecast_high_f": forecast_high_f,
        "sigma_f": sigma_f, "lead_hours": 24, "station": station, "_actual": actual,
    }


def _make_rows(n, station="KORD", start=1):
    """n distinct dates of nws forecast rows for one station, all clean."""
    return [
        _row(f"2026-07-{start + i:02d}", forecast_high_f=70.0 + i % 5,
             sigma_f=2.0, actual=71.0 + i % 5, station=station)
        for i in range(n)
    ]


class TestFitCityTrack:
    def test_no_data_returns_none(self):
        db = _FakeDB({})
        assert fit_city_track("Chicago", db, "fixed") is None

    def test_full_weight_per_city_fit(self):
        db = _FakeDB({"KORD": _make_rows(65)})
        result = fit_city_track("Chicago", db, "fixed")
        assert result is not None
        assert result["n"] == 65
        assert result["provenance"] == "per-city"
        assert result["coeffs"] is not None

    def test_below_pooled_min_excluded(self):
        db = _FakeDB({"KORD": _make_rows(3)})
        result = fit_city_track("Chicago", db, "fixed")
        assert result is not None
        assert result["n"] == 3
        assert result["coeffs"] is None
        assert "excluded" in result["provenance"]

    def test_between_thresholds_blends_with_pooling_group(self):
        # Chicago (KORD, unit=F) pools into "us_f" with Miami/LA/Atlanta/Houston.
        rows = {
            "KORD": _make_rows(20, station="KORD"),
            "KMIA": _make_rows(65, station="KMIA", start=1),
        }
        db = _FakeDB(rows)
        result = fit_city_track("Chicago", db, "fixed")
        assert result is not None
        assert result["n"] == 20
        assert result["coeffs"] is not None
        assert "blended" in result["provenance"]

    def test_ensemble_vs_fixed_use_same_triple_count(self):
        """sigma_source only changes WHICH sigma value is used, never which
        dates are retained -- both tracks must see the same n for a clean
        city given identical rows."""
        db = _FakeDB({"KORD": _make_rows(65)})
        ens = fit_city_track("Chicago", db, "ensemble")
        fix = fit_city_track("Chicago", db, "fixed")
        assert ens["n"] == fix["n"] == 65


class TestRunBacktest:
    def test_aggregates_across_stations(self, monkeypatch):
        import src.scripts.ensemble_sigma_calibration_backtest as mod

        fake_stations = [("KORD", 41.9, -87.9, "Chicago", "KORD", "F", "America/Chicago")]
        monkeypatch.setattr("src.config.STATIONS", fake_stations)
        db = _FakeDB({"KORD": _make_rows(65)})

        result = mod.run_backtest(db)
        assert set(result.keys()) == {"ensemble", "fixed"}
        assert "Chicago" in result["ensemble"]["per_city"]
        assert result["ensemble"]["per_city"]["Chicago"]["n"] == 65

    def test_zero_data_station_still_recorded_with_n_zero(self, monkeypatch):
        """A configured station with no model_forecast_log rows (e.g. a
        delisted city, #765) must still appear in per_city with n=0 -- not
        be silently dropped from the report's station coverage."""
        import src.scripts.ensemble_sigma_calibration_backtest as mod

        fake_stations = [
            ("KORD", 41.9, -87.9, "Chicago", "KORD", "F", "America/Chicago"),
            ("ZSJN", 36.6, 117.0, "Jinan", "ZSJN", "C", "Asia/Shanghai"),
        ]
        monkeypatch.setattr("src.config.STATIONS", fake_stations)
        db = _FakeDB({"KORD": _make_rows(65)})  # Jinan has zero rows

        result = mod.run_backtest(db)
        assert "Jinan" in result["ensemble"]["per_city"]
        assert result["ensemble"]["per_city"]["Jinan"]["n"] == 0
        assert result["ensemble"]["per_city"]["Jinan"]["crps"] is None
        assert "excluded" in result["ensemble"]["per_city"]["Jinan"]["provenance"]
        assert len(result["ensemble"]["all_triples"]) == 65


# ---------------------------------------------------------------------------
# build_report — recommendation gate
# ---------------------------------------------------------------------------

def _result_with_triples(ens_triples, fix_triples, ens_per_city=None, fix_per_city=None):
    return {
        "ensemble": {
            "all_triples": ens_triples,
            "per_city": ens_per_city or {},
        },
        "fixed": {
            "all_triples": fix_triples,
            "per_city": fix_per_city or {},
        },
    }


def _fitted_city(triples):
    """A per_city entry as run_backtest() would produce it for a successfully
    fitted city -- crps/sharpness populated from the given triples, not left
    None (build_report's "cities fitted" count keys off crps is not None)."""
    return {
        "n": len(triples), "crps": mean_crps_for_triples(triples),
        "sharpness": sharpness_stats(triples), "triples": triples,
    }


class TestBuildReportRecommendation:
    def test_promote_when_ensemble_crps_clearly_better_same_sharpness(self):
        # ensemble: tight, well-centred -> low CRPS. fixed: same sigma, offset mean -> worse CRPS.
        ens_triples = [(70.0, 1.0, 70.0)] * 70
        fix_triples = [(75.0, 1.0, 70.0)] * 70
        per_city_ens = {"Chicago": _fitted_city(ens_triples)}
        per_city_fix = {"Chicago": _fitted_city(fix_triples)}
        result = _result_with_triples(ens_triples, fix_triples, per_city_ens, per_city_fix)
        report = build_report(result, [], "2026-07-28", "2026-06-24", "2026-07-28", 31)
        assert "Recommendation: PROMOTE" in report

    def test_hold_when_tracks_are_indistinguishable(self):
        triples = [(70.0, 1.0, 70.0)] * 70
        per_city = {"Chicago": _fitted_city(triples)}
        result = _result_with_triples(triples, list(triples), per_city, dict(per_city))
        report = build_report(result, [], "2026-07-28", "2026-06-24", "2026-07-28", 31)
        assert "Recommendation: HOLD" in report

    def test_hold_when_no_fitted_cities(self):
        result = _result_with_triples([], [])
        report = build_report(result, [], "2026-07-28", "2026-06-24", "2026-07-28", 31)
        assert "Recommendation: HOLD" in report
        assert "insufficient fitted-city coverage" in report

    def test_report_includes_crps_log_cross_check_rows(self):
        triples = [(70.0, 1.0, 70.0)] * 70
        per_city = {"Chicago": _fitted_city(triples)}
        result = _result_with_triples(triples, list(triples), per_city, dict(per_city))
        crps_rows = [("ensemble", "emos_shadow", 81, 27, "2026-07-25", "2026-07-27", 0.9)]
        report = build_report(result, crps_rows, "2026-07-28", "2026-06-24", "2026-07-28", 31)
        assert "emos_shadow" in report
        assert "0.9000" in report or "0.9" in report
