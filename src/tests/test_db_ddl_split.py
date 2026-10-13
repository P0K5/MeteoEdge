"""Issue #1267: schema DDL must not be split on a naive semicolon."""
import sqlite3

import pytest

from src.data import db as dbmod
from src.data.db import Database, _split_sql_statements


def _naive_objects():
    c = sqlite3.connect(":memory:")
    for stmt in dbmod._DDL.strip().split(";"):
        if stmt.strip():
            c.execute(stmt.strip())
    rows = c.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    c.close()
    return rows


def test_semicolon_in_string_literal():
    out = _split_sql_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;")
    assert out == ["INSERT INTO t VALUES ('a;b');", "SELECT 1;"]


def test_semicolon_in_comments():
    sql = "-- note; here\\nSELECT 1; /* a; b */ SELECT 2;"
    sql = sql.replace("\\n", "\n")
    out = _split_sql_statements(sql)
    assert len(out) == 2
    assert "SELECT 1;" in out[0] and "SELECT 2;" in out[1]


def test_trigger_body_kept_whole():
    sql = (
        "CREATE TABLE t (a INTEGER);\n"
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET a = a + 1;\n"
        "  DELETE FROM t WHERE a > 100;\n"
        "END;\n"
        "CREATE INDEX i ON t(a);"
    )
    out = _split_sql_statements(sql)
    assert len(out) == 3
    assert out[1].startswith("CREATE TRIGGER") and out[1].endswith("END;")
    c = sqlite3.connect(":memory:")
    for s in out:
        c.execute(s)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"t", "trg", "i"} <= names


def test_no_trailing_semicolon_and_empty():
    assert _split_sql_statements("") == []
    assert _split_sql_statements(" ;; ") == []
    assert _split_sql_statements("SELECT 1") == ["SELECT 1"]


def test_real_schema_identical_objects(tmp_path):
    before = {(r[0], r[1], r[2]) for r in _naive_objects()}
    d = Database(tmp_path / "x.db")
    rows = d._conn.execute("SELECT type, name, tbl_name FROM sqlite_master").fetchall()
    after = {tuple(r) for r in rows}
    # migrations may only add columns, never objects absent from the DDL
    assert before <= after
    assert before  # sanity: DDL really creates objects
    c = sqlite3.connect(":memory:")
    for s in _split_sql_statements(dbmod._DDL):
        c.execute(s)
    split_rows = c.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert [tuple(r) for r in split_rows] == [tuple(r) for r in _naive_objects()]


def test_semicolon_in_ddl_comment_constructs_database(tmp_path, monkeypatch):
    base = Database(tmp_path / "base.db")
    expected = {tuple(r) for r in base._conn.execute(
        "SELECT type, name, tbl_name FROM sqlite_master").fetchall()}
    base._conn.close()
    ddl = dbmod._DDL.replace(
        "CREATE TABLE IF NOT EXISTS observations (",
        "-- note; a semicolon in a comment; must not split\n"
        "CREATE TABLE IF NOT EXISTS observations (",
        1,
    ).replace("    station       TEXT NOT NULL,",
              "    station       TEXT NOT NULL, -- stn; id",
              1)
    assert ddl != dbmod._DDL
    monkeypatch.setattr(dbmod, "_DDL", ddl)
    d = Database(tmp_path / "semi.db")
    got = {tuple(r) for r in d._conn.execute(
        "SELECT type, name, tbl_name FROM sqlite_master").fetchall()}
    assert got == expected


def test_malformed_statement_error_names_sql(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dbmod, "_DDL", dbmod._DDL + "\nCREATE TABLE broken_tbl_1267 (id INTEGER,;\n"
    )
    with pytest.raises(sqlite3.OperationalError) as ei:
        Database(tmp_path / "bad.db")
    assert "broken_tbl_1267" in str(ei.value)
    assert isinstance(ei.value.__cause__, sqlite3.Error)
