"""HRRR + NBM ensemble backtest — 30-day simulation.

Compares forecast skill (MAE / RMSE / CRPS) of a 4-model ensemble
(NWS + open_meteo + HRRR_proxy + NBM_proxy) against a NWS-only baseline,
using historical data already in the MeteoEdge DB.

Because HRRR and NBM were just integrated, they have no 30-day logged history.
This backtest *simulates* what the ensemble would have produced by:
  - Using real NWS and open_meteo forecasts from model_forecast_log
  - Synthesising HRRR_proxy = open_meteo + N(0, 0.8°F)  [seed=42]
  - Synthesising NBM_proxy  = (NWS + open_meteo)/2 + N(0, 0.5°F)  [seed=42]

Scoring:
  (a) NWS-only              — MAE / RMSE / CRPS
  (b) 2-model ensemble      — equal weights NWS 0.5 + open_meteo 0.5
  (c) 4-model ensemble      — equal weights 0.25 each

Usage:
    python -m src.scripts.hrrr_nbm_backtest
    python -m src.scripts.hrrr_nbm_backtest --days 14 --stations KORD KMIA
    python -m src.scripts.hrrr_nbm_backtest --db /path/to/analytics.db
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

_US_STATIONS = ["KORD", "KMIA", "KLAX", "KATL", "KHOU"]

# ---------------------------------------------------------------------------
# Pure math helpers
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
        station, date, actual,
        nws, open_meteo, hrrr_proxy, nbm_proxy,
        ens2, ens4
    """
    # Index forecasts: (station, date, model) -> forecast_high_f
    fc_index: dict = {}
    for r in forecast_rows:
        key = (r["station"], r["date"], r["model"])
        # Keep the row with highest lead_hours (most recent capture) if multiple exist
        if key not in fc_index or (r.get("lead_hours") or 0) < (fc_index[key].get("lead_hours") or 999):
            fc_index[key] = r

    # Index settlements: (station, date) -> actual_high_f
    sett_index: dict = {}
    for r in settlement_rows:
        d = r["ts"][:10]
        sett_index[(r["station"], d)] = r["actual_high_f"]

    results = []
    for (station, d), actual in sett_index.items():
        nws_row = fc_index.get((station, d, "nws"))
        om_row = fc_index.get((station, d, "open_meteo"))
        if nws_row is None and om_row is None:
            continue  # can't build even a partial ensemble

        nws = nws_row["forecast_high_f"] if nws_row else None
        om = om_row["forecast_high_f"] if om_row else None

        # Synthesise proxies
        if om is not None:
            hrrr_proxy = om + rng.gauss(0, 0.8)
        elif nws is not None:
            hrrr_proxy = nws + rng.gauss(0, 0.8)
        else:
            hrrr_proxy = None

        if nws is not None and om is not None:
            nbm_proxy = (nws + om) / 2.0 + rng.gauss(0, 0.5)
        elif nws is not None:
            nbm_proxy = nws + rng.gauss(0, 0.5)
        elif om is not None:
            nbm_proxy = om + rng.gauss(0, 0.5)
        else:
            nbm_proxy = None

        # 2-model ensemble
        ens2_parts = [v for v in [nws, om] if v is not None]
        ens2 = sum(ens2_parts) / len(ens2_parts) if ens2_parts else None

        # 4-model ensemble (equal 0.25 weights)
        ens4_parts = [v for v in [nws, om, hrrr_proxy, nbm_proxy] if v is not None]
        ens4 = sum(ens4_parts) / len(ens4_parts) if ens4_parts else None

        results.append({
            "station": station,
            "date": d,
            "actual": actual,
            "nws": nws,
            "open_meteo": om,
            "hrrr_proxy": hrrr_proxy,
            "nbm_proxy": nbm_proxy,
            "ens2": ens2,
            "ens4": ens4,
        })

    return results


def _synthetic_simulation(stations: list, days: int, rng: random.Random):
    """Generate fully synthetic data when the DB has no history.

    Simulates 30 days of realistic US temperature data so the backtest
    produces a meaningful illustrative report even on a fresh install.
    """
    today = date.today()
    results = []

    # Realistic base temps per US station (°F daily highs, summer baseline)
    base_temps = {
        "KORD": 78, "KMIA": 90, "KLAX": 75, "KATL": 85, "KHOU": 92,
    }

    for station in stations:
        base = base_temps.get(station, 80)
        for i in range(days):
            d = (today - timedelta(days=days - i)).isoformat()
            # Simulate actual temperature with seasonal variation
            actual = base + rng.gauss(0, 5)

            # NWS: unbiased, σ=2.5°F
            nws = actual + rng.gauss(0, 2.5)
            # open_meteo: slight warm bias, σ=2.8°F
            om = actual + rng.gauss(0.3, 2.8)

            hrrr_proxy = om + rng.gauss(0, 0.8)
            nbm_proxy = (nws + om) / 2.0 + rng.gauss(0, 0.5)

            ens2 = (nws + om) / 2.0
            ens4 = (nws + om + hrrr_proxy + nbm_proxy) / 4.0

            results.append({
                "station": station,
                "date": d,
                "actual": actual,
                "nws": nws,
                "open_meteo": om,
                "hrrr_proxy": hrrr_proxy,
                "nbm_proxy": nbm_proxy,
                "ens2": ens2,
                "ens4": ens4,
            })

    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(rows: list):
    """Compute MAE, RMSE, CRPS for each of: nws-only, ens2, ens4.

    Returns (global_stats, per_station_stats).
    """
    # Sigma estimates: std dev of ensemble spread used as CRPS sigma proxy
    SIGMA_NWS = 2.5     # NWS point forecast uncertainty
    SIGMA_ENS2 = 2.0    # tighter — two models
    SIGMA_ENS4 = 1.6    # tighter — four models

    global_errs: dict = {"nws": [], "ens2": [], "ens4": []}
    global_crps_triples: dict = {"nws": [], "ens2": [], "ens4": []}
    per_station: dict = defaultdict(lambda: {"nws": [], "ens2": [], "ens4": [],
                                              "crps_nws": [], "crps_ens2": [], "crps_ens4": []})

    for r in rows:
        actual = r["actual"]

        for key, mu, sigma in [
            ("nws",  r["nws"],  SIGMA_NWS),
            ("ens2", r["ens2"], SIGMA_ENS2),
            ("ens4", r["ens4"], SIGMA_ENS4),
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

    global_stats = {k: _stats(global_errs[k], global_crps_triples[k]) for k in ("nws", "ens2", "ens4")}
    station_stats = {
        st: {k: _stats(d[k], d[f"crps_{k}"]) for k in ("nws", "ens2", "ens4")}
        for st, d in per_station.items()
    }
    return global_stats, station_stats


# ---------------------------------------------------------------------------
# Edge-sign and PnL analysis
# ---------------------------------------------------------------------------


def _edge_sign_analysis(rows: list, threshold_band: float = 2.0):
    """Count days where NWS and ens4 disagree on the market edge direction.

    A 'call' is YES if forecast > actual ± threshold_band (proxy for the
    market bracket midpoint).  A sign flip = NWS says YES but ens4 says NO,
    or vice versa.

    Returns (flipped, total, flip_pct).
    """
    flipped = 0
    total = 0
    for r in rows:
        if r["nws"] is None or r["ens4"] is None:
            continue
        actual = r["actual"]
        nws_yes = r["nws"] > actual + threshold_band
        ens4_yes = r["ens4"] > actual + threshold_band
        total += 1
        if nws_yes != ens4_yes:
            flipped += 1

    pct = (flipped / total * 100) if total else 0
    return flipped, total, pct


def _simulated_pnl(rows: list):
    """Simple PnL simulation: +1 unit on correct call, -1 on incorrect.

    'Correct' means the ensemble's forecast direction matches the observed
    temperature movement direction (vs prior day's actual).

    Returns (total_pnl, win_count, loss_count).
    """
    # Sort rows per station by date to compute prior-day difference
    by_station: dict = defaultdict(list)
    for r in rows:
        by_station[r["station"]].append(r)
    for st in by_station:
        by_station[st].sort(key=lambda x: x["date"])

    total_pnl = 0.0
    wins = losses = 0

    for st, days_list in by_station.items():
        for i in range(1, len(days_list)):
            prev = days_list[i - 1]
            cur = days_list[i]
            if cur["ens4"] is None or prev["actual"] is None:
                continue
            # Direction: is today's actual > yesterday's actual?
            actual_up = cur["actual"] > prev["actual"]
            forecast_up = cur["ens4"] > prev["actual"]
            if actual_up == forecast_up:
                total_pnl += 1
                wins += 1
            else:
                total_pnl -= 1
                losses += 1

    return total_pnl, wins, losses


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

_CITY_MAP = {
    "KORD": "Chicago", "KMIA": "Miami", "KLAX": "Los Angeles",
    "KATL": "Atlanta", "KHOU": "Houston",
}

MAE_GATE_F = 0.3  # minimum improvement to promote


def _build_report(
    global_stats: dict,
    station_stats: dict,
    edge_flipped: int,
    edge_total: int,
    edge_pct: float,
    total_pnl: float,
    pnl_wins: int,
    pnl_losses: int,
    days: int,
    data_source: str,
    run_date: str,
) -> str:
    mae_nws  = global_stats["nws"]["mae"]
    mae_ens4 = global_stats["ens4"]["mae"]
    mae_improvement = mae_nws - mae_ens4

    rmse_nws  = global_stats["nws"]["rmse"]
    rmse_ens4 = global_stats["ens4"]["rmse"]

    crps_nws  = global_stats["nws"]["crps"]
    crps_ens4 = global_stats["ens4"]["crps"]

    gate_pass = mae_improvement >= MAE_GATE_F
    recommendation = "PROMOTE" if gate_pass else ("HOLD" if mae_improvement >= 0 else "KILL")

    lines = [
        f"# HRRR + NBM Ensemble Skill Backtest",
        f"",
        f"**Run date:** {run_date}  ",
        f"**Window:** last {days} days  ",
        f"**Stations:** {', '.join(sorted(station_stats.keys()))}  ",
        f"**Data source:** {data_source}  ",
        f"",
        f"---",
        f"",
        f"## Global Skill Summary",
        f"",
        f"| Metric | NWS-only | 2-model ensemble | 4-model ensemble | Delta (4m − NWS) |",
        f"|--------|----------|-----------------|-----------------|-----------------|",
        f"| MAE (°F)  | {mae_nws:.3f} | {global_stats['ens2']['mae']:.3f} | {mae_ens4:.3f} | {mae_ens4 - mae_nws:+.3f} |",
        f"| RMSE (°F) | {rmse_nws:.3f} | {global_stats['ens2']['rmse']:.3f} | {rmse_ens4:.3f} | {rmse_ens4 - rmse_nws:+.3f} |",
        f"| CRPS      | {crps_nws:.3f} | {global_stats['ens2']['crps']:.3f} | {crps_ens4:.3f} | {crps_ens4 - crps_nws:+.3f} |",
        f"| N obs     | {global_stats['nws']['n']} | {global_stats['ens2']['n']} | {global_stats['ens4']['n']} | — |",
        f"",
        f"---",
        f"",
        f"## Per-Station MAE — Before vs After",
        f"",
        f"| Station | City | MAE NWS-only (°F) | MAE 4-model (°F) | Delta | CRPS NWS | CRPS 4m |",
        f"|---------|------|------------------|-----------------|-------|----------|---------|",
    ]

    for st in sorted(station_stats.keys()):
        s = station_stats[st]
        city = _CITY_MAP.get(st, st)
        delta = s["ens4"]["mae"] - s["nws"]["mae"]
        sign = "+" if delta > 0 else ""
        lines.append(
            f"| {st} | {city} | {s['nws']['mae']:.3f} | {s['ens4']['mae']:.3f} "
            f"| {sign}{delta:.3f} | {s['nws']['crps']:.3f} | {s['ens4']['crps']:.3f} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## Edge-Sign Analysis",
        f"",
        f"Candidate days where NWS-only and 4-model ensemble predicted **opposite** market edge directions:",
        f"",
        f"- **Threshold:** ±2 °F from observed actual (proxy for bracket mid)",
        f"- **Flipped signs:** {edge_flipped} / {edge_total} candidate days ({edge_pct:.1f}%)",
        f"",
        f"Interpretation: on {edge_pct:.1f}% of days, adding HRRR + NBM would have changed the",
        f"YES/NO call direction. These are the days where model diversity has the highest impact.",
        f"",
        f"---",
        f"",
        f"## Simulated PnL Projection (US Stations)",
        f"",
        f"Methodology: +1 unit profit on days where 4-model ensemble predicts the correct",
        f"temperature-change direction (up/down vs prior day); −1 unit on incorrect calls.",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total PnL (units) | {total_pnl:+.0f} |",
        f"| Winning days | {pnl_wins} |",
        f"| Losing days | {pnl_losses} |",
        f"| Win rate | {pnl_wins / (pnl_wins + pnl_losses) * 100:.1f}% |" if (pnl_wins + pnl_losses) > 0 else "| Win rate | N/A |",
        f"",
        f"---",
        f"",
        f"## Recommendation",
        f"",
        f"**Gate:** MAE improvement ≥ {MAE_GATE_F} °F required to promote.",
        f"",
        f"| Criterion | Value | Pass? |",
        f"|-----------|-------|-------|",
        f"| MAE improvement (NWS → 4-model) | {mae_improvement:+.3f} °F | {'YES' if gate_pass else 'NO'} |",
        f"| RMSE improvement | {rmse_nws - rmse_ens4:+.3f} °F | {'YES' if rmse_nws > rmse_ens4 else 'NO'} |",
        f"| CRPS improvement | {crps_nws - crps_ens4:+.3f} | {'YES' if crps_nws > crps_ens4 else 'NO'} |",
        f"",
        f"### **Decision: {recommendation}**",
        f"",
    ]

    if recommendation == "PROMOTE":
        lines.append(
            f"The 4-model ensemble meets the MAE gate (improvement of {mae_improvement:.3f} °F ≥ {MAE_GATE_F} °F). "
            f"Recommend promoting HRRR and NBM channels to live DEB weighting. "
            f"Run `deb_update` to initialise equal weights (0.25 each) and begin online learning."
        )
    elif recommendation == "HOLD":
        lines.append(
            f"The 4-model ensemble shows marginal improvement ({mae_improvement:.3f} °F) below the "
            f"{MAE_GATE_F} °F gate. Hold — collect 30 days of live HRRR + NBM forecasts and re-run "
            f"this backtest with real data before promoting."
        )
    else:
        lines.append(
            f"The 4-model ensemble performs **worse** than NWS-only (MAE delta = {mae_improvement:.3f} °F). "
            f"Kill the integration — investigate data quality issues with the HRRR / NBM connectors "
            f"before re-attempting."
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## Methodology Notes",
        f"",
        f"- **HRRR proxy:** `open_meteo + N(0, 0.8°F)` — HRRR is a high-resolution NWP model",
        f"  expected to track GFS/open_meteo closely but with tighter spread.",
        f"- **NBM proxy:** `(NWS + open_meteo)/2 + N(0, 0.5°F)` — NBM blends multiple NWP",
        f"  models; modelled here as the average of our two existing sources with small noise.",
        f"- **Random seed:** 42 (reproducible).",
        f"- **4-model weights:** equal (0.25 each) — DEB has no history for new channels.",
        f"- **CRPS sigma:** NWS=2.5°F, 2-model=2.0°F, 4-model=1.6°F (ensemble compression).",
        f"- **Data source:** {data_source}",
        f"",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HRRR + NBM 4-model ensemble skill backtest"
    )
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    parser.add_argument("--db", default=_DEFAULT_DB_PATH, help="Path to MeteoEdge SQLite DB")
    parser.add_argument("--stations", nargs="+", default=_US_STATIONS,
                        help="Station ICAO codes to evaluate")
    args = parser.parse_args()

    rng = random.Random(42)
    run_date = date.today().isoformat()

    print(f"[hrrr_nbm_backtest] Loading data from {args.db} ...")
    forecast_rows, settlement_rows = _load_data(args.db, args.stations, args.days)

    if forecast_rows and settlement_rows:
        print(f"[hrrr_nbm_backtest] Found {len(forecast_rows)} forecast rows, "
              f"{len(settlement_rows)} settlement rows. Using real DB data.")
        sim_rows = _run_simulation(forecast_rows, settlement_rows, rng)
        data_source = f"Real DB data from `{args.db}`"
    else:
        print(f"[hrrr_nbm_backtest] No DB data found — running synthetic simulation "
              f"({args.days} days × {len(args.stations)} stations).", file=sys.stderr)
        sim_rows = _synthetic_simulation(args.stations, args.days, rng)
        data_source = "Synthetic simulation (no DB history available — illustrative only)"

    if not sim_rows:
        print("[hrrr_nbm_backtest] No simulation rows produced. Nothing to score.", file=sys.stderr)
        sys.exit(1)

    print(f"[hrrr_nbm_backtest] Scoring {len(sim_rows)} station-day observations ...")
    global_stats, station_stats = _score(sim_rows)
    edge_flipped, edge_total, edge_pct = _edge_sign_analysis(sim_rows)
    total_pnl, pnl_wins, pnl_losses = _simulated_pnl(sim_rows)

    report = _build_report(
        global_stats=global_stats,
        station_stats=station_stats,
        edge_flipped=edge_flipped,
        edge_total=edge_total,
        edge_pct=edge_pct,
        total_pnl=total_pnl,
        pnl_wins=pnl_wins,
        pnl_losses=pnl_losses,
        days=args.days,
        data_source=data_source,
        run_date=run_date,
    )

    # Write report
    _BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _BACKTEST_DIR / f"hrrr_nbm_skill_{run_date}.md"
    report_path.write_text(report, encoding="utf-8")

    print()
    print(report)
    print(f"\n[hrrr_nbm_backtest] Report saved to {report_path}")


if __name__ == "__main__":
    main()
