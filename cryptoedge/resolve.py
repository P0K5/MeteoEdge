"""Backfill outcomes for windows the live collector did not settle.

The collector settles a window opportunistically on the poll after it ends. Two
gaps remain: a collector restart across a window boundary, and a window whose
price had not yet reached a rail on that one poll. This re-reads them from
Gamma, in batches, and is safe to run repeatedly.

Ground truth is Gamma's settled ``outcomePrices``. A Binance-derived TWAP is a
PROXY only -- these markets resolve on a Chainlink 60s-TWAP stream, and the two
will disagree on some windows. Never write a Binance-derived label here.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from cryptoedge import db as cdb
from cryptoedge import gamma

log = logging.getLogger("cryptoedge.resolve")
BATCH = 20


def backfill(con, max_batches: int = 200, min_age_s: int = 120) -> "tuple[int, int]":
    """Settle pending windows that ended at least *min_age_s* ago.

    Returns (n_settled, n_still_pending). The age floor avoids hammering Gamma
    for a window whose price has not yet moved to a rail.
    """
    cutoff_ms = int((time.time() - min_age_s) * 1000)
    pending = [r["slug"] for r in con.execute(
        "SELECT slug FROM resolutions WHERE resolved_up IS NULL"
        " AND window_end_ms IS NOT NULL AND window_end_ms < ?"
        " ORDER BY window_end_ms", (cutoff_ms,))]
    settled = 0
    for i in range(0, min(len(pending), max_batches * BATCH), BATCH):
        chunk = pending[i:i + BATCH]
        try:
            markets = gamma.fetch_slugs(chunk)
        except RuntimeError as exc:
            log.warning("[resolve] batch failed, stopping: %s", exc)
            break
        for m in markets:
            up = m.get("price_up")
            if up is None:
                continue
            r = 1 if up > 0.99 else (0 if up < 0.01 else None)
            if r is None:
                continue          # still ambiguous -- leave NULL, never guess
            con.execute(
                "UPDATE resolutions SET resolved_up=?, source='gamma', resolved_at=?"
                " WHERE slug=? AND resolved_up IS NULL",
                (r, datetime.now(timezone.utc).isoformat(), m["slug"]))
            settled += 1
        con.commit()
        time.sleep(0.3)
    still = con.execute(
        "SELECT count(*) FROM resolutions WHERE resolved_up IS NULL").fetchone()[0]
    return settled, still


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/cryptoedge.db")
    ap.add_argument("--min-age-s", type=int, default=120)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    con = cdb.connect(a.db)
    n, still = backfill(con, min_age_s=a.min_age_s)
    log.info("[resolve] settled %d | %d still pending", n, still)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
