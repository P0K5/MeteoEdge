"""One-off operational script: explicitly halt live copy-trading order placement.

Context (issue #1176, epic I #1160): `halt_live_trading.py` is the weather
strategy's own human-run emergency stop, but it operates on
``station_overrides`` -- a concept copy-trading doesn't have. Copy-trading's
live kill switch, ``COPY_LIVE_TRADING_ENABLED`` (epic G, #1158/#1163), already
exists and is DB-config-backed (``bot_config`` table), not station-backed, so
this script's entire job is a small, explicit, auditable wrapper: an
immediate human-run way to flip that switch off, via the SAME mechanism the
dashboard's ``PATCH /api/config`` endpoint uses (``Database.get_config`` /
``Database.set_config`` -> ``bot_config`` table), independent of the weather
strategy's own halt tooling.

This does NOT touch ``station_overrides`` or any other weather-strategy
table, and does NOT touch any other copy-trading config key (paper's
``COPY_TRADING_ENABLED``, exposure limits, etc.) -- it reads and writes only
the single ``COPY_LIVE_TRADING_ENABLED`` row, deliberately bypassing
``seed_config()``/``get_live_config()`` (which touch every ``CONFIG_DEFAULTS``
key) to keep the blast radius to exactly one row. It does not place, cancel,
or touch any order or ``copy_live_positions`` row -- only new live signal
execution is gated off (``copy_signal_loop.py`` reads this switch every
cycle); already-open live positions are unaffected and still settle normally.
Trivially reversible (flip the switch back on via the dashboard or
``Database.set_config``).

Usage::

    python -m src.scripts.halt_live_copy_trading --db data/meteoedge.db --dry-run
    python -m src.scripts.halt_live_copy_trading --db data/meteoedge.db
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from src.config import CONFIG_DEFAULTS
from src.data.db import Database

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_KEY = "COPY_LIVE_TRADING_ENABLED"


def halt_live_copy_trading(db: Database, dry_run: bool = False) -> bool:
    """Flip ``COPY_LIVE_TRADING_ENABLED`` to False via the DB config store.

    Returns whether anything changed (or WOULD change, under ``--dry-run``).
    Mirrors ``halt_all_stations()``'s "skip already-halted, return only what
    changed" contract exactly: if the switch is already off (no row, which
    falls back to the ``CONFIG_DEFAULTS`` default of False, or an explicit
    "false" row), this is a no-op that does not touch ``bot_config`` at all --
    no row is created, and an existing row's ``updated_at`` is left alone.

    Never reads or writes any key other than ``COPY_LIVE_TRADING_ENABLED``,
    and never touches ``station_overrides`` or any other table.
    """
    raw = db.get_config(_KEY)
    if raw is None:
        current = CONFIG_DEFAULTS[_KEY]
    else:
        current = raw.lower() in ("true", "1", "yes")

    if not current:
        return False  # already off -- no-op, do not touch the row

    if not dry_run:
        db.set_config(_KEY, "false")
    return True


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True, help="Path to meteoedge.db")
    ap.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")
    args = ap.parse_args(argv)

    if not args.db.exists():
        log.error("[halt-live-copy] no such database: %s", args.db)
        return 1

    db = Database(str(args.db))
    try:
        changed = halt_live_copy_trading(db, dry_run=args.dry_run)
    finally:
        db._conn.close()

    verb = "Would halt" if args.dry_run else "Halted"
    if changed:
        log.info("[halt-live-copy] %s live copy-trading (COPY_LIVE_TRADING_ENABLED -> False).", verb)
    else:
        log.info("[halt-live-copy] no change -- live copy-trading already halted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
