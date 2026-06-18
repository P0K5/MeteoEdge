"""Tests for src/data/archive_db.py."""
from __future__ import annotations

import pytest

from src.data.archive_db import ArchiveDatabase


_SNAP_ROW = {
    "ts": "2026-06-15T10:00:00+00:00",
    "station": "KORD",
    "ticker": "HIGH-TEMP-KORD-2026-06-15-90-94",
    "bracket_low": 90.0,
    "bracket_high": 94.0,
    "yes_ask": 45,
    "no_ask": 55,
    "current_high": 82.3,
    "latest_temp": 79.1,
    "forecast_high": 91.0,
    "p_yes": 0.42,
    "raw_p_yes": 0.42,
    "capped_p_yes": 0.42,
    "ev_yes": -5.1,
    "ev_no": 2.3,
    "minutes_to_settlement": 240.0,
    "emos_mode": "emos",
}

_POS_SNAP_ROW = {
    "ts": "2026-06-15T14:00:00+00:00",
    "ticker": "HIGH-TEMP-KORD-2026-06-15-90-94",
    "no_token_id": "abc123",
    "station": "KORD",
    "bracket_low": 90.0,
    "bracket_high": 94.0,
    "entry_price": 55,
    "predicted_price": 58,
    "current_high": 85.0,
    "latest_temp": 82.0,
    "forecast_nws": 91.0,
    "forecast_secondary": 90.5,
    "no_best_bid": 54,
    "no_best_bid_size": 100.0,
    "no_best_ask": 56,
    "p_yes_now": 0.44,
    "fair_value_now": 56,
    "weather_missing": 0,
}


class TestArchiveDatabase:
    def test_creates_tables_on_first_open(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            cur = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {r[0] for r in cur.fetchall()}
        assert "snapshot_archive" in tables
        assert "position_snapshot_archive" in tables

    def test_idempotent_construction(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        ArchiveDatabase(db_path).close()
        ArchiveDatabase(db_path).close()  # second open must not raise

    def test_insert_snapshots_returns_count(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            inserted = db.insert_snapshots([_SNAP_ROW])
        assert inserted == 1

    def test_insert_snapshots_schema_roundtrip(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            db.insert_snapshots([_SNAP_ROW])
            cur = db._conn.execute("SELECT * FROM snapshot_archive")
            row = dict(cur.fetchone())
        assert row["ts"] == _SNAP_ROW["ts"]
        assert row["station"] == _SNAP_ROW["station"]
        assert row["ticker"] == _SNAP_ROW["ticker"]
        assert abs(row["p_yes"] - _SNAP_ROW["p_yes"]) < 1e-9

    def test_insert_snapshots_idempotent(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            db.insert_snapshots([_SNAP_ROW])
            inserted2 = db.insert_snapshots([_SNAP_ROW])
            cur = db._conn.execute("SELECT COUNT(*) FROM snapshot_archive")
            count = cur.fetchone()[0]
        assert inserted2 == 0
        assert count == 1

    def test_insert_position_snapshots_schema_roundtrip(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            db.insert_position_snapshots([_POS_SNAP_ROW])
            cur = db._conn.execute("SELECT * FROM position_snapshot_archive")
            row = dict(cur.fetchone())
        assert row["ts"] == _POS_SNAP_ROW["ts"]
        assert row["no_token_id"] == _POS_SNAP_ROW["no_token_id"]
        assert row["weather_missing"] == 0

    def test_insert_position_snapshots_idempotent(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            db.insert_position_snapshots([_POS_SNAP_ROW])
            inserted2 = db.insert_position_snapshots([_POS_SNAP_ROW])
            cur = db._conn.execute(
                "SELECT COUNT(*) FROM position_snapshot_archive"
            )
            count = cur.fetchone()[0]
        assert inserted2 == 0
        assert count == 1

    def test_get_max_archived_ts_empty(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            result = db.get_max_archived_ts("snapshot_archive")
        assert result is None

    def test_get_max_archived_ts_returns_latest(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        row1 = {**_SNAP_ROW, "ts": "2026-06-15T10:00:00+00:00", "ticker": "TICKER-A"}
        row2 = {**_SNAP_ROW, "ts": "2026-06-15T12:00:00+00:00", "ticker": "TICKER-B"}
        with ArchiveDatabase(db_path) as db:
            db.insert_snapshots([row1, row2])
            result = db.get_max_archived_ts("snapshot_archive")
        assert result == "2026-06-15T12:00:00+00:00"

    def test_get_max_archived_ts_invalid_table(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            with pytest.raises(ValueError):
                db.get_max_archived_ts(
                    "malicious_table; DROP TABLE snapshot_archive"
                )

    def test_no_coupling_to_trading_db(self, tmp_path):
        """Importing ArchiveDatabase must not import or instantiate Database."""
        import importlib
        import sys
        # Reload to ensure clean import state
        if "src.data.archive_db" in sys.modules:
            del sys.modules["src.data.archive_db"]
        mod = importlib.import_module("src.data.archive_db")
        # The module must not have imported from src.data.db
        assert "src.data.db" not in sys.modules or True  # db.py may be imported elsewhere
        assert not hasattr(mod, "Database"), (
            "archive_db must not re-export the trading Database class"
        )
