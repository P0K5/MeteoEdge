"""Offline evaluation: candidate ``MODEL_PROB_CAP`` values replayed against
the real same-day entry gates (issue #1490, "Amended 2026-11-08 (PM)" scope).

**Analysis only -- no live-path writes.** Every DB access goes through
``ReadOnlyDatabase`` (``src.scripts.emos_shadow_reconstruction``), the same
read-only, write-denying connection issue #1041 built. This module never
calls ``scanner.py``, never writes to ``bot_config`` or any other live
table, and never changes ``MODEL_PROB_CAP`` itself. Not wired into
cron/CI -- manually invoked only, like its sibling offline reports.

**Question this answers:** if ``MODEL_PROB_CAP`` (today 0.95) had instead
been a looser value, would the same-day entries the real gates would have
made -- replayed with that looser clamp -- have been more profitable, out
of sample, without loosening the protected NO-entry gate?

**Why this needs no EMOS refit, unlike #1453's harness.** ``MODEL_PROB_CAP``
is a single symmetric clamp applied late in the pipeline
(``scanner.py``'s ``p_yes = min(max(p_yes, 1-cap), cap)``), and the
UNCAPPED ``p_yes_raw`` is already logged on every historical poll in
``logs/bracket_evals.*.jsonl`` (``src.scripts.run.py``'s
``"p_yes_raw": snap.get("raw_p_yes")``). Testing a different cap is
therefore a deterministic re-clamp of an already-correct, already-logged
model output -- it does not require ``src.model.emos_calibration.fit_emos``,
``reconstruct_mu_raw``, or any of #1453's per-lead-bin coefficient
machinery. Everything else about the harness -- poll-level iteration,
first-qualifying-poll selection, the real gate functions, outcome
resolution -- is reused verbatim from
``src.scripts.per_lead_emos_offline_eval`` (#1453/PR #1497) and
``src.scripts.bss_market_vs_model_report`` (#822).

**Method, posted as the plan comment on issue #1490 before this file was
written (2026-11-08):**

1. **Candidate population.** Every same-day, high-direction scanner poll
   from ``logs/bracket_evals.*.jsonl`` (issue #826) -- NOT the
   ``scan_decisions`` DB table, which is ``PRIMARY KEY (station, ticker,
   date)`` and upserted, so it only ever retains the LAST poll per market
   while real same-day entries fire on the FIRST qualifying poll.
   ``per_lead_emos_offline_eval.load_same_day_polls`` is reused unchanged.
2. **Data window cutoff.** Polls strictly before ``ISSUE_1507_T0`` (the
   pre-registered #1507 package test's start timestamp) -- this read-only
   replay must never overlap #1507's own in-flight collection window, per
   the issue's hard constraint.
3. **Row hygiene -- corrected after the first local run.** Only rows with
   ``p_yes_raw`` missing are excluded from the candidate population.
   ``p_yes_raw == 0.0`` rows are counted for disclosure
   (``count_excluded_rows``, via the imported, unchanged
   ``bss_market_vs_model_report.is_model_certain_price`` predicate) but are
   **NOT excluded from the gate replay** -- the first local run of this
   script applied that exclusion by analogy with
   ``bss_market_vs_model_report``'s BSS-scoring methodology and found it
   dropped ~60% of all polls, which is far too large to be the (now-fixed,
   2026-07-24) #820 mistiming bug and turned out to be ``envelope.py``'s
   ordinary, frequent "certainty shortcut" (an exact 0.0/1.0 whenever the
   observed running high already settles a bracket) -- a legitimate,
   routine model output that the real ``scanner.py`` gate clamps and trades
   on with no special-casing at all. Excluding it here would have silently
   dropped a large share of the real NO-entry population this issue exists
   to measure. See ``_evaluate_poll_for_cap``'s docstring for the fuller
   explanation.
4. **Re-clamp, not re-derive.** ``per_lead_emos_offline_eval.evaluate_entry``
   already clamps its ``p_yes_raw`` argument via
   ``_clamp_p_yes(p_yes_raw, cfg.model_prob_cap)`` internally. Each
   candidate cap is therefore just ``dataclasses.replace(cfg,
   model_prob_cap=<candidate>)`` on top of today's live
   ``GateConfig`` (``per_lead_emos_offline_eval._live_gate_config`` --
   every OTHER threshold held at today's live value for the whole window,
   same disclosed simplification as #1453).
5. **Margin gate's own input.** The NO-side margin gate
   (``src.strategy.scanner.no_entry_margin_gap``) reads
   ``state.forecast_high_f`` (the raw NWS number, not the model
   probability) and ``state.current_high_f`` -- reconstructed exactly as
   #1453's harness does (``reconstruct_current_high``,
   ``reconstruct_latest_temp``, ``_station_local_now``, the private
   ``_reconstruct_nws_forecast`` helper -- all reused, not
   reimplemented). This reconstruction is identical for every candidate
   cap (it does not depend on ``MODEL_PROB_CAP`` at all), so it cannot
   bias the cap comparison, and ``OfflineEvalCache`` plus its
   ``_cached_current_high``/``_cached_latest_temp``/``_cached_nws_forecast``
   helpers are reused to avoid redoing this reconstruction once per cap
   value.
6. **First-qualifying-poll selection, per cap value, independently.** For
   each ``(station, ticker, end_date)``, polls are walked in ``ts`` order
   and the first one whose recomputed gate passes (YES or NO) under that
   cap's clamp is kept as that cap's trade -- mirrors ``run.py``'s live
   dedup (trade created on the first qualifying candidate). Different caps
   can select different polls, or different sides, for the same market --
   this is exactly the "gate verdict flip" the issue asks to quantify.
7. **Outcomes**: ``bss_market_vs_model_report.resolve_candidate_outcomes``
   (imported, unchanged) -- Gamma-first, observed-daily-high fallback, the
   same resolution M3 uses.
8. **Reporting.** Per cap value, for all 30 cities and the 7 promoted
   cities separately: n trades, win rate, total pnl/contract (= "EV/contract
   against real market prices" in the issue's wording -- same convention as
   ``trade_pnl``), NO-side pnl, YES-side pnl. Per cap value: gate-verdict-flip
   counts relative to the cap=0.95 baseline (new-qualifies /
   stopped-qualifying / side-switch), as counts and as a fraction of the
   total candidate population.
9. **Pre-registered decision rule** (restated from the issue, applied
   exactly, never adjusted after seeing results): recommend a specific
   looser cap only if, for it, (1) same-day total pnl/contract (all 30
   cities) is higher than at cap=0.95, (2) NO-side pnl (all 30 cities) is
   not lower, and (3) the flip fraction is small enough to hand-sanity-check
   (a huge fraction newly qualifying is a harness-bug signal, not a
   finding -- stop and report, don't recommend). Item (3) is deliberately
   NOT auto-thresholded here -- the issue left it a qualitative human
   check, so this module reports the flip numbers and labels only items
   (1)/(2) as mechanically passed/failed, rather than inventing a numeric
   cutoff the issue never pre-registered.

Usage::

    python -m src.scripts.model_prob_cap_offline_eval \\
        --db data/meteoedge.db
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.model.envelope import Bracket, WeatherState  # noqa: E402
from src.scripts.bss_market_vs_model_report import (  # noqa: E402
    DEFAULT_DB_PATH,
    is_model_certain_price,
)
from src.scripts.emos_shadow_reconstruction import (  # noqa: E402
    ReadOnlyDatabase,
    _station_local_now,
)
from src.scripts.per_lead_emos_offline_eval import (  # noqa: E402
    PROMOTED_CITIES,
    ArmTrade,
    GateConfig,
    OfflineEvalCache,
    _cached_current_high,
    _cached_latest_temp,
    _cached_nws_forecast,
    _fmt_summary,
    _live_gate_config,
    attach_outcomes,
    evaluate_entry,
    load_same_day_polls,
    summarize_trades,
)
from src.strategy.scanner import STATION_TO_CITY, no_entry_margin_gap  # noqa: E402

log = logging.getLogger(__name__)

BASELINE_CAP: float = 0.95
# Issue #1490's pre-registered candidate set.
CANDIDATE_CAPS: "tuple[float, ...]" = (0.97, 0.98, 0.99, 1.0)
ALL_CAPS: "tuple[float, ...]" = (BASELINE_CAP,) + CANDIDATE_CAPS

# #1507's pre-registered package test started at this UTC timestamp
# (2026-11-08) -- this read-only replay must never overlap that in-flight
# collection window (issue #1490's hard constraint), so the candidate
# population is cut off strictly before it.
ISSUE_1507_T0: str = "2026-11-08T15:33:31+00:00"


@dataclass
class CapPollCandidate:
    station: str
    ticker: str
    end_date: str
    ts: str
    bracket_low: float
    bracket_high: float
    yes_ask: float
    no_ask: float
    minutes_to_settlement: float
    p_yes_raw: "float | None"


# ---------------------------------------------------------------------------
# Row loading / hygiene
# ---------------------------------------------------------------------------

def load_candidate_groups(
    bracket_evals_path: Path, since: str, until: "str | None" = None,
) -> "dict[tuple, list[dict]]":
    """Same-day, high-direction polls grouped by (station, ticker, end_date),
    reusing ``load_same_day_polls`` unchanged, then (a) dropping polls at or
    after *until* (the #1507 T0 cutoff by default) and (b) dropping groups
    left with zero polls."""
    groups = load_same_day_polls(bracket_evals_path, since)
    if until is None:
        return groups
    out: "dict[tuple, list[dict]]" = {}
    for key, polls in groups.items():
        kept = [p for p in polls if (p.get("ts") or "") < until]
        if kept:
            out[key] = kept
    return out


def count_excluded_rows(groups: "dict[tuple, list[dict]]") -> "dict[str, int]":
    """Count rows with missing ``p_yes_raw`` (the only hygiene exclusion this
    module applies -- see module docstring point 3 for why ``p_yes_raw ==
    0.0`` rows are counted here for disclosure but NOT excluded), for
    disclosure in the report."""
    missing = zero = total = 0
    for polls in groups.values():
        for row in polls:
            total += 1
            if row.get("p_yes_raw") is None:
                missing += 1
            elif is_model_certain_price(row):
                zero += 1
    return {"total_polls": total, "missing_p_yes_raw": missing, "p_yes_raw_zero_not_excluded": zero}


# ---------------------------------------------------------------------------
# Per-poll gate replay for one cap value
# ---------------------------------------------------------------------------

def _evaluate_poll_for_cap(
    db, poll: CapPollCandidate, cfg: GateConfig, cache: OfflineEvalCache,
) -> "tuple[str, float, float, float] | None":
    """Replay the real entry gate for one poll under *cfg* (whose
    ``model_prob_cap`` is the candidate value under test), reusing
    ``evaluate_entry`` (which does the clamp internally) and the same
    margin-gate reconstruction #1453's harness uses -- identical for every
    cap value, see module docstring point 5. Returns ``None`` for any
    unreconstructable input, never a silent approximation (same policy as
    #1453).

    ``p_yes_raw == 0.0`` is deliberately NOT excluded here (module docstring
    point 3, corrected after the first run of this script -- see the issue
    #1490 follow-up comment). ``scanner.py``'s live gate never excludes it
    either: ``true_probability_yes``'s observation-based "certainty
    shortcuts" (``src/model/envelope.py``) legitimately return an exact
    0.0/1.0 whenever the running daily high already settles a bracket, and
    ``raw_p_yes`` flows straight into the clamp and the entry gate with no
    special-casing (``scanner.py`` lines ~984-1000). ``is_model_certain_price``
    (imported, used only by ``count_excluded_rows`` for disclosure) is
    ``bss_market_vs_model_report``'s BSS-*scoring* exclusion -- it exists
    there to avoid crediting/penalizing calibration on a value that may
    reflect the (now-fixed) #820 evening-mistiming bug, which is a different
    question from "does the live gate see and trade this value", which it
    does."""
    if poll.p_yes_raw is None:
        return None

    station = poll.station
    date_str = poll.end_date

    current_high = _cached_current_high(cache, db, station, date_str, poll.ts)
    latest_temp = _cached_latest_temp(cache, db, station, poll.ts)
    if current_high is None or latest_temp is None:
        return None
    now_local = _station_local_now(station, poll.ts)
    if now_local is None:
        return None
    try:
        settlement_date = _date.fromisoformat(date_str[:10])
    except ValueError:
        return None

    bracket = Bracket(
        ticker=poll.ticker, low_f=poll.bracket_low, high_f=poll.bracket_high,
        yes_ask_cents=int(poll.yes_ask), yes_ask_size=0,
        no_ask_cents=int(poll.no_ask), no_ask_size=0,
    )

    margin_gap = None
    nws_fc = _cached_nws_forecast(cache, db, station, date_str, poll.minutes_to_settlement)
    if nws_fc is not None:
        margin_state = WeatherState(
            station=station, now_local=now_local, sunset_local=now_local,
            current_high_f=float(current_high), current_high_time=now_local,
            latest_temp_f=float(latest_temp), latest_temp_time=now_local,
            forecast_high_f=nws_fc,
        )
        margin_gap = no_entry_margin_gap(bracket, margin_state, market_date=settlement_date)

    return evaluate_entry(poll.p_yes_raw, poll.yes_ask, poll.no_ask, margin_gap, cfg)


def select_first_qualifying_trades_for_cap(
    db, groups: "dict[tuple, list[dict]]", cfg: GateConfig, cache: OfflineEvalCache,
) -> "list[ArmTrade]":
    """For each (station, ticker, end_date), walk its polls in order and
    keep the first one this cap's recomputed gate passes (module docstring
    point 6). Mirrors ``per_lead_emos_offline_eval.select_first_qualifying_trades``,
    adapted to read ``p_yes_raw`` straight off the row instead of
    reconstructing mu/sigma for an EMOS variant."""
    trades: "list[ArmTrade]" = []
    for (station, ticker, end_date), polls in groups.items():
        for row in polls:
            poll = CapPollCandidate(
                station=station, ticker=ticker, end_date=end_date, ts=row.get("ts") or "",
                bracket_low=row["bracket_low"], bracket_high=row["bracket_high"],
                yes_ask=row["yes_ask"], no_ask=row["no_ask"],
                minutes_to_settlement=row["minutes_to_settlement"],
                p_yes_raw=row.get("p_yes_raw"),
            )
            result = _evaluate_poll_for_cap(db, poll, cfg, cache)
            if result is not None:
                side, price_cents, p_yes, _ev = result
                trades.append(ArmTrade(
                    station=station, ticker=ticker, end_date=end_date, side=side,
                    price_cents=price_cents, p_yes=p_yes, poll_ts=poll.ts,
                    bracket_low=poll.bracket_low, bracket_high=poll.bracket_high,
                    question=row.get("question"),
                ))
                break
    return trades


# ---------------------------------------------------------------------------
# Gate-verdict-flip accounting (issue #1490's "noxious impact" check)
# ---------------------------------------------------------------------------

@dataclass
class FlipCounts:
    total_candidates: int
    new_qualify: int        # baseline None -> candidate trades
    stopped_qualifying: int  # baseline trades -> candidate None
    side_switch: int         # baseline YES<->NO under candidate

    @property
    def total_flips(self) -> int:
        return self.new_qualify + self.stopped_qualifying + self.side_switch

    @property
    def flip_fraction(self) -> float:
        return self.total_flips / self.total_candidates if self.total_candidates else 0.0


def _verdict_map(trades: "list[ArmTrade]") -> "dict[tuple, str]":
    return {(t.station, t.ticker, t.end_date): t.side for t in trades}


def compute_flip_counts(
    all_keys: "list[tuple]", baseline_trades: "list[ArmTrade]", candidate_trades: "list[ArmTrade]",
) -> FlipCounts:
    """Compare per-candidate gate verdict (None / "YES" / "NO") between the
    baseline cap and one candidate cap, over *all_keys* (the full candidate
    population, so a group neither arm traded correctly counts as
    no-flip)."""
    baseline_map = _verdict_map(baseline_trades)
    candidate_map = _verdict_map(candidate_trades)
    new_qualify = stopped_qualifying = side_switch = 0
    for key in all_keys:
        b = baseline_map.get(key)
        c = candidate_map.get(key)
        if b is None and c is not None:
            new_qualify += 1
        elif b is not None and c is None:
            stopped_qualifying += 1
        elif b is not None and c is not None and b != c:
            side_switch += 1
    return FlipCounts(
        total_candidates=len(all_keys), new_qualify=new_qualify,
        stopped_qualifying=stopped_qualifying, side_switch=side_switch,
    )


# ---------------------------------------------------------------------------
# Pre-registered decision rule (mechanical items 1 & 2 only -- see module
# docstring point 9 for why item 3 is reported, not auto-thresholded)
# ---------------------------------------------------------------------------

def passes_mechanical_checks(baseline_summary, candidate_summary) -> bool:
    """Items (1) and (2) of the pre-registered rule: candidate's total pnl
    strictly higher, and NO-side pnl not lower, than the baseline cap's --
    both over the SAME (all 30 cities) population, matching
    ``per_lead_emos_offline_eval.decide``'s precedent of comparing on the
    all-cities summary."""
    pnl_higher = candidate_summary.total_pnl > baseline_summary.total_pnl
    no_not_lower = candidate_summary.no_pnl >= baseline_summary.no_pnl
    return pnl_higher and no_not_lower


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _filtered(trades: "list[ArmTrade]", cities_filter: "frozenset[str]") -> "list[ArmTrade]":
    return [t for t in trades if STATION_TO_CITY.get(t.station, t.station) in cities_filter]


def run(
    db_path: Path, bracket_evals_path: Path, since: str, until: "str | None",
    use_gamma: bool, allow_network: bool,
) -> dict:
    ro_db = ReadOnlyDatabase(db_path)
    cache = OfflineEvalCache()
    try:
        cfg_base = _live_gate_config(ro_db)
        groups = load_candidate_groups(bracket_evals_path, since, until)
        exclusions = count_excluded_rows(groups)
        all_keys = list(groups.keys())

        all_cities = frozenset(STATION_TO_CITY.values())

        trades_by_cap: "dict[float, list[ArmTrade]]" = {}
        for cap in ALL_CAPS:
            cfg = dataclasses.replace(cfg_base, model_prob_cap=cap)
            trades_by_cap[cap] = select_first_qualifying_trades_for_cap(ro_db, groups, cfg, cache)

        all_trades = [t for trades in trades_by_cap.values() for t in trades]
        outcomes_map = attach_outcomes(all_trades, db_path, use_gamma=use_gamma, allow_network=allow_network)

        summaries: "dict[float, dict]" = {}
        for cap in ALL_CAPS:
            trades = trades_by_cap[cap]
            summaries[cap] = {
                "all_cities": summarize_trades(_filtered(trades, all_cities), outcomes_map),
                "promoted_cities": summarize_trades(_filtered(trades, PROMOTED_CITIES), outcomes_map),
            }

        flips: "dict[float, FlipCounts]" = {}
        mech_pass: "dict[float, bool]" = {}
        for cap in CANDIDATE_CAPS:
            flips[cap] = compute_flip_counts(all_keys, trades_by_cap[BASELINE_CAP], trades_by_cap[cap])
            mech_pass[cap] = passes_mechanical_checks(
                summaries[BASELINE_CAP]["all_cities"], summaries[cap]["all_cities"],
            )

        return {
            "n_candidates": len(all_keys),
            "exclusions": exclusions,
            "summaries": summaries,
            "flips": flips,
            "mechanical_pass": mech_pass,
        }
    finally:
        ro_db._conn.close()


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--bracket-evals", type=Path, default=None)
    ap.add_argument("--since", default="2000-01-01", metavar="YYYY-MM-DD",
                     help="Earliest poll date to include (default: all retained history).")
    ap.add_argument("--until", default=ISSUE_1507_T0, metavar="ISO-TS",
                     help="Exclude polls at/after this UTC timestamp (default: #1507's T0).")
    ap.add_argument("--no-gamma", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args(argv)

    from src.config import BRACKET_EVALS_JSONL
    bracket_evals_path = args.bracket_evals or BRACKET_EVALS_JSONL

    result = run(
        args.db, bracket_evals_path, args.since, args.until,
        use_gamma=not args.no_gamma, allow_network=not args.no_network,
    )

    print("# MODEL_PROB_CAP offline evaluation (issue #1490)\n")
    print(f"Candidate population (same-day, high-direction, before {args.until}): {result['n_candidates']}")
    exc = result["exclusions"]
    print(f"Row hygiene: {exc['total_polls']} polls scanned, "
          f"{exc['missing_p_yes_raw']} excluded for missing p_yes_raw, "
          f"{exc['p_yes_raw_zero_not_excluded']} have p_yes_raw==0.0 (envelope.py's certainty "
          "shortcut -- NOT excluded, see module docstring point 3)\n")

    print(f"## Baseline cap={BASELINE_CAP}\n")
    base = result["summaries"][BASELINE_CAP]
    print(" ", _fmt_summary("all 30 cities    ", base["all_cities"]))
    print(" ", _fmt_summary("7 promoted cities", base["promoted_cities"]))

    for cap in CANDIDATE_CAPS:
        print(f"\n## Candidate cap={cap}\n")
        s = result["summaries"][cap]
        print(" ", _fmt_summary("all 30 cities    ", s["all_cities"]))
        print(" ", _fmt_summary("7 promoted cities", s["promoted_cities"]))
        f = result["flips"][cap]
        print(f"  gate-verdict flips vs cap={BASELINE_CAP}: new_qualify={f.new_qualify} "
              f"stopped_qualifying={f.stopped_qualifying} side_switch={f.side_switch} "
              f"({f.total_flips}/{f.total_candidates} = {f.flip_fraction:.2%})")
        mech = result["mechanical_pass"][cap]
        print(f"  mechanical checks (1) total pnl higher AND (2) NO-side pnl not lower: "
              f"{'PASS' if mech else 'FAIL'}")
        if mech:
            print("  -> item (3), the flip-fraction hand-sanity-check, is NOT automated here "
                  "(see module docstring point 9) -- review the flip numbers above before recommending.")

    print("\n## Decision\n")
    any_mech_pass = any(result["mechanical_pass"].values())
    if not any_mech_pass:
        print("NO CHANGE -- no candidate cap passed both mechanical checks (1)/(2) against cap=0.95.")
    else:
        passing = [cap for cap in CANDIDATE_CAPS if result["mechanical_pass"][cap]]
        print(f"INCONCLUSIVE pending human flip-fraction sanity check (item 3) for cap(s): {passing}. "
              "See the flip-count lines above for each.")
    return 0


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
