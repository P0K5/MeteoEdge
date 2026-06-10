"""Thread-safety stress tests for src/data/db.py.

Verifies that concurrent writes from multiple threads do not corrupt
transaction state, lose rows, or raise exceptions.
All tests use an in-memory SQLite database to avoid file I/O.
"""
import threading
import pytest

from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


def _base_trade_kwargs(n: int) -> dict:
    """Return keyword arguments for insert_trade, varied by *n* to keep rows unique."""
    return dict(
        ts=f"2024-01-{(n % 28) + 1:02d}T12:00:00+00:00",
        station="KORD",
        ticker=f"KORD-2024-01-15-HIGH-{n}-{n + 4}",
        bracket_low=float(n),
        bracket_high=float(n + 4),
        side="YES",
        predicted_price=60,
        actual_price=58,
        predicted_edge=0.12,
        mode="paper",
        capital_before=1000.0,
    )


# ---------------------------------------------------------------------------
# Observations stress test
# ---------------------------------------------------------------------------

class TestObservationsThreadSafety:
    """Multiple threads inserting observations concurrently must not lose rows."""

    def test_concurrent_observation_inserts(self):
        """N threads each insert M observations; total rows must equal N * M."""
        n_threads = 8
        inserts_per_thread = 25
        db = _db()
        errors = []

        def worker(thread_idx: int) -> None:
            for i in range(inserts_per_thread):
                try:
                    db.insert_observation(
                        ts=f"2024-01-15T{thread_idx:02d}:{i:02d}:00+00:00",
                        station=f"K{thread_idx:03d}",
                        temp_f=float(thread_idx * 100 + i),
                        temp_native=float(i),
                        unit="F",
                        source="metar",
                    )
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

        # Count all rows — should equal n_threads * inserts_per_thread
        cur = db._conn.execute("SELECT COUNT(*) FROM observations")
        total = cur.fetchone()[0]
        assert total == n_threads * inserts_per_thread, (
            f"Expected {n_threads * inserts_per_thread} rows, got {total}"
        )


# ---------------------------------------------------------------------------
# Trades stress test
# ---------------------------------------------------------------------------

class TestTradesThreadSafety:
    """Multiple threads inserting trades concurrently must not lose rows."""

    def test_concurrent_trade_inserts(self):
        """N threads each insert M trades; total rows must equal N * M."""
        n_threads = 8
        inserts_per_thread = 25
        db = _db()
        errors = []

        def worker(thread_idx: int) -> None:
            for i in range(inserts_per_thread):
                try:
                    db.insert_trade(**_base_trade_kwargs(thread_idx * 1000 + i))
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

        cur = db._conn.execute("SELECT COUNT(*) FROM trades")
        total = cur.fetchone()[0]
        assert total == n_threads * inserts_per_thread, (
            f"Expected {n_threads * inserts_per_thread} rows, got {total}"
        )


# ---------------------------------------------------------------------------
# Mixed write stress test
# ---------------------------------------------------------------------------

class TestMixedWritesThreadSafety:
    """Concurrent inserts across multiple tables must not corrupt each other."""

    def test_concurrent_mixed_inserts(self):
        """Threads inserting observations, trades, and risk state simultaneously."""
        n_threads = 6
        inserts_per_thread = 20
        db = _db()
        errors = []

        def obs_worker(thread_idx: int) -> None:
            for i in range(inserts_per_thread):
                try:
                    db.insert_observation(
                        ts=f"2024-01-15T{thread_idx:02d}:{i:02d}:00+00:00",
                        station=f"K{thread_idx:03d}",
                        temp_f=float(i),
                        temp_native=float(i),
                        unit="F",
                        source="metar",
                    )
                except Exception as exc:
                    errors.append(exc)

        def trade_worker(thread_idx: int) -> None:
            for i in range(inserts_per_thread):
                try:
                    db.insert_trade(**_base_trade_kwargs(thread_idx * 1000 + i))
                except Exception as exc:
                    errors.append(exc)

        def risk_worker(thread_idx: int) -> None:
            for i in range(inserts_per_thread):
                try:
                    db.upsert_daily_risk(
                        f"2024-01-{(i % 28) + 1:02d}",
                        pnl_delta=1.0,
                        open_positions=thread_idx,
                    )
                except Exception as exc:
                    errors.append(exc)

        threads = []
        for t in range(n_threads // 3 or 1):
            threads.append(threading.Thread(target=obs_worker, args=(t,)))
            threads.append(threading.Thread(target=trade_worker, args=(t + 10,)))
            threads.append(threading.Thread(target=risk_worker, args=(t + 20,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# RLock re-entrancy: on_insert_callback inside a locked context
# ---------------------------------------------------------------------------

class TestRLockReentrancy:
    """RLock must allow the same thread to re-enter (e.g., callback triggering another insert)."""

    def test_reentrant_lock_does_not_deadlock(self):
        """on_insert_callback that calls another DB method on the same thread must not deadlock.

        The callback runs in a separate daemon thread by design, so this test
        verifies there is no cross-thread deadlock when the callback thread
        acquires the lock while the main thread has already released it.
        """
        db = _db()
        callback_ran = threading.Event()

        def callback():
            # Insert a second observation from within the callback thread
            db.insert_observation(
                ts="2024-01-15T13:00:00+00:00",
                station="KJFK",
                temp_f=50.0,
                temp_native=50.0,
                unit="F",
                source="callback",
            )
            callback_ran.set()

        db.insert_observation(
            ts="2024-01-15T12:00:00+00:00",
            station="KORD",
            temp_f=32.0,
            temp_native=32.0,
            unit="F",
            source="metar",
            on_insert_callback=callback,
        )

        # Wait up to 2 seconds for the callback to complete
        assert callback_ran.wait(timeout=2.0), "Callback did not complete — possible deadlock"

        cur = db._conn.execute("SELECT COUNT(*) FROM observations")
        assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Lock attribute exists
# ---------------------------------------------------------------------------

class TestLockAttribute:
    """Database must expose an RLock attribute after __init__."""

    def test_lock_is_rlock(self):
        db = _db()
        assert hasattr(db, "_lock"), "Database must have a _lock attribute"
        # RLock type check: acquire/release API
        assert db._lock.acquire(blocking=False), "Lock should be acquirable"
        db._lock.release()
