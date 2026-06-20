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

    def test_no_coupling_to_trading_db(self):
        """Importing ArchiveDatabase must not import or instantiate Database."""
        import subprocess
        import sys
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import src.data.archive_db; import sys; "
                "assert 'src.data.db' not in sys.modules, "
                "'archive_db imported src.data.db unexpectedly'",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"archive_db must not import src.data.db.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestQueryHelpers:
    """Tests for get_snapshot_series and get_position_snapshot_series."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_snap(self, ts: str, station: str, ticker: str) -> dict:
        return {**_SNAP_ROW, "ts": ts, "station": station, "ticker": ticker}

    def _make_pos_snap(self, ts: str, ticker: str, no_token_id: str) -> dict:
        return {**_POS_SNAP_ROW, "ts": ts, "ticker": ticker, "no_token_id": no_token_id}

    # ------------------------------------------------------------------
    # get_snapshot_series
    # ------------------------------------------------------------------

    def test_get_snapshot_series_returns_ordered_rows(self, tmp_path):
        """Seed 3 KORD rows + 1 KMSP; query returns exactly 3 KORD rows in ts order."""
        db_path = tmp_path / "analytics.db"
        rows = [
            self._make_snap("2026-06-15T08:00:00+00:00", "KORD", "TICKER-A"),
            self._make_snap("2026-06-15T10:00:00+00:00", "KORD", "TICKER-B"),
            self._make_snap("2026-06-15T12:00:00+00:00", "KORD", "TICKER-C"),
            self._make_snap("2026-06-15T09:00:00+00:00", "KMSP", "TICKER-D"),
        ]
        with ArchiveDatabase(db_path) as db:
            db.insert_snapshots(rows)
            result = db.get_snapshot_series("KORD", "2026-06-15")
        assert len(result) == 3
        assert [r["ts"] for r in result] == [
            "2026-06-15T08:00:00+00:00",
            "2026-06-15T10:00:00+00:00",
            "2026-06-15T12:00:00+00:00",
        ]
        assert all(r["station"] == "KORD" for r in result)

    def test_get_snapshot_series_empty_for_unknown(self, tmp_path):
        """Returns [] for a station that has no data."""
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            result = db.get_snapshot_series("KXYZ", "2026-06-15")
        assert result == []

    def test_get_snapshot_series_date_boundary(self, tmp_path):
        """Rows from 2026-06-16 are NOT returned when querying 2026-06-15."""
        db_path = tmp_path / "analytics.db"
        rows = [
            self._make_snap("2026-06-15T23:00:00+00:00", "KORD", "TICKER-A"),
            self._make_snap("2026-06-16T00:00:00+00:00", "KORD", "TICKER-B"),
        ]
        with ArchiveDatabase(db_path) as db:
            db.insert_snapshots(rows)
            result = db.get_snapshot_series("KORD", "2026-06-15")
        assert len(result) == 1
        assert result[0]["ts"] == "2026-06-15T23:00:00+00:00"

    # ------------------------------------------------------------------
    # get_position_snapshot_series
    # ------------------------------------------------------------------

    def test_get_position_snapshot_series_returns_ordered_rows(self, tmp_path):
        """Seed 2 position snapshots for a ticker; assert returns 2 in ts order."""
        db_path = tmp_path / "analytics.db"
        ticker = "HIGH-TEMP-KORD-2026-06-15-90-94"
        rows = [
            self._make_pos_snap("2026-06-15T14:00:00+00:00", ticker, "tok-1"),
            self._make_pos_snap("2026-06-15T16:00:00+00:00", ticker, "tok-2"),
        ]
        with ArchiveDatabase(db_path) as db:
            db.insert_position_snapshots(rows)
            result = db.get_position_snapshot_series(ticker, "2026-06-15")
        assert len(result) == 2
        assert result[0]["ts"] == "2026-06-15T14:00:00+00:00"
        assert result[1]["ts"] == "2026-06-15T16:00:00+00:00"

    def test_get_position_snapshot_series_empty(self, tmp_path):
        """Returns [] for a ticker with no data."""
        db_path = tmp_path / "analytics.db"
        with ArchiveDatabase(db_path) as db:
            result = db.get_position_snapshot_series("UNKNOWN-TICKER", "2026-06-15")
        assert result == []
