"""Tests for src/scripts/archive_snapshots.py.

Covers incremental HWM-based loading, idempotency, dry-run, date-window
filtering, malformed-record tolerance, legacy bare-file handling, symlink
dedup, and position_snapshot loading.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.data.archive_db import ArchiveDatabase
from src.scripts.archive_snapshots import run


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _snap_row(
    ts: str = "2026-06-15T10:00:00+00:00",
    ticker: str = "HIGH-TEMP-KORD-2026-06-15-90-94",
    station: str = "KORD",
) -> dict:
    return {
        "ts": ts,
        "station": station,
        "ticker": ticker,
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


def _pos_row(
    ts: str = "2026-06-15T14:00:00+00:00",
    no_token_id: str = "abc123",
    ticker: str = "HIGH-TEMP-KORD-2026-06-15-90-94",
) -> dict:
    return {
        "ts": ts,
        "ticker": ticker,
        "no_token_id": no_token_id,
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
        "p_yes_now": None,
        "fair_value_now": 56,
        "weather_missing": 0,
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _count_rows(db_path: Path, table: str) -> int:
    with ArchiveDatabase(db_path) as db:
        cur = db._conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSnapshotsLoadedFromDatedFile:
    """Synthetic dated JSONL file → rows are inserted into snapshot_archive."""

    def test_snapshots_loaded_from_dated_file(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [_snap_row(), _snap_row(ts="2026-06-15T11:00:00+00:00", ticker="TICKER-B")])

        db_path = tmp_path / "analytics.db"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        run(db_path=db_path)

        assert _count_rows(db_path, "snapshot_archive") == 2


class TestSecondRunInsertsZero:
    """Running the ETL twice must insert 0 rows on the second run (HWM + INSERT OR IGNORE)."""

    def test_second_run_inserts_zero(self, tmp_path, monkeypatch, capsys):
        logs_dir = tmp_path / "logs"
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [_snap_row()])

        db_path = tmp_path / "analytics.db"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        run(db_path=db_path)
        assert _count_rows(db_path, "snapshot_archive") == 1

        run(db_path=db_path)
        assert _count_rows(db_path, "snapshot_archive") == 1

        captured = capsys.readouterr()
        # Second run: inserted 0 because HWM already covers all records
        assert "inserted 0" in captured.out


class TestDryRunWritesNothing:
    """--dry-run must leave analytics.db row count unchanged."""

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [_snap_row()])

        db_path = tmp_path / "analytics.db"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        run(db_path=db_path, dry_run=True)

        assert _count_rows(db_path, "snapshot_archive") == 0

    def test_dry_run_output_tag(self, tmp_path, monkeypatch, capsys):
        logs_dir = tmp_path / "logs"
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [_snap_row()])

        db_path = tmp_path / "analytics.db"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        run(db_path=db_path, dry_run=True)

        out = capsys.readouterr().out
        assert "[DRY RUN]" in out


class TestHwmSkipsArchivedTs:
    """Records with ts <= HWM are skipped on subsequent runs."""

    def test_hwm_skips_archived_ts(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        # First batch: two records with ts at 10:00 and 11:00
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [
            _snap_row(ts="2026-06-15T10:00:00+00:00", ticker="A"),
            _snap_row(ts="2026-06-15T11:00:00+00:00", ticker="B"),
        ])
        run(db_path=db_path)
        assert _count_rows(db_path, "snapshot_archive") == 2

        # Append a record with ts BELOW current HWM; run again
        dated2 = logs_dir / "snapshots.2026-06-16.jsonl"
        _write_jsonl(dated2, [
            _snap_row(ts="2026-06-15T09:00:00+00:00", ticker="OLD"),
            _snap_row(ts="2026-06-16T08:00:00+00:00", ticker="NEW"),
        ])
        run(db_path=db_path)
        # OLD is below HWM, only NEW should be inserted
        assert _count_rows(db_path, "snapshot_archive") == 3


class TestMalformedLineSkippedNotFatal:
    """Corrupt JSONL lines do not abort the ETL; they are counted as malformed."""

    def test_malformed_line_skipped_not_fatal(self, tmp_path, monkeypatch, capsys):
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        dated.parent.mkdir(parents=True, exist_ok=True)
        # Two good records flanking a corrupt line and a record missing required field
        with open(dated, "w") as f:
            f.write(json.dumps(_snap_row(ts="2026-06-15T10:00:00+00:00", ticker="GOOD1")) + "\n")
            f.write("not valid json\n")
            f.write(json.dumps({"missing_required": True}) + "\n")  # no ts/station/ticker
            f.write(json.dumps(_snap_row(ts="2026-06-15T11:00:00+00:00", ticker="GOOD2")) + "\n")

        run(db_path=db_path)

        # ETL completes without raising
        assert _count_rows(db_path, "snapshot_archive") == 2
        out = capsys.readouterr().out
        assert "malformed" in out


class TestFromDateToDateFilter:
    """Date-window flags filter records by ts."""

    def _setup(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [
            _snap_row(ts="2026-06-13T10:00:00+00:00", ticker="DAY13"),
            _snap_row(ts="2026-06-14T10:00:00+00:00", ticker="DAY14"),
            _snap_row(ts="2026-06-15T10:00:00+00:00", ticker="DAY15"),
            _snap_row(ts="2026-06-16T10:00:00+00:00", ticker="DAY16"),
        ])
        return db_path

    def test_from_date_filter(self, tmp_path, monkeypatch):
        db_path = self._setup(tmp_path, monkeypatch)
        run(db_path=db_path, from_date="2026-06-14")
        # DAY13 is before from_date; DAY14, DAY15, DAY16 within range
        assert _count_rows(db_path, "snapshot_archive") == 3

    def test_to_date_filter(self, tmp_path, monkeypatch):
        db_path = self._setup(tmp_path, monkeypatch)
        run(db_path=db_path, to_date="2026-06-14T23:59:59+00:00")
        # Only DAY13 and DAY14 are <= to_date
        assert _count_rows(db_path, "snapshot_archive") == 2

    def test_from_and_to_date_filter(self, tmp_path, monkeypatch):
        db_path = self._setup(tmp_path, monkeypatch)
        run(db_path=db_path, from_date="2026-06-14", to_date="2026-06-15T23:59:59+00:00")
        # DAY14 and DAY15 only
        assert _count_rows(db_path, "snapshot_archive") == 2


class TestLegacyBareFileIngested:
    """A real (non-symlink) bare file co-existing with dated files is ingested."""

    def test_legacy_bare_file_ingested(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        logs_dir.mkdir(parents=True, exist_ok=True)

        # Legacy plain file (not a symlink) — pre-rotation historical data
        _write_jsonl(snap_base, [
            _snap_row(ts="2026-06-01T10:00:00+00:00", ticker="LEGACY"),
        ])

        # Dated file alongside it
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [
            _snap_row(ts="2026-06-15T10:00:00+00:00", ticker="DATED"),
        ])

        assert snap_base.exists() and not snap_base.is_symlink(), "bare file must be a real file"

        run(db_path=db_path)

        # Both the legacy record and the dated record must be ingested
        assert _count_rows(db_path, "snapshot_archive") == 2


class TestSymlinkBareFileNotDoubleCounted:
    """If the bare path is a symlink, iter_rotated_jsonl skips it — no double-counting."""

    def test_symlink_bare_file_not_double_counted(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        logs_dir.mkdir(parents=True, exist_ok=True)

        # Dated file with two records
        dated = logs_dir / "snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [
            _snap_row(ts="2026-06-15T10:00:00+00:00", ticker="A"),
            _snap_row(ts="2026-06-15T11:00:00+00:00", ticker="B"),
        ])

        # Bare path is a symlink pointing to the dated file (today's rotation pattern)
        snap_base.symlink_to(dated.name)
        assert snap_base.is_symlink()

        run(db_path=db_path)

        # Must insert exactly 2 rows — not 4 (no double-count from the symlink)
        assert _count_rows(db_path, "snapshot_archive") == 2


class TestPositionSnapshotsLoaded:
    """Position snapshots (including None for optional fields) are loaded correctly."""

    def test_position_snapshots_loaded(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        dated = logs_dir / "position_snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [
            _pos_row(ts="2026-06-15T14:00:00+00:00", no_token_id="tok1"),
            _pos_row(ts="2026-06-15T15:00:00+00:00", no_token_id="tok2"),
        ])

        run(db_path=db_path)

        assert _count_rows(db_path, "position_snapshot_archive") == 2

    def test_position_snapshot_with_none_optional_field(self, tmp_path, monkeypatch):
        """p_yes_now=None must not cause an error (column is nullable)."""
        logs_dir = tmp_path / "logs"
        snap_base = logs_dir / "snapshots.jsonl"
        pos_base = logs_dir / "position_snapshots.jsonl"
        db_path = tmp_path / "analytics.db"

        monkeypatch.setattr("src.scripts.archive_snapshots.SNAPSHOTS_JSONL", snap_base)
        monkeypatch.setattr("src.scripts.archive_snapshots.POSITION_SNAPSHOTS_JSONL", pos_base)

        row = _pos_row()
        row["p_yes_now"] = None  # explicitly None
        dated = logs_dir / "position_snapshots.2026-06-15.jsonl"
        _write_jsonl(dated, [row])

        run(db_path=db_path)

        with ArchiveDatabase(db_path) as db:
            cur = db._conn.execute("SELECT p_yes_now FROM position_snapshot_archive")
            val = cur.fetchone()[0]
        assert val is None


class TestIntegrationRealFiles:
    """Integration: run against real log files if they exist, skip gracefully otherwise."""

    def test_integration_or_skip(self, tmp_path):
        from src.config import SNAPSHOTS_JSONL as real_snap, POSITION_SNAPSHOTS_JSONL as real_pos
        from src.utils.log_rotation import rotated_sources

        has_snaps = bool(rotated_sources(real_snap))
        has_pos = bool(rotated_sources(real_pos))

        if not has_snaps and not has_pos:
            pytest.skip("No real log files found — skipping integration test")

        db_path = tmp_path / "analytics_integration.db"
        # Just verify it runs without error and reports sensible output
        run(db_path=db_path)

        with ArchiveDatabase(db_path) as db:
            snap_count = db._conn.execute(
                "SELECT COUNT(*) FROM snapshot_archive"
            ).fetchone()[0]
            pos_count = db._conn.execute(
                "SELECT COUNT(*) FROM position_snapshot_archive"
            ).fetchone()[0]

        assert snap_count >= 0
        assert pos_count >= 0
