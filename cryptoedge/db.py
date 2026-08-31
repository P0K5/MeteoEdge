"""SQLite schema for cryptoedge. Writes ONLY to its own database file.

Never opens data/meteoedge.db (see README.md, "Isolation invariant").
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_ts               TEXT    NOT NULL,   -- ISO8601 UTC, when WE observed it
    slug                  TEXT    NOT NULL,   -- e.g. btc-updown-5m-1788268500
    asset                 TEXT    NOT NULL,   -- btc / eth / sol / xrp ...
    window_min            INTEGER NOT NULL,   -- 5 or 15
    market_id             TEXT    NOT NULL,
    window_start_ms       INTEGER NOT NULL,   -- from the slug epoch
    window_end_ms         INTEGER,            -- from endDate
    seconds_to_settlement REAL,
    best_bid              REAL,               -- REAL two-sided book (Up token)
    best_ask              REAL,
    spread                REAL,
    price_up              REAL,               -- outcomePrices[0] (Gamma mid)
    price_down            REAL,               -- outcomePrices[1]
    liquidity             REAL,
    volume                REAL,
    token_up              TEXT,
    token_down            TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_slug     ON quotes(slug);
CREATE INDEX IF NOT EXISTS ix_quotes_poll     ON quotes(poll_ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_quotes_slug_poll ON quotes(slug, poll_ts);

CREATE TABLE IF NOT EXISTS resolutions (
    slug            TEXT PRIMARY KEY,
    asset           TEXT,
    window_min      INTEGER,
    window_start_ms INTEGER,
    window_end_ms   INTEGER,
    resolved_up     INTEGER,      -- 1 = Up, 0 = Down, NULL = not yet / ambiguous
    source          TEXT,         -- 'gamma'
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS poll_runs (
    ts        TEXT PRIMARY KEY,
    n_markets INTEGER,
    ok        INTEGER,
    note      TEXT
);
"""


def connect(db_path: "str | Path") -> sqlite3.Connection:
    """Open (creating if needed) the cryptoedge DB and apply the schema."""
    p = Path(db_path)
    if p.name == "meteoedge.db":
        raise ValueError(
            "cryptoedge must never open meteoedge.db (README.md isolation invariant)"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p.as_posix(), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con
