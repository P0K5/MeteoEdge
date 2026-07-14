"""Tests for scripts/cleanup_settlement_timestamps.py (issue #719).

Covers the double-timezone-suffix backfill: fixing malformed rows, leaving
well-formed rows untouched, idempotency, and the missing-DB error path.
"""
import sqlite3

import pytest

import scripts.cleanup_settlement_timestamps as cleanup_mod


def _create_test_db(tmp_path):
    db_path = tmp_path / "meteoedge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE settlements (id INTEGER PRIMARY KEY, ts TEXT, val REAL)")
    conn.execute(
        "INSERT INTO settlements VALUES (1, '2026-07-12T12:01:01.762227+00:00Z', 1.0)"
    )
    conn.execute(
        "INSERT INTO settlements VALUES (2, '2026-07-12T13:00:00.000000+00:00', 2.0)"
    )
    conn.execute(
        "INSERT INTO settlements VALUES (3, '2026-07-11T09:30:00.500000+00:00Z', 3.0)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_cleanup_fixes_malformed_rows_only(tmp_path, monkeypatch, capsys):
    db_path = _create_test_db(tmp_path)
    monkeypatch.setattr(cleanup_mod, "_DEFAULT_DB_PATH", str(db_path))

    cleanup_mod.main()

    conn = sqlite3.connect(str(db_path))
    try:
        rows = dict(conn.execute("SELECT id, ts FROM settlements").fetchall())
    finally:
        conn.close()

    assert rows[1] == "2026-07-12T12:01:01.762227+00:00"
    assert rows[2] == "2026-07-12T13:00:00.000000+00:00"  # untouched, was already valid
    assert rows[3] == "2026-07-11T09:30:00.500000+00:00"
    assert "Successfully fixed 2 row(s)" in capsys.readouterr().out


def test_cleanup_is_idempotent(tmp_path, monkeypatch, capsys):
    db_path = _create_test_db(tmp_path)
    monkeypatch.setattr(cleanup_mod, "_DEFAULT_DB_PATH", str(db_path))

    cleanup_mod.main()
    capsys.readouterr()  # discard first run's output
    cleanup_mod.main()

    assert "No rows with double timezone suffix found" in capsys.readouterr().out


def test_cleanup_dates_parse_after_fix(tmp_path, monkeypatch):
    db_path = _create_test_db(tmp_path)
    monkeypatch.setattr(cleanup_mod, "_DEFAULT_DB_PATH", str(db_path))

    cleanup_mod.main()

    conn = sqlite3.connect(str(db_path))
    try:
        null_dates = conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE date(ts) IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    assert null_dates == 0


def test_cleanup_exits_nonzero_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_mod, "_DEFAULT_DB_PATH", str(tmp_path / "does-not-exist.db"))

    with pytest.raises(SystemExit) as exc_info:
        cleanup_mod.main()

    assert exc_info.value.code == 1
