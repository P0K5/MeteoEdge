"""Run once a day after NWS publishes the Daily Climate Report (typically ~9am local next day)."""
import csv
import json
import logging
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

from src.config import STATIONS, LOG_DIR, CANDIDATES_CSV, SETTLEMENTS_CSV, STATION_TZ, LIVE_TRADES_JSONL
from src.http_client import fetch


def fetch_daily_climate_high(station: str, target_date: date) -> float | None:
    """
    Pull the actual daily high for a station on target_date using 48h METAR history.
    The spike uses METAR as a ground-truth proxy; the full build should cross-check
    against the NWS Daily Climate Report.
    """
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=48"
    try:
        r = fetch(url, timeout=20)
        r.raise_for_status()
        data = r.json() or []
    except Exception as e:
        log.warning("[settle] [%s] error: %s", station, e)
        return None

    import pytz
    from dateutil import parser as dtparse
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
        log.info("[settle] No candidates to settle.")
        return

    yesterday = date.today() - timedelta(days=1)
    log.info("[settle] Settling for %s", yesterday)

    truth = {}
    for station, *_ in STATIONS:
        h = fetch_daily_climate_high(station, yesterday)
        if h is not None:
            truth[station] = h
            log.info("  [%s] daily high = %.1f°F", station, h)

    new_file = not SETTLEMENTS_CSV.exists()
    with open(CANDIDATES_CSV) as f_in, open(SETTLEMENTS_CSV, "a", newline="") as f_out:
        reader = csv.DictReader(f_in)
        writer = None
        for row in reader:
            ts = row["ts"][:10]
            if ts != yesterday.isoformat():
                continue
            station = row["station"]
            if station not in truth:
                continue
            actual = truth[station]
            lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
            yes_won = lo <= actual <= hi
            won = yes_won if row["flagged_side"] == "YES" else not yes_won

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

    if writer is not None:
        log.info("[settle] Wrote settlements to %s", SETTLEMENTS_CSV)
    else:
        log.info("[settle] No candidates matched %s in %s -- nothing written.", yesterday, CANDIDATES_CSV)

    settle_live_trades(yesterday, truth)


def settle_live_trades(target: date, truth: dict[str, float]) -> None:
    """Write actual P&L back to live_trades.jsonl for a given date.

    Two cases:
    - outcome='filled' (held to expiry): P&L = full win or full loss based on
      the actual daily high vs. bracket. Reads bracket_low/bracket_high from the
      record; skips old records that predate these fields.
    - outcome='sold' (METAR stop-loss exit): P&L already written at sell time,
      nothing to update. The 'pnl' field is already present.

    Records are rewritten in-place: the file is read entirely, updated in memory,
    then written back. Fine for the small trade volumes we have.
    """
    if not LIVE_TRADES_JSONL.exists():
        log.info("[settle] live_trades.jsonl not found -- skipping financial settlement")
        return

    records: list[dict] = []
    try:
        with open(LIVE_TRADES_JSONL) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError as e:
        log.warning("[settle] could not read live_trades.jsonl: %s", e)
        return

    # Build a set of no_token_ids that were stop-loss exited (already have pnl)
    sold_tokens: set[str] = {
        r.get("no_token_id", "")
        for r in records
        if r.get("outcome") == "sold"
    }

    n_updated = 0
    for r in records:
        if r.get("outcome") != "filled":
            continue
        if r.get("end_date", "")[:10] != target.isoformat():
            continue
        if r.get("bracket_low") is None or r.get("bracket_high") is None:
            continue  # Old record without bracket fields — skip
        if r.get("no_token_id", "") in sold_tokens:
            # Position was exited mid-day; pnl already written in the 'sold' record
            continue
        if "pnl" in r:
            continue  # Already settled

        station = r.get("station", "")
        if station not in truth:
            continue

        actual = truth[station]
        lo, hi = float(r["bracket_low"]), float(r["bracket_high"])
        yes_won = lo <= actual <= hi
        side = r.get("side", "NO")
        price_cents = float(r.get("price_cents", 0))
        size_eur = float(r.get("size_eur", 0))
        shares = size_eur / (price_cents / 100) if price_cents else 0

        if side == "NO":
            # NO wins when bracket is NOT hit
            won = not yes_won
            pnl_per_share_cents = (100 - price_cents) if won else -price_cents
        else:
            won = yes_won
            pnl_per_share_cents = (100 - price_cents) if won else -price_cents

        r["pnl"] = round(pnl_per_share_cents / 100 * shares, 4)
        r["actual_high"] = actual
        r["yes_won"] = yes_won
        n_updated += 1

    if n_updated == 0:
        log.info("[settle] no live trades to update for %s", target)
        return

    with open(LIVE_TRADES_JSONL, "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    log.info("[settle] updated %s live trade(s) with P&L for %s", n_updated, target)


if __name__ == "__main__":
    import sys
    from src.logging_config import setup_logging
    setup_logging()
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
        # Reuse settle_yesterday logic but for arbitrary date
        log.info("[settle] Settling for %s", target)
        truth = {}
        for station, *_ in STATIONS:
            h = fetch_daily_climate_high(station, target)
            if h is not None:
                truth[station] = h
                log.info("  [%s] daily high = %.1f°F", station, h)
        new_file = not SETTLEMENTS_CSV.exists()
        with open(CANDIDATES_CSV) as f_in, open(SETTLEMENTS_CSV, "a", newline="") as f_out:
            reader = csv.DictReader(f_in)
            writer = None
            for row in reader:
                if row["ts"][:10] != target.isoformat():
                    continue
                station = row["station"]
                if station not in truth:
                    continue
                actual = truth[station]
                lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
                yes_won = lo <= actual <= hi
                won = yes_won if row["flagged_side"] == "YES" else not yes_won
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
        if writer:
            log.info("[settle] Wrote settlements to %s", SETTLEMENTS_CSV)
        else:
            log.info("[settle] No candidates matched %s -- nothing written.", target)
        settle_live_trades(target, truth)
    else:
        settle_yesterday()
