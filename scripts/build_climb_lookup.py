#!/usr/bin/env python3
"""Build per-station per-season p95 climb-rate lookup table from historical data.

Usage:
    python scripts/build_climb_lookup.py
    python scripts/build_climb_lookup.py --from-db [--force]

Output:
    src/data/climb_lookup.py  — CLIMB_LOOKUP dict used by envelope model

Data source priority:
    1. meteostat hourly observations (3-5 years)
    2. Open-Meteo archive API (fallback)
    3. Synthetic climatological model (fallback when APIs are unavailable)

Methodology:
    For each station × calendar month × local hour:
        p95(daily_high_f - temp_f_at_that_hour_f)  over all historical days.

    The p95 climb at hour H is the 95th percentile of (daily_high - temp_at_H)
    across all days in the calendar month over the historical period.

    When external APIs are blocked, a synthetic model is used based on:
    - Diurnal amplitude: typical daily temperature swing (°F) = daily_range_p95
    - Diurnal shape: sinusoidal minimum at 05:00 local, maximum at 14:00-15:00
    - The p95 climb from hour H = diurnal_amplitude × (fraction of range still ahead)
    Parameters are sourced from NOAA Climate Normals and WMO climate summaries.

Network requirement:
    - meteostat.net and archive-api.open-meteo.com must be reachable.
    - If blocked (403 / timeout), synthetic fallback is used automatically.

--from-db mode safety:
    When using --from-db to regenerate from accumulated observations, the script
    checks that src/data/climb_lookup.py matches HEAD to ensure the fallback
    baseline (used for sparse cells) is not poisoned by uncommitted changes. If
    the file is dirty:
      - Without --force: aborts with a clear message (restore with
        'git checkout -- src/data/climb_lookup.py' or commit first).
      - With --force: proceeds with a warning (for intentional incremental
        refinement on a reviewed baseline).
    If git is unavailable (not a repo / no git binary), a warning is issued but
    execution proceeds (the check is a safety net, not a hard dependency).
"""

import logging
import subprocess
import sys
import datetime
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import STATIONS  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

YEARS_BACK = 4
MIN_DAYS_PER_CELL = 10
P95_QUANTILE = 0.95

# ---------------------------------------------------------------------------
# Climatological parameters for synthetic fallback
#
# diurnal_p95_by_month: per-station dict[month -> p95_daily_range_f]
#   = the p95 of (daily_high - daily_low) for that calendar month
#   Sourced from NOAA Climate Normals 1991-2020 and WMO climate data.
#
# min_hour: typical hour of daily minimum temperature (local time)
# max_hour: typical hour of daily maximum temperature (local time)
# ---------------------------------------------------------------------------

# fmt: off
_CLIM: dict[str, dict] = {
    "KORD": {  # Chicago O'Hare — continental, large seasonal swings
        "min_hour": 5, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 18.0, 2: 19.0, 3: 22.0, 4: 24.0, 5: 25.0, 6: 25.0,
            7: 24.0, 8: 24.0, 9: 23.0, 10: 22.0, 11: 19.0, 12: 17.0,
        },
    },
    "KMIA": {  # Miami — subtropical, low diurnal range year-round
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 16.0, 2: 17.0, 3: 18.0, 4: 18.0, 5: 17.0, 6: 15.0,
            7: 14.0, 8: 14.0, 9: 14.0, 10: 16.0, 11: 17.0, 12: 16.0,
        },
    },
    "KLAX": {  # Los Angeles — marine influence, moderate range, cool summers
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 18.0, 2: 19.0, 3: 18.0, 4: 20.0, 5: 18.0, 6: 16.0,
            7: 16.0, 8: 16.0, 9: 18.0, 10: 20.0, 11: 19.0, 12: 18.0,
        },
    },
    "KATL": {  # Atlanta — humid subtropical, moderate-large diurnal range
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 20.0, 2: 21.0, 3: 23.0, 4: 24.0, 5: 23.0, 6: 22.0,
            7: 21.0, 8: 21.0, 9: 22.0, 10: 23.0, 11: 21.0, 12: 19.0,
        },
    },
    "KHOU": {  # Houston Hobby — humid subtropical, moderate range
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 19.0, 2: 20.0, 3: 21.0, 4: 22.0, 5: 22.0, 6: 21.0,
            7: 20.0, 8: 20.0, 9: 20.0, 10: 21.0, 11: 20.0, 12: 19.0,
        },
    },
    "RKSI": {  # Seoul Incheon — humid continental, large winter-summer contrast
        "min_hour": 5, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 14.0, 2: 15.0, 3: 18.0, 4: 20.0, 5: 20.0, 6: 16.0,
            7: 11.0, 8: 11.0, 9: 15.0, 10: 18.0, 11: 15.0, 12: 13.0,
        },
    },
    "WMKK": {  # Kuala Lumpur — equatorial, very small diurnal range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 12.0, 2: 13.0, 3: 13.0, 4: 12.0, 5: 12.0, 6: 12.0,
            7: 12.0, 8: 12.0, 9: 12.0, 10: 12.0, 11: 11.0, 12: 11.0,
        },
    },
    "RKPK": {  # Busan — humid continental/oceanic, similar to Seoul but milder
        "min_hour": 5, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 13.0, 2: 14.0, 3: 16.0, 4: 18.0, 5: 18.0, 6: 14.0,
            7: 10.0, 8: 10.0, 9: 13.0, 10: 16.0, 11: 14.0, 12: 12.0,
        },
    },
    "ZGSZ": {  # Shenzhen — subtropical monsoon, moderate range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 14.0, 2: 13.0, 3: 12.0, 4: 11.0, 5: 11.0, 6: 10.0,
            7: 10.0, 8: 10.0, 9: 11.0, 10: 12.0, 11: 13.0, 12: 14.0,
        },
    },
    "WSSS": {  # Singapore Changi — equatorial, very low diurnal range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1:  9.0, 2:  9.0, 3:  9.0, 4:  9.0, 5:  9.0, 6:  9.0,
            7:  9.0, 8:  9.0, 9:  9.0, 10:  9.0, 11:  9.0, 12:  9.0,
        },
    },
    "MPMG": {  # Panama City Albrook — tropical, very low diurnal range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 10.0, 2: 11.0, 3: 11.0, 4: 10.0, 5:  9.0, 6:  9.0,
            7:  9.0, 8:  9.0, 9:  9.0, 10:  9.0, 11:  9.0, 12: 10.0,
        },
    },

    # --- Added for issue #571 (international coverage gap). Parameters are
    # rough per-station climatological estimates (WMO climate normals /
    # regional meteorological-service normals), not fitted from observations.
    # Intended as a per-station-shaped first cut, replacing the single
    # US-continental hand-seeded table — NOT a substitute for the real
    # meteostat/open-meteo/DB-derived p95 values once external network access
    # or accumulated observation history is available in the deploy environment.

    # Europe
    "EGLC": {  # London City — maritime temperate, small-moderate range
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1:  7.2, 2:  9.0, 3: 10.8, 4: 12.6, 5: 14.4, 6: 16.2,
            7: 16.2, 8: 16.2, 9: 14.4, 10: 10.8, 11:  9.0, 12:  7.2,
        },
    },
    "LFPB": {  # Paris Le Bourget — temperate continental-influenced
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1:  9.0, 2: 10.8, 3: 14.4, 4: 18.0, 5: 19.8, 6: 21.6,
            7: 21.6, 8: 21.6, 9: 18.0, 10: 14.4, 11: 10.8, 12:  9.0,
        },
    },
    "LIMC": {  # Milan Malpensa — Po valley continental, larger swings
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 10.8, 2: 12.6, 3: 16.2, 4: 18.0, 5: 19.8, 6: 21.6,
            7: 23.4, 8: 23.4, 9: 19.8, 10: 16.2, 11: 12.6, 12: 10.8,
        },
    },
    "EFHK": {  # Helsinki — cold temperate, long-daylight summer amplitude
        "min_hour": 5, "max_hour": 15,
        "diurnal_p95_by_month": {
            1:  5.4, 2:  7.2, 3:  9.0, 4: 12.6, 5: 16.2, 6: 18.0,
            7: 18.0, 8: 16.2, 9: 12.6, 10:  9.0, 11:  7.2, 12:  5.4,
        },
    },
    "EPWA": {  # Warsaw — continental, larger seasonal contrast
        "min_hour": 5, "max_hour": 15,
        "diurnal_p95_by_month": {
            1:  7.2, 2:  9.0, 3: 12.6, 4: 16.2, 5: 18.0, 6: 19.8,
            7: 19.8, 8: 19.8, 9: 16.2, 10: 12.6, 11:  9.0, 12:  7.2,
        },
    },
    "LTFM": {  # Istanbul (new airport) — Black Sea/Marmara transitional
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1:  9.0, 2:  9.0, 3: 10.8, 4: 12.6, 5: 14.4, 6: 16.2,
            7: 16.2, 8: 16.2, 9: 14.4, 10: 12.6, 11: 10.8, 12:  9.0,
        },
    },
    "LTAC": {  # Ankara — continental plateau/steppe, dry-summer swings
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 12.6, 2: 14.4, 3: 18.0, 4: 19.8, 5: 21.6, 6: 25.2,
            7: 27.0, 8: 27.0, 9: 25.2, 10: 19.8, 11: 16.2, 12: 12.6,
        },
    },

    # Asia / Pacific
    "RJTT": {  # Tokyo Haneda — humid subtropical, coastal-moderated
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 14.4, 2: 14.4, 3: 14.4, 4: 14.4, 5: 12.6, 6: 10.8,
            7: 10.8, 8: 12.6, 9: 12.6, 10: 12.6, 11: 14.4, 12: 14.4,
        },
    },
    "RCSS": {  # Taipei Songshan — subtropical, humid, moderate-small range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 10.8, 2: 10.8, 3: 10.8, 4: 10.8, 5: 10.8, 6: 10.8,
            7: 12.6, 8: 12.6, 9: 12.6, 10: 12.6, 11: 10.8, 12: 10.8,
        },
    },
    "ZSPD": {  # Shanghai Pudong — humid subtropical, moderate range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 12.6, 2: 12.6, 3: 12.6, 4: 12.6, 5: 12.6, 6: 10.8,
            7: 10.8, 8: 10.8, 9: 12.6, 10: 14.4, 11: 14.4, 12: 12.6,
        },
    },
    "ZGGG": {  # Guangzhou — Pearl River Delta, same climate class as ZGSZ
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 14.0, 2: 13.0, 3: 12.0, 4: 11.0, 5: 11.0, 6: 10.0,
            7: 10.0, 8: 10.0, 9: 11.0, 10: 12.0, 11: 13.0, 12: 14.0,
        },
    },
    "ZHHH": {  # Wuhan — Yangtze basin, more continental than coastal China
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 14.4, 2: 14.4, 3: 16.2, 4: 16.2, 5: 16.2, 6: 14.4,
            7: 14.4, 8: 14.4, 9: 16.2, 10: 16.2, 11: 14.4, 12: 14.4,
        },
    },
    "ZSJN": {  # Jinan — North China temperate continental monsoon
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 16.2, 2: 18.0, 3: 19.8, 4: 21.6, 5: 21.6, 6: 19.8,
            7: 16.2, 8: 16.2, 9: 19.8, 10: 19.8, 11: 18.0, 12: 16.2,
        },
    },
    "ZHCC": {  # Zhengzhou — North China temperate continental, similar to Jinan
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 16.2, 2: 18.0, 3: 19.8, 4: 21.6, 5: 21.6, 6: 19.8,
            7: 16.2, 8: 16.2, 9: 18.0, 10: 19.8, 11: 18.0, 12: 16.2,
        },
    },
    "RPLL": {  # Manila — tropical, small range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 10.8, 2: 10.8, 3: 12.6, 4: 12.6, 5: 10.8, 6:  9.0,
            7:  9.0, 8:  9.0, 9:  9.0, 10:  9.0, 11:  9.0, 12: 10.8,
        },
    },

    # MENA
    "LLBG": {  # Tel Aviv — Mediterranean coastal, dry-summer moderate range
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 10.8, 2: 10.8, 3: 12.6, 4: 14.4, 5: 14.4, 6: 14.4,
            7: 12.6, 8: 12.6, 9: 14.4, 10: 14.4, 11: 12.6, 12: 10.8,
        },
    },
    "OEJN": {  # Jeddah — Red Sea coastal desert, clear-sky large range
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 18.0, 2: 18.0, 3: 18.0, 4: 16.2, 5: 16.2, 6: 14.4,
            7: 14.4, 8: 14.4, 9: 14.4, 10: 16.2, 11: 16.2, 12: 18.0,
        },
    },

    # Latin America
    "SBGR": {  # Sao Paulo/Guarulhos — subtropical highland (~750m), moderate range
        "min_hour": 6, "max_hour": 15,
        "diurnal_p95_by_month": {
            1: 16.2, 2: 16.2, 3: 16.2, 4: 16.2, 5: 16.2, 6: 16.2,
            7: 16.2, 8: 18.0, 9: 18.0, 10: 16.2, 11: 16.2, 12: 16.2,
        },
    },

    # Oceania
    "NZWN": {  # Wellington — oceanic temperate, wind-moderated small range
        "min_hour": 6, "max_hour": 14,
        "diurnal_p95_by_month": {
            1: 10.8, 2: 10.8, 3: 10.8, 4:  9.9, 5:  9.0, 6:  9.0,
            7:  9.0, 8:  9.0, 9:  9.9, 10: 10.8, 11: 10.8, 12: 10.8,
        },
    },
}
# fmt: on


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = q * (len(sorted_v) - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sorted_v):
        return sorted_v[-1]
    frac = idx - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


# ---------------------------------------------------------------------------
# meteostat fetch (new meteostat 2.x API)
# ---------------------------------------------------------------------------


def fetch_meteostat(icao: str, lat: float, lon: float,
                    start: datetime.datetime, end: datetime.datetime,
                    tz: str) -> list[dict] | None:
    try:
        import pytz as _tz
        import meteostat
        from meteostat import Point
        location = Point(lat, lon, alt=50)
        data = meteostat.hourly(location, start, end)
        df = data.fetch()
        if df is None or df.empty or "temp" not in df.columns:
            return None
        rows = []
        station_tz = _tz.timezone(tz)
        for ts, row in df.iterrows():
            temp = row["temp"]
            try:
                temp_f = float(temp)
                if temp_f != temp_f:  # NaN
                    continue
            except (TypeError, ValueError):
                continue
            local_dt = ts.to_pydatetime().replace(tzinfo=_tz.utc).astimezone(station_tz)
            rows.append({"local_dt": local_dt, "temp_c": celsius_to_fahrenheit(temp_f)})
        return rows if rows else None
    except Exception as exc:
        print(f"    meteostat error for {icao}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Open-Meteo fallback
# ---------------------------------------------------------------------------


def fetch_open_meteo(icao: str, lat: float, lon: float,
                     start: datetime.datetime, end: datetime.datetime,
                     tz: str) -> list[dict] | None:
    import pytz as _pytz
    station_tz = _pytz.timezone(tz)
    rows: list[dict] = []
    current = start
    while current < end:
        chunk_end = min(current + datetime.timedelta(days=365), end)
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": current.strftime("%Y-%m-%d"),
            "end_date": chunk_end.strftime("%Y-%m-%d"),
            "hourly": "temperature_2m",
            "timezone": "UTC",
        }
        try:
            import requests  # noqa: PLC0415
            resp = requests.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params=params, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            times = data["hourly"]["time"]
            temps = data["hourly"]["temperature_2m"]
            for t_str, temp in zip(times, temps):
                if temp is None:
                    continue
                utc_dt = datetime.datetime.fromisoformat(t_str).replace(
                    tzinfo=datetime.timezone.utc
                )
                local_dt = utc_dt.astimezone(station_tz)
                rows.append({"local_dt": local_dt, "temp_c": float(temp)})
            import time
            time.sleep(0.3)
        except Exception as exc:
            print(f"    open-meteo error for {icao} chunk {current.date()}: {exc}",
                  file=sys.stderr)
            return None  # fail fast — don't retry partial chunks
        current = chunk_end + datetime.timedelta(days=1)
    return rows if rows else None


# ---------------------------------------------------------------------------
# Synthetic fallback
# ---------------------------------------------------------------------------


def compute_synthetic_climb(icao: str) -> dict[int, dict[int, float]]:
    """Generate per-station per-month climb table from climatological parameters.

    Uses a simplified diurnal cycle model:
      temp(h) ≈ daily_mean - (diurnal_range/2) * cos(2π*(h - min_hour)/24)
    so the fraction of diurnal range already realized at hour h is:
      realized(h) = (temp(h) - daily_min) / diurnal_range
                  = (1 - cos(2π*(h - min_hour)/24)) / 2
    and remaining p95 climb from hour h:
      p95_climb(h) = diurnal_range_p95 × (1 - realized(h))
                   = diurnal_range_p95 × (1 + cos(2π*(h - min_hour)/24)) / 2

    Hours at or past max_hour with very small remaining fraction are clipped to 0.
    """
    import math
    params = _CLIM[icao]
    min_hour = params["min_hour"]
    max_hour = params["max_hour"]
    result: dict[int, dict[int, float]] = {}
    for month in range(1, 13):
        diurnal_range = params["diurnal_p95_by_month"][month]
        row: dict[int, float] = {}
        for h in range(24):
            # fraction already realized from daily_min to now
            realized = (1.0 - math.cos(2.0 * math.pi * (h - min_hour) / 24.0)) / 2.0
            remaining_fraction = 1.0 - realized
            climb = diurnal_range * max(0.0, remaining_fraction)
            # After the typical max hour + 1, remaining rise is negligible → 0
            if h >= max_hour + 1:
                climb = 0.0
            # Taper the last hour before max to a small value
            elif h == max_hour:
                climb = min(climb, 1.0)
            row[h] = round(climb, 2)
        result[month] = row
    return result


# ---------------------------------------------------------------------------
# Core computation from real observations
# ---------------------------------------------------------------------------


def compute_p95_climb(rows: list[dict]) -> dict[int, dict[int, float]]:
    from collections import defaultdict
    by_date: dict[datetime.date, list[dict]] = defaultdict(list)
    for row in rows:
        by_date[row["local_dt"].date()].append(row)
    daily_highs: dict[datetime.date, float] = {}
    for d, day_rows in by_date.items():
        temps_f = [r["temp_c"] for r in day_rows]  # already stored as °F in fetch_*
        daily_highs[d] = max(temps_f)
    deltas: dict[tuple[int, int], list[float]] = defaultdict(list)
    for row in rows:
        d = row["local_dt"].date()
        if d not in daily_highs:
            continue
        daily_high_f = daily_highs[d]
        temp_f = row["temp_c"]
        climb = max(0.0, daily_high_f - temp_f)
        month = row["local_dt"].month
        hour = row["local_dt"].hour
        deltas[(month, hour)].append(climb)
    result: dict[int, dict[int, float]] = {}
    for month in range(1, 13):
        result[month] = {}
        for hour in range(24):
            vals = deltas.get((month, hour), [])
            if len(vals) >= MIN_DAYS_PER_CELL:
                result[month][hour] = round(quantile(vals, P95_QUANTILE), 2)
            else:
                result[month][hour] = None  # type: ignore[assignment]
    for month in range(1, 13):
        _fill_missing(result[month])
    return result


def _fill_missing(hour_table: dict[int, float | None]) -> None:
    for h in range(19, 24):
        if hour_table[h] is None:
            hour_table[h] = 0.0
    last_known = None
    for h in range(24):
        if hour_table[h] is not None:
            last_known = hour_table[h]
        elif last_known is not None:
            hour_table[h] = last_known
        else:
            hour_table[h] = 0.0


# ---------------------------------------------------------------------------
# Write output file
# ---------------------------------------------------------------------------


def write_lookup_file(lookup: dict[str, dict[int, dict[int, float]]],
                      sources: dict[str, str],
                      out_path: Path) -> None:
    n_synthetic = sum(1 for src in sources.values() if src.startswith("synthetic"))
    n_derived = len(sources) - n_synthetic
    lines = [
        "# Auto-generated by scripts/build_climb_lookup.py",
        "# DO NOT EDIT MANUALLY — regenerate with: python scripts/build_climb_lookup.py",
        "#",
        "# Methodology: p95(daily_high_f - temp_f_at_hour) per station × month × hour.",
        "#",
        f"# Coverage: {len(sources)}/{len(sources)} configured stations present "
        f"({n_derived} observation-derived, {n_synthetic} synthetic climatological fallback).",
        "# Sample window per station is documented below (either the historical date range /",
        "# DB row count used, or an explicit 'synthetic' marker with the reason). See issue #571:",
        "# this generation extends per-station synthetic climatological coverage to all 30",
        "# configured stations (replacing the flat US-shaped hand-seeded fallback in",
        "# src/model/climb_rates.py for those stations). Re-run with",
        "# `python scripts/build_climb_lookup.py --from-db` once sufficient accumulated",
        "# METAR/city-feed observation history exists per station to replace synthetic cells",
        "# with real p95-derived values (see MIN_DAYS_PER_CELL in this script).",
        "#",
        "# Data sources used (per station):",
    ]
    for station, src in sorted(sources.items()):
        lines.append(f"#   {station}: {src}")
    lines += [
        "#",
        "# Units: °F (all stations — C-unit stations converted to °F before computation).",
        "",
        "CLIMB_LOOKUP: dict[str, dict[int, dict[int, float]]] = {",
    ]
    for station, months in sorted(lookup.items()):
        lines.append(f'    "{station}": {{')
        for month in sorted(months.keys()):
            hour_vals = months[month]
            inner = ", ".join(f"{h}: {hour_vals[h]}" for h in sorted(hour_vals.keys()))
            lines.append(f"        {month}: {{{inner}}},")
        lines.append("    },")
    lines.append("}")
    lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"\nWrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def compute_from_db(
    db_path: str,
    existing_lookup: "dict[str, dict[int, dict[int, float]]]",
) -> "tuple[dict[str, dict[int, dict[int, float]]], dict[str, str]]":
    """Replace synthetic values with p95-derived values from real collected observations.

    For each station in STATIONS, fetches all observations from the DB via
    get_hourly_obs_for_climb and computes p95 climb rates. Cells with fewer than
    MIN_DAYS_PER_CELL distinct dates fall back to the existing synthetic value.

    All binning is done in **station-local time** (issue #587): the consumer,
    expected_additional_rise(), indexes CLIMB_LOOKUP by local month/hour, and
    the daily high must be the high of the local calendar day computed across
    ALL of that day's observations — not per hour-cell, which would measure
    within-hour spread instead of climb-to-end-of-day.

    Args:
        db_path: Path to the SQLite database file.
        existing_lookup: The current (synthetic or previously-built) lookup dict.

    Returns:
        (updated_lookup, sources) where sources maps station → data-source string.
    """
    from collections import defaultdict
    from zoneinfo import ZoneInfo
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.data.db import Database  # noqa: E402

    lookup: "dict[str, dict[int, dict[int, float]]]" = {}
    sources: "dict[str, str]" = {}

    with Database(db_path) as db:
        for icao, _lat, _lon, _city, _res, _unit, _tz in STATIONS:
            obs = db.get_hourly_obs_for_climb(icao)

            if not obs:
                # No DB observations — keep existing synthetic values
                if icao in existing_lookup:
                    lookup[icao] = existing_lookup[icao]
                    sources[icao] = "synthetic (no DB observations)"
                    logger.warning(
                        "%s: no DB observations found — keeping synthetic climatological "
                        "climb table (see src/data/climb_lookup.py header for methodology)",
                        icao,
                    )
                else:
                    logger.warning(
                        "%s: no DB observations AND no existing synthetic entry — station "
                        "will be MISSING from CLIMB_LOOKUP; expected_additional_rise() will "
                        "fall back to the hand-seeded _DEFAULT_CLIMB_LOOKUP for this station",
                        icao,
                    )
                continue

            # Pass 1 — localize every observation to the station timezone and
            # group by LOCAL calendar date. The daily high is the max across
            # ALL of that local date's observations (issue #587: computing it
            # per hour-cell measured within-hour spread, not climb).
            tzinfo = ZoneInfo(_tz)
            by_local_date: "dict[str, list[tuple[int, int, float]]]" = defaultdict(list)
            for row in obs:
                dt = datetime.datetime.fromisoformat(row["ts"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                local = dt.astimezone(tzinfo)
                by_local_date[local.date().isoformat()].append(
                    (local.month, local.hour, row["temp_f"])
                )

            daily_highs: "dict[str, float]" = {
                d: max(t for _, _, t in entries) for d, entries in by_local_date.items()
            }

            # Pass 2 — bin deltas into (local month, local hour) cells.
            cell_deltas: "dict[tuple[int, int], list[float]]" = defaultdict(list)
            cell_dates: "dict[tuple[int, int], set[str]]" = defaultdict(set)
            for d, entries in by_local_date.items():
                for month, hour, temp_f in entries:
                    cell_deltas[(month, hour)].append(max(0.0, daily_highs[d] - temp_f))
                    cell_dates[(month, hour)].add(d)

            cell_p95: "dict[tuple[int, int], float]" = {
                cell: round(quantile(deltas, P95_QUANTILE), 2)
                for cell, deltas in cell_deltas.items()
                if len(cell_dates[cell]) >= MIN_DAYS_PER_CELL
            }

            # Build the updated month×hour table, falling back to synthetic for sparse cells
            synthetic = existing_lookup.get(icao, {})
            updated: "dict[int, dict[int, float]]" = {}
            db_cells = 0
            for month in range(1, 13):
                updated[month] = {}
                for hour in range(24):
                    if (month, hour) in cell_p95:
                        updated[month][hour] = cell_p95[(month, hour)]
                        db_cells += 1
                    else:
                        # Fall back to synthetic for this cell
                        updated[month][hour] = synthetic.get(month, {}).get(hour, 0.0)

            total_cells = 12 * 24
            lookup[icao] = updated
            sources[icao] = (
                f"DB observations ({len(obs):,} rows, "
                f"{db_cells}/{total_cells} cells from DB, rest synthetic fallback)"
            )

            print(f"  [{icao}] {len(obs):,} obs → {db_cells}/{total_cells} cells updated from DB")
            if db_cells < total_cells:
                logger.warning(
                    "%s: only %d/%d month×hour cells have >= %d days of DB history — "
                    "remaining cells use the synthetic climatological fallback",
                    icao, db_cells, total_cells, MIN_DAYS_PER_CELL,
                )

            # Plausibility guard (issue #587): a DB-derived month whose hour-6
            # climb craters below half of BOTH neighbouring months is the
            # signature of a broken computation (this exact pattern shipped
            # once: within-hour spread + UTC binning produced a June column of
            # 0-3.6F between a 15.9F May and 17.7F July). Warn loudly.
            for month in range(1, 13):
                if (month, 6) not in cell_p95:
                    continue  # synthetic cell — not our output to judge
                prev_m = 12 if month == 1 else month - 1
                next_m = 1 if month == 12 else month + 1
                val = updated[month][6]
                prev_v = updated[prev_m][6]
                next_v = updated[next_m][6]
                if prev_v > 0 and next_v > 0 and val < 0.5 * prev_v and val < 0.5 * next_v:
                    logger.warning(
                        "%s: PLAUSIBILITY — DB-derived M%02d hour-6 climb %.1fF is less "
                        "than half of both neighbours (M%02d=%.1fF, M%02d=%.1fF). "
                        "Inspect before committing this table.",
                        icao, month, val, prev_m, prev_v, next_m, next_v,
                    )

    # Ensure every STATIONS entry has an entry in lookup and sources
    for icao, _lat, _lon, _city, _res, _unit, _tz in STATIONS:
        if icao not in lookup:
            if icao in existing_lookup:
                lookup[icao] = existing_lookup[icao]
                sources[icao] = "synthetic (no DB observations)"
            else:
                # Station has neither DB observations nor a synthetic baseline —
                # it is genuinely missing from CLIMB_LOOKUP. Named warning so this
                # is never silently swallowed (issue #571 acceptance criterion).
                logger.warning(
                    "%s: MISSING from CLIMB_LOOKUP (no DB observations, no synthetic "
                    "baseline) — expected_additional_rise() will use the hand-seeded "
                    "_DEFAULT_CLIMB_LOOKUP fallback for this station",
                    icao,
                )

    return lookup, sources


def check_climb_lookup_dirty(force: bool = False) -> None:
    """Check if src/data/climb_lookup.py differs from HEAD.

    Args:
        force: If True, proceed with a warning even if dirty. If False, abort if dirty.

    Raises:
        SystemExit: If the file is dirty and force=False.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", "--", "src/data/climb_lookup.py"],
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            # git error (e.g. exit code 128/129 when run outside a git repo) — the
            # dirty check is a safety net, not a hard dependency, so warn and proceed.
            # (returncode 0 = clean, 1 = dirty; anything else means git could not
            # perform the diff, most commonly "not a git repository".)
            logger.warning(
                "git not available or not in a git repo — skipping dirty-baseline check. "
                "If using --from-db, ensure src/data/climb_lookup.py reflects a clean, "
                "reviewed baseline."
            )
            return
        if result.returncode == 1:
            # File is dirty (git diff --quiet exits with code 1 if differences exist)
            if force:
                logger.warning(
                    "climb_lookup.py has uncommitted changes — proceeding with --force. "
                    "The fallback baseline will use the current dirty state."
                )
            else:
                print(
                    "ERROR: climb_lookup.py has uncommitted changes — the fallback baseline "
                    "would inherit poisoned data.\n\n"
                    "Fix options:\n"
                    "  1. Restore the file: git checkout -- src/data/climb_lookup.py\n"
                    "  2. Commit changes: git add src/data/climb_lookup.py && git commit -m '...'\n"
                    "  3. Override (if you know what you're doing): --force flag\n",
                    file=sys.stderr,
                )
                sys.exit(1)
    except FileNotFoundError:
        # git binary not found
        logger.warning(
            "git not available or not in a git repo — skipping dirty-baseline check. "
            "If using --from-db, ensure src/data/climb_lookup.py reflects a clean, "
            "reviewed baseline."
        )


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build per-station p95 climb-rate lookup table.")
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="Replace synthetic values with p95-derived values from real collected DB observations.",
    )
    parser.add_argument(
        "--db-path",
        default="data/meteoedge.db",
        help="Path to the SQLite database file (default: data/meteoedge.db). Used with --from-db.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed with --from-db even if src/data/climb_lookup.py has uncommitted changes "
             "(for intentional incremental refinement on a reviewed baseline).",
    )
    args = parser.parse_args()

    out_path = Path(__file__).parent.parent / "src" / "data" / "climb_lookup.py"

    if args.from_db:
        check_climb_lookup_dirty(force=args.force)
        # Load existing lookup as synthetic baseline
        from src.data.climb_lookup import CLIMB_LOOKUP as _existing  # noqa: E402
        existing_lookup: "dict[str, dict[int, dict[int, float]]]" = dict(_existing)

        print(f"Loading DB observations from {args.db_path} ...")
        lookup, sources = compute_from_db(args.db_path, existing_lookup)

        write_lookup_file(lookup, sources, out_path)

        # Summary table
        print("\n=== Per-station hour-6 p95 climb (°F) by month (--from-db) ===")
        header = f"{'Station':<8} " + " ".join(f"M{m:02d}" for m in range(1, 13))
        print(header)
        for station in sorted(lookup.keys()):
            months = lookup[station]
            if not months:
                row_str = f"{station:<8} " + " ".join("   ?" for _ in range(1, 13))
            else:
                row_str = f"{station:<8} " + " ".join(
                    f"{months.get(m, {}).get(6, 0.0):4.1f}" for m in range(1, 13)
                )
            print(row_str)
        return

    end_dt = datetime.datetime(datetime.date.today().year, 1, 1)
    start_dt = datetime.datetime(end_dt.year - YEARS_BACK, 1, 1)
    print(f"Fetching {start_dt.year}–{end_dt.year - 1} data for {len(STATIONS)} stations")

    lookup: dict[str, dict[int, dict[int, float]]] = {}
    sources: dict[str, str] = {}

    for icao, lat, lon, city, res_station, unit, tz in STATIONS:
        print(f"\n[{icao}] {city} — trying meteostat ...", end=" ", flush=True)
        rows = fetch_meteostat(icao, lat, lon, start_dt, end_dt, tz)
        if rows:
            print(f"got {len(rows):,} obs via meteostat")
            lookup[icao] = compute_p95_climb(rows)
            sources[icao] = f"meteostat ({len(rows):,} hourly obs, {start_dt.year}-{end_dt.year-1})"
            continue

        print("no meteostat data — trying Open-Meteo ...", end=" ", flush=True)
        rows = fetch_open_meteo(icao, lat, lon, start_dt, end_dt, tz)
        if rows:
            print(f"got {len(rows):,} obs via open-meteo")
            lookup[icao] = compute_p95_climb(rows)
            sources[icao] = f"Open-Meteo archive ({len(rows):,} hourly obs, {start_dt.year}-{end_dt.year-1})"
            continue

        print("API unavailable — using synthetic climatological model")
        lookup[icao] = compute_synthetic_climb(icao)
        source_normals = (
            "NOAA Climate Normals 1991-2020 + WMO" if icao.startswith("K")
            else "WMO Climate Normals / regional meteorological-service normals"
        )
        sources[icao] = (
            f"synthetic ({source_normals}, "
            "network access to meteostat/open-meteo was blocked in this environment)"
        )
        logger.warning(
            "%s: no meteostat/open-meteo access — using synthetic climatological climb "
            "table (%s). Re-run with --from-db once accumulated observation history "
            "exists for this station.",
            icao, source_normals,
        )

    # Write output
    write_lookup_file(lookup, sources, out_path)

    # Summary table
    print("\n=== Per-station hour-6 p95 climb (°F) by month ===")
    header = f"{'Station':<8} " + " ".join(f"M{m:02d}" for m in range(1, 13))
    print(header)
    for station in sorted(lookup.keys()):
        months = lookup[station]
        if not months:
            row_str = f"{station:<8} " + " ".join("   ?" for _ in range(1, 13))
        else:
            row_str = f"{station:<8} " + " ".join(
                f"{months.get(m, {}).get(6, 0.0):4.1f}" for m in range(1, 13)
            )
        print(row_str)

    print("\n=== WSSS vs KORD hour-6 validation ===")
    wsss_h6 = [lookup["WSSS"][m][6] for m in range(1, 13)]
    kord_h6 = [lookup["KORD"][m][6] for m in range(1, 13)]
    print(f"  WSSS avg h6 p95: {sum(wsss_h6)/len(wsss_h6):.2f}°F")
    print(f"  KORD avg h6 p95: {sum(kord_h6)/len(kord_h6):.2f}°F")
    print(f"  WSSS < KORD at all months: {all(w < k for w, k in zip(wsss_h6, kord_h6))}")


if __name__ == "__main__":
    main()
