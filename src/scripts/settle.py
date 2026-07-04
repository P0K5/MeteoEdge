"""Run once a day after NWS publishes the Daily Climate Report (typically ~9am local next day)."""
import csv
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

from src.config import STATIONS, LOG_DIR, CANDIDATES_CSV, SETTLEMENTS_CSV, STATION_TZ, LIVE_TRADES_JSONL
from src.http_client import fetch
from src.data.polymarket import fetch_market_final_price


def _open_db():
    """Return a Database handle, or None if the DB cannot be opened."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        print(f"[settle] DB unavailable: {e} -- skipping DB settlement")
        return None


def _open_db():
    """Return a Database handle, or None if the DB cannot be opened."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        log.warning("[settle] DB unavailable: %s -- skipping DB settlement", e)
        return None


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


def settle_shadow_trades(target: date, truth: dict, db=None) -> None:
    """Settle shadow trades for *target* using real outcomes from *truth*.

    Shadow rows were inserted with ``mode='shadow'``, ``capital_before=0.0``,
    and ``actual_price=ask_cents`` (the observed ask at logging time).
    Settlement uses a $1 notional stake so results are normalised for
    cross-period comparison:

    YES side:
        pnl = (100 - yes_ask) / 100   if YES bracket was hit (won)
        pnl = -(yes_ask) / 100        if YES bracket was missed (lost)

    NO side:
        pnl = (100 - no_ask) / 100    if YES bracket was missed (NO won)
        pnl = -(no_ask) / 100         if YES bracket was hit (NO lost)

    Idempotent: rows already having ``settled_at IS NOT NULL`` are skipped.
    Does not touch risk_manager state — shadow trades are observation-only.

    ``truth`` holds the daily HIGH per station, so rows with ``direction=
    'low'`` are skipped entirely (not settled against the high) rather than
    resolved incorrectly. See issue #610; daily-LOW truth settlement is
    tracked separately under Epic C (#458/#452).
    """
    if db is None:
        return

    rows = db.get_unsettled_shadow_trades(target.isoformat())
    if not rows:
        log.info("[settle] no unsettled shadow trades for %s", target)
        return

    n_settled = 0
    n_skipped_low = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in rows:
        station = r.get("station", "")

        # direction='low' rows cannot be settled against `truth`, which is the
        # daily HIGH (see fetch_daily_climate_high). Settling a low bracket
        # against the high produces near-guaranteed fake wins/losses and
        # poisons shadow statistics (issue #610). Proper low-truth settlement
        # is Epic C scope (#458/#452) — skip these rows here.
        if r.get("direction") == "low":
            n_skipped_low += 1
            log.debug(
                "[settle] [shadow] direction=low — skipping row %s (no daily-LOW truth yet)",
                r["id"],
            )
            continue

        if station not in truth:
            log.debug("[settle] [shadow] no truth for station %s — skipping row %s", station, r["id"])
            continue

        actual = truth[station]
        lo, hi = float(r["bracket_low"]), float(r["bracket_high"])
        yes_won = lo <= actual <= hi
        ask = float(r["actual_price"])  # observed ask stored at insert time

        side = r.get("side", "YES")
        if side == "YES":
            won = yes_won
        else:  # NO
            won = not yes_won

        if won:
            pnl = (100 - ask) / 100
        else:
            pnl = -ask / 100

        try:
            db.update_trade_by_id(
                r["id"],
                outcome="filled",
                pnl=round(pnl, 6),
                capital_after=round(pnl, 6),  # capital_before=0 + pnl
                settled_at=now_iso,
            )
            n_settled += 1
            log.debug(
                "[settle] [shadow] id=%s station=%s side=%s yes_won=%s won=%s pnl=%.4f",
                r["id"], station, side, yes_won, won, pnl,
            )
        except Exception as e:
            log.warning("[settle] [shadow] update failed for row %s: %s", r["id"], e)

    log.info(
        "[settle] settled %s shadow trade(s) for %s (skipped %s direction=low row(s))",
        n_settled, target, n_skipped_low,
    )


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

    db = _open_db()
    settle_live_trades(yesterday, truth, db=db)
    settle_shadow_trades(yesterday, truth, db=db)


def _write_db_settlements(records: list[dict], target: date, truth: dict[str, float], db) -> None:
    """Upsert one settlement row per market resolved on *target*.

    Market identity is the 0x… market hash when available; records written
    with synthetic '{STATION}-order-…' tickers are mapped back to the hash
    via no_token_id when another record for the same market carries it.
    Fetches ``market_final_price`` from the Polymarket Gamma API
    (``outcomePrices[0]`` on the resolved market) so the column is
    always populated instead of being left NULL.
    """
    if db is None:
        return
    from src.data.settlements import SettlementWriter

    hash_by_token = {
        r["no_token_id"]: r["ticker"]
        for r in records
        if r.get("no_token_id") and str(r.get("ticker", "")).startswith("0x")
    }
    writer = SettlementWriter(db)
    seen: set[str] = set()
    for r in records:
        if r.get("end_date", "")[:10] != target.isoformat():
            continue
        if r.get("bracket_low") is None or r.get("bracket_high") is None:
            continue
        station = r.get("station", "")
        if station not in truth:
            continue
        ticker = str(r.get("ticker", ""))
        if ticker.startswith("0x"):
            market_key = ticker
        else:
            market_key = hash_by_token.get(r.get("no_token_id", "")) or r.get("no_token_id", "")
        if not market_key or market_key in seen:
            continue
        seen.add(market_key)
        lo, hi = float(r["bracket_low"]), float(r["bracket_high"])
        actual = truth[station]
        # Fetch the market's final resolved YES price from Polymarket Gamma.
        # Returns None gracefully on network failure or if not yet resolved.
        market_final_price = fetch_market_final_price(market_key) if market_key.startswith("0x") else None
        try:
            writer.record_settlement(
                ticker=market_key,
                station=station,
                bracket_low=lo,
                bracket_high=hi,
                actual_high_f=actual,
                resolved_yes=lo <= actual <= hi,
                market_final_price=market_final_price,
            )
        except Exception as e:
            log.warning("[settle] DB settlement write failed for %s: %s", market_key[:14], e)


def settle_live_trades(target: date, truth: dict[str, float], db=None) -> None:
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

    _write_db_settlements(records, target, truth, db)

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

        if db is not None:
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                db.update_trade_by_order(
                    r.get("order_id") or "",
                    pnl=r["pnl"], settled_at=now_iso,
                )
                db.add_settled_pnl(target.isoformat(), r["pnl"])
            except Exception as e:
                log.warning("[settle] DB trade update failed for %s...: %s",
                            str(r.get("order_id"))[:12], e)

    if n_updated == 0:
        log.info("[settle] no live trades to update for %s", target)
    else:
        with open(LIVE_TRADES_JSONL, "w") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
        log.info("[settle] updated %s live trade(s) with P&L for %s", n_updated, target)

    # Detect stuck trades: live mode, outcome IS NULL, older than 36 hours, no open_position
    if db is not None:
        stuck = db._conn.execute(
            """
            SELECT t.id, t.ticker, t.side, t.ts FROM trades t
            WHERE t.mode = 'live'
              AND t.outcome IS NULL
              AND t.ts < datetime('now', '-36 hours')
              AND NOT EXISTS (SELECT 1 FROM open_positions p WHERE p.trade_id = t.id)
            """
        ).fetchall()
        if stuck:
            log.warning(
                "[settle] %d stuck trade(s) outcome IS NULL > 36h: ids=%s",
                len(stuck), [r['id'] for r in stuck]
            )


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
        _db = _open_db()
        settle_live_trades(target, truth, db=_db)
        settle_shadow_trades(target, truth, db=_db)
    else:
        settle_yesterday()
