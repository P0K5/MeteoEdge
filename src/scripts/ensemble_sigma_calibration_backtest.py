"""Ensemble-sigma vs fixed-sigma EMOS calibration backtest (issue #450).

Quantifies the calibration improvement (or regression) from training EMOS on
the persisted per-row ``sigma_f`` (``sigma_source="ensemble"``, issue #449)
versus the legacy constant ``FORECAST_STDDEV_F`` (``sigma_source="fixed"``)
over the full historical ``model_forecast_log`` window.

Methodology (deliberately reuses existing, already-reviewed machinery rather
than inventing a parallel path):

1. **Truth**: ``src.model.emos_calibration.fetch_training_data`` joins each
   forecast date to ``db.get_daily_obs_high`` -- the post-#741 observations
   path that excludes Open-Meteo fallback rows. This backtest never reads
   ``settlements``, so it is fully decoupled from #867's Gamma wrong-market
   contamination (per issue #450's TechLead directive).
2. **Fit**: for each (city, sigma_source), coefficients are fit the SAME way
   ``scripts/run_emos_shadow.py`` fits daily shadow coefficients -- a pure
   per-city ``fit_emos`` at >= FULL_WEIGHT_SAMPLES triples, else a partial-
   pooling shrinkage blend against the city's cross-station pooling group
   (issue #798: ``pooling_group`` / ``shrinkage_weight`` /
   ``blend_coefficients``). This is an IN-SAMPLE fit-then-score, matching the
   convention ``emos_crps_log`` itself already uses -- not a walk-forward
   backtest. Flagged as a methodology caveat in the report, not hidden.
3. **CRPS / sharpness**: ``crps_gaussian`` over each city's own calibrated
   triples; sharpness is the mean/median calibrated sigma (degrees F) --
   directly the object ``sigma_source`` controls.
4. **Reliability diagram**: a continuous Gaussian (mu_cal, sigma_cal) has no
   single "predicted probability" without a bracket. Rather than depend on
   real Polymarket bracket definitions (which would reintroduce the
   settlements/Gamma coupling this backtest is explicitly avoiding), each
   calibrated triple is decomposed into synthetic BRACKET_WIDTH_F-wide bins
   spanning +/- BRACKET_SPAN_SIGMAS around mu_cal, scored with
   ``src.model.envelope.p_normal_between`` -- the exact primitive
   ``next_day_probability_yes`` uses to price real brackets in production.
   Each (bin probability, did-the-actual-high-land-in-this-bin) pair feeds
   the same ``build_reliability`` bucketing already used by
   ``src.scripts.calibration_report`` (0.05-wide buckets).

Usage:
    python -m src.scripts.ensemble_sigma_calibration_backtest [--db PATH]
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKTEST_DIR = _REPO_ROOT / "backtest_results"

BASELINE_REGIME = frozenset({"nws", "open_meteo"})
SIGMA_SOURCES = ("ensemble", "fixed")

# Same constants as scripts/run_emos_shadow.py (issue #798).
FULL_WEIGHT_SAMPLES = 60
POOLED_MIN_CITY_SAMPLES = 5

# Synthetic bracket decomposition for the reliability diagram (see module
# docstring point 4). 1 degree F is finer than most real Polymarket brackets
# (typically 2-6F wide) -- deliberately finer for bin resolution, not an
# attempt to reproduce real market bracket boundaries.
BRACKET_WIDTH_F = 1.0
BRACKET_SPAN_SIGMAS = 4.0

# A per-station reliability read is only presented when a station clears this
# many triples -- below it, 0.05-wide bins are mostly empty/single-count and
# would be noise dressed as signal (exactly what deferred this issue 3x).
STATION_READABLE_N = 60


# ---------------------------------------------------------------------------
# Pure computation (unit tested in src/tests/test_ensemble_sigma_calibration_backtest.py)
# ---------------------------------------------------------------------------

def calibrate_triples(
    raw_triples: "list[tuple[float, float, float]]",
    coeffs: "tuple[float, float, float, float]",
) -> "list[tuple[float, float, float]]":
    """Apply EMOS (a, b, c, d) to raw (mu, sigma, y) triples -> calibrated triples."""
    a, b, c, d = coeffs
    return [(a + b * mu, c + d * sigma, y) for mu, sigma, y in raw_triples]


def bracket_reliability_pairs(
    triples: "list[tuple[float, float, float]]",
    bracket_width: float = BRACKET_WIDTH_F,
    span_sigmas: float = BRACKET_SPAN_SIGMAS,
) -> "list[tuple[float, bool]]":
    """Decompose calibrated (mu, sigma, actual) triples into (bracket_p, hit) pairs.

    For each triple, tiles bracket_width-wide bins across
    [mu - span_sigmas*sigma, mu + span_sigmas*sigma], scores each with
    p_normal_between(lo, hi, mu, sigma) (same primitive live trading uses),
    and records whether the actual value landed in that specific bin. A
    well-calibrated forecast produces, across many such pairs, an observed
    hit-frequency per probability bucket that tracks the bucket's mean
    predicted probability -- the classic reliability-diagram property.
    """
    from src.model.envelope import p_normal_between

    pairs: "list[tuple[float, bool]]" = []
    for mu, sigma, y in triples:
        if sigma <= 0 or any(math.isnan(v) or math.isinf(v) for v in (mu, sigma, y)):
            continue
        lo_bound = math.floor((mu - span_sigmas * sigma) / bracket_width) * bracket_width
        hi_bound = math.ceil((mu + span_sigmas * sigma) / bracket_width) * bracket_width
        k = lo_bound
        while k < hi_bound:
            lo, hi = k, k + bracket_width
            p = p_normal_between(lo, hi, mu, sigma)
            pairs.append((p, lo <= y < hi))
            k += bracket_width
    return pairs


def sharpness_stats(triples: "list[tuple[float, float, float]]") -> dict:
    """Mean/median calibrated sigma (deg F) across triples -- narrower is sharper."""
    sigmas = sorted(s for _, s, _ in triples)
    n = len(sigmas)
    if n == 0:
        return {"n": 0, "mean_sigma": None, "median_sigma": None}
    mid = n // 2
    median = sigmas[mid] if n % 2 else (sigmas[mid - 1] + sigmas[mid]) / 2
    return {"n": n, "mean_sigma": sum(sigmas) / n, "median_sigma": median}


def mean_crps_for_triples(triples: "list[tuple[float, float, float]]") -> "float | None":
    from src.model.crps_score import mean_crps
    return mean_crps(triples)


# ---------------------------------------------------------------------------
# DB-backed fitting (issue #798 partial-pooling, mirroring run_emos_shadow.py)
# ---------------------------------------------------------------------------

def fit_city_track(city: str, db, sigma_source: str, regime=BASELINE_REGIME) -> "dict | None":
    """Fit EMOS coefficients for one (city, sigma_source) over ALL available history.

    Mirrors scripts/run_emos_shadow.py's blended-pass logic exactly (same
    FULL_WEIGHT_SAMPLES / POOLED_MIN_CITY_SAMPLES thresholds, same pooling
    group + shrinkage_weight + blend_coefficients call sequence) so this
    backtest's fits are the same shape of evidence the live shadow pipeline
    already produces -- just scored over the full window in one pass instead
    of accreted day by day.

    Returns None only when the city has zero own triples for this
    sigma_source. Otherwise returns a dict with keys: n, coeffs (None if
    excluded), provenance (str explaining the fit or exclusion reason),
    training_data (the raw triples).
    """
    from src.model.emos_calibration import (
        InsufficientDataError,
        blend_coefficients,
        fetch_training_data,
        fetch_training_data_pooled,
        fit_emos,
        pooling_group,
        shrinkage_weight,
    )
    from src.config import STATIONS, station_city

    try:
        training_data = fetch_training_data(
            city, db, min_samples=1, regime=regime, sigma_source=sigma_source,
        )
    except InsufficientDataError:
        return None

    n = len(training_data)
    if n == 0:
        return None

    if n >= FULL_WEIGHT_SAMPLES:
        return {
            "n": n, "coeffs": fit_emos(training_data),
            "provenance": "per-city", "training_data": training_data,
        }

    if n < POOLED_MIN_CITY_SAMPLES:
        return {
            "n": n, "coeffs": None,
            "provenance": f"excluded (n={n} < POOLED_MIN_CITY_SAMPLES={POOLED_MIN_CITY_SAMPLES})",
            "training_data": training_data,
        }

    group = pooling_group(city)
    if group is None:
        return {
            "n": n, "coeffs": None, "provenance": "excluded (no pooling group)",
            "training_data": training_data,
        }

    group_cities = [
        station_city(cfg) for cfg in STATIONS if pooling_group(station_city(cfg)) == group
    ]
    try:
        pooled_triples, _counts = fetch_training_data_pooled(
            group_cities, db, min_samples=1, regime=regime, sigma_source=sigma_source,
        )
        pooled_fit = fit_emos(pooled_triples)
    except InsufficientDataError:
        return {
            "n": n, "coeffs": None, "provenance": "excluded (pooled group also insufficient)",
            "training_data": training_data,
        }

    city_fit = fit_emos(training_data)
    weight = shrinkage_weight(n, FULL_WEIGHT_SAMPLES)
    coeffs = blend_coefficients(city_fit, pooled_fit, weight)
    return {
        "n": n, "coeffs": coeffs,
        "provenance": f"blended(w={weight:.2f}, group={group})",
        "training_data": training_data,
    }


def run_backtest(db, regime=BASELINE_REGIME) -> dict:
    """Fit + score both sigma_source tracks over every city in STATIONS.

    Returns a dict:
        {sigma_source: {
            "per_city": {city: {n, provenance, crps, sharpness}},
            "all_triples": [...calibrated triples across every fitted city...],
        }}
    """
    from src.config import STATIONS, station_city

    result: dict = {s: {"per_city": {}, "all_triples": []} for s in SIGMA_SOURCES}

    for sigma_source in SIGMA_SOURCES:
        for station_cfg in STATIONS:
            city = station_city(station_cfg)
            fit = fit_city_track(city, db, sigma_source, regime=regime)
            if fit is None:
                # Zero own triples for this city+sigma_source (e.g. a
                # delisted/paused station -- see #765). Still recorded, with
                # n=0, so the report's station coverage is honest about
                # EVERY configured station, not just the ones with data.
                result[sigma_source]["per_city"][city] = {
                    "n": 0, "provenance": "excluded (no training data)",
                    "crps": None, "sharpness": {"n": 0, "mean_sigma": None, "median_sigma": None},
                    "triples": [],
                }
                continue
            entry = {
                "n": fit["n"], "provenance": fit["provenance"],
                "crps": None, "sharpness": {"n": 0, "mean_sigma": None, "median_sigma": None},
                "triples": [],
            }
            if fit["coeffs"] is not None:
                calibrated = calibrate_triples(fit["training_data"], fit["coeffs"])
                entry["crps"] = mean_crps_for_triples(calibrated)
                entry["sharpness"] = sharpness_stats(calibrated)
                entry["triples"] = calibrated
                result[sigma_source]["all_triples"].extend(calibrated)
            result[sigma_source]["per_city"][city] = entry

    return result


def crps_log_summary(db) -> "list[tuple]":
    """Cross-check: (sigma_source, model_mode, n_rows, n_cities, min_date, max_date, avg_crps)
    from the already-logged emos_crps_log table (production shadow-run evidence)."""
    rows = db._conn.execute(
        "SELECT sigma_source, model_mode, COUNT(*), COUNT(DISTINCT city), "
        "MIN(date), MAX(date), AVG(crps_score) "
        "FROM emos_crps_log GROUP BY sigma_source, model_mode ORDER BY sigma_source, model_mode"
    ).fetchall()
    return [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _fmt(v, spec=".4f"):
    return format(v, spec) if v is not None else "n/a"


def build_report(result: dict, crps_log_rows: "list[tuple]", run_date: str,
                  window_start: str, window_end: str, n_dates: int) -> str:
    from src.scripts.calibration_report import build_reliability, format_reliability

    ens = result["ensemble"]
    fix = result["fixed"]

    ens_crps = mean_crps_for_triples(ens["all_triples"])
    fix_crps = mean_crps_for_triples(fix["all_triples"])
    ens_sharp = sharpness_stats(ens["all_triples"])
    fix_sharp = sharpness_stats(fix["all_triples"])

    ens_pairs = bracket_reliability_pairs(ens["all_triples"])
    fix_pairs = bracket_reliability_pairs(fix["all_triples"])

    ens_brier = _brier(ens_pairs)
    fix_brier = _brier(fix_pairs)

    cities_fitted_ens = {c for c, e in ens["per_city"].items() if e["crps"] is not None}
    cities_fitted_fix = {c for c, e in fix["per_city"].items() if e["crps"] is not None}
    cities_excluded_ens = {c for c, e in ens["per_city"].items() if e["crps"] is None}
    cities_excluded_fix = {c for c, e in fix["per_city"].items() if e["crps"] is None}

    crps_delta = (ens_crps - fix_crps) if (ens_crps is not None and fix_crps is not None) else None
    sigma_delta = (
        ens_sharp["mean_sigma"] - fix_sharp["mean_sigma"]
        if ens_sharp["mean_sigma"] is not None and fix_sharp["mean_sigma"] is not None
        else None
    )

    # --- Recommendation gate ---
    # Promote: ensemble-sigma strictly improves CRPS (lower is better) without
    # materially widening sharpness (calibrated sigma), i.e. the improvement
    # isn't just "wider bands score better on average CRPS trivially".
    if crps_delta is None or len(cities_fitted_ens) == 0:
        recommendation = "HOLD"
        rec_reason = "insufficient fitted-city coverage to compare tracks"
    elif crps_delta < -0.01 and (sigma_delta is None or sigma_delta <= 0.25):
        recommendation = "PROMOTE"
        rec_reason = (
            f"ensemble-sigma CRPS is {abs(crps_delta):.4f} lower "
            f"(better) than fixed-sigma with sharpness essentially unchanged "
            f"({_fmt(sigma_delta, '+.3f')} deg F mean calibrated sigma)"
        )
    elif crps_delta <= 0.01:
        recommendation = "HOLD"
        rec_reason = (
            f"CRPS delta ({crps_delta:+.4f}) is within noise of fixed-sigma -- "
            f"not enough signal yet to promote or kill"
        )
    else:
        recommendation = "HOLD"
        rec_reason = (
            f"ensemble-sigma CRPS is {crps_delta:+.4f} WORSE than fixed-sigma "
            f"over this window -- recommend hold, not kill, pending a longer window "
            f"(see per-station table for whether this is broad or concentrated)"
        )

    lines = [
        "# Ensemble-Sigma vs Fixed-Sigma EMOS Calibration Backtest",
        "",
        f"**Run date:** {run_date}  ",
        f"**model_forecast_log window:** {window_start} to {window_end} ({n_dates} distinct dates)  ",
        f"**Issue:** #450 (parent #445)  ",
        "",
        "---",
        "",
        "## Recommendation: " + recommendation,
        "",
        rec_reason + ".",
        "",
        "---",
        "",
        "## Methodology",
        "",
        "- **Truth**: `fetch_training_data` joins each forecast date to "
        "`db.get_daily_obs_high` -- the post-#741 observations path (Open-Meteo "
        "fallback rows excluded). `settlements` is never read, decoupling this "
        "backtest from #867's Gamma wrong-market-read contamination.",
        "- **Fit**: per (city, sigma_source), the SAME partial-pooling blend "
        "`scripts/run_emos_shadow.py` uses (issue #798) -- pure per-city `fit_emos` "
        f"at >= {FULL_WEIGHT_SAMPLES} own triples, else a shrinkage blend against the "
        f"city's cross-station pooling group, excluded entirely below "
        f"{POOLED_MIN_CITY_SAMPLES} own triples (or if the pooling group is also thin).",
        "- **This is an in-sample fit-then-score**, matching `emos_crps_log`'s own "
        "existing convention (see `scripts/run_emos_shadow.py::_persist`), NOT a "
        "walk-forward backtest. Numbers below should be read as \"how well does each "
        "track's best fit describe the window it was fit on\", not as an "
        "out-of-sample skill estimate.",
        f"- **Reliability diagram**: each calibrated triple is decomposed into "
        f"{BRACKET_WIDTH_F:g}-degree-F synthetic brackets spanning +/-{BRACKET_SPAN_SIGMAS:g} "
        "sigma around the calibrated mean, scored with `p_normal_between` (the same "
        "primitive `next_day_probability_yes` uses in live trading) -- this avoids "
        "any dependency on real Polymarket bracket boundaries or settlement data.",
        f"- Regime: baseline stack ({', '.join(sorted(BASELINE_REGIME))}); lead_hours=24 (default).",
        "- **Why per-station CRPS/sigma deltas are often tiny even though the raw sigma_f "
        "inputs genuinely differ per date**: `fit_emos` optimises (c, d) to minimise CRPS "
        "against each track's OWN residuals, so it partially re-absorbs whatever raw sigma "
        "proxy it is handed -- two tracks with different `sigma_raw` scales can converge "
        "to similar `sigma_cal` outputs simply because that is close to the CRPS-minimising "
        "spread for that city's residual distribution either way. A near-zero per-station "
        "delta is therefore a genuine result of EMOS's own recalibration, not a sign the "
        "backtest failed to pick up a real difference in `sigma_f`.",
        "",
        "---",
        "",
        "## Aggregate CRPS",
        "",
        "| Track | Cities fitted | Cities excluded | Triples (n) | Mean CRPS |",
        "|---|---|---|---|---|",
        f"| ensemble | {len(cities_fitted_ens)} | {len(cities_excluded_ens)} | "
        f"{len(ens['all_triples'])} | {_fmt(ens_crps)} |",
        f"| fixed | {len(cities_fitted_fix)} | {len(cities_excluded_fix)} | "
        f"{len(fix['all_triples'])} | {_fmt(fix_crps)} |",
        f"| **delta (ensemble - fixed)** | | | | **{_fmt(crps_delta, '+.4f')}** "
        "(negative = ensemble better) |",
        "",
        "## Aggregate Sharpness (calibrated sigma, deg F -- narrower = sharper)",
        "",
        "| Track | Mean sigma | Median sigma |",
        "|---|---|---|",
        f"| ensemble | {_fmt(ens_sharp['mean_sigma'], '.3f')} | {_fmt(ens_sharp['median_sigma'], '.3f')} |",
        f"| fixed | {_fmt(fix_sharp['mean_sigma'], '.3f')} | {_fmt(fix_sharp['median_sigma'], '.3f')} |",
        f"| **delta** | **{_fmt(sigma_delta, '+.3f')}** | | ",
        "",
        "## Aggregate Reliability + Brier (synthetic bracket decomposition, 0.05-wide buckets)",
        "",
        f"Bracket-probability pairs: ensemble n={len(ens_pairs)}, fixed n={len(fix_pairs)}.",
        format_reliability(build_reliability(ens_pairs), "Reliability -- ensemble sigma"),
        "",
        format_reliability(build_reliability(fix_pairs), "Reliability -- fixed sigma"),
        "",
        f"Brier score (ensemble): {_fmt(ens_brier)}  ",
        f"Brier score (fixed): {_fmt(fix_brier)}  ",
        "",
        "---",
        "",
        "## Per-station breakdown",
        "",
        f"Stations with >= {STATION_READABLE_N} triples get their own reliability read below "
        f"(0.10-wide buckets -- still coarser than the aggregate's 0.05 to keep bins "
        f"populated). Everything else: CRPS/sharpness only -- per-station 0.05-bin "
        "reliability at these sample sizes is exactly the noise-as-signal problem "
        "that deferred this issue three times; not presented.",
        "",
        "| City | n (ensemble) | Provenance (ens) | CRPS (ens) | CRPS (fixed) | "
        "Sigma (ens) | Sigma (fixed) | Reliability readable? |",
        "|---|---|---|---|---|---|---|---|",
    ]

    all_cities = sorted(set(ens["per_city"]) | set(fix["per_city"]))
    for city in all_cities:
        e = ens["per_city"].get(city, {})
        f = fix["per_city"].get(city, {})
        n_ens = e.get("n", 0)
        readable = "yes" if n_ens >= STATION_READABLE_N else "no (thin)"
        lines.append(
            f"| {city} | {n_ens} | {e.get('provenance', 'n/a')} | "
            f"{_fmt(e.get('crps'))} | {_fmt(f.get('crps'))} | "
            f"{_fmt((e.get('sharpness') or {}).get('mean_sigma'), '.3f')} | "
            f"{_fmt((f.get('sharpness') or {}).get('mean_sigma'), '.3f')} | {readable} |"
        )

    readable_cities = [
        c for c in all_cities if ens["per_city"].get(c, {}).get("n", 0) >= STATION_READABLE_N
    ]
    lines += ["", "### Per-station reliability (stations clearing the readable-bin threshold)", ""]
    if not readable_cities:
        lines.append(
            f"No station has >= {STATION_READABLE_N} triples yet -- every per-station "
            "reliability table would be bin-thin. See the aggregate reliability diagram "
            "above for the pooled signal."
        )
    else:
        station_edges = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.001]
        for city in readable_cities:
            e_triples = ens["per_city"][city].get("triples", [])
            f_triples = fix["per_city"].get(city, {}).get("triples", [])
            e_pairs = bracket_reliability_pairs(e_triples)
            f_pairs = bracket_reliability_pairs(f_triples)
            lines.append(f"\n**{city}** (n={len(e_triples)} triples)\n")
            lines.append(format_reliability(build_reliability(e_pairs, station_edges),
                                             f"{city} -- ensemble sigma"))
            lines.append(format_reliability(build_reliability(f_pairs, station_edges),
                                             f"{city} -- fixed sigma"))

    lines += ["", "---", "", "## emos_crps_log cross-check (production shadow-run evidence)", ""]
    lines.append("| sigma_source | model_mode | n_rows | n_cities | min_date | max_date | avg_crps |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in crps_log_rows:
        sigma_source, model_mode, n_rows, n_cities, min_date, max_date, avg_crps = row
        lines.append(
            f"| {sigma_source} | {model_mode} | {n_rows} | {n_cities} | {min_date} | "
            f"{max_date} | {_fmt(avg_crps)} |"
        )
    lines += [
        "",
        "**Caveat**: the `ensemble` sigma_source track in `emos_crps_log` only spans "
        "the days since `USE_ENSEMBLE_SIGMA` was flipped on for the shadow runner "
        "(recent) -- it is thin by construction, NOT because ensemble-sigma training "
        "data itself is scarce. `model_forecast_log.sigma_f` has been populated since "
        f"{window_start}, so this backtest's own fit above uses the full "
        f"{n_dates}-date window regardless of when the shadow runner's flag flipped.",
        "",
    ]

    return "\n".join(lines) + "\n"


def _brier(pairs: "list[tuple[float, bool]]") -> "float | None":
    if not pairs:
        return None
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in pairs) / len(pairs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import os

    default_db = os.getenv("DB_PATH", str(_REPO_ROOT / "data" / "meteoedge.db"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=default_db, help="Path to MeteoEdge SQLite DB")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"[ensemble_sigma_calibration_backtest] DB not found at {args.db} -- nothing to do.",
              file=sys.stderr)
        sys.exit(1)

    from src.data.db import Database
    db = Database(args.db)

    window = db._conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM model_forecast_log"
    ).fetchone()
    window_start, window_end, n_dates = window[0], window[1], window[2]

    print(f"[ensemble_sigma_calibration_backtest] model_forecast_log window: "
          f"{window_start} to {window_end} ({n_dates} dates)")

    result = run_backtest(db)
    crps_log_rows = crps_log_summary(db)

    run_date = date.today().isoformat()
    report = build_report(result, crps_log_rows, run_date, window_start, window_end, n_dates)

    _BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _BACKTEST_DIR / f"ensemble_sigma_calibration_{run_date}.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"[ensemble_sigma_calibration_backtest] wrote {report_path}")
    db.close()


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    main()
