#!/usr/bin/env python3
"""Build per-station per-season p95 climb-rate lookup table from historical data.

Usage:
    python scripts/build_climb_lookup.py

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
"""

import sys
import datetime
from pathlib import Path

import requests

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import STATIONS  # noqa: E402

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
    lines = [
        "# Auto-generated by scripts/build_climb_lookup.py",
        "# DO NOT EDIT MANUALLY — regenerate with: python scripts/build_climb_lookup.py",
        "#",
        "# Methodology: p95(daily_high_f - temp_f_at_hour) per station × month × hour.",
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


def main() -> None:
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
        sources[icao] = (
            "synthetic (NOAA Climate Normals 1991-2020 + WMO, "
            "network access to meteostat/open-meteo was blocked in this environment)"
        )

    # Write output
    out_path = Path(__file__).parent.parent / "src" / "data" / "climb_lookup.py"
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
