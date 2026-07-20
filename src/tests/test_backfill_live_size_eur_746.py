"""Tests for issue #746: backfill trades.size_eur = capital_before on live rows."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.data.db import Database
from src.scripts.backfill_live_size_eur_746 import backfill


def _seed(db, *, mode, capital_before, size_eur, station="KORD"):
    return db.insert_trade(
        ts="2026-07-01T10:00:00+00:00", station=station,
        ticker=f"T-{station}-{mode}-{size_eur}", bracket_low=32.0, bracket_high=36.0,
        side="NO", predicted_price=70, actual_price=70, predicted_edge=5.0,
        mode=mode, capital_before=capital_before, size_eur=size_eur, outcome="filled",
    )


@pytest.fixture
def seeded(tmp_path):
    dbp = tmp_path / "t.db"
    db = Database(str(dbp))
    ids = {
        "live_null": _seed(db, mode="live", capital_before=5.0, size_eur=None),
        "live_already": _seed(db, mode="live", capital_before=5.0, size_eur=3.0),
        "live_zero_cap": _seed(db, mode="live", capital_before=0.0, size_eur=None),
        "shadow": _seed(db, mode="shadow", capital_before=4.0, size_eur=None),
    }
    db.close()
    return dbp, ids


def _size(db, tid):
    return db._conn.execute("SELECT size_eur FROM trades WHERE id=?", (tid,)).fetchone()[0]


def test_backfills_null_live_row(seeded):
    dbp, ids = seeded
    backfill(Path(str(dbp)), dry_run=False)
    db = Database(str(dbp))
    assert _size(db, ids["live_null"]) == 5.0     # set from capital_before
    assert _size(db, ids["live_already"]) == 3.0  # untouched (already set)
    assert _size(db, ids["live_zero_cap"]) is None  # skipped (capital_before not > 0)
    assert _size(db, ids["shadow"]) is None        # untouched (not mode='live')
    db.close()


def test_idempotent_second_run(seeded):
    dbp, ids = seeded
    backfill(Path(str(dbp)), dry_run=False)
    backfill(Path(str(dbp)), dry_run=False)  # no-op: size_eur no longer NULL
    db = Database(str(dbp))
    assert _size(db, ids["live_null"]) == 5.0
    db.close()


def test_dry_run_changes_nothing(seeded):
    dbp, ids = seeded
    backfill(Path(str(dbp)), dry_run=True)
    db = Database(str(dbp))
    assert _size(db, ids["live_null"]) is None
    db.close()
