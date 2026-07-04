"""Promotion prerequisites checker — ensures shadow stations have sufficient data coverage.

Checks 5 gates before promotion to live:
1. Climb-rate history for the station's current month
2. ≥2 distinct forecast models in model_forecast_log (trailing N days)
3. ≥ configurable TAF window count in trailing N days
4. Secondary observation source present (any non-metar source)
5. At least one settled loss in shadow history (pure wins fail)

This module also implements the statistical promotion BAR (issue #559):
``compute_promotion_bar()`` computes, per shadow station+side, the settled
trade count, win rate, Wilson score lower bound, and a cost-aware break-even
win rate derived from the average entry price and the fee model in
``src/strategy/fee.py``. It supersedes the ad-hoc thresholds proposed in
issue #80 (>=5 trades / 100% win rate / >=3 days).

IMPORTANT: this module is ADVISORY TOOLING ONLY. Nothing here auto-promotes a
station, and nothing here is consulted by the live entry gate. It only
reports status for a human (or the dashboard) to act on.
"""
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, date as date_cls

import pytz
from dateutil import parser as dtparse

from src.config import CONFIG_DEFAULTS, STATION_TZ, get_live_config
from src.strategy.fee import estimate_fee_cents

# z-scores are looked up via the inverse normal CDF at call time (see
# _z_for_confidence) so PROMOTION_WILSON_CONFIDENCE can be any value in (0, 1),
# not just the handful of textbook confidence levels.


def check_promotion_prerequisites(
    db, station: str, city: str
) -> dict:
    """Return pass/fail status for each data-coverage prerequisite.

    Args:
        db: Database instance
        station: ICAO code (e.g., "WSSS")
        city: City name (e.g., "Singapore")

    Returns:
        dict with keys:
        - 'climb_rate': bool, True if climb-rate data exists for current month
        - 'model_count': bool, True if ≥2 models in trailing window
        - 'taf_coverage': bool, True if ≥min TAF windows in trailing 30 days
        - 'secondary_obs': bool, True if non-metar observation source exists
        - 'has_settled_loss': bool, True if at least one loss in shadow history
        - 'promotable': bool, True only if all 5 gates pass
        - 'reason': str, human-readable failure reason (empty if promotable)
    """
    # Read env vars with defaults
    min_forecast_models = int(os.getenv("PROMOTION_MIN_FORECAST_MODELS", "2"))
    min_taf_windows = int(os.getenv("PROMOTION_MIN_TAF_WINDOWS", "60"))
    trailing_days = int(os.getenv("PROMOTION_TRAILING_DAYS", "30"))

    result = {
        'climb_rate': False,
        'model_count': False,
        'taf_coverage': False,
        'secondary_obs': False,
        'has_settled_loss': False,
        'promotable': False,
        'reason': '',
    }

    if db is None:
        result['reason'] = 'Database unavailable'
        return result

    # Gate 1: Climb-rate history
    result['climb_rate'] = _check_climb_rate(db, station)

    # Gate 2: ≥2 forecast models
    model_count = _count_distinct_models(db, station, trailing_days)
    result['model_count'] = model_count >= min_forecast_models

    # Gate 3: TAF coverage
    taf_count = _count_taf_windows(db, city, trailing_days)
    result['taf_coverage'] = taf_count >= min_taf_windows

    # Gate 4: Secondary observation source
    result['secondary_obs'] = _has_secondary_observation_source(db, station)

    # Gate 5: Settled loss in shadow history
    result['has_settled_loss'] = _has_settled_loss(db, station)

    # Check if all gates pass
    if all([
        result['climb_rate'],
        result['model_count'],
        result['taf_coverage'],
        result['secondary_obs'],
        result['has_settled_loss'],
    ]):
        result['promotable'] = True
        result['reason'] = ''
    else:
        result['promotable'] = False
        failures = []
        if not result['climb_rate']:
            failures.append('no climb-rate data')
        if not result['model_count']:
            failures.append(f'only {model_count}/{min_forecast_models} models')
        if not result['taf_coverage']:
            failures.append(f'only {taf_count}/{min_taf_windows} TAF windows')
        if not result['secondary_obs']:
            failures.append('no secondary observation source')
        if not result['has_settled_loss']:
            failures.append('no settled loss (pure wins)')
        result['reason'] = 'Failed gates: ' + ', '.join(failures)

    return result


def _check_climb_rate(db, station: str) -> bool:
    """Check if climb-rate data exists for current month."""
    from src.data.climb_lookup import CLIMB_LOOKUP

    month = datetime.now().month
    station_data = CLIMB_LOOKUP.get(station)
    if station_data is None:
        return False
    month_data = station_data.get(month)
    return month_data is not None


def _count_distinct_models(db, station: str, trailing_days: int) -> int:
    """Count distinct models in model_forecast_log over trailing window."""
    since_date = (date_cls.today() - timedelta(days=trailing_days)).isoformat()
    # Use lead_hours=24 slice for consistency with post-#422 schema.
    if hasattr(db, "get_forecast_log_by_lead"):
        log_rows = db.get_forecast_log_by_lead(station, since_date, lead_hours=24)
    else:
        log_rows = db.get_forecast_log(station, since_date)

    distinct_models = set()
    for row in log_rows:
        model = row.get("model")
        if model:
            distinct_models.add(model)

    return len(distinct_models)


def _count_taf_windows(db, city: str, trailing_days: int) -> int:
    """Count TAF windows in trailing days."""
    today = date_cls.today()
    from_ts = (today - timedelta(days=trailing_days)).isoformat()
    to_ts = today.isoformat()

    windows = db.get_taf_windows(city, from_ts, to_ts)
    return len(windows)


def _has_secondary_observation_source(db, station: str) -> bool:
    """Check if any non-metar observation source exists for the station."""
    # Get all observations for the station (search far back to ensure we find any source)
    observations = db.get_observations(station, since="2000-01-01T00:00:00")

    for obs in observations:
        source = obs.get("source", "").lower()
        if source and source != "metar":
            return True

    return False


def _has_settled_loss(db, station: str) -> bool:
    """Check if there's at least one settled loss in the station's history."""
    settlements = db.get_settlements(station, since="2000-01-01T00:00:00")

    all_shadow = db.get_trades(mode="shadow", limit=None)
    shadow_trades = [t for t in all_shadow if t.get("station") == station]

    if not shadow_trades:
        return False

    # Build a map of ticker → settlement result
    settlement_map = {}
    for settlement in settlements:
        ticker = settlement.get("ticker")
        resolved_yes = settlement.get("resolved_yes", 0)
        settlement_map[ticker] = resolved_yes

    # Check if any shadow trade lost
    for trade in shadow_trades:
        ticker = trade["ticker"]
        side = trade["side"]

        if ticker not in settlement_map:
            continue

        resolved_yes = settlement_map[ticker]

        # Determine if this trade lost
        is_loss = False
        if side == "YES" and resolved_yes == 0:
            is_loss = True
        elif side == "NO" and resolved_yes == 1:
            is_loss = True

        if is_loss:
            return True

    return False


# ----------------------------------------------------------------------
# Statistical promotion bar (issue #559)
# ----------------------------------------------------------------------

def _z_for_confidence(confidence: float) -> float:
    """Return the two-sided z-score for *confidence* (e.g. 0.95 -> ~1.96).

    Uses scipy's inverse normal CDF so any confidence level configured via
    PROMOTION_WILSON_CONFIDENCE works, not just the textbook 90/95/99% cases.
    """
    from scipy.stats import norm

    tail = (1.0 - confidence) / 2.0
    return float(norm.ppf(1.0 - tail))


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Return the lower bound of the Wilson score confidence interval.

    This is the standard Wilson score interval for a binomial proportion,
    which (unlike the naive normal approximation) stays well-behaved for
    small *n* and for proportions near 0 or 1 — exactly the regime shadow
    station data lives in.

    Formula (for observed win rate phat = wins/n):
        denom  = 1 + z^2/n
        center = phat + z^2/(2n)
        margin = z * sqrt((phat*(1-phat) + z^2/(4n)) / n)
        lower  = (center - margin) / denom

    Args:
        wins: Number of settled winning trades.
        n: Total number of settled trades.
        z: Two-sided z-score for the desired confidence level (default 1.96,
           i.e. ~95%). Use ``_z_for_confidence()`` to derive this from a
           configured confidence level.

    Returns:
        The lower bound of the confidence interval, in [0, 1]. Returns 0.0
        when n <= 0 (no data — cannot be statistically confident of anything).
    """
    if n <= 0:
        return 0.0

    phat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return (center - margin) / denom


def breakeven_win_rate(avg_entry_price_cents: float) -> float:
    """Return the win rate required to break even at *avg_entry_price_cents*.

    Derivation: a binary contract bought at C cents pays out 100c on a win
    and 0 on a loss; the taker fee F(C) (``src/strategy/fee.py``) is charged
    once, on the entry fill, regardless of outcome. Expected value is zero
    when:

        p*(100 - C - F) - (1-p)*(C + F) = 0
        p*100 - C - F = 0
        p = (C + F) / 100

    So the break-even win rate is simply the all-in entry cost (price + fee)
    expressed as a fraction of the 100c payout — NOT a bare 0.5. This is
    intentionally derived from the real fee model rather than hardcoded, so
    it moves if the fee schedule changes.

    Args:
        avg_entry_price_cents: Average entry price of settled trades, in
            cents (1-99).

    Returns:
        Break-even win probability in [0, 1].
    """
    fee_cents = estimate_fee_cents(avg_entry_price_cents)
    return (avg_entry_price_cents + fee_cents) / 100.0


def _trade_is_win(trade: dict, settlement_map: dict) -> "bool | None":
    """Return True/False if *trade* settled as a win/loss, or None if unsettled."""
    ticker = trade.get("ticker")
    if ticker not in settlement_map:
        return None
    resolved_yes = settlement_map[ticker]
    side = trade.get("side")
    if side == "YES":
        return resolved_yes == 1
    return resolved_yes == 0


def _get_local_date(ts: str, station: str) -> "str | None":
    """Convert timestamp to station-local calendar date (YYYY-MM-DD).

    Returns None if station has no known timezone or parsing fails.
    Falls back to UTC date if conversion fails.
    """
    if station not in STATION_TZ:
        return None

    try:
        t = dtparse.parse(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=pytz.UTC)
        tz = pytz.timezone(STATION_TZ[station])
        return t.astimezone(tz).date().isoformat()
    except (ValueError, OverflowError):
        return None


def compute_promotion_bar(db) -> list:
    """Compute the statistical promotion bar for every shadow station+side.

    ADVISORY ONLY: this never auto-promotes anything and has no effect on the
    live entry gate — it is a read-only report of settled shadow-trade
    statistics against the bar defined in issue #559 (supersedes #80):

        eligible  <=>  n >= PROMOTION_MIN_SETTLED_TRADES
                       AND wilson_lower_bound(wins, n) > breakeven_win_rate(avg_entry)

    Reads live config ONCE up front (not per station+side) to avoid per-call
    DB reads in the scan loop, and fetches all shadow trades / settlements in
    two bulk queries rather than one query per station.

    Args:
        db: Database instance. Returns [] if None.

    Returns:
        A list of dicts, one per (station, side) pair that has at least one
        shadow trade on record, each with:
            station, side, n, wins, win_rate, wilson_lower_bound,
            breakeven_win_rate, avg_entry_price_cents, days_coverage,
            price_valid, eligible, status ('green'|'amber'|'red'), reason
    """
    if db is None:
        return []

    live_cfg = get_live_config(db)
    min_trades = int(live_cfg.get(
        "PROMOTION_MIN_SETTLED_TRADES", CONFIG_DEFAULTS["PROMOTION_MIN_SETTLED_TRADES"]
    ))
    confidence = float(live_cfg.get(
        "PROMOTION_WILSON_CONFIDENCE", CONFIG_DEFAULTS["PROMOTION_WILSON_CONFIDENCE"]
    ))
    min_price_cents = int(live_cfg.get(
        "MIN_PRICE_CENTS", CONFIG_DEFAULTS["MIN_PRICE_CENTS"]
    ))
    z = _z_for_confidence(confidence)

    all_shadow = db.get_trades(mode="shadow", limit=None)
    settlements = db.get_all_settlements(since="2000-01-01T00:00:00")
    settlement_map = {
        s["ticker"]: s.get("resolved_yes", 0) for s in settlements
    }

    # Restrict to direction='high': the bar's truth source (daily high) and
    # settlement path only cover high-side markets. Filtering explicitly here
    # (rather than trusting every row's direction to be correct) keeps this
    # advisory statistic robust to any residual mislabeled rows (issue #610).
    groups: "dict[tuple[str, str], list]" = defaultdict(list)
    for trade in all_shadow:
        if trade.get("direction", "high") != "high":
            continue
        groups[(trade["station"], trade["side"])].append(trade)

    rows = []
    for (station, side) in sorted(groups.keys()):
        trades = groups[(station, side)]
        settled = [t for t in trades if t.get("ticker") in settlement_map]
        n = len(settled)
        wins = sum(1 for t in settled if _trade_is_win(t, settlement_map))

        if n > 0:
            win_rate = wins / n
            wilson_lower = wilson_lower_bound(wins, n, z=z)
            avg_entry = sum(t["actual_price"] for t in settled) / n
            breakeven = breakeven_win_rate(avg_entry)
            price_valid = avg_entry >= min_price_cents
            # Count distinct station-local dates (not UTC dates)
            local_dates = {_get_local_date(t["ts"], station) for t in settled}
            local_dates.discard(None)  # Remove None values from stations without timezone
            days_coverage = len(local_dates)
        else:
            win_rate = 0.0
            wilson_lower = 0.0
            avg_entry = 0.0
            breakeven = 0.0
            price_valid = False
            days_coverage = 0

        clears_bar = n > 0 and wilson_lower > breakeven
        eligible = n >= min_trades and clears_bar

        if n == 0:
            status, reason = "red", "no settled shadow trades yet"
        elif not clears_bar:
            status = "red"
            reason = (
                f"Wilson lower bound {wilson_lower:.3f} does not clear "
                f"break-even {breakeven:.3f} (avg entry {avg_entry:.1f}c)"
            )
        elif n < min_trades:
            status, reason = "amber", f"only {n}/{min_trades} settled trades"
        elif not price_valid:
            status = "amber"
            reason = (
                f"avg entry {avg_entry:.1f}c below MIN_PRICE_CENTS="
                f"{min_price_cents} — shadow data may not reflect live conditions"
            )
        else:
            status, reason = "green", "clears the promotion bar"

        rows.append({
            "station": station,
            "side": side,
            "n": n,
            "wins": wins,
            "win_rate": win_rate,
            "wilson_lower_bound": wilson_lower,
            "breakeven_win_rate": breakeven,
            "avg_entry_price_cents": avg_entry,
            "days_coverage": days_coverage,
            "price_valid": price_valid,
            "eligible": eligible,
            "status": status,
            "reason": reason,
        })

    return rows
