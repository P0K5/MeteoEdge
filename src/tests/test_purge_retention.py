"""Tests for src/scripts/purge_retention.py.

Covers purge logic, idempotency, dry-run mode, retention windows, and edge cases.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from src.data.db import connect_with_busy_timeout
from src.scripts.purge_retention import purge_candidates, purge_guardrail_events, run


def _create_test_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a minimal test database with candidates and guardrail_events tables."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))

    # Create tables matching the real schema
    conn.execute("""
        CREATE TABLE candidates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            station         TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            bracket_low     REAL NOT NULL,
            bracket_high    REAL NOT NULL,
            side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
            predicted_price INTEGER NOT NULL,
            predicted_edge  REAL NOT NULL,
            market_price    INTEGER NOT NULL,
            confidence      REAL NOT NULL,
            minutes_to_settlement REAL NOT NULL,
            flagged_first   INTEGER NOT NULL DEFAULT 1,
            direction       TEXT NOT NULL DEFAULT 'high'
        );
    """)

    conn.execute("""
        CREATE TABLE guardrail_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            station     TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            raw_value   REAL,
            adj_value   REAL,
            delta       REAL,
            ticker      TEXT
        );
    """)

    conn.commit()
    return conn


def _insert_candidate(conn: sqlite3.Connection, ts: str) -> None:
    """Insert a test candidate row at the given timestamp."""
    conn.execute(
        """INSERT INTO candidates
           (ts, station, ticker, bracket_low, bracket_high, side,
            predicted_price, predicted_edge, market_price, confidence,
            minutes_to_settlement)
           VALUES (?, 'KORD', 'HIGH-TEMP-KORD-2026-06-15-90-94', 90.0, 94.0, 'YES',
                   50, 2.5, 48, 0.6, 240.0)
        """,
        (ts,),
    )
    conn.commit()


def _insert_guardrail_event(conn: sqlite3.Connection, ts: str) -> None:
    """Insert a test guardrail_events row at the given timestamp."""
    conn.execute(
        """INSERT INTO guardrail_events
           (ts, station, event_type, raw_value, adj_value, delta, ticker)
           VALUES (?, 'KORD', 'zero_eval_tick', 0.5, 0.5, 0.0, 'HIGH-TEMP-KORD-2026-06-15-90-94')
        """,
        (ts,),
    )
    conn.commit()


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Count rows in a table."""
    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPurgeCandidates:
    """Test purging candidates table."""

    def test_purge_candidates_deletes_old_rows(self, tmp_path):
        """Rows older than retention window are deleted."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()
        new_ts = (now - timedelta(days=10)).isoformat()

        _insert_candidate(conn, old_ts)
        _insert_candidate(conn, old_ts)
        _insert_candidate(conn, new_ts)

        assert _count_rows(conn, "candidates") == 3

        deleted = purge_candidates(conn, days=90, dry_run=False)

        assert deleted == 2
        assert _count_rows(conn, "candidates") == 1

        conn.close()

    def test_purge_candidates_respects_retention_window(self, tmp_path):
        """Rows within retention window are not deleted."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(days=90)).isoformat()
        safe_ts = (now - timedelta(days=89)).isoformat()  # Just inside window

        _insert_candidate(conn, cutoff_ts)
        _insert_candidate(conn, safe_ts)

        deleted = purge_candidates(conn, days=90, dry_run=False)

        assert deleted == 1
        assert _count_rows(conn, "candidates") == 1

        conn.close()

    def test_purge_candidates_dry_run(self, tmp_path):
        """Dry-run counts but does not delete."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()

        _insert_candidate(conn, old_ts)
        _insert_candidate(conn, old_ts)

        deleted = purge_candidates(conn, days=90, dry_run=True)

        assert deleted == 2
        assert _count_rows(conn, "candidates") == 2  # Not actually deleted

        conn.close()

    def test_purge_candidates_empty_table(self, tmp_path):
        """Purging empty table returns 0."""
        conn = _create_test_db(tmp_path)

        deleted = purge_candidates(conn, days=90, dry_run=False)

        assert deleted == 0

        conn.close()

    def test_purge_candidates_idempotent(self, tmp_path):
        """Running purge multiple times is idempotent."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()

        _insert_candidate(conn, old_ts)
        _insert_candidate(conn, old_ts)

        deleted1 = purge_candidates(conn, days=90, dry_run=False)
        deleted2 = purge_candidates(conn, days=90, dry_run=False)

        assert deleted1 == 2
        assert deleted2 == 0
        assert _count_rows(conn, "candidates") == 0

        conn.close()


class TestPurgeGuardrailEvents:
    """Test purging guardrail_events table."""

    def test_purge_guardrail_events_deletes_old_rows(self, tmp_path):
        """Rows older than retention window are deleted."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=70)).isoformat()
        new_ts = (now - timedelta(days=10)).isoformat()

        _insert_guardrail_event(conn, old_ts)
        _insert_guardrail_event(conn, old_ts)
        _insert_guardrail_event(conn, new_ts)

        assert _count_rows(conn, "guardrail_events") == 3

        deleted = purge_guardrail_events(conn, days=60, dry_run=False)

        assert deleted == 2
        assert _count_rows(conn, "guardrail_events") == 1

        conn.close()

    def test_purge_guardrail_events_respects_retention_window(self, tmp_path):
        """Rows within retention window are not deleted."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(days=60)).isoformat()
        safe_ts = (now - timedelta(days=59)).isoformat()  # Just inside window

        _insert_guardrail_event(conn, cutoff_ts)
        _insert_guardrail_event(conn, safe_ts)

        deleted = purge_guardrail_events(conn, days=60, dry_run=False)

        assert deleted == 1
        assert _count_rows(conn, "guardrail_events") == 1

        conn.close()

    def test_purge_guardrail_events_dry_run(self, tmp_path):
        """Dry-run counts but does not delete."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=70)).isoformat()

        _insert_guardrail_event(conn, old_ts)
        _insert_guardrail_event(conn, old_ts)

        deleted = purge_guardrail_events(conn, days=60, dry_run=True)

        assert deleted == 2
        assert _count_rows(conn, "guardrail_events") == 2  # Not actually deleted

        conn.close()

    def test_purge_guardrail_events_empty_table(self, tmp_path):
        """Purging empty table returns 0."""
        conn = _create_test_db(tmp_path)

        deleted = purge_guardrail_events(conn, days=60, dry_run=False)

        assert deleted == 0

        conn.close()

    def test_purge_guardrail_events_idempotent(self, tmp_path):
        """Running purge multiple times is idempotent."""
        conn = _create_test_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=70)).isoformat()

        _insert_guardrail_event(conn, old_ts)
        _insert_guardrail_event(conn, old_ts)

        deleted1 = purge_guardrail_events(conn, days=60, dry_run=False)
        deleted2 = purge_guardrail_events(conn, days=60, dry_run=False)

        assert deleted1 == 2
        assert deleted2 == 0
        assert _count_rows(conn, "guardrail_events") == 0

        conn.close()


class TestPurgeIntegration:
    """Integration tests for the full run() function."""

    def test_run_purges_both_tables(self, tmp_path):
        """run() purges both tables correctly."""
        conn = _create_test_db(tmp_path)
        db_path = tmp_path / "test.db"

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()

        _insert_candidate(conn, old_ts)
        _insert_guardrail_event(conn, old_ts)
        conn.close()

        run(db_path=db_path, candidates_days=90, guardrail_days=60, dry_run=False)

        conn = sqlite3.connect(str(db_path))
        assert _count_rows(conn, "candidates") == 0
        assert _count_rows(conn, "guardrail_events") == 0
        conn.close()

    def test_run_dry_run_mode(self, tmp_path):
        """run() with dry_run=True does not delete."""
        conn = _create_test_db(tmp_path)
        db_path = tmp_path / "test.db"

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()

        _insert_candidate(conn, old_ts)
        _insert_guardrail_event(conn, old_ts)
        conn.close()

        run(db_path=db_path, candidates_days=90, guardrail_days=60, dry_run=True)

        conn = sqlite3.connect(str(db_path))
        assert _count_rows(conn, "candidates") == 1
        assert _count_rows(conn, "guardrail_events") == 1
        conn.close()

    def test_run_respects_different_retention_windows(self, tmp_path):
        """run() respects different retention windows for each table."""
        conn = _create_test_db(tmp_path)
        db_path = tmp_path / "test.db"

        now = datetime.now(timezone.utc)
        ts_70_days_ago = (now - timedelta(days=70)).isoformat()

        # Insert at 70 days ago: candidates should purge (90d window), guardrail should not (60d window)
        _insert_candidate(conn, ts_70_days_ago)
        _insert_guardrail_event(conn, ts_70_days_ago)
        conn.close()

        run(db_path=db_path, candidates_days=90, guardrail_days=60, dry_run=False)

        conn = sqlite3.connect(str(db_path))
        # After 90-day purge: 70 days ago is still within window, not deleted
        # After 60-day purge: 70 days ago is outside window, deleted
        assert _count_rows(conn, "candidates") == 1
        assert _count_rows(conn, "guardrail_events") == 0
        conn.close()

    def test_run_logs_with_timestamp(self, tmp_path, caplog):
        """run() logs summary with timestamp (verifies #721 fix - no longer uses bare print())."""
        import logging
        conn = _create_test_db(tmp_path)
        db_path = tmp_path / "test.db"

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()

        _insert_candidate(conn, old_ts)
        _insert_guardrail_event(conn, old_ts)
        conn.close()

        # Capture log output
        with caplog.at_level(logging.INFO):
            run(db_path=db_path, candidates_days=90, guardrail_days=60, dry_run=False)

        # Check that the log contains the purge_retention summary message
        # This confirms that run() now uses log.info() instead of bare print()
        assert any("[purge_retention]" in record.message for record in caplog.records), \
            "Expected [purge_retention] message not found in logs"

        # Verify the message content
        purge_record = [r for r in caplog.records if "[purge_retention]" in r.message][0]
        assert "candidates: retention=90d, deleted=1" in purge_record.message
        assert "guardrail_events: retention=60d, deleted=1" in purge_record.message


# ---------------------------------------------------------------------------
# Issue #1238 -- chunked deletes and the shared busy timeout
# ---------------------------------------------------------------------------

class TestPurgeChunkedDeletes:
    """A single DELETE over the ENTIRE overdue set holds the write lock for
    however long matching + deleting all of it takes -- unbounded by anything
    this script controls. Chunking bounds any ONE transaction's hold to
    roughly one chunk's delete time, which is what actually matters for the
    01:00-03:00 contention (issue #1238): this job is the one HOLDING the
    lock other writers wait on, not the one waiting.
    """

    def test_chunks_across_multiple_transactions(self, tmp_path):
        conn = _create_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()
        n_rows = 25
        for _ in range(n_rows):
            _insert_candidate(conn, old_ts)
        assert _count_rows(conn, "candidates") == n_rows

        wrapped = mock.MagicMock(wraps=conn)
        wrapped.execute.side_effect = conn.execute
        wrapped.commit.side_effect = conn.commit

        with mock.patch("src.scripts.purge_retention._DELETE_CHUNK_SIZE", 7):
            deleted = purge_candidates(wrapped, days=90, dry_run=False)

        assert deleted == n_rows
        assert _count_rows(conn, "candidates") == 0
        # ceil(25/7) == 4 delete batches -> 4 commits. If this were still one
        # big transaction it would be exactly 1, regardless of row count.
        assert wrapped.commit.call_count == 4

        conn.close()

    def test_chunking_is_still_idempotent_and_exact_at_the_boundary(self, tmp_path):
        """Row count an exact multiple of the chunk size must not loop forever
        or under/over-count (off-by-one guard on the `rowcount < chunk size`
        termination condition)."""
        conn = _create_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()
        for _ in range(10):
            _insert_candidate(conn, old_ts)

        with mock.patch("src.scripts.purge_retention._DELETE_CHUNK_SIZE", 5):
            deleted = purge_candidates(conn, days=90, dry_run=False)

        assert deleted == 10
        assert _count_rows(conn, "candidates") == 0
        conn.close()

    def test_dry_run_does_not_chunk_or_delete(self, tmp_path):
        """dry_run must still report the correct count without touching rows,
        even when the chunked path is active."""
        conn = _create_test_db(tmp_path)
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat()
        for _ in range(10):
            _insert_candidate(conn, old_ts)

        with mock.patch("src.scripts.purge_retention._DELETE_CHUNK_SIZE", 3):
            deleted = purge_candidates(conn, days=90, dry_run=True)

        assert deleted == 10
        assert _count_rows(conn, "candidates") == 10
        conn.close()


class TestPurgeRetentionUsesSharedBusyTimeout:
    """run() must open its connection through connect_with_busy_timeout()
    (issue #1238), not a bare sqlite3.connect() -- see also
    test_db.py::TestEveryScheduledWriterUsesTheSharedBusyTimeout, which
    guards the same property via a source scan across every systemd-scheduled
    writer."""

    def test_run_opens_via_the_shared_helper(self, tmp_path):
        conn = _create_test_db(tmp_path)
        conn.close()
        db_path = tmp_path / "test.db"

        with mock.patch(
            "src.scripts.purge_retention.connect_with_busy_timeout",
            wraps=connect_with_busy_timeout,
        ) as mocked_connect:
            run(db_path=db_path, candidates_days=90, guardrail_days=60, dry_run=False)

        mocked_connect.assert_called_once_with(str(db_path))
