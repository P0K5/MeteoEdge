"""One-off operational script: explicitly halt all live order placement.

Context (2026-08-26): M3 resolved negative (BSS=-0.4123, powered), and the
sigma lever -- the plan's one remaining untested lever -- was stood down
(#1048/#1049, ΔBSS=+0.0083, below the pre-registered +0.05 bar; see
`docs/REMEDIATION_PLAN.md`, "RESOLVED 2026-08-26"). Per the decision gate's
own rule, the live thesis has no untested lever left on the current model.

The live path has placed zero trades in weeks in practice (the near-certainty
entry rule is anti-selective against an honestly-calibrated model — the exact
mechanism M4 was scoped to replace), but that has been an emergent property
of the entry gate, never a deliberate, recorded decision. This script makes
the halt explicit and auditable rather than leaving it as an accident of the
gate's own strictness: every known station is flipped to
``yes_enabled=False, no_enabled=False`` via the SAME production mechanism the
`/api/stations/{metar}/toggle` dashboard endpoint uses
(``Database.set_station_override`` -> ``station_overrides`` table ->
``scanner.py``'s ``shadow_yes``/``shadow_no`` gate) — no new code path, no new
kill switch, just the existing per-station override applied to all stations
at once.

This does NOT touch open positions, does NOT change any probability/model
code, and is trivially reversible (re-toggle any station back on via the
dashboard or ``set_station_override``). Shadow logging (candidate evaluation,
`bracket_evals`, EMOS shadow training) is UNCHANGED -- only live order
placement stops.

Usage::

    python -m src.scripts.halt_live_trading --db data/meteoedge.db --dry-run
    python -m src.scripts.halt_live_trading --db data/meteoedge.db
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from src.config import STATIONS
from src.data.db import Database

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def halt_all_stations(db: Database, dry_run: bool = False) -> "list[str]":
    """Set yes_enabled=False, no_enabled=False for every known station.

    Returns the list of station codes changed (or that WOULD be changed,
    under ``--dry-run``). A station already fully disabled is skipped --
    the return value is exactly "what this run changed", not "every
    station", so a second run against an already-halted DB reports an empty
    list rather than re-touching rows that are already correct.
    """
    changed: "list[str]" = []
    for station_tuple in STATIONS:
        station = station_tuple[0]
        current = db.get_station_override(station)
        if current is not None and not current["yes_enabled"] and not current["no_enabled"]:
            continue  # already halted -- do not touch updated_at for no-op rows
        changed.append(station)
        if not dry_run:
            low_no = current["low_no_enabled"] if current is not None else False
            db.set_station_override(station, yes_enabled=False, no_enabled=False, low_no_enabled=low_no)
    return changed


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True, help="Path to meteoedge.db")
    ap.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")
    args = ap.parse_args(argv)

    if not args.db.exists():
        log.error("[halt-live] no such database: %s", args.db)
        return 1

    db = Database(str(args.db))
    try:
        changed = halt_all_stations(db, dry_run=args.dry_run)
    finally:
        db._conn.close()

    verb = "Would halt" if args.dry_run else "Halted"
    if changed:
        log.info("[halt-live] %s live entries for %d station(s): %s", verb, len(changed), ", ".join(changed))
    else:
        log.info("[halt-live] no stations to change -- all already fully halted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
