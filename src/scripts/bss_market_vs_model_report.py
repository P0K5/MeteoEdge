"""Market-vs-model Brier Skill Score report — Pass 1 and Pass 2 (issue #822).

Both passes run through this one module; ``--population`` selects which.
Everything about HOW a bracket is scored is shared verbatim -- the BSS math,
the row exclusions, the de-duplication rule, outcome resolution, the
market-price convention, reliability and sharpness. Only WHICH brackets are in
scope differs, plus the framing around the number. That is deliberate: the
decision gate must not be able to drift from the read that preceded it.

**Pass 1 -- ``--population gate-selected`` (default).** An early, retrospective,
directional read on the archived ``logs/candidates.*.csv.gz``, which existed
already so no waiting was required. Explicitly **not** the decision-gate
verdict. Ran 2026-07-26: BSS = -0.2813 over 404 brackets / 300 station-days.

**Pass 2 -- ``--population all-bracket``. THIS IS THE M3 DECISION GATE.** Scores
#826's ``logs/bracket_evals.*.jsonl`` -- every bracket the scanner evaluated,
which is the population ``docs/REMEDIATION_PLAN.md``'s decision rule specifies.
The rule and its ``n >= 300`` station-day power requirement are encoded in
``DECISION_RULE`` / ``M3_MIN_STATION_DAYS`` and were fixed **before** any Pass-2
number existed, so the verdict cannot be rationalised after the fact.

**The two passes are not directly comparable.** Pass 1 answered "on the brackets
we chose to trade, were we better than the market?"; Pass 2 answers the general
calibration question over every evaluated bracket. A different number is
expected from population alone.

Two reasons Pass 1 cannot be read as a settling verdict, both structural, not
implementation details:

1. **Pre-fix model.** Every row here was produced by the model *before* the
   #810 timezone fix and the (still-open) #820 evening-window fix. A
   negative BSS condemns the OLD model; a positive one does not clear the
   fixed one, and either way the number will move once #820 lands.
2. **Gate-selected sample.** ``logs/candidates.csv`` only contains brackets
   that already cleared the live entry gates (``MIN_EDGE_CENTS`` /
   ``MIN_PRICE_CENTS`` / the near-certainty envelope check). This answers
   "on the brackets we chose to trade, were we better than the market?" --
   operationally relevant, but not a general calibration measure, and not
   the ``n >= 300`` / all-bracket population the M3 decision gate requires.

Any report this script writes must keep both caveats in its own header --
see ``REQUIRED_DISCLAIMER`` below -- so a reader skimming only the top of the
file cannot mistake this for the M3 verdict.

Methodology (docs/REMEDIATION_PLAN.md, "The decision gate (#822)")::

    BS_model  = mean( (p_yes_raw   - outcome)^2 )
    BS_market = mean( (p_market_yes - outcome)^2 )
    BSS       = 1 - (BS_model / BS_market)

Data sources:

  * ``logs/candidates.*.csv.gz`` (date-rotated + gzip-compressed, see
    ``src/utils/log_rotation.py``) -- one row per *poll* for every candidate
    that already cleared the live entry gates. Carries ``p_yes_raw``,
    ``yes_ask``, ``no_ask``, ``end_date``, ``minutes_to_settlement``.
  * Outcome truth -- see "Outcome resolution" below.

**Outcome resolution (issue #865).** Pass 1 originally joined outcomes from
``data/meteoedge.db`` :: ``settlements``, and that join is what destroyed the
sample: ``settlements`` is only written for brackets MeteoEdge actually
**traded** (~156 rows all-time), so the 2026-07-25 production run dropped 360
of 380 de-duplicated brackets for "no definitive settlement match" and scored
BSS on **n=20** -- 6.7% of the ``n >= 300`` the decision rule requires. That
exclusion was never methodologically motivated: whether a bracket resolved
YES or NO is a fact about the weather and about Polymarket's on-chain
resolution, not about whether we traded it.

So outcomes now come from ``src/scripts/resolve_bracket_outcomes`` (issues
#850/#858/#860/#863), the same resolution capability #822's Pass 2 uses:
Polymarket's definitive resolution where available, falling back to the
station-local observed daily high from ``observations`` -- the precedence
``settle.py`` itself uses, so this repo has one resolution policy rather than
two. ``--outcome-source settlements`` reverts to the original join and
reproduces the 2026-07-25 report exactly.

This changes only *which brackets can be scored*, never how a scored bracket
is scored: the BSS math, the row exclusions, the de-duplication rule and the
market-price convention are all untouched.

Required row-level exclusions (issue #822, issue #820):

  * ``p_yes_raw`` missing (legacy pre-#564 rows carry no raw probability).
  * ``p_yes_raw == 0.0`` -- the certainty-shortcut artifact diagnosed in
    #820 (evening window collapses ``max_env`` onto the finished day's high
    and the envelope model emits an exact 0.0 that reflects the bug, not a
    forecast).
  * ``yes_ask``/``no_ask`` at the 1c/99c rail -- the exchange's price floor
    and ceiling. A rail price is not really "the market's implied
    probability" (it is clipped), so keeping these rows would flatter or
    penalize BS_market on a value the market itself did not freely produce.
  * No outcome resolvable -- Polymarket has not resolved the market decisively
    AND no observation exists for that station-day. Never guessed.

De-duplication: one row per (station, ticker, settlement date) -- a bracket
is polled repeatedly before its market closes, and the entry gates already
select a specific poll's numbers as "the" candidate, so the sample must not
over-count. This mirrors ``src.scripts.calibration_report.pick_samples``'s
"final sample" choice: the row with the LOWEST ``minutes_to_settlement`` is
kept (closest to resolution, i.e. the model/market's last word before the
outcome is known).

Segmentation (issue #822 scope): same-day vs. next-day evaluation (derived
from the station-local calendar date of ``ts`` vs. ``end_date`` -- the CSV
carries no ``is_next_day`` column, unlike the DB ``candidates``/``trades``
tables) and by UTC-offset bucket (derived from ``STATION_TZ`` at ``ts``) --
both diagnostic cuts for whether the pre-fix #820 bug's effect concentrates
in a particular window, per the "evening window" diagnosis in
``docs/REMEDIATION_PLAN.md``.

Self-gating: if zero rows survive loading (e.g. no local ``logs/`` data --
this is the common case in a fresh checkout or a sandboxed dev
environment, since ``logs/`` is gitignored and lives on the bot host) this
script logs an honest message and returns without writing a report --
NEVER writes a report with a fabricated or synthetic BSS number. Correct
this is a real Brier-skill decision-gate input; unlike some backtest
scripts in this repo that fall back to synthetic proxy data when a real
integration has no logged history yet (e.g. ``ecmwf_icon_backtest.py`` for
a genuinely-new data source), there is nothing legitimate to proxy here --
the archived data either exists on the host running this script, or the
run must be treated as blocked, not silently faked.

Usage::

    python -m src.scripts.bss_market_vs_model_report
    python -m src.scripts.bss_market_vs_model_report --candidates-csv logs/candidates.csv \
        --db data/meteoedge.db --out backtest_results

    # cache-only, zero HTTP requests (any ticker not already cached falls
    # back to the observed daily high):
    python -m src.scripts.bss_market_vs_model_report --no-network

    # reproduce the original 2026-07-25 settlements-join report:
    python -m src.scripts.bss_market_vs_model_report --outcome-source settlements

    # PASS 2 -- the M3 decision gate, on the full evaluated-bracket population:
    python -m src.scripts.bss_market_vs_model_report --population all-bracket
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import BRACKET_EVALS_JSONL, CANDIDATES_CSV, STATION_TZ  # noqa: E402
from src.scripts.calibration_report import (  # noqa: E402
    BUCKET_EDGES, brier_score, build_reliability, format_reliability,
)
from src.scripts.resolve_bracket_outcomes import (  # noqa: E402
    COLLISION_BOUNDARY, COLLISION_DISJOINT, COLLISION_UNKNOWN, _DEFAULT_CACHE,
    compute_observed_highs, compute_observed_lows, detect_multi_yes_station_days,
    load_candidate_directions, resolve_bracket_rows, resolve_gamma_outcomes,
    resolve_row_direction,
)
from src.utils.log_rotation import iter_rotated_jsonl, rotated_sources  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

REQUIRED_DISCLAIMER = """\
> **PASS 1 -- NOT THE DECISION GATE.** This scores the model's PRE-#820-FIX
> probabilities on a GATE-SELECTED sample (only brackets that already cleared
> live entry gates -- not the general population of evaluated brackets).
> It is a lower bound / directional read, weeks before the M3 decision gate
> (issue #822, Pass 2), which re-runs this same test on clean, post-fix,
> all-bracket data once #826 has accumulated enough history. **Do not treat
> the number below as settling whether the trading edge is real.**
"""

PASS2_DISCLAIMER = """\
> **PASS 2 -- THIS IS THE M3 DECISION GATE (issue #822).** This scores the
> POST-FIX model on the FULL evaluated-bracket population (`bracket_evals`,
> issue #826), not the gate-selected archive Pass 1 used. The decision rule
> below was fixed in `docs/REMEDIATION_PLAN.md` **before** any number was seen,
> so it cannot be rationalised afterwards.
>
> **The two passes are not directly comparable.** Pass 1 answered *"on the
> brackets we chose to trade, were we better than the market?"*; Pass 2 answers
> the general calibration question over every bracket evaluated. A different
> number is expected from population alone.
"""

# The decision rule, quoted from docs/REMEDIATION_PLAN.md ("The decision gate").
# Stated as data so the report cannot drift from the plan's wording.
DECISION_RULE = (
    ("BSS > 0.05", "The edge is real. Proceed to M4."),
    ("0 < BSS <= 0.05", "Marginal. Stay shadow-only; re-test after the sigma work "
                        "bites. Do not re-enable live."),
    ("BSS <= 0", "**It was a dream.** The public price forecasts weather at least "
                 "as well as we do. Stop the thesis -- pivot the model materially "
                 "or shut the live path down."),
)

# docs/REMEDIATION_PLAN.md: "Requires n >= 300 de-duplicated settled brackets.
# Note on power: all ~11 brackets on a station-day are determined by one daily
# high, so effective sample size is STATION-DAYS (~30/day), not bracket-rows."
# The station-day figure is the one the gate is read against.
M3_MIN_STATION_DAYS = 300

# Population selectors. Pass 1 reads the gate-selected candidate archive; Pass 2
# reads #826's full evaluated-bracket log -- the population the decision rule
# specifies.
POPULATION_GATE_SELECTED = "gate-selected"
POPULATION_ALL_BRACKET = "all-bracket"
POPULATIONS = (POPULATION_GATE_SELECTED, POPULATION_ALL_BRACKET)

# Exchange price rails (cents). A yes_ask/no_ask sitting at the floor/ceiling
# is a clipped, not a freely-produced, market price.
RAIL_LOW_CENTS = 1
RAIL_HIGH_CENTS = 99

DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))
DEFAULT_OUT_DIR = Path("backtest_results")

# Outcome-truth sources (issue #865). "resolver" is the default: Gamma-first
# with an observed-daily-high fallback, scoring every evaluated bracket.
# "settlements" is the original traded-only join, kept solely to reproduce the
# 2026-07-25 report -- it is not the recommended way to run Pass 1.
OUTCOME_SOURCE_RESOLVER = "resolver"
OUTCOME_SOURCE_SETTLEMENTS = "settlements"
OUTCOME_SOURCES = (OUTCOME_SOURCE_RESOLVER, OUTCOME_SOURCE_SETTLEMENTS)


# ---------------------------------------------------------------------------
# Loading logs/candidates.*.csv.gz
# ---------------------------------------------------------------------------

def _iter_csv_rows(base: Path) -> Iterator[dict]:
    """Yield dict rows across every rotated (and possibly gzipped) source for *base*.

    Mirrors ``scripts/prob_cap_shadow_report.py``'s ``_iter_csv_rows`` -- no
    shared CSV-rotation helper exists yet (only the JSONL side,
    ``iter_rotated_jsonl``), so this is intentionally duplicated rather than
    introducing a new cross-script dependency for one loop.
    """
    for path in rotated_sources(base):
        is_gz = path.suffix == ".gz"
        try:
            opener = (
                gzip.open(path, "rt", encoding="utf-8", newline="")
                if is_gz else open(path, "r", encoding="utf-8", newline="")
            )
            with opener as fh:
                yield from csv.DictReader(fh)
        except OSError as exc:
            log.warning("[bss] could not read %s: %s", path, exc)


def _f(row: dict, key: str) -> "float | None":
    val = row.get(key)
    if val is None or val == "" or val == "None":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def load_candidate_rows(candidates_csv: "Path" = CANDIDATES_CSV) -> "list[dict]":
    """Load and numerically normalize every row across the rotated archive.

    Returns dicts with the raw string columns preserved (``ts``, ``station``,
    ``ticker``, ``end_date``, ``question``) plus normalized floats for
    ``p_yes_raw``, ``yes_ask``, ``no_ask``, ``minutes_to_settlement``.
    ``question`` (issue #867) is the Gamma market's own question text, e.g.
    "Will the highest temperature in Paris be 28C on July 3?" -- kept as-is
    so ``infer_bracket_direction`` can parse it downstream.
    """
    out = []
    for row in _iter_csv_rows(candidates_csv):
        out.append({
            "ts": row.get("ts", ""),
            "station": row.get("station", ""),
            "ticker": row.get("ticker", ""),
            "end_date": row.get("end_date", ""),
            "question": row.get("question", ""),
            "bracket_low": _f(row, "bracket_low"),
            "bracket_high": _f(row, "bracket_high"),
            "yes_ask": _f(row, "yes_ask"),
            "no_ask": _f(row, "no_ask"),
            "p_yes_raw": _f(row, "p_yes_raw"),
            "minutes_to_settlement": _f(row, "minutes_to_settlement"),
        })
    return out


def load_bracket_eval_rows(base: "Path" = BRACKET_EVALS_JSONL) -> "list[dict]":
    """Load #826's full evaluated-bracket log for Pass 2 (issue #822).

    This is the population the M3 decision rule specifies: EVERY bracket the
    scanner evaluated, not just those that cleared the live entry gates. Pass 1
    read ``logs/candidates.*.csv.gz``, which is gate-selected and therefore
    answers a narrower, operational question.

    Rows are normalized onto the SAME shape ``load_candidate_rows`` returns, so
    every downstream stage -- exclusions, de-duplication, outcome resolution,
    the BSS math, reliability and sharpness -- is shared verbatim between the
    two passes. Nothing about how a scored bracket is scored differs; only
    which brackets are in scope.

    Two field mappings and one genuine improvement:

    - ``poll_ts`` -> ``ts`` and ``settlement_date`` -> ``end_date``: renames.
      Both denote the same quantities the candidate CSV names differently.
    - ``is_next_day`` is carried through as ``is_next_day_flag``. Pass 1 had to
      *derive* the same/next-day split from station-local ``ts`` vs ``end_date``
      because the CSV has no such column; ``bracket_evals`` records it directly,
      so Pass 2 segments on the recorded value rather than a reconstruction.
    - ``direction`` (issue #876) flows through to ``resolve_bracket_rows``,
      which needs it to score low-direction markets against the observed daily
      LOW rather than the high (issue #867).

    ``emos_mode`` is carried for segmentation, with the #871 caveat: rows logged
    before that fix carry the literal string ``"next_day"`` instead of a model
    mode, and their true mode is NOT recoverable. Any per-mode cut must exclude
    them rather than average them in.
    """
    out = []
    for row in iter_rotated_jsonl(base):
        out.append({
            "ts": str(row.get("poll_ts") or ""),
            "station": str(row.get("station") or ""),
            "ticker": str(row.get("ticker") or ""),
            "end_date": str(row.get("settlement_date") or "")[:10],
            "question": "",
            "bracket_low": _f(row, "bracket_low"),
            "bracket_high": _f(row, "bracket_high"),
            "yes_ask": _f(row, "yes_ask"),
            "no_ask": _f(row, "no_ask"),
            "p_yes_raw": _f(row, "p_yes_raw"),
            "minutes_to_settlement": _f(row, "minutes_to_settlement"),
            "direction": row.get("direction"),
            "is_next_day_flag": row.get("is_next_day"),
            "emos_mode": row.get("emos_mode"),
            "execution_mode": row.get("execution_mode"),
        })
    return out


# ---------------------------------------------------------------------------
# Exclusions (issue #822 / #820)
# ---------------------------------------------------------------------------

def apply_exclusions(rows: "list[dict]") -> "tuple[list[dict], dict[str, int]]":
    """Apply the mandatory Pass-1 row exclusions. Returns (kept, counts)."""
    counts: dict = defaultdict(int)
    kept = []
    for row in rows:
        if row["p_yes_raw"] is None:
            counts["missing_p_yes_raw"] += 1
            continue
        if row["p_yes_raw"] == 0.0:
            counts["p_yes_raw_zero_artifact"] += 1
            continue
        if row["yes_ask"] is None or row["no_ask"] is None:
            counts["missing_market_price"] += 1
            continue
        if (row["yes_ask"] <= RAIL_LOW_CENTS or row["yes_ask"] >= RAIL_HIGH_CENTS
                or row["no_ask"] <= RAIL_LOW_CENTS or row["no_ask"] >= RAIL_HIGH_CENTS):
            counts["rail_1c_99c"] += 1
            continue
        kept.append(row)
    counts["input_rows"] = len(rows)
    counts["kept_after_row_exclusions"] = len(kept)
    return kept, dict(counts)


def dedupe_one_per_bracket_day(rows: "list[dict]") -> "list[dict]":
    """Keep exactly one row per (station, ticker, end_date).

    Keeps the row with the lowest ``minutes_to_settlement`` -- the final,
    closest-to-resolution poll -- mirroring
    ``calibration_report.pick_samples``'s "final sample per market" choice.
    """
    best: "dict[tuple, dict]" = {}
    for row in rows:
        key = (row["station"], row["ticker"], row["end_date"])
        prev = best.get(key)
        mts = row["minutes_to_settlement"]
        if prev is None or (mts is not None and (
                prev["minutes_to_settlement"] is None or mts < prev["minutes_to_settlement"])):
            best[key] = row
    return list(best.values())


# ---------------------------------------------------------------------------
# Outcome join: settlements table (meteoedge.db)
# ---------------------------------------------------------------------------

def _connect_ro(db_path: "Path | None") -> "sqlite3.Connection | None":
    """Open *db_path* read-only. Returns None if missing/unopenable.

    Never creates the file (unlike plain ``sqlite3.connect``) -- this script
    must never write to production databases.
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
        log.warning("[bss] could not open %s read-only: %s", db_path, exc)
        return None


def load_settlement_outcomes(db_path: "Path | None") -> "dict[str, bool]":
    """Return {ticker: resolved_yes} from the settlements table."""
    con = _connect_ro(db_path)
    if con is None:
        return {}
    try:
        cur = con.execute("SELECT ticker, resolved_yes FROM settlements")
        return {r["ticker"]: bool(r["resolved_yes"]) for r in cur.fetchall()}
    except sqlite3.OperationalError as exc:
        log.warning("[bss] settlements read failed: %s", exc)
        return {}
    finally:
        con.close()


def join_outcomes(rows: "list[dict]", outcomes: "dict[str, bool]") -> "tuple[list[dict], int]":
    """Attach ``yes_won`` to each row; drop rows with no definitive settlement.

    Returns (rows_with_outcome, n_dropped_no_settlement).

    This is the LEGACY (``--outcome-source settlements``) path -- see the
    module docstring's "Outcome resolution" note for why it collapses the
    sample, and ``resolve_candidate_outcomes`` for the default.
    """
    out = []
    dropped = 0
    for row in rows:
        yes_won = outcomes.get(row["ticker"])
        if yes_won is None:
            dropped += 1
            continue
        out.append({**row, "yes_won": yes_won})
    return out, dropped


# ---------------------------------------------------------------------------
# Outcome resolution: Gamma-first, observed-high fallback (issue #865)
# ---------------------------------------------------------------------------

def resolve_candidate_outcomes(
    rows: "list[dict]",
    db_path: "Path | None",
    use_gamma: bool = True,
    allow_network: bool = True,
    gamma_cache_path: "Path | None" = _DEFAULT_CACHE,
) -> "tuple[list[dict], dict]":
    """Attach ``yes_won`` to de-duplicated candidate rows without touching
    ``settlements``.

    Delegates entirely to ``src.scripts.resolve_bracket_outcomes`` (issues
    #850/#860) rather than reimplementing resolution here, so Pass 1 and
    Pass 2 can never drift into two different notions of what "the bracket
    resolved YES" means. This function's only real job is adapting the
    candidate-row shape to that module's contract.

    The one field that needs mapping: candidate rows carry ``end_date`` where
    ``bracket_evals`` rows carry ``settlement_date``. They denote the same
    thing -- the market's settlement day -- and ``settle.resolve_trade_date``
    already treats ``end_date`` as the authoritative settlement date for
    exactly this purpose, so the mapping is a rename, not a reinterpretation.

    Returns (rows_with_outcome, counts). ``counts`` carries the funnel and
    ground-truth breakdown the report prints:
    ``n_unresolvable`` (dropped -- never guessed), ``resolved_from_gamma``,
    ``resolved_from_metar``, ``n_station_days`` (the effective sample size --
    all brackets on a station-day share one daily high) and ``gamma`` (the
    fetch/cache stats).

    Also attaches ``direction`` ('high'/'low'/'unknown', issue #867) to every
    row before resolution -- parsed from the candidate's own Gamma
    ``question`` text (``infer_bracket_direction``, unambiguous per #867's
    live-API verification), cross-checked/backfilled against the DB
    ``candidates.direction`` column where a matching ticker row exists (the
    authoritative backstop -- see ``resolve_row_direction``). Direction feeds
    two things downstream: ``resolve_bracket_rows`` scores ``direction='low'``
    rows against the observed daily LOW rather than the daily HIGH, and
    ``detect_multi_yes_station_days`` groups collisions per-direction so a
    low-temperature YES and a high-temperature YES on the same station-day
    are correctly not flagged as impossible.
    """
    keyed = []
    for row in rows:
        end_date = (row.get("end_date") or "")[:10]
        if not end_date:
            continue
        keyed.append({**row, "settlement_date": end_date})

    n_undatable = len(rows) - len(keyed)

    tickers = {r["ticker"] for r in keyed if r.get("ticker")}
    db_directions = load_candidate_directions(db_path, tickers)
    direction_counts: dict = defaultdict(int)
    for row in keyed:
        direction = resolve_row_direction(row, db_directions)
        row["direction"] = direction
        direction_counts[direction] += 1

    station_dates = {
        (r["station"], r["settlement_date"]) for r in keyed
        if r.get("station") and r.get("settlement_date")
    }
    observed_highs = compute_observed_highs(db_path, station_dates)
    observed_lows = compute_observed_lows(db_path, station_dates)

    gamma_resolutions: "dict[str, bool]" = {}
    gamma_stats: dict = {}
    if use_gamma:
        gamma_resolutions, gamma_stats = resolve_gamma_outcomes(
            tickers, cache_path=gamma_cache_path, allow_network=allow_network
        )

    resolved_rows, counts = resolve_bracket_rows(
        keyed, observed_highs, gamma_resolutions, observed_lows
    )

    out = [{**r, "yes_won": bool(r["resolved_yes"])} for r in resolved_rows]
    counts["n_missing_end_date"] = n_undatable
    counts["n_unresolvable"] = len(rows) - len(out)
    counts["n_station_days"] = len({(r["station"], r["settlement_date"]) for r in out})
    counts["gamma"] = gamma_stats
    counts["gamma_enabled"] = bool(use_gamma)
    counts["direction"] = dict(direction_counts)
    return out, dict(counts)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def station_local_date(ts_str: str, station: str) -> "date | None":
    """Return the station-local calendar date of *ts_str*, or None.

    Mirrors ``src/scripts/settle.py::resolve_trade_date``'s STATION_TZ +
    pytz conversion (same pattern, different purpose here: segmentation, not
    settlement-date resolution).
    """
    tz_name = STATION_TZ.get(station)
    if not tz_name or not ts_str:
        return None
    import pytz
    from dateutil import parser as dtparse
    try:
        tz = pytz.timezone(tz_name)
        t = dtparse.parse(ts_str)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(tz).date()
    except (ValueError, OverflowError, pytz.UnknownTimeZoneError):
        return None


def classify_day_segment(row: dict) -> "str | None":
    """Return 'same_day', 'next_day', 'other', or None (undeterminable).

    ``logs/candidates.csv`` carries no ``is_next_day`` column (unlike the DB
    ``candidates``/``trades`` tables written alongside it) -- derived here
    from the station-local calendar date of ``ts`` vs. ``end_date``.

    Pass 2's ``bracket_evals`` rows DO record it (``is_next_day_flag``), so the
    recorded value is preferred when present and the derivation is used only as
    a fallback. A reconstruction that disagrees with what the scanner actually
    decided would mis-segment the decision gate.
    """
    flag = row.get("is_next_day_flag")
    if flag is not None:
        return "next_day" if int(flag) == 1 else "same_day"

    local_date = station_local_date(row.get("ts", ""), row.get("station", ""))
    end_date_str = row.get("end_date", "")
    if local_date is None or not end_date_str:
        return None
    try:
        end_date = date.fromisoformat(end_date_str[:10])
    except ValueError:
        return None
    delta = (end_date - local_date).days
    if delta == 0:
        return "same_day"
    if delta == 1:
        return "next_day"
    return "other"


def utc_offset_bucket(row: dict) -> "str | None":
    """Return a 'UTC+H'/'UTC-H' label for the station's offset at ``ts``."""
    tz_name = STATION_TZ.get(row.get("station", ""))
    ts_str = row.get("ts", "")
    if not tz_name or not ts_str:
        return None
    import pytz
    from dateutil import parser as dtparse
    try:
        tz = pytz.timezone(tz_name)
        t = dtparse.parse(ts_str)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        offset = t.astimezone(tz).utcoffset()
        if offset is None:
            return None
        hours = offset.total_seconds() / 3600.0
        sign = "+" if hours >= 0 else "-"
        return f"UTC{sign}{abs(hours):g}"
    except (ValueError, OverflowError, pytz.UnknownTimeZoneError):
        return None


# ---------------------------------------------------------------------------
# Market-implied probability + BSS
# ---------------------------------------------------------------------------

def market_p_yes(row: dict) -> float:
    """Market-implied P(YES), symmetrized across both sides of the book.

    ``yes_ask`` alone is the cost to buy YES (a natural "market says P(YES)"
    reading), but ``100 - no_ask`` is an independent second reading from the
    NO side. Averaging the two nets out asymmetric bid/ask spread noise
    instead of arbitrarily preferring one side.
    """
    return ((row["yes_ask"]) + (100.0 - row["no_ask"])) / 200.0


def sharpness_histogram(probs: "list[float]", edges: "list[float]" = BUCKET_EDGES) -> "list[dict]":
    """Count of predictions per probability bucket (distribution shape only)."""
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        n = sum(1 for p in probs if lo <= p < hi)
        rows.append({"bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": n})
    return rows


def format_sharpness(rows: "list[dict]", title: str) -> str:
    total = sum(r["n"] for r in rows) or 1
    out = [f"\n=== {title} ===", f"{'bucket':>11} {'n':>6} {'share':>7}"]
    for r in rows:
        out.append(f"{r['bucket']:>11} {r['n']:>6} {100 * r['n'] / total:>6.1f}%")
    return "\n".join(out)


def compute_bss(samples: "list[tuple[dict, bool]]") -> dict:
    """Compute BS_model, BS_market, BSS over de-duped, outcome-joined rows.

    *samples* is a list of (row, yes_won) — actually rows already carry
    ``yes_won`` from join_outcomes(), so this takes the row list directly.
    """
    model_pairs = [(r["p_yes_raw"], r["yes_won"]) for r in samples]
    market_pairs = [(market_p_yes(r), r["yes_won"]) for r in samples]
    bs_model = brier_score(model_pairs)
    bs_market = brier_score(market_pairs)
    bss = None
    if bs_model is not None and bs_market is not None and bs_market > 0:
        bss = 1.0 - (bs_model / bs_market)
    return {
        "n": len(samples),
        "bs_model": bs_model,
        "bs_market": bs_market,
        "bss": bss,
    }


def verdict_label(bss: "float | None") -> str:
    """Map a BSS value to the decision-gate language (informational only for Pass 1)."""
    if bss is None:
        return "n/a (insufficient or degenerate data)"
    if bss > 0.05:
        return "edge appears real (BSS > 0.05)"
    if bss > 0:
        return "marginal (0 < BSS <= 0.05)"
    return "no edge over the market (BSS <= 0)"


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

#: Date LOW-direction market scanning was rolled back behind ``ENABLE_LOW_MARKETS``
#: (issues #733/#734, merged 2026-07-17). Before this date the scanner emitted low
#: markets by default; after it, it does not. The flag's *current* value says
#: nothing about a historical collection window — only the window's dates do.
LOW_MARKET_ROLLBACK_DATE = "2026-07-17"


def sample_date_span(samples: "list[dict]") -> "tuple[str, str] | None":
    """Earliest and latest settlement date (``end_date``) present in *samples*.

    Used to date-check a population against a code-history cutover. Rows with no
    parseable ``end_date`` are ignored; ``None`` means no row carried one.
    """
    dates = sorted({(r.get("end_date") or "")[:10] for r in samples} - {""})
    return (dates[0], dates[-1]) if dates else None


def _direction_gap_note(samples: "list[dict]", d_unknown: int) -> "list[str]":
    """Explain rows whose market direction could not be resolved.

    Direction matters because an unknown-direction row is scored against the
    observed daily HIGH. A LOW market scored that way is scored on the wrong
    physical quantity, so the BSS above would be measuring the wrong thing.

    The safety argument here is deliberately **date-based, not flag-based**.
    ``ENABLE_LOW_MARKETS`` being off today is not evidence about a historical
    collection window: the flag was *introduced* by #733/#734 to switch low
    markets off, and they were scanned by default before that. So the question is
    never "is the flag off?" but "does this population's date span start after the
    rollback?" — which is what this note answers from the data itself.
    """
    span = sample_date_span(samples)
    lines = [
        f"> {d_unknown} row(s) carry no resolvable direction and are scored against "
        f"the observed daily HIGH. `bracket_evals` gained a `direction` field only in "
        f"#876 (2026-07-28) and logs no question text, so earlier rows depend on the "
        f"`candidates.direction` backstop.\n"
    ]
    if span is None:
        lines.append(
            f"> **Direction exposure UNKNOWN.** No row carries a settlement date, so this "
            f"population cannot be dated against the {LOW_MARKET_ROLLBACK_DATE} "
            f"LOW-market rollback (#733/#734). Do not cite a verdict from it.\n"
        )
        return lines
    first, last = span
    if first >= LOW_MARKET_ROLLBACK_DATE:
        lines.append(
            f"> **Not a contamination risk for this population.** It spans "
            f"`{first}`..`{last}`, entirely after the {LOW_MARKET_ROLLBACK_DATE} rollback "
            f"of LOW-direction scanning (#733/#734), so no low market could have entered "
            f"it and an unknown-direction row is a high market. Note the argument is the "
            f"*date span*, not the current value of `ENABLE_LOW_MARKETS`: that flag was "
            f"introduced to turn low markets off, and they were scanned by default before "
            f"it existed.\n"
        )
    else:
        lines.append(
            f"> **CONTAMINATION RISK — this population predates the rollback.** It spans "
            f"`{first}`..`{last}`, and LOW-direction scanning was only switched off on "
            f"{LOW_MARKET_ROLLBACK_DATE} (#733/#734). Rows before that date can be low "
            f"markets scored against the daily HIGH. #875's confirmation run found 43 low "
            f"markets in 425 pre-rollback brackets (~10%), so treat the BSS above as "
            f"contaminated and re-run on a post-{LOW_MARKET_ROLLBACK_DATE} population "
            f"before citing any verdict.\n"
        )
    return lines


def _ground_truth_section(samples: "list[dict]", ocounts: dict) -> "list[str]":
    """Render the outcome-provenance section for the resolver path.

    Two things a reader needs before trusting the BSS above:

    1. **Which ground truth decided each bracket.** Gamma is Polymarket's
       official on-chain outcome; METAR is our observed daily high, which
       #644 measured disagreeing with the official result ~22% of the time.
       A report dominated by METAR rows deserves more scepticism than one
       dominated by Gamma rows, so the split is stated rather than buried.
    2. **Impossible outcomes.** A station-day has one daily high, so at most
       one bracket can contain it. Multiple YES brackets on the same
       station-day means the ground truth is wrong somewhere, and it biases
       the YES rate the BSS is computed against -- so it is *counted* here,
       never assumed away. The count is split by collision shape because the
       two shapes have different causes and different fixes, and reporting one
       number sends the reader to the wrong issue (which is what the
       2026-07-26 run did):

       - ``boundary`` -- the brackets touch or overlap, so an observed high on
         the shared edge satisfies the old inclusive ``lo <= x <= hi`` for both.
         Issue #861 -- fixed: ``[lo, hi)`` (upper bound exclusive) now matches
         live Gamma at 96.9% accuracy; a boundary collision here is a regression.
       - ``disjoint`` -- the brackets do not touch, so NO interval convention
         can produce both and the resolution source itself is wrong. Issue
         #867. This is the more serious of the two: on 2026-07-26 all four
         such station-days were Gamma-resolved, and Gamma decides ~95% of the
         population the M3 verdict will be scored on.
    """
    lines = ["## Outcome ground truth\n"]
    gamma_n = ocounts.get("resolved_from_gamma", 0)
    metar_n = ocounts.get("resolved_from_metar", 0)
    total = (gamma_n + metar_n) or 1
    lines.append("| Source | n | Share |")
    lines.append("|---|---|---|")
    lines.append(f"| Polymarket definitive resolution (`gamma`) | {gamma_n} | "
                 f"{100 * gamma_n / total:.1f}% |")
    lines.append(f"| Observed daily high fallback (`metar`) | {metar_n} | "
                 f"{100 * metar_n / total:.1f}% |")
    lines.append("")

    dir_counts = ocounts.get("direction") or {}
    if dir_counts:
        d_high = dir_counts.get("high", 0)
        d_low = dir_counts.get("low", 0)
        d_unknown = dir_counts.get("unknown", 0)
        lines.append(
            f"Market direction (issue #867 -- the logged `direction` field, else "
            f"`candidates.direction`, else the question text): {d_high} high, {d_low} low, "
            f"{d_unknown} unknown.\n"
        )
        if d_unknown:
            lines.extend(_direction_gap_note(samples, d_unknown))

    gamma_stats = ocounts.get("gamma") or {}
    if gamma_stats:
        lines.append(
            f"Gamma lookups: {gamma_stats.get('n_cache_hits', 0)} cache hits, "
            f"{gamma_stats.get('n_fetched', 0)} fetched "
            f"({gamma_stats.get('n_newly_resolved', 0)} newly resolved, "
            f"{gamma_stats.get('n_indecisive', 0)} indecisive, "
            f"{gamma_stats.get('n_fetch_errors', 0)} errors, "
            f"{gamma_stats.get('n_skipped_offline', 0)} skipped offline). "
            f"Indecisive/errored tickers fall back to the observed high.\n"
        )
    elif not ocounts.get("gamma_enabled", True):
        lines.append("Gamma resolution DISABLED (`--no-gamma`): every row above was "
                     "resolved from the observed daily high, a proxy #644 measured "
                     "disagreeing with the official outcome ~22% of the time.\n")

    multi_yes = detect_multi_yes_station_days(samples)
    if multi_yes:
        affected = sum(m["n_yes"] for m in multi_yes)
        n_boundary = sum(1 for m in multi_yes if m["collision_kind"] == COLLISION_BOUNDARY)
        n_disjoint = sum(1 for m in multi_yes if m["collision_kind"] == COLLISION_DISJOINT)
        n_unknown = sum(1 for m in multi_yes if m["collision_kind"] == COLLISION_UNKNOWN)
        lines.append(
            f"⚠️ **Impossible-outcome exposure: {len(multi_yes)} station-day-direction(s), "
            f"{affected} bracket-rows** resolved YES on more than one bracket of the SAME "
            f"direction (high vs. low temperature markets are grouped separately -- issue "
            f"#867). A station-day has one daily high and one daily low, so at most one "
            f"bracket per direction can contain it -- these rows inflate the YES count and "
            f"bias BS_model/BS_market. Diagnostic only; nothing is auto-corrected.\n"
        )
        lines.append("| Collision shape | Station-days | Issue |")
        lines.append("|---|---|---|")
        lines.append(f"| `boundary` — brackets touch or overlap | {n_boundary} | "
                     f"#861 (which interval convention is correct) |")
        lines.append(f"| `disjoint` — brackets do not touch, same direction | {n_disjoint} | "
                     f"#867 (no interval convention can produce this — the resolution "
                     f"source is wrong) |")
        if n_unknown:
            lines.append(f"| `unknown` — bracket bounds missing | {n_unknown} | — |")
        lines.append("")
        lines.append("| Station | Settlement date | Direction | Shape | YES brackets | Observed high | Observed low |")
        lines.append("|---|---|---|---|---|---|---|")
        for m in multi_yes[:20]:
            brackets = ", ".join(
                f"{lo}-{hi} ({src})" for lo, hi, src in m["brackets"]
            )
            lines.append(f"| {m['station']} | {m['settlement_date']} | "
                         f"{m.get('direction', '?')} | `{m['collision_kind']}` | {brackets} | "
                         f"{m['observed_high']} | {m.get('observed_low')} |")
        if len(multi_yes) > 20:
            lines.append(f"| … | {len(multi_yes) - 20} more | | | | | |")
        lines.append("")
    else:
        lines.append("Impossible-outcome check (issues #861 / #867): **0** station-days "
                     "resolved YES on more than one bracket of the same direction.\n")
    return lines


def _decision_gate_section(global_stats: dict, ocounts: dict) -> "list[str]":
    """Render the M3 verdict against the pre-registered decision rule (#822).

    The rule and its power requirement are quoted from
    ``docs/REMEDIATION_PLAN.md`` and were fixed **before** any Pass-2 number
    existed. This section states the rule first and the number second, in that
    order, so the verdict reads as an application of a standing rule rather
    than a judgement formed after seeing the result.

    The power check is deliberately a separate line from the verdict. A BSS
    computed on too few station-days is not a weaker verdict -- it is not a
    verdict at all, which is exactly how the n=20 Pass-1 run came to be briefly
    read as "edge appears real".
    """
    lines = ["## M3 decision gate -- the pre-registered rule\n"]
    lines.append("Fixed in `docs/REMEDIATION_PLAN.md` before this number was seen.\n")
    lines.append("| Result | Verdict |")
    lines.append("|---|---|")
    for condition, verdict in DECISION_RULE:
        lines.append(f"| **{condition}** | {verdict} |")
    lines.append("")

    station_days = ocounts.get("n_station_days", 0)
    bss = global_stats.get("bss")
    powered = station_days >= M3_MIN_STATION_DAYS

    lines.append("### Power check\n")
    lines.append(
        f"The rule requires **n >= {M3_MIN_STATION_DAYS}**. All ~11 brackets on a "
        f"station-day are determined by one daily high, so the effective sample size "
        f"is **station-days**, not bracket-rows.\n"
    )
    lines.append("| Measure | Value | Required | Met |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| Station-days | **{station_days}** | {M3_MIN_STATION_DAYS} | "
        f"{'YES' if powered else '**NO**'} |"
    )
    lines.append(f"| De-duplicated bracket-rows | {global_stats.get('n', 0)} | — | — |")
    lines.append("")

    if not powered:
        lines.append(
            f"> **UNDERPOWERED -- THIS IS NOT A VERDICT.** {station_days} station-days is "
            f"{100 * station_days / M3_MIN_STATION_DAYS:.0f}% of the required sample. The "
            f"BSS below is reported for completeness and **must not be read as the M3 "
            f"decision**, in either direction. Re-run once the population reaches "
            f"{M3_MIN_STATION_DAYS} station-days.\n"
        )
    elif bss is None:
        lines.append("> **NO VERDICT** -- BSS is undefined (degenerate BS_market).\n")
    else:
        lines.append(f"### Verdict: {verdict_label(bss)}\n")
        lines.append(
            f"BSS = **{bss:.4f}** on {station_days} station-days, at or above the "
            f"required power. Per the rule above, this is the M3 decision.\n"
        )
    return lines


def build_report(samples: "list[dict]", exclusion_counts: dict, n_no_settlement: int,
                 run_date: str, outcome_meta: "dict | None" = None,
                 population: str = POPULATION_GATE_SELECTED) -> str:
    """Assemble the markdown report for either pass.

    *outcome_meta* describes how outcomes were resolved --
    ``{"source": OUTCOME_SOURCE_*, "counts": {...}}``. Defaults to the legacy
    settlements join so the original call signature keeps working.

    *population* selects Pass 1 (``gate-selected``) or Pass 2
    (``all-bracket``). Only the framing differs -- the disclaimer, the verdict
    section and the power gate. Every computed quantity below is identical
    between the two passes, which is the point: the decision rule must not be
    able to drift from the read that preceded it.
    """
    outcome_meta = outcome_meta or {"source": OUTCOME_SOURCE_SETTLEMENTS, "counts": {}}
    source = outcome_meta.get("source", OUTCOME_SOURCE_SETTLEMENTS)
    ocounts = outcome_meta.get("counts") or {}
    is_resolver = source == OUTCOME_SOURCE_RESOLVER
    is_pass2 = population == POPULATION_ALL_BRACKET

    global_stats = compute_bss(samples)

    by_segment: "dict[str, list[dict]]" = defaultdict(list)
    for r in samples:
        seg = classify_day_segment(r) or "undeterminable"
        by_segment[seg].append(r)

    by_offset: "dict[str, list[dict]]" = defaultdict(list)
    for r in samples:
        bucket = utc_offset_bucket(r) or "undeterminable"
        by_offset[bucket].append(r)

    model_samples = [(r["p_yes_raw"], r["yes_won"]) for r in samples]
    market_samples = [(market_p_yes(r), r["yes_won"]) for r in samples]

    lines = []
    if is_pass2:
        lines.append("# Market-vs-Model Skill Test -- PASS 2 / M3 DECISION GATE "
                     "(issue #822)\n")
    else:
        lines.append("# Market-vs-Model Skill Test -- Pass 1 (issue #822)\n")
    lines.append(f"**Run date:** {run_date}  ")
    if is_resolver:
        src = ("`logs/bracket_evals.*.jsonl` (issue #826, FULL evaluated-bracket "
               "population)" if is_pass2 else
               "`logs/candidates.*.csv.gz` (archived, gate-selected)")
        lines.append(
            f"**Data source:** {src}  \n"
            "**Outcome truth:** `resolve_bracket_outcomes` -- Polymarket definitive "
            "resolution, falling back to the station-local observed daily high "
            "(issues #850/#860/#865)  \n"
        )
    else:
        lines.append("**Data source:** `logs/candidates.*.csv.gz` (archived, gate-selected) "
                     "joined to `settlements` (meteoedge.db)  \n")
    lines.append(PASS2_DISCLAIMER if is_pass2 else REQUIRED_DISCLAIMER)
    lines.append("\n---\n")

    lines.append("## Exclusion funnel\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| Input rows (all polls, all dates) | {exclusion_counts.get('input_rows', 0)} |")
    lines.append(f"| Excluded: missing `p_yes_raw` | {exclusion_counts.get('missing_p_yes_raw', 0)} |")
    lines.append(
        f"| Excluded: `p_yes_raw == 0.0` (certainty-shortcut artifact, #820) | "
        f"{exclusion_counts.get('p_yes_raw_zero_artifact', 0)} |"
    )
    lines.append(f"| Excluded: missing market price | {exclusion_counts.get('missing_market_price', 0)} |")
    lines.append(f"| Excluded: 1c/99c rail | {exclusion_counts.get('rail_1c_99c', 0)} |")
    lines.append(f"| Kept after row exclusions | {exclusion_counts.get('kept_after_row_exclusions', 0)} |")
    if is_resolver:
        lines.append(
            f"| De-duplicated to one row per (station, ticker, settlement date) | "
            f"{ocounts.get('input_rows', global_stats['n'] + n_no_settlement)} |"
        )
        lines.append(
            f"| Excluded: no outcome resolvable (no Gamma resolution, no observed high) | "
            f"{n_no_settlement} |"
        )
    else:
        lines.append(f"| Excluded: no definitive settlement match | {n_no_settlement} |")
    lines.append(f"| **Final de-duplicated sample (n)** | **{global_stats['n']}** |")
    if is_resolver:
        lines.append(
            f"| **Effective sample size (station-days)** | "
            f"**{ocounts.get('n_station_days', 0)}** |"
        )
    lines.append("")

    lines.append("## Global result\n")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| n | {global_stats['n']} |")
    bs_m = global_stats["bs_model"]
    bs_k = global_stats["bs_market"]
    bss = global_stats["bss"]
    lines.append(f"| BS_model | {bs_m:.4f} |" if bs_m is not None else "| BS_model | n/a |")
    lines.append(f"| BS_market | {bs_k:.4f} |" if bs_k is not None else "| BS_market | n/a |")
    lines.append(f"| BSS | {bss:.4f} |" if bss is not None else "| BSS | n/a |")
    if is_pass2:
        lines.append(f"| **M3 VERDICT** | **{verdict_label(bss)}** |")
    else:
        lines.append(f"| Pass-1 reading (NOT the M3 verdict) | {verdict_label(bss)} |")
    lines.append("")
    if is_resolver:
        lines.append(
            f"Note on power (docs/REMEDIATION_PLAN.md): all brackets on a station-day "
            f"share one daily-high outcome, so they are not independent draws -- the "
            f"effective sample size is **{ocounts.get('n_station_days', 0)} station-days**, "
            f"not the {global_stats['n']} bracket-rows the BSS above is computed over. "
            f"Read the station-day figure against the decision rule's power requirement.\n"
        )
    else:
        lines.append("Note on power (docs/REMEDIATION_PLAN.md): all brackets on a station-day "
                     "share one daily-high outcome, so the effective sample size is "
                     "station-days, not bracket-rows. This report states the de-duplicated "
                     "bracket-row n; it does not further collapse to station-days.\n")

    if is_pass2:
        lines.extend(_decision_gate_section(global_stats, ocounts))

    if is_resolver:
        lines.extend(_ground_truth_section(samples, ocounts))

    lines.append("## Segment: same-day vs. next-day\n")
    if is_pass2:
        lines.append("(From the `is_next_day` flag `bracket_evals` RECORDS -- not a "
                     "reconstruction. Pass 1 had to derive this from station-local `ts` vs "
                     "`end_date` because the candidates CSV carries no such column.)\n")
    else:
        lines.append("(Derived from station-local `ts` date vs. `end_date` -- "
                     "`logs/candidates.csv` carries no `is_next_day` column.)\n")
    lines.append("| Segment | n | BS_model | BS_market | BSS |")
    lines.append("|---|---|---|---|---|")
    for seg in sorted(by_segment):
        stats = compute_bss(by_segment[seg])
        bsm = f"{stats['bs_model']:.4f}" if stats["bs_model"] is not None else "n/a"
        bsk = f"{stats['bs_market']:.4f}" if stats["bs_market"] is not None else "n/a"
        bssv = f"{stats['bss']:.4f}" if stats["bss"] is not None else "n/a"
        lines.append(f"| {seg} | {stats['n']} | {bsm} | {bsk} | {bssv} |")
    lines.append("")

    lines.append("## Segment: UTC-offset bucket\n")
    lines.append("| Bucket | n | BS_model | BS_market | BSS |")
    lines.append("|---|---|---|---|---|")
    for bucket in sorted(by_offset):
        stats = compute_bss(by_offset[bucket])
        bsm = f"{stats['bs_model']:.4f}" if stats["bs_model"] is not None else "n/a"
        bsk = f"{stats['bs_market']:.4f}" if stats["bs_market"] is not None else "n/a"
        bssv = f"{stats['bss']:.4f}" if stats["bss"] is not None else "n/a"
        lines.append(f"| {bucket} | {stats['n']} | {bsm} | {bsk} | {bssv} |")
    lines.append("")

    lines.append("## Reliability")
    lines.append(format_reliability(build_reliability(model_samples), "Model (p_yes_raw)"))
    lines.append(format_reliability(build_reliability(market_samples), "Market (symmetrized implied P(YES))"))

    lines.append("\n## Sharpness")
    lines.append(format_sharpness(sharpness_histogram([p for p, _ in model_samples]), "Model (p_yes_raw)"))
    lines.append(format_sharpness(sharpness_histogram([p for p, _ in market_samples]), "Market (symmetrized implied P(YES))"))

    lines.append("\n---\n")
    lines.append("## Methodology notes\n")
    if is_pass2:
        lines.append("- `p_model` = `p_yes_raw` (pre-clamp). **POST-#820-fix** -- "
                     "`bracket_evals` logging began 2026-07-24, so every row here was "
                     "produced by the fixed model.")
    else:
        lines.append("- `p_model` = `p_yes_raw` (pre-clamp, pre-#820-fix probability).")
    lines.append("- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- symmetrized across both "
                 "sides of the book (see `market_p_yes()`); using `yes_ask` alone is an "
                 "equally defensible alternative and would read slightly differently on wide "
                 "spreads.")
    lines.append("- De-duplication keeps the final (lowest `minutes_to_settlement`) poll per "
                 "(station, ticker, settlement date), mirroring "
                 "`calibration_report.pick_samples`.")
    if is_resolver:
        lines.append(
            "- Outcome truth: `resolve_bracket_outcomes.resolve_bracket_rows()` -- "
            "Polymarket's definitive resolution where available, else the station-local "
            "observed daily temperature, **dispatched on market direction**: a `high` "
            "market is YES iff the observed daily HIGH falls in the bracket, a `low` "
            "market iff the observed daily LOW does. A low market is never scored "
            "against the high (issue #867). The interval is `[bracket_low, "
            "bracket_high)` -- upper bound EXCLUSIVE, which #861 measured against live "
            "Gamma at 96.9% agreement vs. 48.8% for the inclusive reading. This is the "
            "same precedence `settle.py` applies and the same capability #822's Pass 2 "
            "uses. Brackets that neither source can resolve are dropped, never guessed.")
        lines.append(
            "- **Not joined to `settlements`** (issue #865). That table only covers "
            "brackets MeteoEdge actually traded (~156 rows all-time); joining it dropped "
            "360 of 380 de-duplicated brackets on the 2026-07-25 run, leaving n=20. "
            "Whether a bracket resolved YES is a fact about the weather, not about "
            "whether we traded it. Use `--outcome-source settlements` to reproduce that "
            "earlier report.")
    else:
        lines.append("- Outcome truth: `settlements.resolved_yes` (meteoedge.db), keyed by ticker. "
                     "**Legacy path** (`--outcome-source settlements`): scores only brackets "
                     "MeteoEdge traded. See issue #865.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_report(candidates_csv: Path, db_path: Path, out_dir: Path,
               run_date: "str | None" = None,
               outcome_source: str = OUTCOME_SOURCE_RESOLVER,
               use_gamma: bool = True,
               allow_network: bool = True,
               gamma_cache_path: "Path | None" = _DEFAULT_CACHE,
               population: str = POPULATION_GATE_SELECTED,
               bracket_evals: "Path | None" = None) -> int:
    """Load, filter, resolve outcomes, score, and (if there is real data) write
    the report.

    Self-gating -- each point logs a clear reason and returns 0 (safe to
    schedule any day) rather than writing a report with no real signal in it:

      1. no candidate rows at all (no local ``logs/`` data),
      2. resolver path: no readable ``data/meteoedge.db`` (checked BEFORE any
         Gamma fetch, so a sandbox run costs zero HTTP requests),
      3. settlements path: no settlement outcomes at all,
      4. no row survives outcome resolution.
    """
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    if outcome_source not in OUTCOME_SOURCES:
        raise ValueError(
            f"outcome_source must be one of {OUTCOME_SOURCES}, got {outcome_source!r}"
        )
    if population not in POPULATIONS:
        raise ValueError(
            f"population must be one of {POPULATIONS}, got {population!r}"
        )

    is_pass2 = population == POPULATION_ALL_BRACKET
    if is_pass2:
        source_path = bracket_evals or BRACKET_EVALS_JSONL
        raw_rows = load_bracket_eval_rows(source_path)
    else:
        source_path = candidates_csv
        raw_rows = load_candidate_rows(source_path)
    if not raw_rows:
        log.info(
            "[bss] no rows found under %s (rotated sources) -- nothing to score. "
            "This is expected in a fresh checkout / dev sandbox; logs/ is "
            "gitignored and lives on the bot host. Not writing a report.",
            source_path,
        )
        return 0

    kept_rows, exclusion_counts = apply_exclusions(raw_rows)
    deduped = dedupe_one_per_bracket_day(kept_rows)

    if outcome_source == OUTCOME_SOURCE_RESOLVER:
        # Gate on the DB before resolve_candidate_outcomes() so a run with no
        # local database never issues a Gamma request it cannot use.
        if _connect_ro(db_path) is None:
            log.info(
                "[bss] no readable database at %s -- cannot resolve observed daily "
                "highs. Not writing a report.", db_path,
            )
            return 0
        samples, outcome_counts = resolve_candidate_outcomes(
            deduped, db_path, use_gamma=use_gamma, allow_network=allow_network,
            gamma_cache_path=gamma_cache_path,
        )
        n_unresolved = outcome_counts.get("n_unresolvable", 0)
        if not samples:
            log.info("[bss] no row could be resolved from Gamma or observations "
                     "-- not writing a report.")
            return 0
        log.info(
            "[bss] resolved %d/%d de-duplicated brackets (%d gamma, %d metar) "
            "across %d station-days",
            len(samples), len(deduped), outcome_counts.get("resolved_from_gamma", 0),
            outcome_counts.get("resolved_from_metar", 0),
            outcome_counts.get("n_station_days", 0),
        )
    else:
        outcomes = load_settlement_outcomes(db_path)
        if not outcomes:
            log.info(
                "[bss] settlements table at %s is empty or unavailable -- cannot join "
                "outcomes. Not writing a report.", db_path,
            )
            return 0
        samples, n_unresolved = join_outcomes(deduped, outcomes)
        outcome_counts = {}
        if not samples:
            log.info("[bss] no rows had a definitive settlement match -- not writing a report.")
            return 0

    report = build_report(
        samples, exclusion_counts, n_unresolved, run_date,
        outcome_meta={"source": outcome_source, "counts": outcome_counts},
        population=population,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "bss_market_vs_model_pass2" if is_pass2 else "bss_market_vs_model_pass1"
    out_path = out_dir / f"{stem}_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    if is_pass2:
        station_days = outcome_counts.get("n_station_days", 0)
        log.info(
            "[bss] PASS 2 / M3 GATE -- wrote %s | n=%d bracket-rows, %d station-days "
            "(need %d) | %s",
            out_path, len(samples), station_days, M3_MIN_STATION_DAYS,
            "POWERED" if station_days >= M3_MIN_STATION_DAYS
            else "UNDERPOWERED -- not a verdict",
        )
    else:
        log.info("[bss] wrote %s (n=%d)", out_path, len(samples))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates-csv", type=Path, default=CANDIDATES_CSV)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None,
                    help="Report date stamp (default: today, UTC)")
    ap.add_argument("--population", choices=POPULATIONS,
                    default=POPULATION_GATE_SELECTED,
                    help="'gate-selected' (default, Pass 1): score the archived "
                         "candidates CSV -- only brackets that cleared the live entry "
                         "gates. 'all-bracket' (Pass 2 / M3 DECISION GATE): score "
                         "#826's full bracket_evals log, the population the decision "
                         "rule specifies.")
    ap.add_argument("--bracket-evals", type=Path, default=None,
                    help="Override the bracket_evals JSONL path "
                         "(--population all-bracket only)")
    ap.add_argument("--outcome-source", choices=OUTCOME_SOURCES,
                    default=OUTCOME_SOURCE_RESOLVER,
                    help="'resolver' (default): Gamma-first with observed-daily-high "
                         "fallback, scores every evaluated bracket. 'settlements': the "
                         "legacy traded-only join that produced the n=20 2026-07-25 "
                         "report (issue #865).")
    ap.add_argument("--no-gamma", action="store_true",
                    help="Resolve purely from observed daily highs -- a proxy #644 "
                         "measured disagreeing with the official outcome ~22%% of the "
                         "time. Diagnostic use only.")
    ap.add_argument("--no-network", action="store_true",
                    help="Serve Gamma resolutions from the local cache only; issue zero "
                         "HTTP requests. Uncached tickers fall back to the observed high.")
    args = ap.parse_args(argv)
    return run_report(
        args.candidates_csv, args.db, args.out, args.run_date,
        outcome_source=args.outcome_source,
        use_gamma=not args.no_gamma,
        allow_network=not args.no_network,
        population=args.population,
        bracket_evals=args.bracket_evals,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
