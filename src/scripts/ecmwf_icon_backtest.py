"""ECMWF + ICON international ensemble backtest — 30-day simulation.

Compares forecast skill (MAE / RMSE / CRPS) of a multi-model international
ensemble against an Open-Meteo-only baseline, using historical data already
in the MeteoEdge DB.

Because ECMWF and ICON were just integrated, they have no 30-day logged history.
This backtest *simulates* what the ensemble would have produced by:
  - Using real open_meteo forecasts from model_forecast_log for international stations
  - Synthesising ECMWF_proxy = open_meteo + N(0, 0.6°F)  [seed=42]
    (ECMWF HRES is high-quality, tighter spread than GFS)
  - Synthesising ICON_proxy  = open_meteo + N(0, 0.7°F)  [seed=42, EU only]
    (ICON-EU is excellent for Europe but slightly noisier than ECMWF)

Ensemble construction:
  - EU stations (EGLC, LFPB, LIMC, EFHK, EPWA, LTFM, LTAC):
      equal-weight 3-model average (open_meteo + ecmwf_proxy + icon_proxy)
  - Non-EU international stations (RKSI, WMKK, RKPK, ZGSZ, WSSS, MPMG):
      equal-weight 2-model average (open_meteo + ecmwf_proxy)

Scoring:
  (a) open_meteo-only   — MAE / RMSE / CRPS
  (b) ensemble          — equal weights

Gate: MAE improvement ≥ 0.3 °F to promote.

Usage:
    python -m src.scripts.ecmwf_icon_backtest
    python -m src.scripts.ecmwf_icon_backtest --days 14
    python -m src.scripts.ecmwf_icon_backtest --db /path/to/meteoedge.db
"""

import argparse
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = os.getenv("DB_PATH", str(_REPO_ROOT / "data" / "meteoedge.db"))
_BACKTEST_DIR = _REPO_ROOT / "backtest_results"

# International stations to evaluate
_EU_STATIONS = ["EGLC", "LFPB", "LIMC", "EFHK", "EPWA", "LTFM", "LTAC"]
_NON_EU_INTL_STATIONS = ["RKSI", "WMKK", "RKPK", "ZGSZ", "WSSS", "MPMG"]
_ALL_INTL_STATIONS = _NON_EU_INTL_STATIONS + _EU_STATIONS

# ---------------------------------------------------------------------------
# Pure math helpers (identical to hrrr_nbm_backtest)
# ---------------------------------------------------------------------------


def _mae(errors: list) -> float:
    return sum(abs(e) for e in errors) / len(errors) if errors else float("nan")


def _rmse(errors: list) -> float:
    return math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else float("nan")


def _crps_gaussian(mu: float, sigma: float, y: float) -> float:
    """CRPS for Gaussian (mu, sigma) vs observation y."""
    if sigma <= 0:
        return abs(mu - y)
    z = (y - mu) / sigma
    phi_z = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    Phi_z = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return sigma * (z * (2 * Phi_z - 1) + 2 * phi_z - 1.0 / math.sqrt(math.pi))


def _mean_crps(triples: list) -> float:
    """Mean CRPS over list of (mu, sigma, y) tuples."""
    if not triples:
        return float("nan")
    return sum(_crps_gaussian(mu, sigma, y) for mu, sigma, y in triples) / len(triples)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_data(db_path: str, stations: list, days: int):
    """Pull forecast logs and settlements from the DB.

    Returns:
        (forecast_rows, settlement_rows)  — lists of dicts.
        Empty lists if the DB or tables don't exist yet.
    """
    import sqlite3

    if not Path(db_path).exists():
        return [], []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        since_date = (date.today() - timedelta(days=days)).isoformat()
        ph = ",".join("?" * len(stations))

        forecast_rows = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM model_forecast_log "
                f"WHERE station IN ({ph}) AND date >= ? ORDER BY date ASC",
                (*stations, since_date),
            ).fetchall()
        ]
        settlement_rows = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM settlements "
                f"WHERE station IN ({ph}) AND ts >= ? ORDER BY ts ASC",
                (*stations, since_date),
            ).fetchall()
        ]
        conn.close()
        return forecast_rows, settlement_rows
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] DB read failed ({exc}); falling back to synthetic data.", file=sys.stderr)
        return [], []


# ---------------------------------------------------------------------------
# Simulation core
# ---------------------------------------------------------------------------


def _run_simulation(forecast_rows: list, settlement_rows: list, rng: random.Random):
    """Run the ensemble simulation on real DB data.

    Returns per-station per-day result dicts with keys:
        station, date, actual, open_meteo,
        ecmwf_proxy, icon_proxy (None for non-EU),
        ensemble
    """
    # Index forecasts: (station, date, model) -> row
    fc_index: dict = {}
    for r in forecast_rows:
        key = (r["station"], r["date"], r["model"])
        # Keep the row with lowest lead_hours (closest-to-valid capture) if multiple exist
        if key not in fc_index or (r.get("lead_hours") or 0) < (fc_index[key].get("lead_hours") or 999):
            fc_index[key] = r

    # Index settlements: (station, date) -> actual_high_f
    sett_index: dict = {}
    for r in settlement_rows:
        d = r["ts"][:10]
        sett_index[(r["station"], d)] = r["actual_high_f"]

    results = []
    for (station, d), actual in sett_index.items():
        om_row = fc_index.get((station, d, "open_meteo"))
        if om_row is None:
            continue  # need open_meteo as the base for proxies

        om = om_row["forecast_high_f"]
        is_eu = station in _EU_STATIONS

        # Synthesise proxies
        ecmwf_proxy = om + rng.gauss(0, 0.6)
        icon_proxy = (om + rng.gauss(0, 0.7)) if is_eu else None

        # Build ensemble
        if is_eu:
            ens_parts = [om, ecmwf_proxy, icon_proxy]
        else:
            ens_parts = [om, ecmwf_proxy]
        ensemble = sum(ens_parts) / len(ens_parts)

        results.append({
            "station": station,
            "date": d,
            "actual": actual,
            "open_meteo": om,
            "ecmwf_proxy": ecmwf_proxy,
            "icon_proxy": icon_proxy,
            "ensemble": ensemble,
            "is_eu": is_eu,
        })

    return results


def _synthetic_simulation(stations: list, days: int, rng: random.Random):
    """Generate fully synthetic data when the DB has no history.

    Simulates realistic international temperature data so the backtest
    produces a meaningful illustrative report even on a fresh install.
    """
    today = date.today()
    results = []

    # Realistic base temps per international station (°F daily highs)
    base_temps = {
        # EU
        "EGLC": 64, "LFPB": 66, "LIMC": 70, "EFHK": 55,
        "EPWA": 62, "LTFM": 73, "LTAC": 71,
        # Asia / non-EU
        "RKSI": 77, "WMKK": 91, "RKPK": 78,
        "ZGSZ": 88, "WSSS": 90, "MPMG": 88,
    }

    for station in stations:
        base = base_temps.get(station, 75)
        is_eu = station in _EU_STATIONS
        for i in range(days):
            d = (today - timedelta(days=days - i)).isoformat()
            actual = base + rng.gauss(0, 5)

            # open_meteo: slight warm bias, σ=2.8°F
            om = actual + rng.gauss(0.3, 2.8)

            ecmwf_proxy = om + rng.gauss(0, 0.6)
            icon_proxy = (om + rng.gauss(0, 0.7)) if is_eu else None

            if is_eu:
                ensemble = (om + ecmwf_proxy + icon_proxy) / 3.0
            else:
                ensemble = (om + ecmwf_proxy) / 2.0

            results.append({
                "station": station,
                "date": d,
                "actual": actual,
                "open_meteo": om,
                "ecmwf_proxy": ecmwf_proxy,
                "icon_proxy": icon_proxy,
                "ensemble": ensemble,
                "is_eu": is_eu,
            })

    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(rows: list):
    """Compute MAE, RMSE, CRPS for open_meteo-only and ensemble.

    Returns (global_stats, per_station_stats).
    """
    # Sigma estimates: std dev of ensemble spread used as CRPS sigma proxy
    SIGMA_OM = 2.8      # open_meteo point forecast uncertainty
    SIGMA_ENS2 = 2.2    # 2-model (non-EU): tighter
    SIGMA_ENS3 = 1.9    # 3-model (EU): tighter still

    global_errs: dict = {"open_meteo": [], "ensemble": []}
    global_crps_triples: dict = {"open_meteo": [], "ensemble": []}
    per_station: dict = defaultdict(lambda: {
        "open_meteo": [], "ensemble": [],
        "crps_open_meteo": [], "crps_ensemble": [],
    })

    for r in rows:
        actual = r["actual"]
        sigma_ens = SIGMA_ENS3 if r["is_eu"] else SIGMA_ENS2

        for key, mu, sigma in [
            ("open_meteo", r["open_meteo"], SIGMA_OM),
            ("ensemble",   r["ensemble"],   sigma_ens),
        ]:
            if mu is None:
                continue
            err = mu - actual
            global_errs[key].append(err)
            global_crps_triples[key].append((mu, sigma, actual))
            per_station[r["station"]][key].append(err)
            per_station[r["station"]][f"crps_{key}"].append((mu, sigma, actual))

    def _stats(errs, crps_triples):
        return {
            "mae":  _mae(errs),
            "rmse": _rmse(errs),
            "crps": _mean_crps(crps_triples),
            "n":    len(errs),
        }

    global_stats = {
        k: _stats(global_errs[k], global_crps_triples[k])
        for k in ("open_meteo", "ensemble")
    }
    station_stats = {
        st: {k: _stats(d[k], d[f"crps_{k}"]) for k in ("open_meteo", "ensemble")}
        for st, d in per_station.items()
    }
    return global_stats, station_stats


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

_CITY_MAP = {
    "EGLC": "London", "LFPB": "Paris", "LIMC": "Milan",
    "EFHK": "Helsinki", "EPWA": "Warsaw", "LTFM": "Istanbul", "LTAC": "Ankara",
    "RKSI": "Seoul", "WMKK": "Kuala Lumpur", "RKPK": "Busan",
    "ZGSZ": "Shenzhen", "WSSS": "Singapore", "MPMG": "Panama City",
}

MAE_GATE_F = 0.3  # minimum improvement to promote


def _build_report(
    global_stats: dict,
    station_stats: dict,
    days: int,
    data_source: str,
    run_date: str,
) -> str:
    mae_om  = global_stats["open_meteo"]["mae"]
    mae_ens = global_stats["ensemble"]["mae"]
    mae_improvement = mae_om - mae_ens

    rmse_om  = global_stats["open_meteo"]["rmse"]
    rmse_ens = global_stats["ensemble"]["rmse"]

    crps_om  = global_stats["open_meteo"]["crps"]
    crps_ens = global_stats["ensemble"]["crps"]

    gate_pass = mae_improvement >= MAE_GATE_F
    recommendation = "PROMOTE" if gate_pass else ("HOLD" if mae_improvement >= 0 else "KILL")

    eu_stations_present = sorted(s for s in station_stats if s in _EU_STATIONS)
    non_eu_stations_present = sorted(s for s in station_stats if s in _NON_EU_INTL_STATIONS)

    lines = [
        f"# ECMWF + ICON International Ensemble Skill Backtest",
        f"",
        f"**Run date:** {run_date}  ",
        f"**Window:** last {days} days  ",
        f"**EU stations (3-model):** {', '.join(eu_stations_present) or 'none'}  ",
        f"**Non-EU intl stations (2-model):** {', '.join(non_eu_stations_present) or 'none'}  ",
        f"**Data source:** {data_source}  ",
        f"",
        f"---",
        f"",
        f"## Global Skill Summary",
        f"",
        f"| Metric | Open-Meteo only | Ensemble | Delta (ens − baseline) |",
        f"|--------|-----------------|----------|------------------------|",
        f"| MAE (°F)  | {mae_om:.3f} | {mae_ens:.3f} | {mae_ens - mae_om:+.3f} |",
        f"| RMSE (°F) | {rmse_om:.3f} | {rmse_ens:.3f} | {rmse_ens - rmse_om:+.3f} |",
        f"| CRPS      | {crps_om:.3f} | {crps_ens:.3f} | {crps_ens - crps_om:+.3f} |",
        f"| N obs     | {global_stats['open_meteo']['n']} | {global_stats['ensemble']['n']} | — |",
        f"",
        f"---",
        f"",
        f"## Per-Station MAE — Baseline vs Ensemble",
        f"",
        f"| Station | City | Region | Ensemble | MAE baseline (°F) | MAE ensemble (°F) | Delta | CRPS base | CRPS ens |",
        f"|---------|------|--------|----------|-------------------|-------------------|-------|-----------|----------|",
    ]

    for st in sorted(station_stats.keys()):
        s = station_stats[st]
        city = _CITY_MAP.get(st, st)
        region = "EU (3-model)" if st in _EU_STATIONS else "Non-EU (2-model)"
        ens_label = "OM+ECMWF+ICON" if st in _EU_STATIONS else "OM+ECMWF"
        delta = s["ensemble"]["mae"] - s["open_meteo"]["mae"]
        sign = "+" if delta > 0 else ""
        lines.append(
            f"| {st} | {city} | {region} | {ens_label} | {s['open_meteo']['mae']:.3f} "
            f"| {s['ensemble']['mae']:.3f} | {sign}{delta:.3f} "
            f"| {s['open_meteo']['crps']:.3f} | {s['ensemble']['crps']:.3f} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## Recommendation",
        f"",
        f"**Gate:** MAE improvement ≥ {MAE_GATE_F} °F required to promote.",
        f"",
        f"| Criterion | Value | Pass? |",
        f"|-----------|-------|-------|",
        f"| MAE improvement (baseline → ensemble) | {mae_improvement:+.3f} °F | {'YES' if gate_pass else 'NO'} |",
        f"| RMSE improvement | {rmse_om - rmse_ens:+.3f} °F | {'YES' if rmse_om > rmse_ens else 'NO'} |",
        f"| CRPS improvement | {crps_om - crps_ens:+.3f} | {'YES' if crps_om > crps_ens else 'NO'} |",
        f"",
        f"### **Decision: {recommendation}**",
        f"",
    ]

    if recommendation == "PROMOTE":
        lines.append(
            f"The ECMWF + ICON ensemble meets the MAE gate (improvement of "
            f"{mae_improvement:.3f} °F ≥ {MAE_GATE_F} °F). "
            f"Recommend promoting ECMWF and ICON channels to live DEB weighting. "
            f"Set `FORECAST_STACK=intl_ecmwf_icon` in the DB config."
        )
    elif recommendation == "HOLD":
        lines.append(
            f"The ensemble shows marginal improvement ({mae_improvement:.3f} °F) below the "
            f"{MAE_GATE_F} °F gate. Hold — collect 30 days of live ECMWF + ICON forecasts "
            f"and re-run this backtest with real data before promoting."
        )
    else:
        lines.append(
            f"The ensemble performs **worse** than the Open-Meteo baseline "
            f"(MAE delta = {mae_improvement:.3f} °F). "
            f"Kill the integration — investigate data quality issues with the ECMWF / ICON "
            f"connectors before re-attempting."
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## Methodology Notes",
        f"",
        f"- **ECMWF proxy:** `open_meteo + N(0, 0.6°F)` — ECMWF HRES is a high-resolution",
        f"  global NWP model expected to track closely to Open-Meteo but with slightly",
        f"  tighter spread (ECMWF is the leading global NWP model).",
        f"- **ICON proxy (EU only):** `open_meteo + N(0, 0.7°F)` — ICON-EU from DWD is",
        f"  excellent for European stations but slightly noisier than ECMWF.",
        f"- **EU ensemble:** equal-weight 3-model (open_meteo + ecmwf_proxy + icon_proxy).",
        f"- **Non-EU ensemble:** equal-weight 2-model (open_meteo + ecmwf_proxy).",
        f"- **Random seed:** 42 (reproducible).",
        f"- **CRPS sigma:** baseline=2.8°F, 2-model=2.2°F, 3-model=1.9°F (ensemble compression).",
        f"- **Data source:** {data_source}",
        f"",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ECMWF + ICON international ensemble skill backtest"
    )
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    parser.add_argument("--db", default=_DEFAULT_DB_PATH, help="Path to MeteoEdge SQLite DB")
    parser.add_argument("--stations", nargs="+", default=_ALL_INTL_STATIONS,
                        help="Station ICAO codes to evaluate")
    args = parser.parse_args()

    rng = random.Random(42)
    run_date = date.today().isoformat()

    print(f"[ecmwf_icon_backtest] Loading data from {args.db} ...")
    forecast_rows, settlement_rows = _load_data(args.db, args.stations, args.days)

    if forecast_rows and settlement_rows:
        print(f"[ecmwf_icon_backtest] Found {len(forecast_rows)} forecast rows, "
              f"{len(settlement_rows)} settlement rows. Using real DB data.")
        sim_rows = _run_simulation(forecast_rows, settlement_rows, rng)
        data_source = f"Real DB data from `{args.db}`"
    else:
        print(f"[ecmwf_icon_backtest] No DB data found — running synthetic simulation "
              f"({args.days} days × {len(args.stations)} stations).", file=sys.stderr)
        sim_rows = _synthetic_simulation(args.stations, args.days, rng)
        data_source = "Synthetic simulation (no DB history available — illustrative only)"

    if not sim_rows:
        print("[ecmwf_icon_backtest] No simulation rows produced. Nothing to score.", file=sys.stderr)
        sys.exit(1)

    print(f"[ecmwf_icon_backtest] Scoring {len(sim_rows)} station-day observations ...")
    global_stats, station_stats = _score(sim_rows)

    report = _build_report(
        global_stats=global_stats,
        station_stats=station_stats,
        days=args.days,
        data_source=data_source,
        run_date=run_date,
    )

    # Write report
    _BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _BACKTEST_DIR / f"ecmwf_icon_skill_{run_date}.md"
    report_path.write_text(report, encoding="utf-8")

    print()
    print(report)
    print(f"\n[ecmwf_icon_backtest] Report saved to {report_path}")


if __name__ == "__main__":
    main()
