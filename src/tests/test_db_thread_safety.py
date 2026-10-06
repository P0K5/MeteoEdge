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


# ---------------------------------------------------------------------------
# Issue #1335: shared connection must be lock-guarded for READS too
# ---------------------------------------------------------------------------

class TestSharedConnectionMixedReadWrite:
    """N threads doing mixed reads and writes against ONE Database for a fixed
    duration must raise nothing.

    Production shares a single ``sqlite3.Connection`` between the bot poll
    thread and the FastAPI threadpool. Before #1335 only writes took
    ``Database._lock``, so reads could touch the connection's statement cache
    concurrently (``KeyError: ('SELECT effective_mode ...',)`` in the field).
    Uses a file-backed WAL database, like production, rather than ``:memory:``.
    """

    DURATION_SECONDS = 3.0
    N_READERS = 8
    N_WRITERS = 4

    def test_mixed_reads_and_writes_raise_nothing(self, tmp_path):
        import time

        db = Database(tmp_path / "shared_conn_stress.db")
        db.insert_followed_wallet(address="0xseed", stake_per_trade=1.0, added_at="2024-01-01T00:00:00+00:00")
        db.set_emos_effective_mode("chicago", "shadow")

        errors: list = []
        errors_lock = threading.Lock()
        counts = {"reads": 0, "writes": 0}
        counts_lock = threading.Lock()
        start = threading.Barrier(self.N_READERS + self.N_WRITERS)
        deadline = [0.0]

        def record(exc: BaseException) -> None:
            with errors_lock:
                errors.append(exc)

        def bump(key: str) -> None:
            with counts_lock:
                counts[key] += 1

        def reader(idx: int) -> None:
            reads = (
                lambda: db.get_open_copy_live_positions(),
                lambda: db.get_followed_wallets(),
                lambda: db.get_emos_effective_mode("chicago"),
                lambda: db.get_obs_highs_range("KORD", "2024-01-01"),
                lambda: db.get_trades(limit=10),
                lambda: db.get_observations("KORD", "2024-01-01T00:00:00+00:00"),
            )
            start.wait()
            i = 0
            while time.monotonic() < deadline[0]:
                try:
                    reads[(idx + i) % len(reads)]()
                    bump("reads")
                except Exception as exc:  # noqa: BLE001 - the whole point is to catch any
                    record(exc)
                i += 1

        def writer(idx: int) -> None:
            start.wait()
            i = 0
            while time.monotonic() < deadline[0]:
                try:
                    db.insert_observation(
                        ts=f"2024-01-15T{idx:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00",
                        station="KORD",
                        temp_f=float(i),
                        temp_native=float(i),
                        unit="F",
                        source="metar",
                    )
                    address = f"0x{idx:02d}{i:06d}"
                    db.insert_followed_wallet(
                        address=address, stake_per_trade=1.0, added_at="2024-01-01T00:00:00+00:00"
                    )
                    db.update_followed_wallet_last_seen(address, i)
                    db.set_emos_effective_mode("chicago", "shadow" if i % 2 else "live")
                    db.insert_trade(**_base_trade_kwargs(idx * 100_000 + i))
                    bump("writes")
                except Exception as exc:  # noqa: BLE001
                    record(exc)
                i += 1

        threads = [
            threading.Thread(target=reader, args=(r,)) for r in range(self.N_READERS)
        ] + [
            threading.Thread(target=writer, args=(w,)) for w in range(self.N_WRITERS)
        ]
        deadline[0] = time.monotonic() + self.DURATION_SECONDS
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=self.DURATION_SECONDS + 60)
        assert not any(t.is_alive() for t in threads), "stress threads did not finish"

        assert not errors, f"{len(errors)} exceptions under concurrent use, first: {errors[:3]!r}"
        # The run must actually have exercised both paths, not exited early.
        assert counts["reads"] > 0 and counts["writes"] > 0, counts

        # Every write that reported success must be durably visible afterwards.
        obs_rows = db._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert obs_rows == counts["writes"], (obs_rows, counts)


class TestSharedConnectionLockingGuard:
    """Static guard (issue #1335): every ``Database`` method that touches
    ``self._conn`` must do so lexically inside ``with self._lock:``.

    A structural check rather than a timing test: a race that only shows up
    under load can pass a stress run and still be present, and new read
    methods are the regression risk. ``__init__`` is the one exemption --
    it creates the connection and the lock, and the object is not yet shared.
    """

    def test_every_conn_access_is_under_lock(self):
        import ast
        from pathlib import Path

        import src.data.db as db_module

        tree = ast.parse(Path(db_module.__file__).read_text(encoding="utf-8"))
        database = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Database"
        )

        def is_self_attr(node: ast.AST, attr: str) -> bool:
            return (
                isinstance(node, ast.Attribute)
                and node.attr == attr
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            )

        unguarded: list[str] = []
        for fn in database.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name == "__init__":
                continue
            # Map each node to its parent so we can walk outward from a _conn access.
            parents: dict[int, ast.AST] = {}
            for parent in ast.walk(fn):
                for child in ast.iter_child_nodes(parent):
                    parents[id(child)] = parent
            for node in ast.walk(fn):
                if not is_self_attr(node, "_conn"):
                    continue
                guarded = False
                cur = node
                while id(cur) in parents and cur is not fn:
                    cur = parents[id(cur)]
                    if isinstance(cur, ast.With) and any(
                        is_self_attr(item.context_expr, "_lock") for item in cur.items
                    ):
                        guarded = True
                        break
                if not guarded:
                    unguarded.append(f"{fn.name}:{node.lineno}")

        assert not unguarded, (
            "self._conn used outside `with self._lock:` (issue #1335): "
            + ", ".join(sorted(set(unguarded)))
        )

    def test_no_in_process_module_reads_conn_directly(self):
        """Modules that run in the bot/dashboard process must go through the
        lock-guarded ``Database`` API, never ``<db>._conn`` (issue #1335).

        Excluded: ``src/data/db.py`` (checked above), ``src/data/archive_db.py``
        (its own class, its own lock), and ``src/scripts`` (one-shot CLI
        processes with a private connection, so nothing shares it).
        """
        from pathlib import Path

        import src.data.db as db_module

        src_root = Path(db_module.__file__).resolve().parents[1]
        allowed = {
            Path(db_module.__file__).resolve(),
            (src_root / "data" / "archive_db.py").resolve(),
        }
        offenders: list[str] = []
        for path in src_root.rglob("*.py"):
            rel = path.relative_to(src_root)
            if rel.parts[0] in {"scripts", "tests"} or path.resolve() in allowed:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "._conn" in line:
                    offenders.append(f"{rel}:{lineno}")
        assert not offenders, "direct ._conn access outside Database (issue #1335): " + ", ".join(offenders)
