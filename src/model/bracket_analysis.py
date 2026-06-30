"""Polymarket bracket mapping: compare per-bracket model probability to market odds.

B2 of epic #510 (Edge Tab). Takes the ensemble distribution produced by
get_ensemble_distribution() (#511) and maps it to the Polymarket temperature
brackets available for a given station/date, returning a per-bracket table of
model probability, market probability, and signed edge.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any

from dateutil import parser as dtparse

from src.data.polymarket import get_weather_markets
from src.model.ensemble_distribution import get_ensemble_distribution
from src.strategy.scanner import (
    is_highest_temp_market,
    parse_bracket_from_market,
    STATION_TO_CITY,
)

log = logging.getLogger(__name__)


def _market_settlement_date(market: dict) -> datetime.date | None:
    """Return the UTC settlement date for a market, or None if unparseable."""
    date_str = (
        market.get("endDate")
        or market.get("end_date_iso")
        or market.get("endDateIso")
        or market.get("close_time")
    )
    if not date_str:
        return None
    try:
        dt = dtparse.parse(str(date_str))
        return dt.date()
    except Exception:
        return None


def _market_yes_prob(market: dict) -> float | None:
    """Extract the current YES probability (0-100) from outcomePrices.

    Polymarket encodes outcomePrices as a JSON string like '["0.65", "0.35"]'
    where index 0 is YES.  Returns a float in [0, 100] or None on failure.
    """
    raw = market.get("outcomePrices")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except Exception:
            return None
    else:
        prices = raw

    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []

    yes_idx = 0
    if outcomes:
        for i, o in enumerate(outcomes):
            if str(o).lower() == "yes":
                yes_idx = i
                break

    try:
        return float(prices[yes_idx]) * 100.0
    except (IndexError, TypeError, ValueError):
        return None


def _model_prob_for_bracket(
    distribution: dict[int, int],
    low_f: float,
    high_f: float,
) -> float | None:
    """Compute model probability (0-100) for a bracket [low_f, high_f).

    The ensemble distribution uses floor(°F) → count of members in that
    integer bin.  We sum counts for all bins whose floor falls within
    [low_f, high_f) and divide by total member count.

    Returns None when the distribution is empty.
    """
    if not distribution:
        return None

    total = sum(distribution.values())
    if total == 0:
        return None

    in_bracket = sum(
        count
        for temp_floor, count in distribution.items()
        if low_f <= temp_floor < high_f
    )
    return (in_bracket / total) * 100.0


def get_bracket_analysis(
    station: str,
    date: datetime.date,
    db=None,
) -> list[dict[str, Any]]:
    """Return a per-bracket comparison table for *station* on *date*.

    Fetches live Polymarket weather markets and filters to those matching
    *station* and *date*. For each bracket, computes the model probability
    derived from the ensemble distribution (B1, #511) and the signed edge
    versus Polymarket's implied probability.

    Args:
        station: METAR station code, e.g. ``"KORD"``.
        date: The target settlement date (UTC).
        db: Optional Database instance passed through to
            ``get_ensemble_distribution()`` for bias-corrected members.

    Returns:
        List of dicts sorted ascending by ``bracket_low``, each containing:

        - ``range`` (str)  — display label, e.g. ``"82–84°F"``
        - ``bracket_low`` (float)  — lower bound in °F
        - ``bracket_high`` (float)  — upper bound in °F
        - ``polymarket_prob`` (float | None)  — market YES probability 0-100,
          or ``None`` when no Polymarket market exists for this bracket.
        - ``model_prob`` (float | None)  — model probability 0-100 derived
          from the ensemble distribution, or ``None`` when no ensemble data.
        - ``edge`` (float | None)  — ``model_prob - polymarket_prob`` in
          percentage points; ``None`` when either side is missing.
    """
    # --- 1. Fetch ensemble distribution (B1) ---
    ensemble = get_ensemble_distribution(station, date, db)
    distribution: dict[int, int] = ensemble.get("distribution", {}) if ensemble else {}

    # --- 2. Fetch and filter Polymarket markets ---
    all_markets = get_weather_markets()

    # Keep only highest-temp markets for this station on this date
    station_markets: list[dict] = []
    for market in all_markets:
        is_temp, mkt_station = is_highest_temp_market(market)
        if not is_temp or mkt_station != station:
            continue
        mkt_date = _market_settlement_date(market)
        if mkt_date != date:
            continue
        station_markets.append(market)

    # --- 3. Parse brackets and compute per-bracket probabilities ---
    # Build a dict keyed by (low_f, high_f) → polymarket_prob
    poly_brackets: dict[tuple[float, float], float | None] = {}
    for market in station_markets:
        bracket = parse_bracket_from_market(market)
        if bracket is None:
            continue
        key = (bracket.low_f, bracket.high_f)
        prob = _market_yes_prob(market)
        poly_brackets[key] = prob

    # Collect all unique bracket keys (union of poly and model coverage)
    # We always surface every Polymarket bracket; model prob may be None.
    all_keys: set[tuple[float, float]] = set(poly_brackets.keys())

    # --- 4. Build result rows ---
    rows: list[dict[str, Any]] = []
    for low_f, high_f in all_keys:
        polymarket_prob = poly_brackets.get((low_f, high_f))

        if distribution:
            model_prob = _model_prob_for_bracket(distribution, low_f, high_f)
        else:
            model_prob = None

        if model_prob is not None and polymarket_prob is not None:
            edge: float | None = model_prob - polymarket_prob
        else:
            edge = None

        # Build a human-readable range label
        if low_f <= -49:
            range_label = f"≤{int(high_f)}°F"
        elif high_f >= 199:
            range_label = f"≥{int(low_f)}°F"
        else:
            range_label = f"{int(low_f)}–{int(high_f)}°F"

        rows.append({
            "range": range_label,
            "bracket_low": low_f,
            "bracket_high": high_f,
            "polymarket_prob": polymarket_prob,
            "model_prob": model_prob,
            "edge": edge,
        })

    rows.sort(key=lambda r: r["bracket_low"])
    return rows
