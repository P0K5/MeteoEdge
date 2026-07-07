"""Run once a day after NWS publishes the Daily Climate Report (typically ~9am local next day)."""
import csv
import gzip
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

from src.config import STATIONS, LOG_DIR, CANDIDATES_CSV, SETTLEMENTS_CSV, STATION_TZ, LIVE_TRADES_JSONL
from src.http_client import fetch
from src.data.polymarket import fetch_market_final_price, fetch_market_resolution
from src.utils.log_rotation import iter_rotated_jsonl, rotated_sources

# How many days back each settle run re-checks unsettled rows. Markets that
# have not resolved on Polymarket by the 12:00 UTC run (the common case — UMA
# resolution lands hours later) stay pending and are retried on every
# subsequent daily run within this window (issue #644).
SETTLE_LOOKBACK_DAYS = int(os.getenv("SETTLE_LOOKBACK_DAYS", "14"))


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

    Resolution policy (issue #644): rows with a real 0x market hash settle
    ONLY from the definitive Gamma resolution — a market that has not
    resolved yet stays unsettled and is retried on later runs (rows up to
    SETTLE_LOOKBACK_DAYS old are re-selected each run). METAR truth settled
    trades with the wrong sign ~22% of the time and is now reserved for
    legacy synthetic-ticker rows, which have no market to query.
    """
    if db is None:
        return

    rows = db.get_unsettled_shadow_trades(
        target.isoformat(), lookback_days=SETTLE_LOOKBACK_DAYS
    )
    if not rows:
        log.info("[settle] no unsettled shadow trades for %s", target)
        return

    n_settled = 0
    n_skipped_low = 0
    n_pending = 0
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

        ticker = str(r.get("ticker") or "")
        if ticker.startswith("0x"):
            # Real market: the on-chain resolution is the only accepted truth.
            yes_won = fetch_market_resolution(ticker)
            if yes_won is None:
                n_pending += 1
                log.debug(
                    "[settle] [shadow] market %s... not resolved yet — row %s stays pending",
                    ticker[:14], r["id"],
                )
                continue
            resolution_source = "gamma"
        else:
            # Legacy synthetic ticker: no market to query — METAR truth is the
            # only option, and only for the run's own target date.
            if r.get("ts", "")[:10] != target.isoformat() or station not in truth:
                n_pending += 1
                continue
            actual = truth[station]
            lo, hi = float(r["bracket_low"]), float(r["bracket_high"])
            # C-bucket bracket: top edge is exclusive [lo, hi)
            yes_won = lo <= actual < hi
            resolution_source = "metar"

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
                "[settle] [shadow] id=%s station=%s side=%s yes_won=%s won=%s pnl=%.4f"
                " source=%s",
                r["id"], station, side, yes_won, won, pnl, resolution_source,
            )
        except Exception as e:
            log.warning("[settle] [shadow] update failed for row %s: %s", r["id"], e)

    log.info(
        "[settle] settled %s shadow trade(s) for %s (%s pending resolution, "
        "skipped %s direction=low row(s))",
        n_settled, target, n_pending, n_skipped_low,
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
        # Prefer the definitive market resolution over METAR truth (#644):
        # the METAR-derived comparison disagreed with the official resolution
        # in ~22% of audited settlements.
        if market_final_price is not None and (market_final_price >= 95 or market_final_price <= 5):
            resolved_yes = market_final_price >= 95
            resolution_source = "gamma"
        else:
            resolved_yes = lo <= actual <= hi
            resolution_source = "metar"
        try:
            writer.record_settlement(
                ticker=market_key,
                station=station,
                bracket_low=lo,
                bracket_high=hi,
                actual_high_f=actual,
                resolved_yes=resolved_yes,
                market_final_price=market_final_price,
                resolution_source=resolution_source,
            )
        except Exception as e:
            log.warning("[settle] DB settlement write failed for %s: %s", market_key[:14], e)


def resolve_trade_date(row: dict) -> "date | None":
    """Return the settlement date a live trade row belongs to, or None if unknown.

    Prefers the row's ``end_date`` column (populated at insert time from the
    market's endDate, see #609). Legacy rows written before that migration
    have ``end_date IS NULL`` and fall back to the station-local calendar date
    of ``ts`` (same STATION_TZ + pytz conversion used by fetch_daily_climate_high).
    """
    end_date = row.get("end_date")
    if end_date:
        try:
            return date.fromisoformat(str(end_date)[:10])
        except ValueError:
            pass

    station = row.get("station") or ""
    ts = row.get("ts") or ""
    if not station or not ts or station not in STATION_TZ:
        return None

    import pytz
    from dateutil import parser as dtparse
    try:
        tz = pytz.timezone(STATION_TZ[station])
        t = dtparse.parse(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=pytz.UTC)
        return t.astimezone(tz).date()
    except (ValueError, OverflowError):
        return None


def _enrich_jsonl_with_settlements(patches: dict[str, dict]) -> None:
    """Best-effort: write pnl/actual_high/yes_won back into the rotated JSONL
    record matching each order_id, so the dashboard's closed-positions panel
    (src/dashboard/api.py:_settled_jsonl_positions, which still reads
    live_trades.jsonl rather than the DB) can display DB-settled trades.

    Patches the FIRST outcome='filled' record with a matching order_id and no
    existing 'pnl' field across every rotated source (plain + dated .jsonl +
    .jsonl.gz). Unmatched/unparseable lines are preserved verbatim -- this
    never drops data the way the old whole-file rewrite could.

    Never raises: DB settlement (the source of truth as of #609) must succeed
    regardless of JSONL write-back failures.
    """
    if not patches:
        return
    remaining = set(patches)
    try:
        for path in rotated_sources(LIVE_TRADES_JSONL):
            if not remaining:
                break
            is_gz = path.suffix == ".gz"
            try:
                opener = gzip.open(path, "rt", encoding="utf-8") if is_gz else open(path, "r", encoding="utf-8")
                with opener as f:
                    lines = f.readlines()
            except OSError as e:
                log.warning("[settle] jsonl enrichment: could not read %s: %s", path, e)
                continue

            changed = False
            new_lines = []
            for line in lines:
                stripped = line.strip()
                rec = None
                if stripped:
                    try:
                        rec = json.loads(stripped)
                    except json.JSONDecodeError:
                        rec = None
                order_id = rec.get("order_id") if rec else None
                if (
                    rec is not None
                    and order_id in remaining
                    and rec.get("outcome") == "filled"
                    and "pnl" not in rec
                ):
                    rec.update(patches[order_id])
                    new_lines.append(json.dumps(rec, default=str) + "\n")
                    remaining.discard(order_id)
                    changed = True
                else:
                    new_lines.append(line)

            if not changed:
                continue
            try:
                writer = gzip.open(path, "wt", encoding="utf-8") if is_gz else open(path, "w", encoding="utf-8")
                with writer as f:
                    f.writelines(new_lines)
            except OSError as e:
                log.warning("[settle] jsonl enrichment: could not write %s: %s", path, e)
    except Exception as e:
        log.warning("[settle] jsonl enrichment failed (non-fatal): %s", e)


def settle_live_trades(target: date, truth: dict[str, float], db=None) -> None:
    """Settle live held-to-expiry trades for *target* from the DB ``trades`` table.

    Issue #609: this used to rewrite ``live_trades.jsonl`` in place, but that
    file has been frozen since log rotation moved writes to dated files
    (``live_trades.YYYY-MM-DD.jsonl``), so every run silently settled nothing.
    The DB is now the source of truth, mirroring settle_shadow_trades():

    - Selects live ``outcome='filled'`` rows with ``settled_at IS NULL`` via
      ``db.get_unsettled_live_trades()`` and filters to rows dated within
      ``[target - SETTLE_LOOKBACK_DAYS, target]`` in Python via
      resolve_trade_date() (end_date column, or station-local ts fallback
      for legacy rows). Rows whose market has not definitively resolved on
      Polymarket stay unsettled and are retried on every later run inside
      the window (issue #644) — METAR truth is never substituted for a real
      0x market, because it disagreed with the official resolution in ~22%
      of audited settlements.
    - PnL: entry price = ``actual_price`` cents, shares = capital_before /
      (actual_price/100) -- capital_before holds the EUR size committed at
      order placement (the live-trade equivalent of the old JSONL
      'size_eur' field; see LiveTrader.place_order). Full win pays
      (100 - actual_price) per share, full loss pays -actual_price per share;
      NO wins when the bracket is NOT hit. Matches the pre-#609 JSONL formula.
    - Sold (early-exit) rows are never touched here: order_manager's
      _record_sell_in_db() flips the SAME row's outcome to 'sold' (matched by
      order_id) and sets settled_at at sell time, so get_unsettled_live_trades()
      (outcome='filled') naturally excludes them -- there is no separate row
      per order_id to double-settle.
    - Cleans up the row's open_positions entry (matched by order_id, which is
      shared between the trades row and its open_positions row) so resolved
      markets don't linger as "open" after settlement.

    ``live_trades.jsonl`` is enrichment only, never the source of truth:
    - _write_db_settlements() (the `settlements` table writer) is fed from
      iter_rotated_jsonl() so it still sees rotated per-day files; any read
      or write failure there is logged and swallowed, never blocking DB
      settlement below.
    - After DB settlement, pnl/actual_high/yes_won are best-effort patched
      back into the matching rotated JSONL record (by order_id) so the
      dashboard's closed-positions panel, which still reads live_trades.jsonl,
      can display these trades. See _enrich_jsonl_with_settlements().
    """
    try:
        records = list(iter_rotated_jsonl(LIVE_TRADES_JSONL))
    except Exception as e:
        log.warning("[settle] could not read rotated live_trades JSONL: %s", e)
        records = []

    try:
        _write_db_settlements(records, target, truth, db)
    except Exception as e:
        log.warning("[settle] _write_db_settlements failed (non-fatal): %s", e)

    if db is None:
        log.info("[settle] DB unavailable -- skipping live trade settlement for %s", target)
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    n_settled = 0
    n_pending = 0
    skip_reasons: list[str] = []
    jsonl_patches: dict[str, dict] = {}
    earliest = target - timedelta(days=SETTLE_LOOKBACK_DAYS)

    for r in db.get_unsettled_live_trades():
        row_date = resolve_trade_date(r)
        # Issue #644: process every overdue unsettled row within the lookback
        # window, not just the target date. Markets that had not resolved by
        # an earlier run get retried here until they resolve or age out.
        if row_date is None or row_date > target or row_date < earliest:
            continue

        row_id = r.get("id")
        if r.get("direction", "high") == "low":
            # Live trading only ever executes direction='high' candidates
            # (low-side is shadow-only, see scanner.py) but guard defensively
            # against settling a low-market row against the daily HIGH truth
            # (same hazard fixed for shadow rows in #610).
            skip_reasons.append(f"id={row_id} direction=low")
            continue
        if r.get("bracket_low") is None or r.get("bracket_high") is None:
            skip_reasons.append(f"id={row_id} missing bracket")
            continue

        station = r.get("station", "")
        ticker = str(r.get("ticker") or "")
        if ticker.startswith("0x"):
            # Real market: settle ONLY from the definitive on-chain resolution.
            # METAR truth booked the wrong sign in 28/126 audited settlements
            # (#644) because the 12:00 UTC run predates UMA resolution — a
            # not-yet-resolved market must stay pending, never be guessed.
            yes_won = fetch_market_resolution(ticker)
            if yes_won is None:
                n_pending += 1
                log.debug(
                    "[settle] [live] market %s... not resolved yet — row %s stays pending",
                    ticker[:14], row_id,
                )
                continue
            actual = truth.get(station)  # may be None; only needed for JSONL patch
            resolution_source = "gamma"
        else:
            # Legacy synthetic ticker: no market to query. METAR truth is the
            # only option, and only for the run's own target date (truth is
            # fetched for that date alone — reusing it for older rows is the
            # stale-truth bug this issue fixes).
            if row_date != target:
                skip_reasons.append(f"id={row_id} legacy ticker, no truth for {row_date}")
                continue
            if station not in truth:
                skip_reasons.append(f"id={row_id} no truth for {station}")
                continue
            actual = truth[station]
            lo, hi = float(r["bracket_low"]), float(r["bracket_high"])
            # C-bucket bracket: top edge is exclusive [lo, hi)
            yes_won = lo <= actual < hi
            resolution_source = "metar"

        side = r.get("side", "NO")
        price_cents = float(r.get("actual_price") or 0)
        # capital_before is the EUR amount committed at order placement --
        # the DB equivalent of the JSONL 'size_eur' field (see
        # LiveTrader.place_order, which sets capital_before=size_usdc).
        size_eur = float(r.get("capital_before") or 0)
        shares = size_eur / (price_cents / 100) if price_cents else 0

        # NO wins when the bracket is NOT hit; YES wins when it is.
        won = (not yes_won) if side == "NO" else yes_won
        pnl_per_share_cents = (100 - price_cents) if won else -price_cents
        pnl = round(pnl_per_share_cents / 100 * shares, 4)

        try:
            db.update_trade_by_id(row_id, pnl=pnl, settled_at=now_iso)
            # Credit the row's OWN trade date, not the run's target — overdue
            # rows settled late must not distort a different day's PnL.
            db.add_settled_pnl(row_date.isoformat(), pnl)
            order_id = r.get("order_id")
            if order_id:
                db.close_position(order_id)  # drop the resolved open_positions row
                jsonl_patches[order_id] = {
                    "pnl": pnl, "actual_high": actual, "yes_won": yes_won,
                }
            n_settled += 1
            log.debug(
                "[settle] [live] id=%s station=%s side=%s yes_won=%s won=%s pnl=%.4f"
                " source=%s",
                row_id, station, side, yes_won, won, pnl, resolution_source,
            )
        except Exception as e:
            skip_reasons.append(f"id={row_id} update failed: {e}")
            log.warning("[settle] [live] DB update failed for row %s: %s", row_id, e)

    _enrich_jsonl_with_settlements(jsonl_patches)

    if n_settled == 0 and n_pending == 0 and not skip_reasons:
        log.info("[settle] no live trades to update for %s", target)
    else:
        log.info(
            "[settle] settled %s live trade(s) from DB for %s (%s pending resolution, %s skipped%s)",
            n_settled, target, n_pending, len(skip_reasons),
            f": {'; '.join(skip_reasons)}" if skip_reasons else "",
        )

    # Detect stuck trades: live mode, outcome IS NULL, older than 36 hours, no open_position
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
