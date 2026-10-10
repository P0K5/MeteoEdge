"""One-shot correction of the 3 copy_live_positions rows corrupted by
issue #1347 (fixed in #1350).

Background:
    ``LiveTrader.get_order_fill_cost_usd`` summed a trade's top-level
    ``size``/``price`` unconditionally. Those fields belong to the TAKER's
    own order and outcome; when our order instead filled as a MAKER leg
    (the common case -- resting liquidity hit by someone else's taker
    order on the complementary outcome, e.g. our NO @ 0.21 matched against
    their YES @ 0.79), the top-level fields describe the taker's side, not
    ours, and must not be used for our own cost. #1350 fixed this
    forward -- new fills correctly use ``maker_orders[]`` when we were the
    maker -- but it explicitly left the 3 already-corrupted historical
    rows untouched (out of scope per that issue's own acceptance
    criteria). This script applies the correction, now that the true
    values are known precisely.

    Three known rows, all entered 2026-10-09 02:48-02:56 UTC, all settled
    as losses, all on the same live-copy wallet (POLYMARKET_DEPOSIT_WALLET):

        id  | recorded filled_stake_usd | true filled_stake_usd | source of truth
        ----|----------------------------|------------------------|------------------
        334 | 11.2891                    | 3.0009                  | Data API trades (14 + 0.29 shares @ our own 0.21) AND #1350's own authenticated get_trades() reproduction
        336 | 6.0903                     | 2.9997                  | #1350's own authenticated get_trades() reproduction (9.09 shares @ our own 0.33)
        338 | 11.2891                    | 3.0009                  | Data API trades (14 + 0.29 shares @ our own 0.21) AND #1350's own authenticated get_trades() reproduction

    Since all three settled as losses, ``settled_pnl_usd`` is corrected to
    exactly ``-filled_stake_usd`` (``compute_realized_pnl_usd``'s own
    documented loss formula -- see ``src/data/copy_pnl.py``).

Safety:
    - Each row is matched against its EXPECTED (recorded, corrupted)
      ``filled_stake_usd``/``settled_pnl_usd``/``status`` before being
      touched. A row that doesn't match exactly (e.g. already corrected,
      or changed for an unrelated reason) is skipped with a loud warning,
      never guessed at or force-overwritten.
    - Idempotent: a row already carrying the CORRECTED values is detected
      and skipped on a second run -- safe to run twice.
    - A missing row id is reported and skipped, not treated as fatal.
    - Live (writing) by default, matching this directory's existing
      one-shot-correction convention (see
      ``quarantine_mislabeled_shadow_trades.py``) -- pass --dry-run to
      preview first, which operators should always do before the real run.

Usage (run against the production DB by the operator; not run in CI):
    python -m src.scripts.correct_1347_maker_leg_fill_cost --dry-run  # preview only
    python -m src.scripts.correct_1347_maker_leg_fill_cost            # live run
    python -m src.scripts.correct_1347_maker_leg_fill_cost --db-path /path/to/meteoedge.db

DB path resolution:
    Honors the DB_PATH environment variable, matching the canonical DB
    layer at src/data/db.py. --db-path overrides the env var if passed.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

_DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))

# (id, expected_recorded_filled_stake_usd, expected_recorded_settled_pnl_usd,
#  corrected_filled_stake_usd) -- corrected settled_pnl_usd is always
# -corrected_filled_stake_usd since all 3 rows are losses.
_ROWS_TO_CORRECT = [
    (334, 11.2891, -11.2891, 3.0009),
    (336, 6.0903, -6.0903, 2.9997),
    (338, 11.2891, -11.2891, 3.0009),
]

_EPSILON = 1e-4


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def correct(db_path: Path, dry_run: bool = False) -> int:
    """Correct the 3 known rows. Returns a process exit code."""
    if not db_path.exists():
        print(f"[correct-1347] DB not found: {db_path}")
        return 1

    conn = _connect(db_path)

    n_fixed = 0
    n_already_done = 0
    n_missing = 0
    n_mismatched = 0

    for row_id, exp_filled, exp_pnl, new_filled in _ROWS_TO_CORRECT:
        new_pnl = -new_filled
        row = conn.execute(
            "SELECT id, status, filled_stake_usd, stake_usd, settled_pnl_usd "
            "FROM copy_live_positions WHERE id=?",
            (row_id,),
        ).fetchone()

        if row is None:
            print(f"[correct-1347] id={row_id}: NOT FOUND -- skipping")
            n_missing += 1
            continue

        already_done = (
            row["status"] == "settled"
            and row["filled_stake_usd"] is not None
            and abs(float(row["filled_stake_usd"]) - new_filled) < _EPSILON
            and row["settled_pnl_usd"] is not None
            and abs(float(row["settled_pnl_usd"]) - new_pnl) < _EPSILON
        )
        if already_done:
            print(f"[correct-1347] id={row_id}: already corrected -- skipping")
            n_already_done += 1
            continue

        matches = (
            row["status"] == "settled"
            and row["filled_stake_usd"] is not None
            and abs(float(row["filled_stake_usd"]) - exp_filled) < _EPSILON
            and row["settled_pnl_usd"] is not None
            and abs(float(row["settled_pnl_usd"]) - exp_pnl) < _EPSILON
        )
        if not matches:
            print(
                f"[correct-1347] id={row_id}: MISMATCH -- expected recorded "
                f"status=settled filled_stake_usd={exp_filled} settled_pnl_usd={exp_pnl}; "
                f"found status={row['status']} filled_stake_usd={row['filled_stake_usd']} "
                f"settled_pnl_usd={row['settled_pnl_usd']}. Refusing to touch this row."
            )
            n_mismatched += 1
            continue

        action = "[dry-run] would set" if dry_run else "setting"
        print(
            f"[correct-1347] id={row_id}: {action} filled_stake_usd={new_filled} "
            f"(was {row['filled_stake_usd']}), settled_pnl_usd={new_pnl} "
            f"(was {row['settled_pnl_usd']})"
        )
        if not dry_run:
            conn.execute(
                "UPDATE copy_live_positions SET filled_stake_usd=?, settled_pnl_usd=? "
                "WHERE id=?",
                (new_filled, new_pnl, row_id),
            )
        n_fixed += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(
        f"\n[correct-1347] Summary: {n_fixed} {'would be ' if dry_run else ''}corrected, "
        f"{n_already_done} already done, {n_missing} not found, {n_mismatched} mismatched"
    )
    if n_mismatched:
        print("[correct-1347] FAILED: mismatched row(s) found -- investigate before re-running")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Correct the 3 copy_live_positions rows (334, 336, 338) whose "
            "filled_stake_usd/settled_pnl_usd were inflated by the issue "
            "#1347 maker/taker leg bug (fixed forward by #1350) to their "
            "true, on-chain-verified values."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would change without modifying the DB",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite DB (default: {_DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()
    raise SystemExit(correct(args.db_path, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
