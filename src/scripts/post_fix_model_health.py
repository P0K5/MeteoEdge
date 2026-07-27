"""Post-fix model health report -- did M0/M2 change the model's behaviour? (issue #869)

**This is a LEADING INDICATOR, not a skill test.** There are no outcomes in
it. It answers *"is the model still behaving pathologically?"*, never *"is the
model right?"* -- a pass here is necessary, not sufficient, for #822's M3
decision gate to be worth running.

Why it exists (``docs/REMEDIATION_PLAN.md``, M1/M2/M3): Pass 1 returned
``BSS = -0.2813`` on 300 station-days, and diagnosed the mechanism precisely --
**62.6% of the pre-fix model's output sat at a probability rail**, 33.4% at
``p_yes_raw <= 0.02`` (observed YES rate 24.4%) and 29.2% at ``>= 0.95``
(observed 81.4%), while the market's dominant bucket was calibrated to 0.6pp.

Sharpness is the only lever that could carry a model from -0.28 to positive,
and it is exactly what M2's sigma work (#799/#798/#824) was meant to fix. The
crucial property of the three checks below is that they score the **prediction
side only** -- so they run today, on data already accruing, weeks before any
outcome settles:

1. **Rail concentration** -- the headline. Still ~60% at the rails after the
   sigma work means M3 is a foregone conclusion and the spend should stop.
2. **``p_yes_raw == 0.0`` artifact rate** -- an M0 regression check. This exact
   certainty shortcut was 2265/12698 = **17.8%** of archived rows; #820 should
   have collapsed it toward zero. If it has not, M0 is incomplete and every
   downstream number inherits the problem.
3. **Sigma identifiability** -- an M2 regression check. The M2 thesis was that
   ``sigma_raw`` being the constant ``FORECAST_STDDEV_F = 2.0`` made the EMOS
   ``c``/``d`` coefficients unfittable (``d ~ 0.001`` across all 30 cities).
   Coefficients still pinned near zero mean #799 did not achieve what it was
   for, whatever the code now does.

Data sources (host-local; check 3 is the only one that touches the DB):

  * ``logs/bracket_evals.*.jsonl`` -- #826's full evaluated-bracket log, which
    began 2026-07-24 and is therefore entirely post-#820. Carries
    ``p_yes_raw``, ``emos_mode``, ``is_next_day``, ``poll_ts``,
    ``settlement_date``.
  * ``data/meteoedge.db`` :: ``emos_calibration`` (the fitted ``a``/``b``/``c``/
    ``d`` per city, mode, forecast_source, sigma_source) and
    ``model_forecast_log`` (``sigma_f`` coverage by model).

Self-gating, matching ``bss_market_vs_model_report`` and
``resolve_bracket_outcomes``: with no local ``logs/`` and no database (both
gitignored, host-only) this logs an honest line and writes nothing rather than
fabricating a health report. Read-only against the database -- opened
``mode=ro``, never ``Database()``, whose ``__init__`` runs migrations and an
unconditional ``DELETE FROM deb_weight_log`` purge.

Usage::

    python -m src.scripts.post_fix_model_health
    python -m src.scripts.post_fix_model_health --bracket-evals logs/bracket_evals.jsonl \\
        --db data/meteoedge.db --out backtest_results

    # restrict to rows at or after the date a fix landed:
    python -m src.scripts.post_fix_model_health --since 2026-07-25
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pytz
from dateutil import parser as dtparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import BRACKET_EVALS_JSONL, STATION_TZ  # noqa: E402
from src.scripts.calibration_report import BUCKET_EDGES  # noqa: E402
from src.utils.log_rotation import iter_rotated_jsonl  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))
DEFAULT_OUT_DIR = Path("backtest_results")

# Pre-fix baselines, measured on the 2026-07-26 Pass-1 run
# (backtest_results/bss_market_vs_model_pass1_2026-07-26.md). Every table below
# states the post-fix number against these, because "29% at the top rail" only
# means something next to the 29.2% it is supposed to have improved on.
BASELINE_LOW_RAIL_SHARE = 0.334      # p_yes_raw in [0.00, 0.02)
BASELINE_HIGH_RAIL_SHARE = 0.292     # p_yes_raw in [0.95, 1.00]
BASELINE_RAIL_SHARE = 0.626          # combined
BASELINE_ZERO_ARTIFACT_RATE = 0.178  # 2265 / 12698 archived rows
BASELINE_D_COEFFICIENT = 0.001       # d ~ 0.001 across all 30 cities (#799)

# A fitted d at or below this is indistinguishable from the unidentifiable
# regime #799 set out to escape -- an order of magnitude above the 0.001
# baseline, so a coefficient that merely wobbled does not count as movement.
D_IDENTIFIABLE_THRESHOLD = 0.01

LOW_RAIL_MAX = 0.02
HIGH_RAIL_MIN = 0.95


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_bracket_eval_rows(base: Path = BRACKET_EVALS_JSONL,
                           since: "str | None" = None) -> "list[dict]":
    """Load every ``bracket_evals`` row, optionally from *since* (YYYY-MM-DD).

    Filters on ``settlement_date`` rather than ``poll_ts`` because a fix's
    effect belongs to the market day being priced, not the moment the poll
    happened to fire.
    """
    rows = list(iter_rotated_jsonl(base))
    if not since:
        return rows
    return [r for r in rows if str(r.get("settlement_date") or "")[:10] >= since]


def _connect_ro(db_path: "Path | None") -> "sqlite3.Connection | None":
    """Open *db_path* read-only. Returns None if missing/unopenable.

    Mirrors ``resolve_bracket_outcomes._connect_ro`` -- never creates the file,
    never migrates, never writes.
    """
    if db_path is None:
        return None
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError as exc:
        log.warning("[health] could not open %s read-only: %s", db_path, exc)
        return None


def _f(value) -> "float | None":
    if value is None or value == "" or value == "None":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Check 1: rail concentration
# ---------------------------------------------------------------------------

def rail_concentration(rows: "list[dict]", edges: "list[float]" = BUCKET_EDGES) -> dict:
    """Sharpness histogram of ``p_yes_raw`` plus the rail shares (issue #869).

    The rails are where the pre-fix model lived: 62.6% of its mass sat at
    ``<= 0.02`` or ``>= 0.95``, and it was wrong 24.4% / 18.6% of the time
    there. This is the single number that says whether M2's sigma work changed
    the model's character.

    Buckets use ``calibration_report.BUCKET_EDGES`` so this histogram is
    directly comparable to the Pass-1 report's sharpness table.
    """
    probs = [p for p in (_f(r.get("p_yes_raw")) for r in rows) if p is not None]
    n = len(probs)
    histogram = []
    for lo, hi in zip(edges, edges[1:]):
        count = sum(1 for p in probs if lo <= p < hi)
        histogram.append({
            "bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "n": count,
            "share": count / n if n else None,
        })
    # The top bucket is half-open in the loop above; 1.0 itself belongs in it.
    if n and edges[-1] <= 1.0:
        exact_top = sum(1 for p in probs if p >= edges[-1])
        histogram[-1]["n"] = exact_top
        histogram[-1]["share"] = exact_top / n

    n_low = sum(1 for p in probs if p <= LOW_RAIL_MAX)
    n_high = sum(1 for p in probs if p >= HIGH_RAIL_MIN)
    return {
        "n": n,
        "histogram": histogram,
        "n_low_rail": n_low,
        "n_high_rail": n_high,
        "low_rail_share": n_low / n if n else None,
        "high_rail_share": n_high / n if n else None,
        "rail_share": (n_low + n_high) / n if n else None,
    }


def rail_concentration_by_mode(rows: "list[dict]") -> "dict[str, dict]":
    """Rail concentration split by ``emos_mode``.

    A promoted-EMOS population must be readable separately from the legacy
    envelope: they are different models, and averaging them hides whichever one
    moved.
    """
    by_mode: "dict[str, list[dict]]" = defaultdict(list)
    for row in rows:
        by_mode[str(row.get("emos_mode") or "unknown")].append(row)
    return {mode: rail_concentration(rs) for mode, rs in sorted(by_mode.items())}


# ---------------------------------------------------------------------------
# Check 2: p_yes_raw == 0.0 artifact rate (M0 regression check)
# ---------------------------------------------------------------------------

def _station_local_hour(row: dict) -> "int | None":
    """Hour-of-day of ``poll_ts`` in the station's local timezone.

    #820's diagnosis was specifically the evening window (the 19:00-CDT UTC
    rollover), so the artifact rate is only interpretable against local time.
    """
    tz_name = STATION_TZ.get(str(row.get("station") or ""))
    ts_str = row.get("poll_ts")
    if not tz_name or not ts_str:
        return None
    try:
        tz = pytz.timezone(tz_name)
        t = dtparse.parse(str(ts_str))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(tz).hour
    except (ValueError, OverflowError, TypeError, pytz.UnknownTimeZoneError):
        return None


def zero_artifact_rate(rows: "list[dict]") -> dict:
    """Rate of exact ``p_yes_raw == 0.0`` -- #820's certainty shortcut (#869).

    An exact zero is not a forecast. It is the envelope model reporting
    impossibility after ``expected_additional_rise`` returned 0 past the
    diurnal peak and ``max_env`` collapsed onto a finished day's high. Every
    live entry in the pre-fix regime fired on one.

    Broken out by ``is_next_day`` and by station-local hour because that is
    where #820 said the artifact lives -- a residual rate concentrated in the
    evening window means the fix is partial rather than absent.
    """
    n_total = 0
    n_zero = 0
    by_next_day: "dict[str, dict]" = defaultdict(lambda: {"n": 0, "n_zero": 0})
    by_hour: "dict[int, dict]" = defaultdict(lambda: {"n": 0, "n_zero": 0})

    for row in rows:
        p = _f(row.get("p_yes_raw"))
        if p is None:
            continue
        n_total += 1
        is_zero = (p == 0.0)
        n_zero += int(is_zero)

        nd_key = {1: "next_day", 0: "same_day"}.get(row.get("is_next_day"), "unknown")
        by_next_day[nd_key]["n"] += 1
        by_next_day[nd_key]["n_zero"] += int(is_zero)

        hour = _station_local_hour(row)
        if hour is not None:
            by_hour[hour]["n"] += 1
            by_hour[hour]["n_zero"] += int(is_zero)

    def _rate(d):
        return {**d, "rate": d["n_zero"] / d["n"] if d["n"] else None}

    return {
        "n": n_total,
        "n_zero": n_zero,
        "rate": n_zero / n_total if n_total else None,
        "by_next_day": {k: _rate(v) for k, v in sorted(by_next_day.items())},
        "by_local_hour": {k: _rate(v) for k, v in sorted(by_hour.items())},
    }


# ---------------------------------------------------------------------------
# Check 3: sigma identifiability (M2 regression check)
# ---------------------------------------------------------------------------

def emos_d_coefficients(db_path: "Path | None") -> dict:
    """Fitted EMOS ``d`` coefficients, grouped by ``sigma_source`` (issue #869).

    #799's whole thesis: with ``sigma_raw`` a constant, ``d`` multiplies a
    predictor that never varies, so no amount of data can identify it -- and it
    came out at ``d ~ 0.001`` across all 30 cities. If ensemble sigma is
    genuinely flowing through training, ``d`` must now move. If it has not,
    #799 changed the plumbing without changing the fit.

    Grouped by ``sigma_source`` because #848 coupled train and serve under
    ``USE_ENSEMBLE_SIGMA``: rows fitted against the old constant sigma and rows
    fitted against ensemble spread are different populations and must not be
    averaged together.
    """
    con = _connect_ro(db_path)
    if con is None:
        return {"available": False, "by_sigma_source": {}}
    try:
        cur = con.execute(
            "SELECT city, model_mode, forecast_source, sigma_source, lead_hours, "
            "c, d, crps_score, trained_at FROM emos_calibration"
        )
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        log.warning("[health] emos_calibration read failed: %s", exc)
        return {"available": False, "by_sigma_source": {}}
    finally:
        con.close()

    by_source: "dict[str, list[dict]]" = defaultdict(list)
    for r in rows:
        by_source[str(r.get("sigma_source") or "unknown")].append(r)

    out: "dict[str, dict]" = {}
    for source, rs in sorted(by_source.items()):
        ds = sorted(d for d in (_f(r.get("d")) for r in rs) if d is not None)
        n_identifiable = sum(1 for d in ds if abs(d) > D_IDENTIFIABLE_THRESHOLD)
        median = None
        if ds:
            mid = len(ds) // 2
            median = ds[mid] if len(ds) % 2 else (ds[mid - 1] + ds[mid]) / 2
        out[source] = {
            "n_rows": len(rs),
            "n_with_d": len(ds),
            "min": ds[0] if ds else None,
            "median": median,
            "max": ds[-1] if ds else None,
            "n_identifiable": n_identifiable,
            "identifiable_share": n_identifiable / len(ds) if ds else None,
        }
    return {"available": True, "by_sigma_source": out}


def sigma_f_coverage(db_path: "Path | None") -> dict:
    """Non-null ``sigma_f`` coverage in ``model_forecast_log``, by model (#869).

    The predictor #824 captured. Coverage is the precondition for check 3
    above: ``d`` cannot become identifiable on rows that carry no spread.
    """
    con = _connect_ro(db_path)
    if con is None:
        return {"available": False, "by_model": {}}
    try:
        cur = con.execute(
            "SELECT model, COUNT(*) AS n, "
            "SUM(CASE WHEN sigma_f IS NOT NULL THEN 1 ELSE 0 END) AS n_sigma "
            "FROM model_forecast_log GROUP BY model ORDER BY model"
        )
        by_model = {}
        for r in cur.fetchall():
            n = r["n"] or 0
            n_sigma = r["n_sigma"] or 0
            by_model[str(r["model"])] = {
                "n": n, "n_sigma": n_sigma,
                "coverage": n_sigma / n if n else None,
            }
        return {"available": True, "by_model": by_model}
    except sqlite3.OperationalError as exc:
        log.warning("[health] model_forecast_log read failed: %s", exc)
        return {"available": False, "by_model": {}}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _pct(value: "float | None") -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def _delta(value: "float | None", baseline: float) -> str:
    """Signed change against a pre-fix baseline, in percentage points."""
    if value is None:
        return "n/a"
    delta_pp = (value - baseline) * 100
    return f"{delta_pp:+.1f}pp"


def build_report(rails: dict, rails_by_mode: dict, zeros: dict, d_coeffs: dict,
                 sigma_cov: dict, run_date: str, since: "str | None") -> str:
    lines = []
    lines.append("# Post-Fix Model Health -- leading indicator (issue #869)\n")
    lines.append(f"**Run date:** {run_date}  ")
    lines.append("**Data source:** `logs/bracket_evals.*.jsonl` (#826, post-#820) + "
                 "`emos_calibration` / `model_forecast_log` (meteoedge.db)  ")
    if since:
        lines.append(f"**Filtered:** settlement_date >= {since}  ")
    lines.append("")
    lines.append(
        "> **NOT A SKILL TEST.** There are no outcomes in this report. It answers *\"is the "
        "> model still behaving pathologically?\"*, never *\"is the model right?\"* -- a pass "
        "> here is necessary, not sufficient, for #822's M3 gate to be worth running. All "
        "> baselines are the pre-fix model measured on the 2026-07-26 Pass-1 run.\n"
    )
    lines.append("\n---\n")

    # --- Check 1 -----------------------------------------------------------
    lines.append("## 1. Rail concentration -- the headline\n")
    lines.append(
        "Sharpness is the only lever that could carry a model from BSS -0.28 to positive. "
        "The pre-fix model put **62.6%** of its output at a probability rail and was wrong "
        "24.4% / 18.6% of the time there.\n"
    )
    lines.append("| Metric | Post-fix | Pre-fix baseline | Change |")
    lines.append("|---|---|---|---|")
    lines.append(f"| n (rows with `p_yes_raw`) | {rails['n']} | 12230 | — |")
    lines.append(f"| Low rail (`<= {LOW_RAIL_MAX}`) | {_pct(rails['low_rail_share'])} | "
                 f"{_pct(BASELINE_LOW_RAIL_SHARE)} | {_delta(rails['low_rail_share'], BASELINE_LOW_RAIL_SHARE)} |")
    lines.append(f"| High rail (`>= {HIGH_RAIL_MIN}`) | {_pct(rails['high_rail_share'])} | "
                 f"{_pct(BASELINE_HIGH_RAIL_SHARE)} | {_delta(rails['high_rail_share'], BASELINE_HIGH_RAIL_SHARE)} |")
    lines.append(f"| **Combined at a rail** | **{_pct(rails['rail_share'])}** | "
                 f"**{_pct(BASELINE_RAIL_SHARE)}** | "
                 f"**{_delta(rails['rail_share'], BASELINE_RAIL_SHARE)}** |")
    lines.append("")
    lines.append("**Reading:** a combined rail share still near 60% means the sigma work did "
                 "not change the model's character, and M3 is a foregone conclusion. A "
                 "material drop is the first real evidence M2 bit.\n")

    lines.append("### Sharpness histogram\n")
    lines.append("| Bucket | n | Share |")
    lines.append("|---|---|---|")
    for b in rails["histogram"]:
        lines.append(f"| {b['bucket']} | {b['n']} | {_pct(b['share'])} |")
    lines.append("")

    if len(rails_by_mode) > 1:
        lines.append("### By `emos_mode`\n")
        lines.append("| Mode | n | Low rail | High rail | Combined |")
        lines.append("|---|---|---|---|---|")
        for mode, stats in rails_by_mode.items():
            lines.append(
                f"| {mode} | {stats['n']} | {_pct(stats['low_rail_share'])} | "
                f"{_pct(stats['high_rail_share'])} | {_pct(stats['rail_share'])} |"
            )
        lines.append("")

    # --- Check 2 -----------------------------------------------------------
    lines.append("## 2. `p_yes_raw == 0.0` artifact rate -- M0 regression check\n")
    lines.append(
        "An exact zero is not a forecast: it is the envelope model reporting impossibility "
        "after `expected_additional_rise` returned 0 past the diurnal peak. Every live entry "
        "in the pre-fix regime fired on one. #820 should have collapsed this toward zero.\n"
    )
    lines.append("| Metric | Post-fix | Pre-fix baseline | Change |")
    lines.append("|---|---|---|---|")
    lines.append(f"| n | {zeros['n']} | 12698 | — |")
    lines.append(f"| Exact `p_yes_raw == 0.0` | {zeros['n_zero']} | 2265 | — |")
    lines.append(f"| **Rate** | **{_pct(zeros['rate'])}** | **{_pct(BASELINE_ZERO_ARTIFACT_RATE)}** | "
                 f"**{_delta(zeros['rate'], BASELINE_ZERO_ARTIFACT_RATE)}** |")
    lines.append("")
    lines.append("| Segment | n | Zeros | Rate |")
    lines.append("|---|---|---|---|")
    for seg, v in zeros["by_next_day"].items():
        lines.append(f"| {seg} | {v['n']} | {v['n_zero']} | {_pct(v['rate'])} |")
    lines.append("")
    residual_hours = {h: v for h, v in zeros["by_local_hour"].items() if v["n_zero"]}
    if residual_hours:
        lines.append("Residual zeros by station-local hour -- #820's diagnosis was the evening "
                     "window specifically, so a residue clustered there means the fix is "
                     "partial rather than absent:\n")
        lines.append("| Local hour | n | Zeros | Rate |")
        lines.append("|---|---|---|---|")
        for hour, v in sorted(residual_hours.items()):
            lines.append(f"| {hour:02d}:00 | {v['n']} | {v['n_zero']} | {_pct(v['rate'])} |")
        lines.append("")
    else:
        lines.append("**No residual exact zeros at any local hour.**\n")

    # --- Check 3 -----------------------------------------------------------
    lines.append("## 3. Sigma identifiability -- M2 regression check\n")
    lines.append(
        f"#799: with `sigma_raw` a constant, `d` multiplies a predictor that never varies, "
        f"so it cannot be identified -- it came out at `d ~ {BASELINE_D_COEFFICIENT}` across "
        f"all 30 cities. If ensemble sigma is genuinely flowing through training, `d` must "
        f"move. A fitted `|d| > {D_IDENTIFIABLE_THRESHOLD}` counts as identifiable (an order "
        f"of magnitude above the baseline, so a coefficient that merely wobbled does not "
        f"count).\n"
    )
    if not d_coeffs["available"]:
        lines.append("`emos_calibration` unavailable -- cannot assess.\n")
    elif not d_coeffs["by_sigma_source"]:
        lines.append("`emos_calibration` is empty -- no coefficients fitted yet.\n")
    else:
        lines.append("| sigma_source | Rows | min `d` | median `d` | max `d` | Identifiable |")
        lines.append("|---|---|---|---|---|---|")
        for source, v in d_coeffs["by_sigma_source"].items():
            def _num(x):
                return f"{x:.5f}" if x is not None else "n/a"
            lines.append(
                f"| {source} | {v['n_rows']} | {_num(v['min'])} | {_num(v['median'])} | "
                f"{_num(v['max'])} | {v['n_identifiable']}/{v['n_with_d']} "
                f"({_pct(v['identifiable_share'])}) |"
            )
        lines.append("")
        lines.append("**Reading:** rows fitted against the old constant sigma and rows fitted "
                     "against ensemble spread are different populations (#848 coupled train "
                     "and serve under `USE_ENSEMBLE_SIGMA`) -- compare the ensemble "
                     "`sigma_source` row against the constant one, not against the total.\n")

    lines.append("### `sigma_f` coverage in `model_forecast_log`\n")
    lines.append("The predictor #824 captured. `d` cannot become identifiable on rows that "
                 "carry no spread, so coverage is the precondition for the table above.\n")
    if not sigma_cov["available"]:
        lines.append("`model_forecast_log` unavailable -- cannot assess.\n")
    else:
        lines.append("| Model | Rows | With `sigma_f` | Coverage |")
        lines.append("|---|---|---|---|")
        for model, v in sigma_cov["by_model"].items():
            lines.append(f"| {model} | {v['n']} | {v['n_sigma']} | {_pct(v['coverage'])} |")
        lines.append("")

    lines.append("---\n")
    lines.append("## What a pass and a fail look like\n")
    lines.append("| | Pass | Fail |")
    lines.append("|---|---|---|")
    lines.append("| Rail concentration | Materially below 62.6% | Still ~60% → M3 is decided; "
                 "stop spending on it |")
    lines.append("| Zero artifact rate | Near 0% | Still material → #820 incomplete, every "
                 "downstream number inherits it |")
    lines.append("| `d` identifiability | `d` moved off ~0.001 on ensemble rows | Still pinned "
                 "→ #799 changed plumbing, not the fit |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_report(bracket_evals_base: Path, db_path: Path, out_dir: Path,
               run_date: "str | None" = None, since: "str | None" = None) -> int:
    """Load, measure, and (if there is real data) write the report.

    Self-gates when no ``bracket_evals`` rows exist -- the common fresh-checkout
    or dev-sandbox case, since ``logs/`` is gitignored and lives on the bot
    host. Returns 0 either way so this is safe to schedule any day.

    A missing database is NOT fatal here: checks 1 and 2 read only the JSONL
    log, so the report still carries its headline finding and simply records
    check 3 as unavailable.
    """
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    rows = load_bracket_eval_rows(bracket_evals_base, since=since)
    if not rows:
        log.info(
            "[health] no rows found under %s (rotated sources) -- nothing to measure. "
            "This is expected in a fresh checkout / dev sandbox; logs/ is gitignored "
            "and lives on the bot host. Not writing a report.",
            bracket_evals_base,
        )
        return 0

    rails = rail_concentration(rows)
    if not rails["n"]:
        log.info("[health] no row carries a usable p_yes_raw -- not writing a report.")
        return 0

    rails_by_mode = rail_concentration_by_mode(rows)
    zeros = zero_artifact_rate(rows)
    d_coeffs = emos_d_coefficients(db_path)
    sigma_cov = sigma_f_coverage(db_path)

    log.info(
        "[health] %d rows | rail share %s (baseline %s) | zero-artifact rate %s (baseline %s)",
        rails["n"], _pct(rails["rail_share"]), _pct(BASELINE_RAIL_SHARE),
        _pct(zeros["rate"]), _pct(BASELINE_ZERO_ARTIFACT_RATE),
    )

    report = build_report(rails, rails_by_mode, zeros, d_coeffs, sigma_cov, run_date, since)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"post_fix_model_health_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[health] wrote %s", out_path)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=BRACKET_EVALS_JSONL)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None,
                    help="Report date stamp (default: today, UTC)")
    ap.add_argument("--since", default=None,
                    help="Only rows with settlement_date >= this (YYYY-MM-DD), e.g. the "
                         "date a fix landed")
    args = ap.parse_args(argv)
    return run_report(args.bracket_evals, args.db, args.out, args.run_date, args.since)


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
