"""SQLite persistence layer for MeteoEdge."""
import os
import sqlite3
import threading
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
    mode            TEXT NOT NULL CHECK(mode IN ('paper','live','shadow')),
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

CREATE TABLE IF NOT EXISTS emos_calibration (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    city                TEXT NOT NULL,
    model_mode          TEXT NOT NULL,
    a                   REAL NOT NULL,
    b                   REAL NOT NULL,
    c                   REAL NOT NULL,
    d                   REAL NOT NULL,
    crps_score          REAL,
    ready_for_promotion INTEGER DEFAULT 0,
    trained_at          TEXT,
    UNIQUE(city, model_mode)
);

CREATE TABLE IF NOT EXISTS emos_mode_override (
    city            TEXT PRIMARY KEY,
    effective_mode  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_overrides (
    station     TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    yes_enabled INTEGER NOT NULL DEFAULT 1,
    no_enabled  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);
"""


class Database:
    """Wraps a SQLite connection with typed helpers for all MeteoEdge tables."""

    def __init__(self, path: "str | Path" = _DEFAULT_PATH) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
        """Run idempotent schema migrations."""
        for table, col, definition in [
            ("observations", "cadence_min", "INTEGER"),
            ("observations", "is_official", "INTEGER DEFAULT 1"),
            ("trades", "actual_fee_cents", "REAL"),
            ("trades", "size_eur", "REAL"),
            ("trades", "close_reason", "TEXT"),
            ("trades", "minutes_to_settlement_at_close", "REAL"),
            ("trades", "bid_depth_at_close", "INTEGER"),
            ("station_overrides", "yes_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("station_overrides", "no_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ]:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {definition}"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

        # Index on trades.close_reason — added after migration ensures column exists
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_close_reason ON trades(close_reason)"
        )
        self._conn.commit()

        # Back-compat: rows where legacy enabled=0 → shadow both sides
        self._conn.execute(
            "UPDATE station_overrides SET yes_enabled=0, no_enabled=0 WHERE enabled=0"
        )
        self._conn.commit()

        # Migration: extend trades.mode CHECK to include 'shadow'.
        # SQLite cannot ALTER a CHECK constraint in place — requires table rebuild.
        # Detect whether the old constraint (without 'shadow') is still present by
        # inspecting sqlite_master for the CREATE TABLE source text.
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if row and "'shadow'" not in row[0]:
            # Temporarily disable FK checks so the DROP TABLE does not violate
            # the open_positions.trade_id → trades(id) foreign key.
            self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                with self._conn:
                    self._conn.execute("DROP TABLE IF EXISTS trades_new")
                    self._conn.execute(
                        """
                        CREATE TABLE trades_new (
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
                            mode            TEXT NOT NULL CHECK(mode IN ('paper','live','shadow')),
                            order_id        TEXT,
                            outcome         TEXT,
                            pnl             REAL,
                            capital_before  REAL NOT NULL,
                            capital_after   REAL,
                            settled_at      TEXT,
                            actual_fee_cents REAL,
                            size_eur        REAL
                        )
                        """
                    )
                    self._conn.execute(
                        "INSERT INTO trades_new SELECT "
                        "id,ts,station,ticker,bracket_low,bracket_high,side,"
                        "predicted_price,actual_price,slippage,predicted_edge,mode,"
                        "order_id,outcome,pnl,capital_before,capital_after,settled_at,"
                        "actual_fee_cents,size_eur "
                        "FROM trades"
                    )
                    self._conn.execute("DROP TABLE trades")
                    self._conn.execute(
                        "ALTER TABLE trades_new RENAME TO trades"
                    )
                    self._conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_trades_station_ts "
                        "ON trades(station, ts)"
                    )
                    self._conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode)"
                    )
            finally:
                self._conn.execute("PRAGMA foreign_keys=ON")

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
        on_insert_callback=None,
    ) -> int:
        """Insert a weather observation; returns the new row id.

        cadence_min and is_official are optional and require the #106 schema
        migration (ALTER TABLE adding those columns) to have run first.

        on_insert_callback: optional callable fired in a daemon thread after
        commit, enabling event-driven intraday correction without blocking the
        collector.
        """
        with self._lock:
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
            row_id = cur.lastrowid
        if on_insert_callback is not None:
            t = threading.Thread(target=on_insert_callback, daemon=True)
            t.start()
        return row_id

    def get_observations(self, station: str, since: str) -> list:
        """Return observations for *station* at or after *since* (ISO timestamp), oldest first."""
        cur = self._conn.execute(
            "SELECT * FROM observations WHERE station=? AND ts>=? ORDER BY ts ASC",
            (station, since),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_observations_multi_station(self, stations: "list[str]", since: str) -> list:
        """Return observations for *stations* (multiple DB keys) at or after *since*, oldest first.

        Used to union city-keyed (high-cadence) and ICAO-keyed (METAR) rows for
        the same physical station so daily-high computation sees all available data.
        Duplicate timestamps across sources are kept — the caller (daily-high logic)
        takes the peak temp, so duplicates are harmless.

        Args:
            stations: List of DB ``station`` values to query (e.g. ``["Singapore", "WSSS"]``).
            since: ISO timestamp lower bound (inclusive).

        Returns:
            List of observation dicts ordered by ts ascending.
        """
        if not stations:
            return []
        placeholders = ",".join("?" * len(stations))
        cur = self._conn.execute(
            f"SELECT * FROM observations WHERE station IN ({placeholders}) AND ts>=? ORDER BY ts ASC",
            (*stations, since),
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
        with self._lock:
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
        size_eur: "float | None" = None,
    ) -> int:
        """Insert a trade record; returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,actual_price,slippage,predicted_edge,mode,order_id,"
                "outcome,pnl,capital_before,capital_after,settled_at,size_eur) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, actual_price, slippage, predicted_edge, mode,
                    order_id, outcome, pnl, capital_before, capital_after, settled_at, size_eur,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_trade_by_order(
        self,
        order_id: str,
        *,
        ticker: "str | None" = None,
        outcome: "str | None" = None,
        pnl: "float | None" = None,
        capital_after: "float | None" = None,
        settled_at: "str | None" = None,
    ) -> int:
        """Update the trade row(s) matching *order_id*; only non-None fields
        are written. Returns the number of rows updated (0 if no match)."""
        fields = {
            "ticker": ticker,
            "outcome": outcome,
            "pnl": pnl,
            "capital_after": capital_after,
            "settled_at": settled_at,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return 0
        set_clause = ", ".join(f"{col}=?" for col in updates)
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    f"UPDATE trades SET {set_clause} WHERE order_id=?",
                    (*updates.values(), order_id),
                )
        return cur.rowcount

    def update_trade_by_id(
        self,
        trade_id: int,
        *,
        outcome: "str | None" = None,
        pnl: "float | None" = None,
        capital_after: "float | None" = None,
        settled_at: "str | None" = None,
    ) -> int:
        """Update the trade row matching *trade_id* by primary key.

        Used for shadow trade settlement where no order_id exists.
        Only non-None fields are written.  Returns the number of rows updated.
        """
        fields = {
            "outcome": outcome,
            "pnl": pnl,
            "capital_after": capital_after,
            "settled_at": settled_at,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return 0
        set_clause = ", ".join(f"{col}=?" for col in updates)
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    f"UPDATE trades SET {set_clause} WHERE id=?",
                    (*updates.values(), trade_id),
                )
        return cur.rowcount

    def update_trade_close_telemetry(
        self,
        order_id: str,
        *,
        close_reason: "str | None" = None,
        minutes_to_settlement_at_close: "float | None" = None,
        bid_depth_at_close: "int | None" = None,
    ) -> int:
        """Write close telemetry columns onto the trade row matching *order_id*.

        Called after every position close to record: close_reason
        (take_profit/forced_exit/stop_loss/settled/manual), minutes remaining to
        settlement at the time of close, and the best-bid depth that was present.
        Returns the number of rows updated (0 if no match).
        """
        fields = {
            "close_reason": close_reason,
            "minutes_to_settlement_at_close": minutes_to_settlement_at_close,
            "bid_depth_at_close": bid_depth_at_close,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return 0
        set_clause = ", ".join(f"{col}=?" for col in updates)
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    f"UPDATE trades SET {set_clause} WHERE order_id=?",
                    (*updates.values(), order_id),
                )
        return cur.rowcount

    def get_close_reason_stats(self) -> list[dict]:
        """Return P&L, win rate, count, avg PnL, and worst PnL grouped by close_reason.

        Covers live and paper trades (excludes shadow).  Trades where close_reason
        is NULL are grouped under 'settled' (legacy rows without telemetry).
        """
        cur = self._conn.execute(
            """
            SELECT
                COALESCE(close_reason, 'settled')                           AS close_reason,
                COUNT(*)                                                     AS count,
                ROUND(SUM(COALESCE(pnl, 0)), 2)                             AS total_pnl,
                ROUND(AVG(COALESCE(pnl, 0)), 4)                             AS avg_pnl,
                ROUND(MIN(COALESCE(pnl, 0)), 4)                             AS worst_pnl,
                SUM(CASE WHEN COALESCE(pnl, 0) > 0 THEN 1 ELSE 0 END)      AS win_count
            FROM trades
            WHERE mode != 'shadow'
              AND pnl IS NOT NULL
            GROUP BY COALESCE(close_reason, 'settled')
            ORDER BY total_pnl DESC
            """
        )
        rows = []
        for row in cur.fetchall():
            count = row["count"] or 0
            win_count = row["win_count"] or 0
            rows.append({
                "close_reason": row["close_reason"],
                "count": count,
                "total_pnl": float(row["total_pnl"] or 0.0),
                "avg_pnl": float(row["avg_pnl"] or 0.0),
                "worst_pnl": float(row["worst_pnl"] or 0.0),
                "win_rate": round(win_count / count, 4) if count else None,
            })
        return rows

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

    def get_unsettled_shadow_trades(self, target_date: str) -> list:
        """Return shadow trades for *target_date* (YYYY-MM-DD) that are not yet settled."""
        cur = self._conn.execute(
            "SELECT * FROM trades "
            "WHERE mode='shadow' AND settled_at IS NULL AND DATE(ts)=?",
            (target_date,),
        )
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM open_positions WHERE order_id=?", (order_id,)
                )

    def close_positions_by_token(self, token_id: str) -> int:
        """Remove all open_positions rows for *token_id* (market resolved).

        Returns the number of rows deleted.
        """
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM open_positions WHERE token_id=?", (token_id,)
                )
        return cur.rowcount

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

    def get_open_position_by_token(self, token_id: str) -> list[dict]:
        """Return open positions for a single token_id (parameterised WHERE clause).

        Avoids full table scan by filtering at the SQL level for the manual sell path.
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
            WHERE op.token_id = ?
            ORDER BY op.entry_ts ASC
            """,
            (token_id,),
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
        with self._lock:
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

    def add_settled_pnl(self, date_str: str, pnl_delta: float) -> None:
        """Accumulate settlement PnL into the daily risk row for *date_str*
        without touching the open_positions counter."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO risk_state(trade_date,daily_pnl,open_positions,updated_at) "
                    "VALUES(?,?,0,?) "
                    "ON CONFLICT(trade_date) DO UPDATE SET "
                    "daily_pnl=daily_pnl+excluded.daily_pnl, "
                    "updated_at=excluded.updated_at",
                    (date_str, pnl_delta, self._now()),
                )

    # ------------------------------------------------------------------
    # taf_windows
    # ------------------------------------------------------------------

    def insert_taf_window(self, row: dict) -> None:
        """Insert a TAF window record."""
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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

    def get_trailing_deltas(self, city: str, window_days: int) -> list[float]:
        """Return delta_f values for city over the trailing window_days calendar days."""
        from datetime import date as _date, timedelta
        since_date = (_date.today() - timedelta(days=window_days)).isoformat()
        cur = self._conn.execute(
            "SELECT delta_f FROM intraday_corrections "
            "WHERE city=? AND date>=? ORDER BY date ASC, obs_time ASC",
            (city, since_date),
        )
        return [float(row[0]) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # emos_calibration
    # ------------------------------------------------------------------

    def upsert_emos_coefficients(
        self, *, city: str, model_mode: str,
        a: float, b: float, c: float, d: float,
        crps_score: "float | None" = None,
        trained_at: "str | None" = None,
        ready_for_promotion: int = 0,
    ) -> None:
        """Insert or replace EMOS calibration coefficients for a city/mode pair."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO emos_calibration
                   (city, model_mode, a, b, c, d, crps_score, ready_for_promotion, trained_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (city, model_mode, a, b, c, d, crps_score, ready_for_promotion, trained_at),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # stations overview
    # ------------------------------------------------------------------

    def get_stations_last_obs_ts(self) -> dict:
        """Return the most-recent observation timestamp per station.

        Returns a dict mapping station → ISO timestamp string (or None when no
        observation exists for that station).  Uses the (station, ts) index for
        an efficient MAX(ts) GROUP BY scan.
        """
        cur = self._conn.execute(
            "SELECT station, MAX(ts) AS last_obs_ts FROM observations GROUP BY station"
        )
        return {row["station"]: row["last_obs_ts"] for row in cur.fetchall()}

    def get_stations_open_positions_count(self) -> dict:
        """Return the count of open positions per station.

        Returns a dict mapping station → integer count (0 for stations with no
        open positions).
        """
        cur = self._conn.execute(
            "SELECT station, COUNT(*) AS cnt FROM open_positions GROUP BY station"
        )
        return {row["station"]: row["cnt"] for row in cur.fetchall()}

    def get_stations_trade_stats(self) -> dict:
        """Return trade stats per station: trade_count, filled_count, win_rate, total_pnl, last_trade_ts.

        Mirrors the logic in the /stations endpoint but runs as a single SQL
        aggregation instead of loading all trades into Python.  Returns a dict
        mapping station → stats dict.
        """
        cur = self._conn.execute(
            """
            SELECT
                station,
                COUNT(*)                                                                        AS trade_count,
                SUM(CASE WHEN outcome='filled' AND COALESCE(pnl,0) != 0 THEN 1 ELSE 0 END)    AS filled_count,
                SUM(CASE WHEN outcome='filled' AND pnl > 0 THEN 1 ELSE 0 END)                  AS win_count,
                SUM(COALESCE(pnl, 0))                                                           AS total_pnl,
                MAX(ts)                                                                         AS last_trade_ts
            FROM trades
            GROUP BY station
            """
        )
        result = {}
        for row in cur.fetchall():
            filled = row["filled_count"] or 0
            win_count = row["win_count"] or 0
            win_rate = round(win_count / filled, 4) if filled else None
            result[row["station"]] = {
                "trade_count": row["trade_count"],
                "filled_count": filled,
                "win_rate": win_rate,
                "total_pnl": round(float(row["total_pnl"] or 0.0), 2),
                "last_trade_ts": row["last_trade_ts"],
            }
        return result

    def get_emos_coefficients(self, city: str, model_mode: str) -> "dict | None":
        """Return EMOS coefficients dict for (city, model_mode), or None if not found."""
        cur = self._conn.execute(
            "SELECT a, b, c, d, crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration WHERE city=? AND model_mode=?",
            (city, model_mode),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "a": row[0], "b": row[1], "c": row[2], "d": row[3],
            "crps_score": row[4], "ready_for_promotion": row[5], "trained_at": row[6],
        }

    def get_all_emos_calibration(self) -> list[dict]:
        """Return all rows from emos_calibration, one dict per (city, model_mode) pair."""
        cur = self._conn.execute(
            "SELECT city, model_mode, a, b, c, d, crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration ORDER BY city, model_mode"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_settled_days_available(self, station: str) -> int:
        """Return count of distinct dates in model_forecast_log joined to observations for a station.

        A "settled day" is a date where a forecast exists AND a METAR observation
        (source='metar') also exists, meaning the actual high can be determined.
        """
        cur = self._conn.execute(
            """
            SELECT COUNT(DISTINCT mfl.date)
            FROM model_forecast_log mfl
            WHERE mfl.station = ?
              AND EXISTS (
                  SELECT 1 FROM observations o
                  WHERE o.station = ?
                    AND o.source = 'metar'
                    AND DATE(o.ts) = mfl.date
              )
            """,
            (station, station),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def get_emos_effective_mode(self, city: str) -> "str | None":
        """Return the effective EMOS mode override for a city, or None if not set."""
        cur = self._conn.execute(
            "SELECT effective_mode FROM emos_mode_override WHERE city=?",
            (city,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def set_emos_effective_mode(self, city: str, effective_mode: str) -> None:
        """Upsert the effective EMOS mode for a city."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO emos_mode_override(city, effective_mode, updated_at) "
                "VALUES(?, ?, ?)",
                (city, effective_mode, self._now()),
            )
            self._conn.commit()

    def toggle_emos_ready_for_promotion(self, city: str) -> "int | None":
        """Toggle ready_for_promotion (0 ↔ 1) on the emos_shadow row for city.

        Returns the new value (0 or 1), or None if no shadow row exists.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT ready_for_promotion FROM emos_calibration "
                "WHERE city=? AND model_mode='emos_shadow'",
                (city,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            new_val = 0 if row[0] else 1
            self._conn.execute(
                "UPDATE emos_calibration SET ready_for_promotion=? "
                "WHERE city=? AND model_mode='emos_shadow'",
                (new_val, city),
            )
            self._conn.commit()
        return new_val

    # ------------------------------------------------------------------
    # bot_config
    # ------------------------------------------------------------------

    def get_config(self, key: str) -> "str | None":
        """Return the stored value for *key*, or None if no row exists."""
        cur = self._conn.execute(
            "SELECT value FROM bot_config WHERE key=?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        """Upsert a config value. Thread-safe via the existing RLock."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO bot_config(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, self._now()),
            )
            self._conn.commit()

    def get_all_config(self) -> "dict[str, str]":
        """Return all bot_config rows as a plain {key: value} dict."""
        cur = self._conn.execute("SELECT key, value FROM bot_config")
        return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # station_overrides
    # ------------------------------------------------------------------

    def get_station_override(self, station: str) -> "dict | None":
        """Return {yes_enabled, no_enabled} for *station*, or None if no override exists."""
        cur = self._conn.execute(
            "SELECT yes_enabled, no_enabled FROM station_overrides WHERE station=?", (station,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"yes_enabled": bool(row[0]), "no_enabled": bool(row[1])}

    def set_station_override(self, station: str, yes_enabled: bool, no_enabled: bool) -> None:
        """Upsert yes_enabled and no_enabled for *station*. Thread-safe via the existing RLock."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO station_overrides(station, yes_enabled, no_enabled, updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(station) DO UPDATE SET "
                "yes_enabled=excluded.yes_enabled, no_enabled=excluded.no_enabled, "
                "updated_at=excluded.updated_at",
                (station, int(yes_enabled), int(no_enabled), self._now()),
            )
            self._conn.commit()

    def get_all_station_overrides(self) -> "dict[str, dict]":
        """Return all station_overrides rows as {station: {yes_enabled, no_enabled}}."""
        cur = self._conn.execute(
            "SELECT station, yes_enabled, no_enabled FROM station_overrides"
        )
        return {
            row[0]: {"yes_enabled": bool(row[1]), "no_enabled": bool(row[2])}
            for row in cur.fetchall()
        }
