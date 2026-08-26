"""Envelope (max_env) sweep tradability report (issue #1069, `docs/REMEDIATION_PLAN.md`
"M3c", pre-registered by PR #1068 -- read that section in full before touching this
file; it is the fixed spec, this module is only its implementation).

**Does NOT reopen M3 or M3b.** M3's verdict (BSS = -0.4123) and M3b's verdict
(FAIL, concentrated -- issue #1063/#1064) both stand, final. M3b found the
model-certain class (rows where `conditional_bracket_probability`
(`src/model/envelope.py`) returns exactly 0.0 because a bracket sits entirely
outside `[current_high, max_env]`) has positive pooled EV but 93.8% of it sits
in a single `no_ask=99` bucket, right at its own 1.0% breakeven. `max_env`
comes from a static, partly-synthetic, deliberately-conservative p95 climb
table (`CLIMB_LOOKUP`, last regenerated 2026-07-12). This asks whether tuning
that ONE parameter -- the generator of the only class that beats the market's
price -- does better, without reopening either prior verdict.

**Amendment (Tech Lead PM, posted on issue #1069 before this report was
written): a cheap price-distribution diagnostic runs FIRST, with a legitimate
stop-early path, before the full EV machinery below.** #1063/#1064 closed FAIL
because the buckets that could carry a broad result were tiny (96: n=21, 97:
n=10, 98: n=63) while 93.8% of the class sat at `no_ask=99`. The
"breakeven-clearance in >=3 buckets" clause can only ever be satisfied if a
variant's newly-certain rows land at CHEAPER `no_ask` prices, not merely add
volume at 99 -- worth checking before a full build. This does NOT soften the
pre-registered FINDING/NULL rule; it only sequences the work. See
`run_diagnostic` / `bucket_thickening_check` / `STOP_EARLY_TEXT` below.

**Method -- reuses production code and existing report machinery, never
re-derives it** (the same constraint #1041/#1044/#1063/#1048 held themselves
to):

- Population loading / windowing / de-duplication / outcome resolution:
  ``load_bracket_eval_rows`` / ``filter_rows_since`` /
  ``dedupe_one_per_bracket_day`` / ``resolve_candidate_outcomes`` --
  imported from ``bss_market_vs_model_report`` unchanged. Same window
  convention (``--since``, filtered on POLL time).
- ``current_high`` / ``latest_temp`` reconstruction from ``observations``
  (issue #1044) and ``ReadOnlyDatabase`` / ``_station_local_now`` --
  imported from ``emos_shadow_reconstruction`` unchanged.
- EV math / bucketing / concentration reporting -- ``ev_per_contract``,
  ``bucket_no_ask``, ``group_ev_stats``, ``group_rows``, ``concentration_by``,
  ``NO_ASK_BUCKETS`` -- imported from ``model_certain_tradability_report``
  (issue #1063/#1064) unchanged, per this issue's explicit instruction to
  reuse that module's helpers directly rather than re-deriving them.
- ``wilson_interval`` -- imported from ``certainty_exclusion_check`` unchanged.
- The climb-table quantile primitive (``quantile()``) and
  ``MIN_DAYS_PER_CELL`` -- imported from ``scripts.build_climb_lookup``
  unchanged (the same p95 methodology that module uses for ``--from-db``,
  parameterized here to other quantile levels; see ``build_db_quantile_table``).

**Classification is geometric-only, not a Gaussian re-evaluation.** Reusing
`conditional_bracket_probability` verbatim would require reconstructing the
row's forecast mean/stddev too (a materially larger reconstruction this issue
does not ask for -- #1069 asks specifically about `max_env`, not mu/sigma).
Inspecting that function shows the exact-zero classification is fully
determined by geometry alone in every branch except one rare numerical edge
case (`surviving <= _MASS_EPS`, mass "collapsed" against a forecast that
puts ~0 probability in a wide surviving interval): the function returns 0.0
iff the clipped `[current_high, max_env]` interval does not overlap the
bracket. ``is_certain_zero`` below reproduces exactly that geometric
reduction and documents the one edge case it does not cover (see its
docstring) rather than silently approximating it.

**Hard constraints (unconditional, per the pre-registration and the issue):**
read-only against `data/meteoedge.db` (``ReadOnlyDatabase``, ``mode=ro`` +
write-denying authorizer); never writes `bracket_evals` or any live table;
never overwrites `src/data/climb_lookup.py` -- every regenerated table (V1,
V2, V4) is an in-memory dict built by this module, never written to that
path; no config changes; no live-trading changes of any kind -- #1053/#1054's
halt is unconditional and unaffected by this report's result either way.

Usage::

    python -m src.scripts.envelope_sweep_tradability_report --since 2026-08-06
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import STATIONS  # noqa: E402
from src.data.climb_lookup import CLIMB_LOOKUP  # noqa: E402
from src.model.climb_rates import expected_additional_rise  # noqa: E402
from src.config import get_canonical_station_feeds  # noqa: E402
from src.scripts.bss_market_vs_model_report import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_OUT_DIR,
    BRACKET_EVALS_JSONL,
    _connect_ro,
    dedupe_one_per_bracket_day,
    filter_rows_since,
    is_model_certain_price,
    load_bracket_eval_rows,
    resolve_candidate_outcomes,
)
from src.scripts.certainty_exclusion_check import wilson_interval  # noqa: E402
from src.scripts.emos_shadow_reconstruction import (  # noqa: E402
    ReadOnlyDatabase,
    _station_local_now,
    reconstruct_current_high,
    reconstruct_latest_temp,
)
from src.scripts.model_certain_tradability_report import (  # noqa: E402
    NO_ASK_BUCKETS,
    bucket_no_ask,
    concentration_by,
    ev_per_contract,
    group_ev_stats,
    group_rows,
)
from scripts.build_climb_lookup import MIN_DAYS_PER_CELL, quantile  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pre-registered window (docs/REMEDIATION_PLAN.md, "M3c", signed off 2026-08-26)
# ---------------------------------------------------------------------------

#: Held-out fit/explore half. V3 is fit exclusively from observations in this
#: window (strict train/test split); V1/V2/V4 fit no per-row parameter but are
#: still reported on both halves so the PASS/FAIL read can be restricted to
#: held-out data only.
FIT_START = "2026-08-06"
FIT_END = "2026-08-15"
#: Held-out evaluate half -- the ONLY half the stopping rule (and the
#: diagnostic's stop-early check) reads.
EVAL_START = "2026-08-16"
EVAL_END = "2026-08-25"

HALVES = ("fit", "evaluate")

#: Anomaly bucketing for V3 (°F departure from the station-hour normal).
ANOMALY_THRESHOLD_F = 2.0
#: V3 fits from only ~10 days of observations -- MIN_DAYS_PER_CELL (10,
#: imported from build_climb_lookup.py) would starve every (station, hour,
#: anomaly-bucket) cell by construction. This is a deliberately SMALLER,
#: V3-specific threshold, documented as a widening rather than reused
#: silently -- see AnomalyClimbModel.
MIN_DAYS_PER_CELL_V3 = 4

#: Reference `no_ask` value per NO_ASK_BUCKETS label, used both for the
#: "n needed to certify" diagnostic and (for the full EV path) breakeven
#: bucket checks. The worst case (most favourable to a NO) within each
#: bucket, per the buckets' own definitions in model_certain_tradability_report.
BUCKET_REFERENCE_NO_ASK = {"<=95": 95.0, "96": 96.0, "97": 97.0, "98": 98.0, "99": 99.0}

#: M3b's own reference figures (#1063/#1064, module docstring: "~4368-row
#: -scale class and ~1.0% observed YES"), used ONLY for the V0 control check
#: below -- never as a pass/fail gate on the sweep itself.
M3B_REFERENCE_N = 4368
M3B_REFERENCE_YES_RATE = 0.01
#: Tolerance band and minimum scale ratio for the control check to read "OK"
#: rather than "WARN" (see build_diagnostic_report's control-check section
#: for why a WARN here is an expected, documented deviation rather than a
#: stop condition).
M3B_YES_RATE_TOLERANCE = 0.02
M3B_MIN_SCALE_RATIO = 0.30

#: "Rule-of-three" style minimum resolved-n for a bucket to be certifiable at
#: all at its own breakeven -- n_needed = ceil(3 / breakeven_rate). Matches
#: the Tech Lead PM's amendment reference figures exactly: no_ask=98 (2.0%
#: breakeven) -> 150; no_ask=96 (4.0%) -> 75; no_ask=95 (5.0%) -> 60.
def n_needed_to_certify(bucket_label: str) -> int:
    ref_no_ask = BUCKET_REFERENCE_NO_ASK[bucket_label]
    breakeven = (100.0 - ref_no_ask) / 100.0
    import math
    return math.ceil(3.0 / breakeven)


# ---------------------------------------------------------------------------
# Row scope, window assignment (issue #1069)
# ---------------------------------------------------------------------------

def in_scope(row: dict) -> bool:
    """Same-day, high-direction rows only -- identical scope restriction
    ``emos_shadow_reconstruction.reconstruct_bracket_row`` and
    ``sigma_lever_reconstruction_report.reconstruct_bracket_row_variants``
    both use, for the same reason: ``reconstruct_current_high``/
    ``reconstruct_latest_temp`` and the station-local-day framing this
    module reuses from them only cover that population.
    """
    if row.get("is_next_day_flag"):
        try:
            if int(row["is_next_day_flag"]):
                return False
        except (TypeError, ValueError):
            pass
    return (row.get("direction") or "high") == "high"


def which_half(row: dict) -> "str | None":
    """'fit', 'evaluate', or None (outside the pre-registered window)."""
    d = (row.get("ts") or "")[:10]
    if FIT_START <= d <= FIT_END:
        return "fit"
    if EVAL_START <= d <= EVAL_END:
        return "evaluate"
    return None


# ---------------------------------------------------------------------------
# Geometric certainty classification (issue #1069)
# ---------------------------------------------------------------------------

def is_certain_zero(bracket_low: "float | None", bracket_high: "float | None",
                    current_high: "float | None", max_env: "float | None") -> bool:
    """Reproduce ``envelope.conditional_bracket_probability``'s exact-zero
    branches from geometry alone (no mean/stddev reconstruction -- see the
    module docstring for why that is in scope for this issue and how the one
    branch this does NOT cover is bounded).

    Mirrors the three branches of ``conditional_bracket_probability``:

    * ``max_env <= current_high`` -> every bracket not containing
      ``current_high`` is certain-zero (the day's high is already fixed).
    * otherwise, clip to ``[current_high, max_env]``; an empty clipped
      interval (``hi_eff <= lo_eff``) is certain-zero.
    * a non-empty clipped interval is NOT classified certain-zero here even
      though ``conditional_bracket_probability`` can still return 0.0 in the
      rare ``surviving <= _MASS_EPS`` numerical-collapse case (mass ~0 in a
      geometrically wide surviving interval) -- that branch needs the row's
      forecast mean/stddev, which this module deliberately does not
      reconstruct (out of scope for a `max_env`-only sweep). This makes
      every variant's certain-zero count a slight UNDER-count relative to
      the true `p_yes_raw == 0.0` population; documented, not corrected.

    Returns ``False`` (not certain, i.e. contested) for missing inputs --
    undiagnosable rows are excluded upstream, never guessed here.
    """
    if bracket_low is None or bracket_high is None or current_high is None or max_env is None:
        return False
    if max_env <= current_high:
        return not (bracket_low <= current_high < bracket_high)
    lo_eff = max(bracket_low, current_high)
    hi_eff = min(bracket_high, max_env)
    return hi_eff <= lo_eff


# ---------------------------------------------------------------------------
# Row reconstruction: current_high / latest_temp / station-local now (#1044)
# ---------------------------------------------------------------------------

def reconstruct_row_inputs(db, row: dict) -> "dict | None":
    """Return ``{'station', 'now_local', 'latest_temp', 'current_high',
    'bracket_low', 'bracket_high'}`` for one row, or ``None`` if any required
    input is unreconstructable. A row's own ``current_high``/``latest_temp``
    are used verbatim when present (``bracket_evals`` has never populated
    either -- issue #1044 -- so in practice this is always the
    observations-derived path).
    """
    station = row.get("station")
    end_date = (row.get("end_date") or "")[:10]
    poll_ts = row.get("ts", "")
    bracket_low = row.get("bracket_low")
    bracket_high = row.get("bracket_high")
    if not station or not end_date or bracket_low is None or bracket_high is None:
        return None

    current_high = row.get("current_high")
    if current_high is None:
        current_high = reconstruct_current_high(db, station, end_date, poll_ts)
    latest_temp = row.get("latest_temp")
    if latest_temp is None:
        latest_temp = reconstruct_latest_temp(db, station, poll_ts)
    if current_high is None or latest_temp is None:
        return None

    now_local = _station_local_now(station, poll_ts)
    if now_local is None:
        return None

    return {
        "station": station,
        "now_local": now_local,
        "latest_temp": float(latest_temp),
        "current_high": float(current_high),
        "bracket_low": float(bracket_low),
        "bracket_high": float(bracket_high),
    }


# ---------------------------------------------------------------------------
# Climb models: V0 (control) + V1/V2/V4 (DB-derived quantile tables) + V3
# (anomaly-conditioned, fit-window only) -- issue #1069
# ---------------------------------------------------------------------------

class ClimbModel:
    """Interface every variant implements: `additional_rise` gives the
    expected additional °F rise from *now_local* to end-of-day, used to
    compute ``max_env = latest_temp + additional_rise(...)``, exactly the
    same composition ``compute_envelope`` (``src/model/envelope.py``) uses
    live -- with the climb table swapped out.
    """
    name: str
    label: str

    def additional_rise(self, station: str, now_local: datetime, latest_temp: float) -> float:
        raise NotImplementedError


class V0Control(ClimbModel):
    name = "V0"
    label = "Current p95 CLIMB_LOOKUP -- CONTROL"

    def additional_rise(self, station, now_local, latest_temp):
        return expected_additional_rise(now_local, station=station)


def build_db_quantile_table(
    db, quantile_level: float, fallback_lookup: dict,
    ts_min: "str | None" = None, ts_max: "str | None" = None,
) -> "dict[str, dict[int, dict[int, float]]]":
    """Per-station, station-local (month, hour) climb-rate table at
    *quantile_level*, derived from `observations` -- the SAME methodology
    ``scripts.build_climb_lookup.compute_from_db`` uses for its p95
    ``--from-db`` table (station-local day grouping, ``daily_high -
    temp_at_hour``, ``MIN_DAYS_PER_CELL`` sparse-cell fallback, observations
    unioned across ``get_canonical_station_feeds``), reused verbatim except
    for two deliberate generalizations: an arbitrary *quantile_level*
    (V1=0.90, V2=0.85, V4=0.95) and an optional ``[ts_min, ts_max]``
    restriction (unused for V1/V2/V4, which use every available observation
    -- see the module docstring's "V1/V2/V4 fit no per-row parameter" note;
    used by ``AnomalyClimbModel`` for its own, stricter fit).

    Falls back to *fallback_lookup*'s own cell (the already-committed,
    partly-synthetic ``CLIMB_LOOKUP``) for any (station, month, hour) with
    fewer than ``MIN_DAYS_PER_CELL`` distinct local dates -- identical
    fallback rule ``compute_from_db`` applies. Scratch-only: returns an
    in-memory dict, never writes ``src/data/climb_lookup.py``.
    """
    table: "dict[str, dict[int, dict[int, float]]]" = {}
    for icao, _lat, _lon, _city, _res, _unit, tz_name in STATIONS:
        feed_keys = get_canonical_station_feeds(icao)
        obs: "list[dict]" = []
        for key in feed_keys:
            obs.extend(db.get_hourly_obs_for_climb(key))
        if ts_min is not None:
            obs = [o for o in obs if o["ts"] >= ts_min]
        if ts_max is not None:
            obs = [o for o in obs if o["ts"] <= ts_max]

        synthetic = fallback_lookup.get(icao, {})
        if not obs:
            table[icao] = synthetic
            continue

        tzinfo = ZoneInfo(tz_name)
        by_local_date: "dict[str, list[tuple[int, int, float]]]" = defaultdict(list)
        for row in obs:
            dt = datetime.fromisoformat(row["ts"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(tzinfo)
            by_local_date[local.date().isoformat()].append(
                (local.month, local.hour, row["temp_f"])
            )
        daily_highs = {d: max(t for _, _, t in entries) for d, entries in by_local_date.items()}

        cell_deltas: "dict[tuple[int, int], list[float]]" = defaultdict(list)
        cell_dates: "dict[tuple[int, int], set[str]]" = defaultdict(set)
        for d, entries in by_local_date.items():
            for month, hour, temp_f in entries:
                cell_deltas[(month, hour)].append(max(0.0, daily_highs[d] - temp_f))
                cell_dates[(month, hour)].add(d)

        updated: "dict[int, dict[int, float]]" = {}
        for month in range(1, 13):
            updated[month] = {}
            for hour in range(24):
                dates = cell_dates.get((month, hour), set())
                if len(dates) >= MIN_DAYS_PER_CELL:
                    updated[month][hour] = round(
                        quantile(cell_deltas[(month, hour)], quantile_level), 2)
                else:
                    updated[month][hour] = synthetic.get(month, {}).get(hour, 0.0)
        table[icao] = updated
    return table


class TableClimbModel(ClimbModel):
    """V1 (p90), V2 (p85), V4 (p95-from-observations) -- a precomputed
    per-station (month, hour) table, looked up by ``now_local``."""

    def __init__(self, name: str, label: str, table: "dict[str, dict[int, dict[int, float]]]"):
        self.name = name
        self.label = label
        self._table = table

    def additional_rise(self, station, now_local, latest_temp):
        month_table = self._table.get(station, {})
        hour_table = month_table.get(now_local.month, {})
        return hour_table.get(now_local.hour, 0.0)


def _anomaly_bucket(departure: float) -> str:
    if departure < -ANOMALY_THRESHOLD_F:
        return "below"
    if departure > ANOMALY_THRESHOLD_F:
        return "above"
    return "near"


class AnomalyClimbModel(ClimbModel):
    """V3 -- remaining rise conditioned on the current departure from the
    station-hour normal, fit STRICTLY from fit-window (``FIT_START``..
    ``FIT_END``) observations -- the mandatory train/test split (issue #1069:
    "V3 is fit from observations, so this is a strict train/test split for
    it"). Never reads evaluate-window observations at fit time.

    Fit, per station and local hour: ``normal`` = mean observed temp_f at
    that (station, hour) over the fit window; each fit-window observation's
    ``departure = temp_f - normal`` buckets it into 'below'/'near'/'above'
    (``ANOMALY_THRESHOLD_F``); within each (station, hour, bucket) cell, the
    p95 of ``daily_high - temp_f`` (station-local day, same convention as
    ``build_db_quantile_table``) is the fitted climb value.

    Sparse-cell fallback ladder (documented, not silent): a
    (station, hour, bucket) cell with fewer than ``MIN_DAYS_PER_CELL_V3``
    distinct fit-window dates falls back to the (station, hour) POOLED p95
    (all buckets combined); if that pooled cell is ALSO sub-threshold, falls
    back to V0's own static ``CLIMB_LOOKUP`` cell for (station, month=8,
    hour) -- August is the only month the fit window spans.
    """
    name = "V3"
    label = "Anomaly-conditioned climb (fit-window observations only)"

    def __init__(self, db):
        self._normal: "dict[tuple[str, int], float]" = {}
        self._bucketed: "dict[tuple[str, int, str], float]" = {}
        self._pooled: "dict[tuple[str, int], float]" = {}
        self._fit(db)

    def _fit(self, db) -> None:
        ts_min = f"{FIT_START}T00:00:00+00:00"
        ts_max = f"{FIT_END}T23:59:59+00:00"
        for icao, _lat, _lon, _city, _res, _unit, tz_name in STATIONS:
            feed_keys = get_canonical_station_feeds(icao)
            obs: "list[dict]" = []
            for key in feed_keys:
                obs.extend(db.get_hourly_obs_for_climb(key))
            obs = [o for o in obs if ts_min <= o["ts"] <= ts_max]
            if not obs:
                continue

            tzinfo = ZoneInfo(tz_name)
            by_local_date: "dict[str, list[tuple[int, float]]]" = defaultdict(list)
            for row in obs:
                dt = datetime.fromisoformat(row["ts"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone(tzinfo)
                by_local_date[local.date().isoformat()].append((local.hour, row["temp_f"]))
            daily_highs = {d: max(t for _, t in entries) for d, entries in by_local_date.items()}

            # Pass 1: per-(station, hour) normal = mean observed temp.
            hour_temps: "dict[int, list[float]]" = defaultdict(list)
            for d, entries in by_local_date.items():
                for hour, temp_f in entries:
                    hour_temps[hour].append(temp_f)
            for hour, temps in hour_temps.items():
                self._normal[(icao, hour)] = sum(temps) / len(temps)

            # Pass 2: bucket climb-to-eod by (hour, anomaly bucket) AND
            # pooled by hour alone, using the just-fit normal.
            bucket_deltas: "dict[tuple[int, str], list[float]]" = defaultdict(list)
            bucket_dates: "dict[tuple[int, str], set[str]]" = defaultdict(set)
            pooled_deltas: "dict[int, list[float]]" = defaultdict(list)
            pooled_dates: "dict[int, set[str]]" = defaultdict(set)
            for d, entries in by_local_date.items():
                for hour, temp_f in entries:
                    climb = max(0.0, daily_highs[d] - temp_f)
                    normal = self._normal[(icao, hour)]
                    bucket = _anomaly_bucket(temp_f - normal)
                    bucket_deltas[(hour, bucket)].append(climb)
                    bucket_dates[(hour, bucket)].add(d)
                    pooled_deltas[hour].append(climb)
                    pooled_dates[hour].add(d)

            for (hour, bucket), deltas in bucket_deltas.items():
                if len(bucket_dates[(hour, bucket)]) >= MIN_DAYS_PER_CELL_V3:
                    self._bucketed[(icao, hour, bucket)] = round(quantile(deltas, 0.95), 2)
            for hour, deltas in pooled_deltas.items():
                if len(pooled_dates[hour]) >= MIN_DAYS_PER_CELL_V3:
                    self._pooled[(icao, hour)] = round(quantile(deltas, 0.95), 2)

    def additional_rise(self, station, now_local, latest_temp):
        hour = now_local.hour
        normal = self._normal.get((station, hour))
        if normal is not None:
            bucket = _anomaly_bucket(latest_temp - normal)
            value = self._bucketed.get((station, hour, bucket))
            if value is not None:
                return value
        pooled = self._pooled.get((station, hour))
        if pooled is not None:
            return pooled
        return CLIMB_LOOKUP.get(station, {}).get(8, {}).get(hour, 0.0)


def build_variants(db) -> "list[ClimbModel]":
    """The five pre-registered variants, in order (issue #1069's table)."""
    v1_table = build_db_quantile_table(db, 0.90, CLIMB_LOOKUP)
    v2_table = build_db_quantile_table(db, 0.85, CLIMB_LOOKUP)
    v4_table = build_db_quantile_table(db, 0.95, CLIMB_LOOKUP)
    return [
        V0Control(),
        TableClimbModel("V1", "p90 climb (DB-derived, all available observations)", v1_table),
        TableClimbModel("V2", "p85 climb (DB-derived, all available observations)", v2_table),
        AnomalyClimbModel(db),
        TableClimbModel(
            "V4", "p95 regenerated from accumulated observations (scratch, "
                  "never written to src/data/climb_lookup.py)", v4_table),
    ]


# ---------------------------------------------------------------------------
# Reclassification: one row -> {variant_name: is_certain_zero} (issue #1069)
# ---------------------------------------------------------------------------

def reclassify_row(inputs: dict, variants: "list[ClimbModel]") -> "dict[str, bool]":
    """Recompute the certain-zero classification for one row's reconstructed
    inputs under every variant's own ``max_env``. Never reads the row's
    stored ``p_yes_raw`` -- issue #1069's explicit requirement ("Reclassify
    every row, do not filter the existing zeros")."""
    out: "dict[str, bool]" = {}
    for variant in variants:
        additional = variant.additional_rise(
            inputs["station"], inputs["now_local"], inputs["latest_temp"])
        max_env = max(inputs["current_high"], inputs["latest_temp"] + additional)
        out[variant.name] = is_certain_zero(
            inputs["bracket_low"], inputs["bracket_high"],
            inputs["current_high"], max_env,
        )
    return out


# ---------------------------------------------------------------------------
# Diagnostic (Tech Lead PM amendment, issue #1069) -- runs BEFORE the full
# EV machinery, with a legitimate stop-early path.
# ---------------------------------------------------------------------------

def no_ask_histogram(rows: "list[dict]") -> "dict[str, int]":
    """Counts of *rows* by `bucket_no_ask`, over ``NO_ASK_BUCKETS`` in order.
    Rows with no `no_ask` are dropped (undiagnosable, not guessed into a
    bucket)."""
    counts = {label: 0 for label in NO_ASK_BUCKETS}
    for row in rows:
        label = bucket_no_ask(row.get("no_ask"))
        if label is not None:
            counts[label] += 1
    return counts


def bucket_thickening_check(
    class_rows: "dict[str, list[dict]]",
) -> "dict[str, dict]":
    """Per variant, per sub-99 bucket: n after the variant is applied vs.
    ``n_needed_to_certify`` for that bucket. Returns
    ``{variant_name: {bucket_label: {'n': int, 'n_needed': int, 'thickened': bool}}}``.
    A bucket is "thickened" when its post-variant n crosses the threshold
    needed to certify at the 95% upper bound at all -- NOT when it actually
    clears breakeven (that is a separate, later question the full EV
    machinery answers; this only asks whether the sample could ever answer
    it).
    """
    out: "dict[str, dict]" = {}
    for variant_name, rows in class_rows.items():
        hist = no_ask_histogram(rows)
        out[variant_name] = {
            label: {
                "n": hist[label],
                "n_needed": n_needed_to_certify(label),
                "thickened": hist[label] >= n_needed_to_certify(label),
            }
            for label in ("<=95", "96", "97", "98")
        }
    return out


STOP_EARLY_TEXT = (
    "the envelope changes which brackets are clipped, but not the price "
    "level at which they trade, so the breadth clause remains unsatisfiable "
    "and the sweep cannot reach FINDING."
)


def decide_stop_early(thickening: "dict[str, dict[str, dict]]") -> bool:
    """True iff NO variant thickens ANY sub-99 bucket past its certifiable
    threshold on the EVALUATE half -- the amendment's stop-early condition.
    ``thickening`` covers only the evaluate half (the half that decides the
    verdict); the fit half is diagnostic context only, never part of this
    decision."""
    for buckets in thickening.values():
        for info in buckets.values():
            if info["thickened"]:
                return False
    return True


def _fmt_pct(v: "float | None") -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def build_diagnostic_report(
    run_date: str, since: "str | None", variants: "list[ClimbModel]",
    class_rows_by_half: "dict[str, dict[str, list[dict]]]",
    newly_certain_by_half: "dict[str, dict[str, list[dict]]]",
    thickening_by_half: "dict[str, dict[str, dict]]",
    stop_early: bool,
    funnel: dict,
) -> str:
    lines = []
    lines.append("# Envelope (max_env) Sweep Tradability Report (issue #1069, M3c)\n")
    lines.append(f"**Run date:** {run_date}  ")
    lines.append(
        "**DOES NOT REOPEN M3 OR M3b.** M3's verdict (BSS = -0.4123) and "
        "M3b's verdict (FAIL, concentrated -- #1063/#1064) both stand, "
        "final. See the module docstring "
        "(`src/scripts/envelope_sweep_tradability_report.py`) and "
        "`docs/REMEDIATION_PLAN.md`'s \"M3c\" section.  \n"
    )
    lines.append(
        "**No live trading regardless of result.** #1053/#1054's halt is "
        "unconditional and unaffected by this report either way.  \n"
    )
    lines.append(
        f"**Window:** rows polled on or after `{since}`, restricted to the "
        f"pre-registered held-out split -- fit `{FIT_START}..{FIT_END}`, "
        f"evaluate `{EVAL_START}..{EVAL_END}`.  \n"
    )
    lines.append("\n---\n")

    lines.append("## Amendment -- diagnostic-first sequencing\n")
    lines.append(
        "Per the Tech Lead PM's amendment on issue #1069: a cheap "
        "price-distribution diagnostic runs before the full EV machinery, "
        "with a legitimate stop-early path. This does NOT change the "
        "pre-registered FINDING/NULL stopping rule in "
        "`docs/REMEDIATION_PLAN.md` -- it only sequences the work so an "
        "unsatisfiable-by-construction sweep is caught early.\n"
    )

    lines.append("## Population funnel\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    for label, count in funnel.items():
        lines.append(f"| {label} | {count} |")
    lines.append("")

    lines.append("## V0 control check (must reproduce M3b before scoring other variants)\n")
    v0_pooled = (
        class_rows_by_half["fit"].get("V0", []) + class_rows_by_half["evaluate"].get("V0", [])
    )
    v0_stats = group_ev_stats(v0_pooled)
    n_ratio = (v0_stats["n_resolved"] / M3B_REFERENCE_N) if M3B_REFERENCE_N else 0.0
    rate_ok = (
        v0_stats["observed_yes"] is not None
        and abs(v0_stats["observed_yes"] - M3B_REFERENCE_YES_RATE) <= M3B_YES_RATE_TOLERANCE
    )
    scale_ok = n_ratio >= M3B_MIN_SCALE_RATIO
    control_verdict = "OK" if (rate_ok and scale_ok) else "WARN"
    lines.append(
        f"Reconstructed V0 class (pooled fit+evaluate, this module's own "
        f"geometric reclassification -- see module docstring): "
        f"n={v0_stats['n']}, n_resolved={v0_stats['n_resolved']}, "
        f"observed YES={_fmt_pct(v0_stats['observed_yes'])}. M3b reference "
        f"(#1063/#1064, full population, no direction restriction): "
        f"n~={M3B_REFERENCE_N}, observed YES~={_fmt_pct(M3B_REFERENCE_YES_RATE)}.\n"
    )
    lines.append(f"**Control check: {control_verdict}**\n")
    if control_verdict == "WARN":
        lines.append(
            "Expected, documented deviation, not a stop condition: this "
            "module's reconstruction covers only same-day, high-direction "
            "rows (`in_scope`) -- the same restriction "
            "`emos_shadow_reconstruction`/`sigma_lever_reconstruction_report` "
            "carry, because `reconstruct_current_high`/`reconstruct_latest_temp` "
            "only cover that population. M3b's own population also includes "
            "low-direction and next-day rows this module cannot classify, so "
            "a lower reconstructed n is expected; the observed-YES-rate "
            "order of magnitude is the more load-bearing part of this check.\n"
        )

    for variant in variants:
        lines.append(f"## Variant {variant.name} -- {variant.label}\n")
        for half in HALVES:
            class_rows = class_rows_by_half[half].get(variant.name, [])
            newly = newly_certain_by_half[half].get(variant.name, [])
            hist = no_ask_histogram(class_rows)
            newly_hist = no_ask_histogram(newly)
            total = sum(hist.values()) or 1
            lines.append(f"### {half.capitalize()} half\n")
            lines.append(
                "| no_ask bucket | class n | class share | newly-certain n | "
                "newly-certain share of class bucket |"
            )
            lines.append("|---|---|---|---|---|")
            for label in NO_ASK_BUCKETS:
                class_n = hist[label]
                newly_n = newly_hist[label]
                share_of_bucket = (newly_n / class_n) if class_n else None
                lines.append(
                    f"| {label} | {class_n} | {_fmt_pct(class_n / total)} | "
                    f"{newly_n} | {_fmt_pct(share_of_bucket)} |"
                )
            lines.append(f"| **total** | **{sum(hist.values())}** | -- | "
                         f"**{sum(newly_hist.values())}** | -- |\n")

            if variant.name != "V0":
                lines.append("**Certification adequacy (sub-99 buckets), this half:**\n")
                th = bucket_thickening_check({variant.name: class_rows})[variant.name]
                lines.append("| bucket | n | n needed to certify (rule-of-three) | thickened? |")
                lines.append("|---|---|---|---|")
                for label in ("<=95", "96", "97", "98"):
                    info = th[label]
                    verdict = "yes" if info["thickened"] else "**unpassable at this n**"
                    lines.append(
                        f"| {label} | {info['n']} | {info['n_needed']} | {verdict} |"
                    )
                lines.append(
                    "\n*\"Unpassable at this n\" means even zero adverse "
                    "events could not clear that bucket's breakeven at the "
                    "95% upper bound -- a sample-size finding, not evidence "
                    "against the edge.*\n"
                )

    lines.append("## Stop-early decision (evaluate half only)\n")
    if stop_early:
        lines.append(f"**STOP.** {STOP_EARLY_TEXT}\n")
        lines.append(
            "No variant lifted any sub-99 `no_ask` bucket's post-variant n "
            "past the sample size needed to certify at its own breakeven, "
            "on the evaluate half. Per the amendment, the full EV machinery "
            "is not built for this run -- this diagnostic is the complete "
            "deliverable.\n"
        )
    else:
        lines.append(
            "**CONTINUE.** At least one variant materially thickened a "
            "sub-99 `no_ask` bucket on the evaluate half -- proceeding to "
            "the full pre-registered EV analysis below.\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full EV machinery (pre-registered stopping rule) -- only reached when the
# diagnostic above does not stop early.
# ---------------------------------------------------------------------------

def variant_total_ev(rows: "list[dict]") -> "float | None":
    """Sum, across `no_ask` buckets, of each bucket's own
    `group_ev_stats`-derived `total_ev_point` -- the pre-registration's
    "TOTAL held-out EV", computed per-bucket (never one pooled rate across a
    population that spans real price dispersion by construction -- the same
    reasoning `model_certain_tradability_report`'s EV-by-bucket table is
    built on)."""
    by_bucket = group_rows(rows, lambda r: bucket_no_ask(r.get("no_ask")))
    total = 0.0
    any_value = False
    for members in by_bucket.values():
        stats = group_ev_stats(members)
        if stats["total_ev_point"] is not None:
            total += stats["total_ev_point"]
            any_value = True
    return total if any_value else None


def apply_envelope_stopping_rule(
    variant: ClimbModel, variant_class_rows: "list[dict]", v0_class_rows: "list[dict]",
    newly_certain_rows: "list[dict]",
) -> "tuple[str, str]":
    """Pre-registered FINDING/NULL rule (docs/REMEDIATION_PLAN.md "M3c"),
    applied mechanically on the EVALUATE half only. Returns (verdict, reasoning).
    """
    v0_total_ev = variant_total_ev(v0_class_rows)
    variant_total = variant_total_ev(variant_class_rows)
    ev_ok = (
        v0_total_ev is not None and variant_total is not None
        and v0_total_ev > 0 and variant_total >= 1.25 * v0_total_ev
    )

    by_bucket = group_rows(variant_class_rows, lambda r: bucket_no_ask(r.get("no_ask")))
    below_breakeven_buckets = []
    for label, members in by_bucket.items():
        stats = group_ev_stats(members)
        if stats["observed_yes"] is None:
            continue
        breakeven = (100.0 - BUCKET_REFERENCE_NO_ASK[label]) / 100.0
        if stats["observed_yes"] < breakeven:
            below_breakeven_buckets.append(label)
    buckets_ok = len(below_breakeven_buckets) >= 3

    v0_stats = group_ev_stats(v0_class_rows)
    newly_stats = group_ev_stats(newly_certain_rows)
    newly_ok = (
        newly_stats["observed_yes"] is not None and v0_stats["observed_yes"] is not None
        and newly_stats["observed_yes"] <= v0_stats["observed_yes"]
    )

    reasoning = (
        f"TOTAL held-out EV: V0={v0_total_ev if v0_total_ev is not None else 'n/a'}, "
        f"{variant.name}={variant_total if variant_total is not None else 'n/a'} "
        f"({'>= +25%' if ev_ok else '< +25%'} required). "
        f"Below-breakeven buckets: {below_breakeven_buckets} "
        f"({'>= 3' if buckets_ok else '< 3'} required). "
        f"Newly-certain observed YES {_fmt_pct(newly_stats['observed_yes'])} vs. "
        f"V0 class observed YES {_fmt_pct(v0_stats['observed_yes'])} "
        f"({'no worse' if newly_ok else 'WORSE'})."
    )
    if ev_ok and buckets_ok and newly_ok:
        return "FINDING", reasoning
    return "NULL", reasoning


def build_full_ev_report(
    variants: "list[ClimbModel]",
    class_rows_by_half: "dict[str, dict[str, list[dict]]]",
    newly_certain_by_half: "dict[str, dict[str, list[dict]]]",
) -> str:
    lines = ["\n---\n", "## Full EV analysis (evaluate half decides the verdict)\n"]
    eval_class = class_rows_by_half["evaluate"]
    eval_newly = newly_certain_by_half["evaluate"]
    v0_rows = eval_class.get("V0", [])

    for variant in variants:
        if variant.name == "V0":
            continue
        rows = eval_class.get(variant.name, [])
        newly = eval_newly.get(variant.name, [])
        verdict, reasoning = apply_envelope_stopping_rule(variant, rows, v0_rows, newly)
        conc = concentration_by(rows, group_rows(rows, lambda r: bucket_no_ask(r.get("no_ask"))))
        lines.append(f"### {variant.name} -- {variant.label}\n")
        lines.append(f"Class n (evaluate half): {len(rows)}; newly-certain n: {len(newly)}.\n")
        lines.append(f"Concentration: total priced EV {conc['total_ev']} over {conc['n_priced']} contracts.\n")
        lines.append(f"**Verdict: {verdict}**\n\n{reasoning}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_report(
    bracket_evals: "Path | None", db_path: Path, out_dir: Path,
    run_date: "str | None" = None, since: "str | None" = FIT_START,
    use_gamma: bool = True, allow_network: bool = True,
) -> int:
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    source_path = bracket_evals or BRACKET_EVALS_JSONL
    raw_rows = load_bracket_eval_rows(source_path)
    if not raw_rows:
        log.info("[m3c] no rows found under %s -- nothing to score.", source_path)
        return 0

    raw_rows, _ = filter_rows_since(raw_rows, since)
    raw_rows = [r for r in raw_rows if (r.get("ts") or "")[:10] <= EVAL_END]
    if not raw_rows:
        log.info("[m3c] no rows polled inside the pre-registered window -- nothing to score.")
        return 0

    deduped = dedupe_one_per_bracket_day(raw_rows)
    in_scope_rows = [r for r in deduped if in_scope(r)]
    windowed = [(r, which_half(r)) for r in in_scope_rows]
    windowed = [(r, half) for r, half in windowed if half is not None]
    if not windowed:
        log.info("[m3c] no in-scope, in-window rows -- nothing to score.")
        return 0

    if _connect_ro(db_path) is None:
        log.info("[m3c] no readable database at %s -- cannot reconstruct/resolve. "
                 "Not writing a report.", db_path)
        return 0

    ro_db = ReadOnlyDatabase(db_path)
    try:
        variants = build_variants(ro_db)

        reconstructed: "list[tuple[dict, str, dict]]" = []
        for row, half in windowed:
            inputs = reconstruct_row_inputs(ro_db, row)
            if inputs is None:
                continue
            classes = reclassify_row(inputs, variants)
            reconstructed.append((row, half, classes))
    finally:
        ro_db._conn.close()

    if not reconstructed:
        log.info("[m3c] no row could be reconstructed (current_high/latest_temp) -- "
                 "nothing to score.")
        return 0

    all_reconstructed_rows = [row for row, _, _ in reconstructed]
    samples, outcome_meta = resolve_candidate_outcomes(
        all_reconstructed_rows, db_path, use_gamma=use_gamma, allow_network=allow_network,
    )
    # resolve_candidate_outcomes returns NEW dicts (``{**r, "yes_won": ...}``)
    # for rows it could resolve, dropping unresolvable ones -- re-key on the
    # (station, ticker, end_date) identity used everywhere else in this
    # pipeline rather than object identity, which does not survive that copy.
    resolved_by_key = {
        (s.get("station"), s.get("ticker"), s.get("end_date")): s for s in samples
    }

    class_rows_by_half: "dict[str, dict[str, list[dict]]]" = {
        half: defaultdict(list) for half in HALVES
    }
    newly_certain_by_half: "dict[str, dict[str, list[dict]]]" = {
        half: defaultdict(list) for half in HALVES
    }
    for row, half, classes in reconstructed:
        key = (row.get("station"), row.get("ticker"), row.get("end_date"))
        resolved_row = resolved_by_key.get(key)
        if resolved_row is None:
            continue
        v0_certain = classes.get("V0", False)
        for variant in variants:
            if classes.get(variant.name, False):
                class_rows_by_half[half][variant.name].append(resolved_row)
                if variant.name != "V0" and not v0_certain:
                    newly_certain_by_half[half][variant.name].append(resolved_row)

    thickening_by_half = {
        half: bucket_thickening_check(class_rows_by_half[half])
        for half in HALVES
    }
    stop_early = decide_stop_early(thickening_by_half["evaluate"])

    reconstructed_keys = {
        (row.get("station"), row.get("ticker"), row.get("end_date"))
        for row, _, _ in reconstructed
    }
    n_resolved = len(reconstructed_keys & set(resolved_by_key))
    funnel = {
        "De-duplicated, in-window bracket-days": len(windowed),
        "Same-day/high-direction (in scope)": sum(1 for _, h in windowed if h),
        "Reconstructed (current_high/latest_temp/now_local)": len(reconstructed),
        "Resolved to an outcome": n_resolved,
        "Unresolvable (dropped, never guessed)": outcome_meta.get("n_unresolvable", 0),
        "Station-days (resolved)": outcome_meta.get("n_station_days", 0),
    }

    report = build_diagnostic_report(
        run_date, since, variants, class_rows_by_half,
        newly_certain_by_half, thickening_by_half, stop_early, funnel,
    )
    if not stop_early:
        report += build_full_ev_report(variants, class_rows_by_half, newly_certain_by_half)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"envelope_sweep_tradability_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[m3c] wrote %s (stop_early=%s)", out_path, stop_early)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--since", default=FIT_START, metavar="YYYY-MM-DD",
                    help="Score only rows POLLED on or after this UTC date "
                         "(default: the pre-registered fit-window start).")
    ap.add_argument("--no-gamma", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args(argv)
    return run_report(
        args.bracket_evals, args.db, args.out, args.run_date, args.since,
        use_gamma=not args.no_gamma, allow_network=not args.no_network,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
