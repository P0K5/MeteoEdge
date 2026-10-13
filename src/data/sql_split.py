"""Shared, dependency-free SQL script splitting for schema DDL (issue #1267).

Kept separate from src/data/db.py so archive_db.py can use it without
importing the live Database module (which pulls in config/strategy code).
"""
from __future__ import annotations

import sqlite3


def _split_sql_statements(script: str) -> "list[str]":
    """Split a SQL script into individual statements (issue #1267).

    A naive ``split(";")`` breaks on a ``;`` inside a string literal, a
    comment, or a ``CREATE TRIGGER ... BEGIN ... END`` body. Instead, grow a
    candidate statement up to each ``;`` and cut only where SQLite itself
    (``sqlite3.complete_statement``) says the text is a complete statement.
    Statements are returned stripped, without empty fragments, and are still
    executed one at a time by the caller (same semantics as before).

    Failure diagnostics: because statements stay individual, the caller
    wraps each ``execute`` and re-raises with the failing statement's text in
    the message (original error kept as ``__cause__``), so a malformed schema
    is identified by its SQL rather than a bare "incomplete input".
    """
    statements: "list[str]" = []
    start = 0
    pos = script.find(";")
    while pos != -1:
        candidate = script[start:pos + 1]
        if sqlite3.complete_statement(candidate):
            candidate = candidate.strip()
            if candidate.rstrip(";").strip():
                statements.append(candidate)
            start = pos + 1
        pos = script.find(";", pos + 1)
    tail = script[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def execute_ddl_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute *script* statement by statement; name the failing SQL on error.

    Re-raises sqlite3.OperationalError whose message includes the failing
    statement text (first 300 chars); the original error is ``__cause__``.
    """
    for stmt in _split_sql_statements(script):
        try:
            conn.execute(stmt)
        except sqlite3.Error as exc:
            raise sqlite3.OperationalError(
                f"schema DDL statement failed: {exc}: {stmt[:300]!r}"
            ) from exc
