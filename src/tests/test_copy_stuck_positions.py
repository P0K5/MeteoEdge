"""Issue #1301: stuck copy-position escalation, abandon, incomplete denominator."""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.data.db import Database
from src.scripts import copy_abandon
from src.scripts.copy_settle import run_once
from src.scripts.daily_health_report import _build_copy_stuck

W = "0xwallet1"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _seed(db, market="0xm", hours_ago=60.0, address=W, price=0.45, stake=5.0):
    sid = db.insert_copy_signal(address=address, market=market, source_price=price,
                                detected_at=_iso(hours_ago), outcome_index=0)
    pid = db.insert_copy_position(signal_id=sid, address=address, market=market,
                                  outcome_index=0, entry_price=price,
                                  stake_usd=stake, entry_ts=_iso(hours_ago))
    db.link_copy_signal_to_position(sid, pid)
    return pid


def _run(db):
    esc: list = []
    with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=None):
        run_once(db=db, escalations=esc)
    return esc


def _status(db, pid):
    return db._conn.execute("SELECT status FROM copy_positions WHERE id=?", (pid,)).fetchone()[0]


def test_stuck_position_reported_with_record_and_stays_open():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=60)
    esc = _run(db)
    assert len(esc) == 1
    rec = esc[0]
    assert rec["position_id"] == pid and rec["wallet"] == W and rec["market"] == "0xm"
    assert rec["category"] == "AGED_48H" and rec["level"] == 1
    assert 59 < rec["age_hours"] < 61 and rec["entry_price"] == 0.45
    assert _status(db, pid) == "open"


def test_fresh_position_not_reported():
    db = Database(":memory:")
    _seed(db, hours_ago=5)
    assert _run(db) == []


def test_pathological_is_distinct_category():
    db = Database(":memory:")
    _seed(db, hours_ago=24 * 8)
    esc = _run(db)
    assert [e["category"] for e in esc] == ["PATHOLOGICAL_7D"] and esc[0]["level"] == 2


def test_repeated_runs_do_not_reemit_and_escalation_steps_once():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=60)
    assert len(_run(db)) == 1
    assert _run(db) == [] and _run(db) == []
    # ages past the pathological threshold -> exactly one more (new level)
    db._conn.execute("UPDATE copy_positions SET entry_ts=? WHERE id=?", (_iso(24 * 9), pid))
    db._conn.commit()
    assert [e["level"] for e in _run(db)] == [2]
    assert _run(db) == []


def test_no_path_reaches_abandoned_without_operator():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 30)
    for _ in range(5):
        _run(db)
        assert _status(db, pid) == "open"


def test_abandon_is_audited_and_idempotent():
    db = Database(":memory:")
    pid = _seed(db)
    assert copy_abandon.abandon(db, pid, "andre", "UMA stalled", 0.55) is True
    row = dict(db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (pid,)).fetchone())
    assert row["status"] == "abandoned" and row["abandoned_by"] == "andre"
    assert row["abandon_reason"] == "UMA stalled" and row["abandon_observed_price"] == 0.55
    assert row["abandoned_at"] and row["settled_pnl_usd"] is None
    assert copy_abandon.abandon(db, pid, "other", "again") is False
    assert db._conn.execute("SELECT abandoned_by FROM copy_positions WHERE id=?", (pid,)).fetchone()[0] == "andre"
    assert db.get_open_copy_positions() == []


def test_abandon_requires_operator_and_reason_and_valid_price():
    db = Database(":memory:")
    pid = _seed(db)
    with pytest.raises(ValueError):
        db.abandon_copy_position(pid, "", "r", "t")
    with pytest.raises(ValueError):
        db.abandon_copy_position(pid, "op", " ", "t")
    with pytest.raises(ValueError):
        copy_abandon.abandon(db, pid, "op", "r", 1.5)
    assert _status(db, pid) == "open"


def test_abandon_cannot_touch_settled_row():
    db = Database(":memory:")
    pid = _seed(db)
    db.settle_copy_position(pid, 1.0, _iso(0))
    assert copy_abandon.abandon(db, pid, "op", "r") is False
    assert _status(db, pid) == "settled"


def test_per_wallet_pnl_reports_incomplete_denominator():
    db = Database(":memory:")
    done = _seed(db, market="0xdone", hours_ago=300)
    db.settle_copy_position(done, 2.0, _iso(0))
    _seed(db, market="0xstuck", hours_ago=24 * 9, stake=7.0)   # pathological
    _seed(db, market="0xaged", hours_ago=60, stake=3.0)         # aged only: not counted
    ab = _seed(db, market="0xdead", hours_ago=24 * 10)
    db.abandon_copy_position(ab, "op", "dead", _iso(0))
    (row,) = db.get_copy_realized_pnl_by_wallet(W)
    assert row["n_settled"] == 1
    assert row["n_unresolved"] == 1 and row["unresolved_stake_usd"] == 7.0
    assert row["n_abandoned"] == 1


def test_health_report_section_lists_details():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 9)
    lines, flags = _build_copy_stuck(db, datetime.now(timezone.utc))
    text = "\n".join(lines)
    assert flags == {"n_aged": 0, "n_pathological": 1}
    assert f"id={pid}" in text and W in text and "0xm" in text and "PATHOLOGICAL_7D" in text


def test_migration_rebuilds_old_check_and_preserves_rows(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    pid = _seed(db)
    db._conn.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE copy_positions RENAME TO cp_old")
    conn.execute(
        "CREATE TABLE copy_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "signal_id INTEGER NOT NULL REFERENCES copy_signals(id), address TEXT NOT NULL, "
        "market TEXT NOT NULL, outcome_index INTEGER NOT NULL, entry_price REAL NOT NULL, "
        "stake_usd REAL NOT NULL, entry_ts TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settled')), "
        "settled_pnl_usd REAL, settled_at TEXT)"
    )
    conn.execute("INSERT INTO copy_positions (id,signal_id,address,market,outcome_index,"
                 "entry_price,stake_usd,entry_ts) SELECT id,signal_id,address,market,"
                 "outcome_index,entry_price,stake_usd,entry_ts FROM cp_old")
    conn.execute("DROP TABLE cp_old")
    conn.commit()
    conn.close()
    db2 = Database(path)
    assert _status(db2, pid) == "open"
    assert db2.abandon_copy_position(pid, "op", "r", _iso(0)) is True
