"""SQLite persistence layer for MeteoEdge."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_PATH = os.getenv("DB_PATH", "data/meteoedge.db")

_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    station       TEXT NOT NULL,
    temp_f        REAL NOT NULL,
    temp_native   REAL NOT NULL,
    unit          TEXT NOT NULL CHECK(unit IN ('F','C')),
    current_high  REAL,
    source        TEXT NOT NULL,
    raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_station_ts ON observations(station, ts);

CREATE TABLE IF NOT EXISTS candidates (
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
    flagged_first   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_cand_station_ts ON candidates(station, ts);
CREATE INDEX IF NOT EXISTS idx_cand_ticker ON candidates(ticker);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    station         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    bracket_low     REAL NOT NULL,
    bracket_high    REAL NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
    predicted_price INTEGER NOT NULL,
    actual_price    INTEGER NOT NULL,
    slippage        INTEGER,
    predicted_edge  REAL NOT NULL,
    mode            TEXT NOT NULL CHECK(mode IN ('paper','live')),
    order_id        TEXT,
    outcome         TEXT,
    pnl             REAL,
    capital_before  REAL NOT NULL,
    capital_after   REAL,
    settled_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_station_ts ON trades(station, ts);
CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);

CREATE TABLE IF NOT EXISTS settlements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    station         TEXT NOT NULL,
    ticker          TEXT NOT NULL UNIQUE,
    bracket_low     REAL NOT NULL,
    bracket_high    REAL NOT NULL,
    actual_high_f   REAL NOT NULL,
    resolved_yes    INTEGER NOT NULL,
    market_final_price INTEGER,
    source          TEXT NOT NULL DEFAULT 'polymarket'
);
CREATE INDEX IF NOT EXISTS idx_settlements_station_ts ON settlements(station, ts);

CREATE TABLE IF NOT EXISTS open_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    station         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
    order_id        TEXT NOT NULL,
    entry_price     INTEGER NOT NULL,
    shares          REAL NOT NULL,
    entry_ts        TEXT NOT NULL,
    stop_loss_cents INTEGER,
    take_profit_cents INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_positions_order ON open_positions(order_id);

CREATE TABLE IF NOT EXISTS risk_state (
    trade_date      TEXT PRIMARY KEY,
    daily_pnl       REAL NOT NULL DEFAULT 0.0,
    open_positions  INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taf_windows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    city        TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    group_type  TEXT NOT NULL,
    temp        REAL,
    wind_kt     REAL,
    sig_wx      TEXT,
    raw_text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_taf_city_from ON taf_windows(city, valid_from);

CREATE TABLE IF NOT EXISTS model_weights (
    city    TEXT NOT NULL,
    model   TEXT NOT NULL,
    date    TEXT NOT NULL,
    weight  REAL NOT NULL,
    rmse    REAL NOT NULL,
    PRIMARY KEY (city, model, date)
);
CREATE INDEX IF NOT EXISTS idx_mw_city_date ON model_weights(city, date);

CREATE TABLE IF NOT EXISTS model_forecast_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    model           TEXT NOT NULL,
    date            TEXT NOT NULL,
    forecast_high_f REAL NOT NULL,
    logged_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mfl_station_model_date
    ON model_forecast_log(station, model, date);

CREATE TABLE IF NOT EXISTS intraday_corrections (
    city           TEXT NOT NULL,
    date           TEXT NOT NULL,
    obs_time       TEXT NOT NULL,
    obs_temp_f     REAL NOT NULL,
    model_temp_f   REAL NOT NULL,
    delta_f        REAL NOT NULL,
    corrected_mu_f REAL NOT NULL,
    decay_factor   REAL NOT NULL,
    PRIMARY KEY (city, date, obs_time)
);
CREATE INDEX IF NOT EXISTS idx_ic_city_date ON intraday_corrections(city, date);
"""


class Database:
    """Wraps a SQLite connection with typed helpers for all MeteoEdge tables."""

    def __init__(self, path: "str | Path" = _DEFAULT_PATH) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Run idempotent migrations on the observations table."""
        for col, definition in [
            ("cadence_min", "INTEGER"),
            ("is_official", "INTEGER DEFAULT 1"),
        ]:
            try:
                self._conn.execute(
                    f"ALTER TABLE observations ADD COLUMN {col} {definition}"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, *args):
        """Context manager exit; closes the connection."""
        self.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------

    def insert_observation(
        self,
        *,
        ts: str,
        station: str,
        temp_f: float,
        temp_native: float,
        unit: str,
        source: str,
        current_high: "float | None" = None,
        raw_json: "str | None" = None,
        cadence_min: "int | None" = None,
        is_official: "int | None" = None,
    ) -> int:
        """Insert a weather observation; returns the new row id.

        cadence_min and is_official are optional and require the #106 schema
        migration (ALTER TABLE adding those columns) to have run first.
        """
        if cadence_min is not None or is_official is not None:
            cur = self._conn.execute(
                "INSERT INTO observations"
                "(ts,station,temp_f,temp_native,unit,current_high,source,raw_json,"
                "cadence_min,is_official) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, temp_f, temp_native, unit, current_high,
                    source, raw_json, cadence_min, is_official,
                ),
            )
        else:
            cur = self._conn.execute(
                "INSERT INTO observations"
                "(ts,station,temp_f,temp_native,unit,current_high,source,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts, station, temp_f, temp_native, unit, current_high, source, raw_json),
            )
        self._conn.commit()
        return cur.lastrowid

    def get_observations(self, station: str, since: str) -> list:
        """Return observations for *station* at or after *since* (ISO timestamp), oldest first."""
        cur = self._conn.execute(
            "SELECT * FROM observations WHERE station=? AND ts>=? ORDER BY ts ASC",
            (station, since),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_latest_observation(self, source: str, station: str) -> "dict | None":
        """Return the most recent observation for a source and station, or None if none exists."""
        cur = self._conn.execute(
            "SELECT * FROM observations WHERE source=? AND station=? ORDER BY ts DESC LIMIT 1",
            (source, station),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # candidates
    # ------------------------------------------------------------------

    def insert_candidate(
        self,
        *,
        ts: str,
        station: str,
        ticker: str,
        bracket_low: float,
        bracket_high: float,
        side: str,
        predicted_price: int,
        predicted_edge: float,
        market_price: int,
        confidence: float,
        minutes_to_settlement: float,
        flagged_first: int = 1,
    ) -> int:
        """Insert a trade candidate; returns the new row id."""
        cur = self._conn.execute(
            "INSERT INTO candidates"
            "(ts,station,ticker,bracket_low,bracket_high,side,"
            "predicted_price,predicted_edge,market_price,confidence,"
            "minutes_to_settlement,flagged_first) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts, station, ticker, bracket_low, bracket_high, side,
                predicted_price, predicted_edge, market_price, confidence,
                minutes_to_settlement, flagged_first,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def count_candidates_today(self, ticker: str) -> int:
        """Return the number of candidates logged today (UTC) for *ticker*."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE ticker=? AND DATE(ts)=DATE('now')",
            (ticker,),
        )
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # trades
    # ------------------------------------------------------------------

    def insert_trade(
        self,
        *,
        ts: str,
        station: str,
        ticker: str,
        bracket_low: float,
        bracket_high: float,
        side: str,
        predicted_price: int,
        actual_price: int,
        predicted_edge: float,
        mode: str,
        capital_before: float,
        slippage: "int | None" = None,
        order_id: "str | None" = None,
        outcome: "str | None" = None,
        pnl: "float | None" = None,
        capital_after: "float | None" = None,
        settled_at: "str | None" = None,
    ) -> int:
        """Insert a trade record; returns the new row id."""
        cur = self._conn.execute(
            "INSERT INTO trades"
            "(ts,station,ticker,bracket_low,bracket_high,side,"
            "predicted_price,actual_price,slippage,predicted_edge,mode,order_id,"
            "outcome,pnl,capital_before,capital_after,settled_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts, station, ticker, bracket_low, bracket_high, side,
                predicted_price, actual_price, slippage, predicted_edge, mode,
                order_id, outcome, pnl, capital_before, capital_after, settled_at,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_trades(self, limit: "int | None" = 50, mode: "str | None" = None) -> list:
        """Return trades ordered by most-recent-first.

        Args:
            limit: Maximum rows to return (``None`` = no limit).
            mode:  Filter to ``'paper'`` or ``'live'`` if provided.
        """
        if mode is not None:
            sql = "SELECT * FROM trades WHERE mode=? ORDER BY ts DESC"
            params: tuple = (mode,)
        else:
            sql = "SELECT * FROM trades ORDER BY ts DESC"
            params = ()
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cur = self._conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # settlements
    # ------------------------------------------------------------------

    def insert_settlement(
        self,
        *,
        ts: str,
        station: str,
        ticker: str,
        bracket_low: float,
        bracket_high: float,
        actual_high_f: float,
        resolved_yes: int,
        market_final_price: "int | None" = None,
        source: str = "polymarket",
    ) -> None:
        """Upsert a settlement record (unique on ticker)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO settlements"
            "(ts,station,ticker,bracket_low,bracket_high,"
            "actual_high_f,resolved_yes,market_final_price,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ts, station, ticker, bracket_low, bracket_high,
                actual_high_f, resolved_yes, market_final_price, source,
            ),
        )
        self._conn.commit()

    def get_settlements(self, station: str, since: str) -> list:
        """Return settlements for *station* at or after *since*, oldest first."""
        cur = self._conn.execute(
            "SELECT * FROM settlements WHERE station=? AND ts>=? ORDER BY ts ASC",
            (station, since),
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # open_positions
    # ------------------------------------------------------------------

    def open_position(
        self,
        *,
        trade_id: int,
        station: str,
        ticker: str,
        token_id: str,
        side: str,
        order_id: str,
        entry_price: int,
        shares: float,
        entry_ts: str,
        stop_loss_cents: "int | None" = None,
        take_profit_cents: "int | None" = None,
    ) -> None:
        """Atomically insert an open position."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO open_positions"
                "(trade_id,station,ticker,token_id,side,"
                "order_id,entry_price,shares,entry_ts,stop_loss_cents,take_profit_cents) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_id, station, ticker, token_id, side, order_id,
                    entry_price, shares, entry_ts, stop_loss_cents, take_profit_cents,
                ),
            )

    def close_position(self, order_id: str) -> None:
        """Atomically remove an open position by order_id."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM open_positions WHERE order_id=?", (order_id,)
            )

    def get_open_positions(self) -> list:
        """Return all open positions ordered by entry time ascending.

        Joins with trades to include bracket_low, bracket_high, and
        predicted_price.  Aliases token_id → no_token_id, entry_price →
        price_cents, and computes size_eur so callers match the JSONL schema.
        """
        cur = self._conn.execute(
            """
            SELECT
                op.id, op.trade_id, op.station,
                op.ticker, op.token_id,
                op.token_id          AS no_token_id,
                op.side, op.order_id,
                op.entry_price,
                op.entry_price       AS price_cents,
                op.shares,
                ROUND(op.shares * op.entry_price / 100.0, 4) AS size_eur,
                op.entry_ts,
                op.stop_loss_cents,
                op.take_profit_cents,
                t.bracket_low,
                t.bracket_high,
                t.predicted_price
            FROM open_positions op
            LEFT JOIN trades t ON t.id = op.trade_id
            ORDER BY op.entry_ts ASC
            """
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # risk_state
    # ------------------------------------------------------------------

    def get_daily_pnl(self, date_str: str) -> float:
        """Return accumulated PnL for *date_str* (YYYY-MM-DD); 0.0 if no row exists."""
        cur = self._conn.execute(
            "SELECT daily_pnl FROM risk_state WHERE trade_date=?", (date_str,)
        )
        row = cur.fetchone()
        return row[0] if row else 0.0

    def upsert_daily_risk(
        self, date_str: str, pnl_delta: float, open_positions: int
    ) -> None:
        """Atomically accumulate *pnl_delta* into the daily risk row for *date_str*.

        On first insert the row is created; on conflict ``daily_pnl`` is incremented
        by ``pnl_delta`` and ``open_positions`` / ``updated_at`` are replaced.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO risk_state(trade_date,daily_pnl,open_positions,updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "daily_pnl=daily_pnl+excluded.daily_pnl, "
                "open_positions=excluded.open_positions, "
                "updated_at=excluded.updated_at",
                (date_str, pnl_delta, open_positions, self._now()),
            )

    # ------------------------------------------------------------------
    # taf_windows
    # ------------------------------------------------------------------

    def insert_taf_window(self, row: dict) -> None:
        """Insert a TAF window record."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO taf_windows(city,issued_at,valid_from,valid_to,group_type,temp,wind_kt,sig_wx,raw_text) "
                "VALUES(:city,:issued_at,:valid_from,:valid_to,:group_type,:temp,:wind_kt,:sig_wx,:raw_text)",
                row,
            )

    def delete_stale_taf_windows(self, city: str, issued_at: str) -> int:
        """Delete all taf_windows rows for *city* with the given *issued_at*.

        Used before re-inserting a freshly fetched TAF to avoid duplicates.
        Returns the number of rows deleted.
        """
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM taf_windows WHERE city=? AND issued_at=?",
                (city, issued_at),
            )
        return cur.rowcount

    def get_taf_windows(self, city: str, from_ts: str, to_ts: str) -> list[dict]:
        """Return taf_windows for *city* where valid_from is in [from_ts, to_ts], ordered by valid_from."""

        cur = self._conn.execute(
            "SELECT * FROM taf_windows "
            "WHERE city=? AND valid_from>=? AND valid_from<=? "
            "ORDER BY valid_from ASC",
            (city, from_ts, to_ts),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # model_weights
    # ------------------------------------------------------------------

    def upsert_model_weight(self, *, city: str, model: str, date: str, weight: float, rmse: float) -> None:
        """Upsert a model weight record (unique on city, model, date)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO model_weights(city,model,date,weight,rmse) VALUES(?,?,?,?,?)",
                (city, model, date, weight, rmse),
            )

    def get_model_weights(self, city: str) -> list[dict]:
        """Return model weights for *city*, ordered by date descending (most recent first)."""
        cur = self._conn.execute(
            "SELECT * FROM model_weights WHERE city=? ORDER BY date DESC",
            (city,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # model_forecast_log
    # ------------------------------------------------------------------

    def upsert_forecast_log(self, *, station: str, model: str, date: str, forecast_high_f: float) -> None:
        """Upsert a forecast log record (unique on station, model, date)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO model_forecast_log"
                "(station,model,date,forecast_high_f,logged_at) VALUES(?,?,?,?,?)",
                (station, model, date, forecast_high_f, self._now()),
            )

    def get_forecast_log(self, station: str, since_date: str) -> list[dict]:
        """Return forecast logs for *station* on or after *since_date*, ordered by date ascending."""
        cur = self._conn.execute(
            "SELECT * FROM model_forecast_log WHERE station=? AND date>=? ORDER BY date ASC",
            (station, since_date),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # intraday_corrections
    # ------------------------------------------------------------------

    def upsert_intraday_correction(
        self,
        *,
        city: str,
        date: str,
        obs_time: str,
        obs_temp_f: float,
        model_temp_f: float,
        delta_f: float,
        corrected_mu_f: float,
        decay_factor: float,
    ) -> None:
        """Upsert an intraday correction record (unique on city, date, obs_time)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO intraday_corrections"
                "(city,date,obs_time,obs_temp_f,model_temp_f,delta_f,corrected_mu_f,decay_factor) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (city, date, obs_time, obs_temp_f, model_temp_f, delta_f, corrected_mu_f, decay_factor),
            )

    def get_intraday_corrections(self, city: str, date: str) -> list[dict]:
        """Return intraday corrections for city on date, ordered by obs_time ascending."""
        cur = self._conn.execute(
            "SELECT * FROM intraday_corrections WHERE city=? AND date=? ORDER BY obs_time ASC",
            (city, date),
        )
        return [dict(r) for r in cur.fetchall()]
