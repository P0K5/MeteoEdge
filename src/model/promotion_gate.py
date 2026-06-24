"""Promotion prerequisites checker — ensures shadow stations have sufficient data coverage.

Checks 5 gates before promotion to live:
1. Climb-rate history for the station's current month
2. ≥2 distinct forecast models in model_forecast_log (trailing N days)
3. ≥ configurable TAF window count in trailing N days
4. Secondary observation source present (any non-metar source)
5. At least one settled loss in shadow history (pure wins fail)
"""
import os
from datetime import datetime, timedelta, date as date_cls


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
