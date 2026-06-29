"""Shadow forecast health checker — four data quality checks.

Checks:
  1. COVERAGE     — models with zero station rows on any day in the window
  2. RANGE        — forecast values outside physical bounds (-60°F to 130°F)
  3. DIVERGENCE   — models diverging > 15°F for same (station, date, lead)
  4. CALIBRATION  — settled shadow trades: actual win rate vs predicted probability,
                    plus per-model MAE against settled actuals

Exit codes:
  0 — COVERAGE and RANGE pass (divergence and calibration are informational)
  1 — COVERAGE or RANGE failed, or DB not found

Usage:
    python -m src.scripts.check_shadow_health
    python -m src.scripts.check_shadow_health --days 14 --lead-hours 24
    python -m src.scripts.check_shadow_health --write-guardrails
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = os.getenv("DB_PATH", str(_REPO_ROOT / "data" / "meteoedge.db"))

_KNOWN_MODELS = ["nws", "open_meteo", "gfs", "gefs", "hrrr", "nbm", "ecmwf", "icon"]

_TEMP_MIN_F = -60.0
_TEMP_MAX_F = 130.0

_DIVERGENCE_THRESHOLD_F = 15.0

_CAL_MIN_SAMPLES = 5


def _open_conn(db_path: str) -> "sqlite3.Connection | None":
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Check 1 — Coverage
# ---------------------------------------------------------------------------

def check_coverage(conn: sqlite3.Connection, days: int) -> tuple[list[str], bool]:
    """Report per-model fill rates; flag any model with a zero-row day."""
    since = (date.today() - timedelta(days=days)).isoformat()

    try:
        rows = conn.execute(
            "SELECT model, date, COUNT(DISTINCT station) AS n "
            "FROM model_forecast_log "
            "WHERE date >= ? "
            "GROUP BY model, date "
            "ORDER BY model, date",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ["  (model_forecast_log table not found — run capture_forecasts.py first)"], False

    if not rows:
        return ["  (no rows in model_forecast_log for this window)"], False

    # Build: model -> {date -> n_stations}
    by_model: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        by_model[r["model"]][r["date"]] = r["n"]

    date_list = [
        (date.today() - timedelta(days=i)).isoformat()
        for i in range(days, 0, -1)
    ]

    lines: list[str] = []
    lines.append(f"  {'MODEL':<14}  {'FILL':>5}  ZERO DAYS")

    date_set = set(date_list)
    any_zero = False
    for model in sorted(by_model.keys()):
        counts = by_model[model]

        # Separate dates inside the historical window from outside it (today / future
        # target dates written by 24h-lead captures).
        hist_counts = {d: n for d, n in counts.items() if d in date_set}
        outside_dates = sorted(d for d in counts if d not in date_set)

        zero_days = [d for d in date_list if hist_counts.get(d, 0) == 0]
        max_n = max(hist_counts.values(), default=0)
        filled_days = sum(1 for d in date_list if hist_counts.get(d, 0) > 0)
        fill_pct = filled_days / days * 100 if days > 0 else 0.0

        if zero_days:
            # Only a real failure if there is no recent data outside the window either.
            # A model that just started capturing will have data for today/tomorrow but
            # no historical rows yet — show "STARTED RECENTLY" rather than flagging.
            if outside_dates and filled_days == 0:
                flag = "  "
                latest = max(outside_dates)
                zero_str = f"STARTED RECENTLY (first capture: {latest})"
            else:
                any_zero = True
                flag = " !"
                zero_str = ", ".join(zero_days[:5])
                if len(zero_days) > 5:
                    zero_str += f" (+{len(zero_days) - 5} more)"
        else:
            flag = "  "
            zero_str = "none"

        lines.append(
            f"  {model:<14}{flag}  {fill_pct:4.0f}%  {zero_str}"
            f"  (max {max_n} stations/day)"
        )

    return lines, not any_zero


# ---------------------------------------------------------------------------
# Check 2 — Range
# ---------------------------------------------------------------------------

def check_range(
    conn: sqlite3.Connection,
    days: int,
    write_guardrails: bool,
    db_api=None,
) -> tuple[list[str], bool]:
    """Flag forecast_high_f values outside [-60°F, 130°F]."""
    since = (date.today() - timedelta(days=days)).isoformat()

    try:
        rows = conn.execute(
            "SELECT station, model, date, lead_hours, forecast_high_f, logged_at "
            "FROM model_forecast_log "
            "WHERE date >= ? AND (forecast_high_f < ? OR forecast_high_f > ?) "
            "ORDER BY logged_at DESC",
            (since, _TEMP_MIN_F, _TEMP_MAX_F),
        ).fetchall()
    except sqlite3.OperationalError:
        return ["  (model_forecast_log not found)"], True

    if not rows:
        return [f"  OK — no values outside [{_TEMP_MIN_F}°F, {_TEMP_MAX_F}°F]."], True

    lines = [
        f"  {len(rows)} out-of-range row(s) "
        f"(bounds: {_TEMP_MIN_F}°F – {_TEMP_MAX_F}°F):"
    ]
    for r in rows:
        lines.append(
            f"    {r['station']:<6} {r['model']:<12} {r['date']}  "
            f"lead={r['lead_hours']}h  value={r['forecast_high_f']:.1f}°F  "
            f"logged={r['logged_at'][:19]}"
        )
        if write_guardrails and db_api is not None:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            try:
                db_api.log_guardrail_event(
                    ts=ts,
                    station=r["station"],
                    event_type="oor_forecast",
                    raw_value=float(r["forecast_high_f"]),
                    adj_value=float(r["forecast_high_f"]),
                )
            except Exception:
                pass

    return lines, False


# ---------------------------------------------------------------------------
# Check 3 — Divergence
# ---------------------------------------------------------------------------

def check_divergence(
    conn: sqlite3.Connection,
    days: int,
    lead_hours: int,
) -> tuple[list[str], bool]:
    """Flag (station, date) pairs where max–min across models > threshold."""
    since = (date.today() - timedelta(days=days)).isoformat()

    try:
        rows = conn.execute(
            "SELECT station, date, model, forecast_high_f "
            "FROM model_forecast_log "
            "WHERE date >= ? AND lead_hours = ? "
            "ORDER BY station, date, model",
            (since, lead_hours),
        ).fetchall()
    except sqlite3.OperationalError:
        return ["  (model_forecast_log not found)"], True

    # Group by (station, date)
    groups: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for r in rows:
        groups[(r["station"], r["date"])][r["model"]] = r["forecast_high_f"]

    lines: list[str] = []
    flagged = 0

    for (station, d), model_vals in sorted(groups.items()):
        if len(model_vals) < 2:
            continue
        vals = list(model_vals.values())
        span = max(vals) - min(vals)
        if span > _DIVERGENCE_THRESHOLD_F:
            flagged += 1
            detail = "  ".join(f"{m}={v:.1f}" for m, v in sorted(model_vals.items()))
            lines.append(
                f"    {station:<6} {d}  span={span:.1f}°F  [{detail}]"
            )

    if flagged == 0:
        return [f"  OK — no pair diverges > {_DIVERGENCE_THRESHOLD_F}°F."], True

    header = [f"  {flagged} combo(s) exceed {_DIVERGENCE_THRESHOLD_F}°F span (warning only):"]
    return header + lines, True


# ---------------------------------------------------------------------------
# Check 4 — Calibration
# ---------------------------------------------------------------------------

def check_calibration(conn: sqlite3.Connection, cal_days: int) -> tuple[list[str], bool]:
    """Shadow trade win-rate calibration + per-model MAE vs actuals."""
    since = (date.today() - timedelta(days=cal_days)).isoformat()
    lines: list[str] = []

    # -- 4a. Shadow trade ensemble calibration --
    try:
        shadow_rows = conn.execute(
            "SELECT predicted_price, pnl "
            "FROM trades "
            "WHERE mode='shadow' AND pnl IS NOT NULL AND ts >= ? "
            "ORDER BY ts",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        shadow_rows = []

    if not shadow_rows:
        lines.append("  No settled shadow trades yet — skipping calibration.")
    else:
        buckets: dict[int, list[float]] = defaultdict(list)
        for r in shadow_rows:
            # Clamp to [5, 95] before bucketing: predicted_price=100 is a
            # boundary artifact of the probability cap (MODEL_PROB_CAP=0.95)
            # and belongs in the 90-99¢ bucket, not an impossible 100-109¢ one.
            price = min(max(int(r["predicted_price"]), 5), 95)
            mid = (price // 10) * 10 + 5
            buckets[mid].append(r["pnl"])

        lines.append(
            f"  Shadow trade calibration ({len(shadow_rows)} settled, trailing {cal_days}d):"
        )
        lines.append(f"  {'BUCKET':>8}  {'PREDICTED':>10}  {'WIN RATE':>10}  {'COUNT':>6}  STATUS")
        for mid in sorted(buckets.keys()):
            pnls = buckets[mid]
            if len(pnls) < _CAL_MIN_SAMPLES:
                continue
            predicted_p = mid / 100.0
            win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
            diff = abs(win_rate - predicted_p)
            status = "WARN" if diff > 0.15 else "OK"
            lo, hi = mid - 5, mid + 4
            lines.append(
                f"  {lo:3d}–{hi:3d}¢  "
                f"{predicted_p * 100:8.0f}%  "
                f"{win_rate * 100:9.1f}%  "
                f"{len(pnls):5d}  {status}"
            )

    # -- 4b. Per-model MAE vs settled actuals --
    lines.append("")
    try:
        mae_rows = conn.execute(
            "SELECT m.model, m.forecast_high_f, s.actual_high_f "
            "FROM model_forecast_log m "
            "JOIN settlements s "
            "  ON m.station = s.station "
            "  AND (substr(s.ts, 1, 10) = m.date "
            "       OR substr(s.ts, 1, 10) = date(m.date, '+1 day')) "
            "WHERE m.date >= ? "
            "  AND m.lead_hours = 24 "
            "  AND s.actual_high_f IS NOT NULL",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        mae_rows = []

    if not mae_rows:
        lines.append("  Per-model MAE: no settled actuals to compare against yet.")
    else:
        by_model: dict[str, list[float]] = defaultdict(list)
        for r in mae_rows:
            by_model[r["model"]].append(abs(r["forecast_high_f"] - r["actual_high_f"]))

        lines.append(f"  Per-model MAE vs actual highs (lead=24h, trailing {cal_days}d):")
        lines.append(f"  {'MODEL':<14}  {'MAE (°F)':>10}  {'SAMPLES':>8}")
        for model in sorted(by_model.keys()):
            errs = by_model[model]
            mae = sum(errs) / len(errs)
            lines.append(f"  {model:<14}  {mae:9.2f}°F  {len(errs):7d}")

    return lines, True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Check shadow forecast data quality: coverage, range, "
            "divergence, calibration."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        metavar="N",
        help="Look-back window (days) for coverage, range, divergence (default: 7).",
    )
    parser.add_argument(
        "--lead-hours",
        type=int,
        default=24,
        metavar="H",
        help="Lead-time bin for divergence check (default: 24).",
    )
    parser.add_argument(
        "--cal-days",
        type=int,
        default=30,
        metavar="N",
        help="Look-back window (days) for calibration check (default: 30).",
    )
    parser.add_argument(
        "--write-guardrails",
        action="store_true",
        default=False,
        help="Write out-of-range findings to the guardrail_events table.",
    )
    parser.add_argument(
        "--db",
        default=_DEFAULT_DB_PATH,
        metavar="PATH",
        help=f"Path to SQLite database (default: {_DEFAULT_DB_PATH}).",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("Shadow Forecast Health Check")
    print(f"DB:              {args.db}")
    print(f"Coverage window: {args.days}d    Calibration window: {args.cal_days}d")
    print("=" * 70)

    conn = _open_conn(args.db)
    if conn is None:
        print(f"\n[ERROR] Database not found: {args.db}")
        print("Run the ingestion pipeline first.")
        sys.exit(1)

    db_api = None
    if args.write_guardrails:
        try:
            from src.data.db import Database
            db_api = Database(args.db)
        except Exception as exc:
            print(f"[WARN] Cannot open Database for guardrail writes: {exc}")

    all_ok = True

    # 1. Coverage
    print("\n1. COVERAGE")
    print("-" * 50)
    cov_lines, cov_ok = check_coverage(conn, args.days)
    for line in cov_lines:
        print(line)
    if cov_ok:
        print("  [PASS]")
    else:
        print("  [FAIL] One or more models had zero rows on a day in the window.")
        all_ok = False

    # 2. Range
    print("\n2. RANGE")
    print("-" * 50)
    rng_lines, rng_ok = check_range(conn, args.days, args.write_guardrails, db_api)
    for line in rng_lines:
        print(line)
    if rng_ok:
        print("  [PASS]")
    else:
        print("  [FAIL] Out-of-range forecast values detected.")
        if args.write_guardrails:
            print("  Findings written to guardrail_events (type='oor_forecast').")
        all_ok = False

    # 3. Divergence
    print(f"\n3. DIVERGENCE  (lead={args.lead_hours}h, threshold={_DIVERGENCE_THRESHOLD_F}°F)")
    print("-" * 50)
    div_lines, _ = check_divergence(conn, args.days, args.lead_hours)
    for line in div_lines:
        print(line)
    print("  [INFO] Warning signal only — does not affect exit code.")

    # 4. Calibration
    print(f"\n4. CALIBRATION  (trailing {args.cal_days}d)")
    print("-" * 50)
    cal_lines, _ = check_calibration(conn, args.cal_days)
    for line in cal_lines:
        print(line)
    print("  [INFO] Reference for promotion decisions — does not affect exit code.")

    conn.close()

    print()
    print("=" * 70)
    if all_ok:
        print("OVERALL: HEALTHY")
        sys.exit(0)
    else:
        print("OVERALL: ISSUES FOUND — see [FAIL] items above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
