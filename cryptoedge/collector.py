"""Poll Polymarket crypto up/down markets and record their quotes.

Places NO orders and opens no MeteoEdge database. See README.md.

Cost: ONE Gamma request per cycle covers every asset and window, so the default
15s cadence is ~5,760 requests/day total.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from cryptoedge import db as cdb
from cryptoedge import gamma

log = logging.getLogger("cryptoedge.collector")
_STOP = False


def _sig(_s, _f):
    global _STOP
    _STOP = True
    log.info("signal received -- finishing current cycle then exiting")


def _record_resolution(con, m, now_ms: int) -> None:
    """Settle an ENDED market from Gamma's own outcomePrices.

    Deliberately NOT gated on ``closed``: Gamma settles ``outcomePrices`` to
    ["1","0"] / ["0","1"] several minutes before it flips ``closed=True``
    (observed live 2026-08-31 -- a window 144s past its end still read
    ``closed=False`` with prices already at ["0","1"]). Gating on ``closed``
    therefore captured nothing.

    The gate is instead: the window has ENDED, and the price is decisively at a
    rail. A price still mid-range after the end is left NULL rather than
    guessed -- an ambiguous settlement is not a coin flip. ``resolve.py``
    re-reads those later.
    """
    end = m.get("window_end_ms")
    up = m.get("price_up")
    if up is None or end is None or now_ms <= end:
        return
    resolved = 1 if up > 0.99 else (0 if up < 0.01 else None)
    if resolved is None:
        return
    con.execute(
        "UPDATE resolutions SET resolved_up=?, source='gamma', resolved_at=?"
        " WHERE slug=? AND resolved_up IS NULL",
        (resolved, datetime.now(timezone.utc).isoformat(), m["slug"]))


def poll_once(con, assets=None, windows=None) -> int:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    ts = now.isoformat()
    rows, note = [], ""
    slugs = gamma.window_slugs(sorted(assets), sorted(windows), int(now.timestamp()))
    try:
        markets = gamma.fetch_slugs(slugs)
    except RuntimeError as exc:
        log.warning("[poll] gamma fetch failed: %s", exc)
        con.execute("INSERT OR REPLACE INTO poll_runs VALUES (?,?,?,?)",
                    (ts, 0, 0, str(exc)[:300]))
        con.commit()
        return 0
    for m in markets:
        # A closed market is still recorded: its settled outcomePrices are what
        # resolve.py reads as ground truth. Only the quote columns go stale.
        end = m["window_end_ms"]
        rows.append((
            ts, m["slug"], m["asset"], m["window_min"], m["market_id"],
            m["window_start_ms"], end,
            (end - now_ms) / 1000.0 if end else None,
            m["best_bid"], m["best_ask"], m["spread"],
            m["price_up"], m["price_down"], m["liquidity"], m["volume"],
            m["token_up"], m["token_down"],
        ))
        # INSERT the resolutions row BEFORE settling it -- _record_resolution
        # is an UPDATE and silently no-ops if the row does not exist yet.
        con.execute(
            "INSERT OR IGNORE INTO resolutions"
            " (slug, asset, window_min, window_start_ms, window_end_ms)"
            " VALUES (?,?,?,?,?)",
            (m["slug"], m["asset"], m["window_min"], m["window_start_ms"], end))
        _record_resolution(con, m, now_ms)
    con.executemany(
        "INSERT OR IGNORE INTO quotes (poll_ts, slug, asset, window_min, market_id,"
        " window_start_ms, window_end_ms, seconds_to_settlement, best_bid, best_ask,"
        " spread, price_up, price_down, liquidity, volume, token_up, token_down)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT OR REPLACE INTO poll_runs VALUES (?,?,?,?)",
                (ts, len(rows), 1, note))
    con.commit()
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/cryptoedge.db")
    ap.add_argument("--interval", type=float, default=15.0, help="seconds")
    ap.add_argument("--assets", default="btc,eth", help="comma list")
    ap.add_argument("--windows", default="5,15", help="comma list of minutes")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    assets = {s.strip() for s in a.assets.split(",") if s.strip()}
    windows = {int(w) for w in a.windows.split(",")}
    con = cdb.connect(a.db)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    log.info("collector start db=%s interval=%.1fs assets=%s windows=%s",
             a.db, a.interval, assets or "all", sorted(windows))
    n_cycles = 0
    while not _STOP:
        t0 = time.time()
        try:
            n = poll_once(con, assets, windows)
            n_cycles += 1
            if n_cycles % 40 == 1:
                total = con.execute("SELECT count(*) FROM quotes").fetchone()[0]
                log.info("[poll] %d live markets | %d quotes stored total", n, total)
        except Exception:
            log.exception("[poll] unexpected error -- continuing")
        if a.once:
            break
        time.sleep(max(0.0, a.interval - (time.time() - t0)))
    con.close()
    log.info("collector stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
