"""Run once a day after NWS publishes the Daily Climate Report (typically ~9am local next day)."""
import csv
from datetime import date, timedelta
import httpx
from config import STATIONS, LOG_DIR, CANDIDATES_CSV, SETTLEMENTS_CSV, USER_AGENT


def fetch_daily_climate_high(station: str, target_date: date) -> float | None:
    """
    Pull the NWS Daily Climate Report max temperature.
    The climate product is text; we parse the "MAXIMUM TEMPERATURE" line.
    In practice for the spike, cross-check against METAR 24h max as a fallback.
    """
    # Fallback: use the daily METAR max for the target date as "truth"
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=48"
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        data = r.json() or []
    except Exception as e:
        print(f"[settle] {station} error: {e}")
        return None

    import pytz
    from dateutil import parser as dtparse
    from spike import STATION_TZ
    tz = pytz.timezone(STATION_TZ[station])
    best = None
    for m in data:
        temp_c = m.get("temp")
        obs = m.get("reportTime") or m.get("obsTime")
        if temp_c is None or obs is None:
            continue
        try:
            t = dtparse.parse(obs)
            if t.tzinfo is None:
                t = t.replace(tzinfo=pytz.UTC)
            if t.astimezone(tz).date() != target_date:
                continue
            temp_f = (float(temp_c) * 9 / 5) + 32
            if best is None or temp_f > best:
                best = temp_f
        except Exception:
            continue
    return best


def settle_yesterday():
    """For each candidate from yesterday, record whether it would have won."""
    if not CANDIDATES_CSV.exists():
        print("No candidates to settle.")
        return

    yesterday = date.today() - timedelta(days=1)
    print(f"Settling for {yesterday}")

    # Pull truth per station once
    truth = {}
    for station, _, _, _, _ in STATIONS:
        h = fetch_daily_climate_high(station, yesterday)
        if h is not None:
            truth[station] = h
            print(f"  {station} daily high = {h:.1f}°F")

    # Read candidates, match on date, compute outcomes
    new_file = not SETTLEMENTS_CSV.exists()
    with open(CANDIDATES_CSV) as f_in, open(SETTLEMENTS_CSV, "a", newline="") as f_out:
        reader = csv.DictReader(f_in)
        writer = None
        for row in reader:
            ts = row["ts"][:10]  # YYYY-MM-DD
            if ts != yesterday.isoformat():
                continue
            station = row["station"]
            if station not in truth:
                continue
            actual = truth[station]
            lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
            yes_won = lo <= actual <= hi
            won = yes_won if row["flagged_side"] == "YES" else not yes_won

            # P&L in cents per contract ignoring fees (approximation for the spike)
            if row["flagged_side"] == "YES":
                pnl = (100 - float(row["flagged_price"])) if yes_won else -float(row["flagged_price"])
            else:
                pnl = (100 - float(row["flagged_price"])) if (not yes_won) else -float(row["flagged_price"])

            out = {**row, "actual_high": actual, "yes_won": yes_won,
                   "candidate_won": won, "pnl_cents": round(pnl, 2)}
            if writer is None:
                writer = csv.DictWriter(f_out, fieldnames=list(out.keys()))
                if new_file:
                    writer.writeheader()
            writer.writerow(out)

    print(f"Wrote settlements to {SETTLEMENTS_CSV}")


if __name__ == "__main__":
    settle_yesterday()
