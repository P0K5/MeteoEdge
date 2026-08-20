"""Tests for scripts/market_arb_scan.py — the model-free market-side scan.

The scan exists to answer two questions about the MARKET's own quotes, with no
model input at all: is there a complement arb within a bracket, and is there a
dutch book across a ladder.  Its first live run answered both negatively and
surfaced a third thing nobody was looking for -- `_safe_price`'s silent 0.5
substitution reaching `bracket_evals` (#1028/#1029).

What these tests pin down, in the order the report presents it:

- The complement and ladder arithmetic, including the fact that `no_ask` is
  derived (`yes + no == 100` by construction in Gamma `outcomePrices`), so the
  ladder figure reduces to `sum(yes) - 100`.  A test asserts the reduction
  rather than leaving it as a comment, because the whole first reading of the
  live run turned on missing it.
- Ladder completeness rejection: a gapped ladder is not a dutch book, and #917
  is why gap-free cannot be assumed.
- The 0.5-fallback signature is the EXACT pair (50, 50).  `(50, 49)` is a real
  market and must survive; widening this to "near 50" would silently drop real
  rows from a gate population.
- De-duplication matches the gate's rule -- lowest `minutes_to_settlement` per
  (station, ticker, settlement_date) -- since the whole materiality argument in
  #1028 rests on which fallback rows survive it.
- The scan computes NO score of any kind.  It runs before the pre-registered
  gate, so importing Brier machinery would let a progress check spoil it.  This
  is asserted against the module source, in the manner of
  certainty_exclusion_check's own guard.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "market_arb_scan.py"


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("market_arb_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["market_arb_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


mas = _load_module()


def _ladder(tmp_path, legs, station="KORD", sd="2026-08-10", hour="2026-08-10T14",
            mins=120.0, path=None):
    """Write a contiguous ladder of (yes, no) legs and return the log path."""
    path = path or (tmp_path / "logs" / "bracket_evals.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for i, (yes, no) in enumerate(legs):
            fh.write(json.dumps({
                "station": station, "ticker": f"{station}-{sd}-{i}",
                "settlement_date": sd, "direction": "high",
                "poll_ts": f"{hour}:00:00+00:00",
                "bracket_low": 70.0 + 2 * i, "bracket_high": 70.0 + 2 * (i + 1),
                "yes_ask": yes, "no_ask": no, "minutes_to_settlement": mins,
            }) + "\n")
    return path


# --- complement -------------------------------------------------------------

def test_complement_identity_is_reported_not_mistaken_for_an_arb(tmp_path):
    """Gamma prices sum to 100 by construction; that is not a hit."""
    p = _ladder(tmp_path, [(30, 70), (25, 75), (45, 55)])
    res = mas.scan(None, p)
    assert res["prov"]["complement_exact"] == 3
    assert sum(1 for x in res["comp_gross"] if x > 0) == 0


def test_complement_hit_requires_the_pair_to_sum_below_100(tmp_path):
    p = _ladder(tmp_path, [(30, 69), (25, 75), (45, 55)])
    res = mas.scan(None, p)
    assert sum(1 for x in res["comp_gross"] if x > 0) == 1


# --- ladder -----------------------------------------------------------------

def test_ladder_figure_reduces_to_sum_yes_minus_100(tmp_path):
    """The dutch-book expression is algebraically sum(yes) - 100 whenever
    no == 100 - yes, which is always, absent CLOB enrichment."""
    legs = [(10, 90), (20, 80), (35, 65), (43, 57)]
    p = _ladder(tmp_path, legs)
    lad = mas.scan(None, p)["ladders"][0]
    assert lad["gross"] == sum(y for y, _ in legs) - 100
    assert lad["sum_yes"] == 108


def test_gapped_ladder_is_rejected_not_scored(tmp_path):
    """A gap means the legs are not exhaustive, so N-1 payers does not hold."""
    p = tmp_path / "logs" / "bracket_evals.jsonl"
    p.parent.mkdir(parents=True)
    with open(p, "w") as fh:
        for i, lo in enumerate([70.0, 72.0, 90.0]):     # 74 -> 90 is a gap
            fh.write(json.dumps({
                "station": "KORD", "ticker": f"t{i}", "settlement_date": "2026-08-10",
                "direction": "high", "poll_ts": "2026-08-10T14:00:00+00:00",
                "bracket_low": lo, "bracket_high": lo + 2.0,
                "yes_ask": 30, "no_ask": 70, "minutes_to_settlement": 120.0,
            }) + "\n")
    res = mas.scan(None, p)
    assert res["ladders"] == []
    assert res["incomplete"] == 1


# --- 0.5 fallback (#1028) ---------------------------------------------------

def test_fallback_signature_is_the_exact_pair(tmp_path):
    """(50, 50) is the fabrication; (50, 49) and (49, 50) are real markets.

    Widening this to a tolerance would drop genuine rows from a scored
    population -- the opposite of the bug it is meant to catch.
    """
    p = _ladder(tmp_path, [(50, 50), (50, 49), (49, 50), (51, 49)])
    prov = mas.scan(None, p)["prov"]
    assert prov["fallback_50"] == 1


def test_whole_station_fallback_is_attributed_to_its_poll_hour(tmp_path):
    p = _ladder(tmp_path, [(50, 50)] * 11, station="MPMG", hour="2026-08-15T20")
    res = mas.scan(None, p)
    assert res["fb_by_hour"][("2026-08-15T20", "MPMG")] == 11


def test_dedup_keeps_the_lowest_minutes_row_like_the_gate(tmp_path):
    """#1028's materiality rests on this: de-dup preserves the nearest-to-
    settlement row, which is where a 50c stand-in is most wrong."""
    p = _ladder(tmp_path, [(50, 50)], mins=900.0)
    _ladder(tmp_path, [(3, 97)], mins=45.0, path=p)      # same ticker, later poll
    d = mas.scan(None, p)["dedup"]
    assert d["n"] == 1
    assert d["fb"] == 0                                   # healthy row supersedes it


def test_dedup_reports_a_surviving_fallback(tmp_path):
    p = _ladder(tmp_path, [(50, 50)], mins=45.0)
    _ladder(tmp_path, [(3, 97)], mins=900.0, path=p)      # earlier poll, superseded
    d = mas.scan(None, p)["dedup"]
    assert d["fb"] == 1
    assert d["fb_station_days"] == 1


# --- windowing and pre-registration safety ----------------------------------

def test_since_filters_on_poll_time(tmp_path):
    p = _ladder(tmp_path, [(30, 70)], sd="2026-08-01", hour="2026-08-01T14")
    _ladder(tmp_path, [(30, 70)], sd="2026-08-10", hour="2026-08-10T14", path=p)
    assert mas.scan("2026-08-06", p)["n_rows"] == 1
    assert mas.scan(None, p)["n_rows"] == 2


def test_scan_computes_no_score():
    """The gate is pre-registered and this tool runs before it. A progress
    check must not be able to read a skill score off it."""
    src = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("brier", "BS_model", "BS_market", "bss_market_vs_model"):
        assert forbidden.lower() not in src.lower(), forbidden
    assert "crps" not in src.lower()


def test_report_renders_without_crashing_on_a_minimal_window(tmp_path):
    p = _ladder(tmp_path, [(10, 90), (20, 80), (35, 65), (43, 57)])
    text = mas.report(mas.scan(None, p), None, p)
    assert "Price provenance" in text
    assert "de-duplication" in text
