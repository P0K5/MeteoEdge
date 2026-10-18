"""Regression test for issue #1471.

The 'shadow' CHECK-upgrade rebuild of ``trades`` in ``Database._migrate`` runs
after the ADD COLUMN migrations, so a pre-'shadow' DB that already carries
close_reason / minutes_to_settlement_at_close / bid_depth_at_close /
is_next_day must keep that data across the rebuild.
"""
import sqlite3

from src.data.db import Database

_OLD_TRADES = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, station TEXT NOT NULL, ticker TEXT NOT NULL,
    bracket_low REAL NOT NULL, bracket_high REAL NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('YES','NO')),
    predicted_price INTEGER NOT NULL, actual_price INTEGER NOT NULL,
    slippage INTEGER, predicted_edge REAL NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('paper','live')),
    order_id TEXT, outcome TEXT, pnl REAL,
    capital_before REAL NOT NULL, capital_after REAL, settled_at TEXT,
    estimated_fee_cents REAL, size_eur REAL,
    close_reason TEXT, minutes_to_settlement_at_close REAL,
    bid_depth_at_close INTEGER,
    direction TEXT NOT NULL DEFAULT 'high', p_yes_raw REAL, end_date TEXT,
    is_next_day INTEGER NOT NULL DEFAULT 0
)
"""


def test_shadow_rebuild_preserves_close_and_next_day_columns(tmp_path):
    path = tmp_path / "t.db"
    Database(path=str(path)).close()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE trades")
    conn.execute(_OLD_TRADES)
    conn.execute(
        "INSERT INTO trades (ts,station,ticker,bracket_low,bracket_high,side,"
        "predicted_price,actual_price,predicted_edge,mode,capital_before,"
        "close_reason,minutes_to_settlement_at_close,bid_depth_at_close,"
        "is_next_day) VALUES ('2025-01-01T00:00:00+00:00','KORD','T',75,77,"
        "'NO',10,12,5.0,'live',100.0,'stop_loss',42.5,7,1)"
    )
    conn.commit()
    conn.close()

    db = Database(path=str(path))
    row = db.get_trades(limit=None)[0]
    assert row["close_reason"] == "stop_loss"
    assert row["minutes_to_settlement_at_close"] == 42.5
    assert row["bid_depth_at_close"] == 7
    assert row["is_next_day"] == 1
    # Assert the restored indexes' actual definitions, not just their names --
    # IF NOT EXISTS would silently no-op against a same-named index left with
    # a stale definition from a different code path.
    index_list = {
        r[1]: {"unique": r[2], "partial": r[4]}
        for r in db._conn.execute("PRAGMA index_list(trades)")
    }
    assert index_list["idx_trades_close_reason"] == {"unique": 0, "partial": 0}
    close_reason_cols = [
        r[2] for r in db._conn.execute("PRAGMA index_info(idx_trades_close_reason)")
    ]
    assert close_reason_cols == ["close_reason"]

    assert index_list["idx_trades_shadow_unique"] == {"unique": 1, "partial": 1}
    shadow_cols = [
        r[2] for r in db._conn.execute("PRAGMA index_info(idx_trades_shadow_unique)")
    ]
    # The trailing None is the substr(ts,1,10) expression term -- SQLite
    # reports expression index terms with cid=-2 and no column name.
    assert shadow_cols == [
        "station", "bracket_low", "bracket_high", "side", "direction", None,
    ]
    db.close()
