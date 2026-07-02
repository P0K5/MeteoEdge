"""SQLite persistence layer for MeteoEdge."""
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
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
    flagged_first   INTEGER NOT NULL DEFAULT 1,
    direction       TEXT NOT NULL DEFAULT 'high'
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
    settled_at      TEXT,
    direction       TEXT NOT NULL DEFAULT 'high'
);
CREATE INDEX IF NOT EXISTS idx_trades_station_ts ON trades(station, ts);
CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_shadow_unique
    ON trades(station, bracket_low, bracket_high, side, substr(ts,1,10))
    WHERE mode='shadow';

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
    source          TEXT NOT NULL DEFAULT 'polymarket',
    direction       TEXT NOT NULL DEFAULT 'high'
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

CREATE TABLE IF NOT EXISTS intraday_corrections (
    city           TEXT NOT NULL,
    station        TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    date           TEXT NOT NULL,
    obs_time       TEXT NOT NULL,
    obs_temp_f     REAL NOT NULL,
    model_temp_f   REAL NOT NULL,
    delta_f        REAL NOT NULL,
    corrected_mu_f REAL NOT NULL,
    decay_factor   REAL NOT NULL,
    basis_weights  TEXT NOT NULL DEFAULT '{"open_meteo": 1.0}',
    PRIMARY KEY (city, station, source, date, obs_time)
);
CREATE INDEX IF NOT EXISTS idx_ic_city_date ON intraday_corrections(city, date);

CREATE TABLE IF NOT EXISTS emos_calibration (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    city                TEXT NOT NULL,
    model_mode          TEXT NOT NULL,
    forecast_source     TEXT NOT NULL DEFAULT 'nws_open_meteo',
    a                   REAL NOT NULL,
    b                   REAL NOT NULL,
    c                   REAL NOT NULL,
    d                   REAL NOT NULL,
    crps_score          REAL,
    ready_for_promotion INTEGER DEFAULT 0,
    trained_at          TEXT,
    UNIQUE(city, model_mode, forecast_source)
);

CREATE TABLE IF NOT EXISTS emos_mode_override (
    city            TEXT PRIMARY KEY,
    effective_mode  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardrail_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    station     TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_value   REAL,
    adj_value   REAL,
    delta       REAL,
    ticker      TEXT
);

CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_overrides (
    station        TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 1,
    yes_enabled    INTEGER NOT NULL DEFAULT 1,
    no_enabled     INTEGER NOT NULL DEFAULT 1,
    low_no_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emos_crps_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    city       TEXT NOT NULL,
    date       TEXT NOT NULL,
    crps_score REAL NOT NULL,
    model_mode TEXT NOT NULL DEFAULT 'emos_shadow',
    logged_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deb_weight_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    city        TEXT NOT NULL,
    date        TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    logged_at   TEXT NOT NULL
);
"""


def compute_win_rate(filled: int, wins: int) -> "float | None":
    """Canonical win-rate definition: wins / filled. None when filled == 0.

    Args:
        filled: Count of settled trades (outcome IN ('filled','sold') AND pnl IS NOT NULL).
        wins: Count of profitable settled trades (outcome IN ('filled','sold') AND pnl > 0).

    Returns:
        wins / filled if filled > 0, else None.
    """
    return wins / filled if filled else None


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
            ("candidates", "direction", "TEXT NOT NULL DEFAULT 'high'"),
            ("trades", "direction", "TEXT NOT NULL DEFAULT 'high'"),
            ("settlements", "direction", "TEXT NOT NULL DEFAULT 'high'"),
            ("station_overrides", "low_no_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("emos_calibration", "forecast_source", "TEXT NOT NULL DEFAULT 'nws_open_meteo'"),
            # Issue #551 stage 1: nullable raw (pre-MODEL_PROB_CAP) probability,
            # logged alongside the existing capped value. NULL for rows written
            # before this migration.
            ("candidates", "p_yes_raw", "REAL"),
            ("trades", "p_yes_raw", "REAL"),
            # Issue #553: sample count for model_weights to distinguish calibrated
            # models (sample_count >= MIN_SAMPLES) from cold-start (sample_count < MIN_SAMPLES).
            ("model_weights", "sample_count", "INTEGER DEFAULT 0"),
            # Issue #572: snapshot of the DEB weights dict used to build the
            # intraday consensus basis for this delta, so the residual layer
            # can later segment pre/post-fix deltas instead of mixing the
            # open_meteo-only regime and the live-DEB-weighted regime in one
            # rolling window. Default matches the legacy hardcoded basis, so
            # historical rows (written before this migration) are tagged
            # consistently with genuine fallback rows.
            ("intraday_corrections", "basis_weights", "TEXT NOT NULL DEFAULT '{\"open_meteo\": 1.0}'"),
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
        # Composite indices on (station, direction) — added after ALTER TABLE migration
        for tbl in ("candidates", "trades", "settlements"):
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{tbl}_station_direction "
                f"ON {tbl}(station, direction)"
            )
        self._conn.commit()

        # Migration: add station + source columns to intraday_corrections and
        # update PK to (city, station, source, date, obs_time).
        # SQLite cannot modify a PRIMARY KEY in place — requires table rebuild.
        # Detect old schema by checking whether 'station' column is absent.
        ic_info = self._conn.execute(
            "PRAGMA table_info(intraday_corrections)"
        ).fetchall()
        ic_cols = {row[1] for row in ic_info}
        if "station" not in ic_cols:
            with self._conn:
                self._conn.execute("DROP TABLE IF EXISTS intraday_corrections_new")
                self._conn.execute(
                    """
                    CREATE TABLE intraday_corrections_new (
                        city           TEXT NOT NULL,
                        station        TEXT NOT NULL DEFAULT '',
                        source         TEXT NOT NULL DEFAULT '',
                        date           TEXT NOT NULL,
                        obs_time       TEXT NOT NULL,
                        obs_temp_f     REAL NOT NULL,
                        model_temp_f   REAL NOT NULL,
                        delta_f        REAL NOT NULL,
                        corrected_mu_f REAL NOT NULL,
                        decay_factor   REAL NOT NULL,
                        basis_weights  TEXT NOT NULL DEFAULT '{"open_meteo": 1.0}',
                        PRIMARY KEY (city, station, source, date, obs_time)
                    )
                    """
                )
                self._conn.execute(
                    """
                    INSERT INTO intraday_corrections_new
                        (city, station, source, date, obs_time,
                         obs_temp_f, model_temp_f, delta_f,
                         corrected_mu_f, decay_factor, basis_weights)
                    SELECT city, '' AS station, '' AS source, date, obs_time,
                           obs_temp_f, model_temp_f, delta_f,
                           corrected_mu_f, decay_factor,
                           '{"open_meteo": 1.0}' AS basis_weights
                    FROM intraday_corrections
                    """
                )
                self._conn.execute("DROP TABLE intraday_corrections")
                self._conn.execute(
                    "ALTER TABLE intraday_corrections_new "
                    "RENAME TO intraday_corrections"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ic_city_date "
                    "ON intraday_corrections(city, date)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ic_city_station_source_date "
                    "ON intraday_corrections(city, station, source, date)"
                )

        # Back-compat: rows where legacy enabled=0 → shadow both sides
        self._conn.execute(
            "UPDATE station_overrides SET yes_enabled=0, no_enabled=0 WHERE enabled=0"
        )
        self._conn.commit()

        # Migration: extend model_forecast_log to include lead_hours, issued_at, sigma_f.
        # Strategy (idempotent):
        #   1. If model_forecast_log_legacy_v1 does not exist: rename current table to legacy,
        #      create new table with full schema (including new columns and new unique index).
        #   2. If legacy table already exists but current table has the old unique index
        #      (station, model, date) — drop+recreate current table (partial migration).
        #   3. If both legacy table exists and current table has the new schema — no-op.
        self._migrate_forecast_log()

        # Migration: add partial UNIQUE index for shadow-trade dedup (issue #376).
        # CREATE UNIQUE INDEX IF NOT EXISTS is idempotent — safe to run on every startup.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_shadow_unique "
            "ON trades(station, bracket_low, bracket_high, side, substr(ts,1,10)) "
            "WHERE mode='shadow'"
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
                            size_eur        REAL,
                            direction       TEXT NOT NULL DEFAULT 'high',
                            p_yes_raw       REAL
                        )
                        """
                    )
                    # direction/p_yes_raw may not exist in old table — coalesce
                    old_cols_q = self._conn.execute(
                        "PRAGMA table_info(trades)"
                    ).fetchall()
                    old_col_names = {r[1] for r in old_cols_q}
                    direction_expr = (
                        "direction" if "direction" in old_col_names else "'high'"
                    )
                    p_yes_raw_expr = (
                        "p_yes_raw" if "p_yes_raw" in old_col_names else "NULL"
                    )
                    self._conn.execute(
                        "INSERT INTO trades_new SELECT "
                        "id,ts,station,ticker,bracket_low,bracket_high,side,"
                        "predicted_price,actual_price,slippage,predicted_edge,mode,"
                        "order_id,outcome,pnl,capital_before,capital_after,settled_at,"
                        f"actual_fee_cents,size_eur,{direction_expr},{p_yes_raw_expr} "
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

    def _migrate_forecast_log(self) -> None:
        """Idempotent migration for model_forecast_log.

        Decision tree (checked in order):

        A) New columns already present (lead_hours, sigma_f) → ensure index exists,
           no-op otherwise.  This covers both fresh DBs opened a second time and
           fully-migrated production DBs.

        B) New columns absent AND existing table has rows (pre-#422 production data) →
           rename to model_forecast_log_legacy_v1 (preserving all data), then create
           a fresh empty table with the full schema.

        C) New columns absent AND existing table is empty (first-ever DB open on
           fresh install) → ALTER TABLE to add the three new columns and create index.
           Avoids creating a spurious empty legacy table on fresh installations.

        D) Partial migration (legacy present, current table lacks new columns) →
           rebuild current table in place (rare crash-recovery path).

        The legacy table is never dropped; it is kept as a permanent read-only
        audit trail of nowcast-snapshot rows captured before the migration.
        """
        # Inspect current table columns
        mfl_info = self._conn.execute(
            "PRAGMA table_info(model_forecast_log)"
        ).fetchall()
        mfl_cols = {row[1] for row in mfl_info}
        has_new_schema = "lead_hours" in mfl_cols and "sigma_f" in mfl_cols

        # Check whether legacy table exists
        legacy_exists = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='model_forecast_log_legacy_v1'"
        ).fetchone() is not None

        _new_table_ddl = """
            CREATE TABLE model_forecast_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                station         TEXT NOT NULL,
                model           TEXT NOT NULL,
                date            TEXT NOT NULL,
                forecast_high_f REAL NOT NULL,
                logged_at       TEXT NOT NULL,
                lead_hours      INTEGER,
                issued_at       TEXT,
                sigma_f         REAL
            )
        """
        _new_index_ddl = (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mfl_station_model_date_lead "
            "ON model_forecast_log(station, model, date, lead_hours)"
        )

        if has_new_schema:
            # Case A: schema already correct — just ensure index exists.
            self._conn.execute(_new_index_ddl)
            self._conn.commit()
            return

        # Count existing rows to distinguish fresh vs pre-migration DB.
        row_count = self._conn.execute(
            "SELECT COUNT(*) FROM model_forecast_log"
        ).fetchone()[0]

        if not legacy_exists and row_count > 0:
            # Case B: pre-#422 production DB with nowcast snapshots.
            # Rename old table to legacy, drop its orphaned index, create fresh table.
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE model_forecast_log "
                    "RENAME TO model_forecast_log_legacy_v1"
                )
                self._conn.execute(
                    "DROP INDEX IF EXISTS idx_mfl_station_model_date"
                )
                self._conn.execute(_new_table_ddl)
                self._conn.execute(_new_index_ddl)

        elif not legacy_exists and row_count == 0:
            # Case C: fresh install — add new columns to the empty table in place.
            with self._conn:
                for col, defn in [
                    ("lead_hours", "INTEGER"),
                    ("issued_at", "TEXT"),
                    ("sigma_f", "REAL"),
                ]:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE model_forecast_log ADD COLUMN {col} {defn}"
                        )
                    except sqlite3.OperationalError:
                        pass  # column already exists
                # Drop old index if it exists (single-column key won't work for v2)
                self._conn.execute(
                    "DROP INDEX IF EXISTS idx_mfl_station_model_date"
                )
                self._conn.execute(_new_index_ddl)

        else:
            # Case D: legacy exists but current table still lacks new columns
            # (crash-recovery / partial migration path).
            with self._conn:
                self._conn.execute("DROP TABLE IF EXISTS model_forecast_log_new")
                self._conn.execute(_new_table_ddl.replace(
                    "CREATE TABLE model_forecast_log",
                    "CREATE TABLE model_forecast_log_new",
                ))
                self._conn.execute(
                    """
                    INSERT INTO model_forecast_log_new
                        (station, model, date, forecast_high_f, logged_at)
                    SELECT station, model, date, forecast_high_f, logged_at
                    FROM model_forecast_log
                    """
                )
                self._conn.execute("DROP TABLE model_forecast_log")
                self._conn.execute(
                    "ALTER TABLE model_forecast_log_new RENAME TO model_forecast_log"
                )
                self._conn.execute(_new_index_ddl)

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
        direction: str = "high",
        p_yes_raw: "float | None" = None,
    ) -> int:
        """Insert a trade candidate; returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO candidates"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,predicted_edge,market_price,confidence,"
                "minutes_to_settlement,flagged_first,direction,p_yes_raw) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, predicted_edge, market_price, confidence,
                    minutes_to_settlement, flagged_first, direction, p_yes_raw,
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
        direction: str = "high",
        p_yes_raw: "float | None" = None,
    ) -> int:
        """Insert a trade record; returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,actual_price,slippage,predicted_edge,mode,order_id,"
                "outcome,pnl,capital_before,capital_after,settled_at,size_eur,direction,"
                "p_yes_raw) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, actual_price, slippage, predicted_edge, mode,
                    order_id, outcome, pnl, capital_before, capital_after, settled_at,
                    size_eur, direction, p_yes_raw,
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

    def update_trade_costs(
        self,
        order_id: str,
        *,
        actual_fee_cents: "float | None" = None,
        size_eur: "float | None" = None,
    ) -> int:
        """Backfill cost accounting columns on the trade row matching *order_id*.

        Only non-None values are written; returns rows updated (0 = no match).
        """
        fields = {"actual_fee_cents": actual_fee_cents, "size_eur": size_eur}
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

    def get_trade_by_order_id(self, order_id: str) -> "dict | None":
        """Return the trade row matching *order_id*, or None if not found.

        Returns the full trade row dict with all column fields.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM trades WHERE order_id=? LIMIT 1",
                (order_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_trade_cost_summary(self, days: int = 30) -> dict:
        """Return cost-accounting totals for live trades over the trailing *days*.

        Covers only live, closed trades so paper/shadow noise is excluded.
        """
        since = (date.today() - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            """
            SELECT
                COUNT(*)                                          AS trade_count,
                SUM(CASE WHEN actual_fee_cents IS NOT NULL
                         THEN 1 ELSE 0 END)                      AS fee_populated_count,
                ROUND(SUM(COALESCE(actual_fee_cents, 0)) / 100.0, 4)
                                                                  AS total_fee_eur,
                ROUND(AVG(COALESCE(actual_fee_cents, 0)) / 100.0, 4)
                                                                  AS avg_fee_eur,
                ROUND(SUM(COALESCE(size_eur, 0)), 4)              AS total_size_eur,
                ROUND(SUM(COALESCE(pnl, 0)), 4)                   AS total_pnl
            FROM trades
            WHERE mode = 'live'
              AND outcome = 'sold'
              AND ts >= ?
            """,
            (since,),
        )
        row = cur.fetchone()
        if row is None:
            return {
                "period_days": days, "trade_count": 0, "fee_populated_count": 0,
                "total_fee_eur": 0.0, "avg_fee_eur": 0.0,
                "total_size_eur": 0.0, "total_pnl": 0.0,
            }
        return {
            "period_days": days,
            "trade_count": int(row["trade_count"] or 0),
            "fee_populated_count": int(row["fee_populated_count"] or 0),
            "total_fee_eur": float(row["total_fee_eur"] or 0.0),
            "avg_fee_eur": float(row["avg_fee_eur"] or 0.0),
            "total_size_eur": float(row["total_size_eur"] or 0.0),
            "total_pnl": float(row["total_pnl"] or 0.0),
        }

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

    def get_trades(
        self,
        limit: "int | None" = 50,
        mode: "str | None" = None,
        direction: "str | None" = None,
    ) -> list:
        """Return trades ordered by most-recent-first.

        Args:
            limit:     Maximum rows to return (``None`` = no limit).
            mode:      Filter to ``'paper'`` or ``'live'`` if provided.
            direction: Filter to ``'high'`` or ``'low'`` if provided.
        """
        conditions = []
        params: list = []
        if mode is not None:
            conditions.append("mode=?")
            params.append(mode)
        if direction is not None:
            conditions.append("direction=?")
            params.append(direction)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM trades{where} ORDER BY ts DESC"
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

    def upsert_shadow_trade(
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
        capital_before: float = 0.0,
        direction: str = "high",
        p_yes_raw: "float | None" = None,
    ) -> tuple[int, bool]:
        """Insert a shadow trade row, or update actual_price if one already exists today.

        Dedup key: (station, bracket_low, bracket_high, side, direction, day).

        Returns (row_id, created) where created=True means a new row was inserted,
        False means an existing row's actual_price was updated.
        """
        today = ts[:10]  # YYYY-MM-DD prefix
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM trades "
                "WHERE mode='shadow' AND station=? AND bracket_low=? AND bracket_high=? "
                "AND side=? AND direction=? AND substr(ts,1,10)=?",
                (station, bracket_low, bracket_high, side, direction, today),
            )
            row = cur.fetchone()
            if row is not None:
                # Existing row: update actual_price to the latest observed value
                self._conn.execute(
                    "UPDATE trades SET actual_price=? WHERE id=?",
                    (actual_price, row[0]),
                )
                self._conn.commit()
                return row[0], False
            # No existing row: insert normally
            cur = self._conn.execute(
                "INSERT INTO trades"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,actual_price,slippage,predicted_edge,mode,order_id,"
                "outcome,pnl,capital_before,capital_after,settled_at,direction,p_yes_raw) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, actual_price, None, predicted_edge, "shadow",
                    None, None, None, capital_before, None, None, direction, p_yes_raw,
                ),
            )
            self._conn.commit()
            return cur.lastrowid, True

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
        direction: str = "high",
    ) -> None:
        """Upsert a settlement record (unique on ticker)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settlements"
                "(ts,station,ticker,bracket_low,bracket_high,"
                "actual_high_f,resolved_yes,market_final_price,source,direction) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high,
                    actual_high_f, resolved_yes, market_final_price, source, direction,
                ),
            )
            self._conn.commit()

    def get_settlements(
        self,
        station: str,
        since: str,
        direction: "str | None" = None,
    ) -> list:
        """Return settlements for *station* at or after *since*, oldest first.

        Args:
            direction: Optional filter — ``'high'`` or ``'low'``.
        """
        if direction is not None:
            cur = self._conn.execute(
                "SELECT * FROM settlements WHERE station=? AND ts>=? AND direction=?"
                " ORDER BY ts ASC",
                (station, since, direction),
            )
        else:
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

    def upsert_model_weight(self, *, city: str, model: str, date: str, weight: float, rmse: float, sample_count: int = 0) -> None:
        """Upsert a model weight record (unique on city, model, date).

        Args:
            sample_count: number of matched-pair samples used to compute RMSE.
                         Values < MIN_SAMPLES indicate cold-start (fallback) weights.
        """
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO model_weights(city,model,date,weight,rmse,sample_count) VALUES(?,?,?,?,?,?)",
                    (city, model, date, weight, rmse, sample_count),
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
        """Upsert a forecast log record (unique on station, model, date, lead_hours=NULL).

        Legacy compat: sets lead_hours=NULL. New code should use upsert_forecast_log_v2()
        which accepts lead_hours and sigma_f for EMOS-quality captures.
        """
        with self._lock:
            with self._conn:
                # DELETE + INSERT to handle NULL lead_hours dedup (NULL != NULL in UNIQUE index).
                self._conn.execute(
                    "DELETE FROM model_forecast_log "
                    "WHERE station=? AND model=? AND date=? AND lead_hours IS NULL",
                    (station, model, date),
                )
                self._conn.execute(
                    "INSERT INTO model_forecast_log"
                    "(station,model,date,forecast_high_f,logged_at,lead_hours,issued_at,sigma_f)"
                    " VALUES(?,?,?,?,?,NULL,NULL,NULL)",
                    (station, model, date, forecast_high_f, self._now()),
                )

    def upsert_forecast_log_v2(
        self,
        *,
        station: str,
        model: str,
        date: str,
        forecast_high_f: float,
        lead_hours: int,
        issued_at: "str | None" = None,
        sigma_f: "float | None" = None,
    ) -> None:
        """Upsert a forecast log record keyed on (station, model, date, lead_hours).

        This is the v2 writer used by the cron capture worker. Each (station, model,
        date, lead_hours) combination is stored as an independent row, enabling EMOS
        to train on forecast-vs-actual triples at fixed lead times.

        Args:
            station:        METAR station code (e.g. "KORD").
            model:          Model name: "nws", "open_meteo", or "gfs".
            date:           Forecast target date as YYYY-MM-DD string.
            forecast_high_f: Forecasted daily high in °F.
            lead_hours:     Integer lead time in hours (e.g. 24, 12, 6, 3).
            issued_at:      UTC ISO-8601 timestamp when this capture was taken. Defaults to now.
            sigma_f:        Ensemble spread (°F); None for sources that don't expose it.
        """
        now = self._now()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO model_forecast_log"
                    "(station,model,date,forecast_high_f,logged_at,lead_hours,issued_at,sigma_f)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (station, model, date, forecast_high_f, now,
                     lead_hours, issued_at or now, sigma_f),
                )

    def get_forecast_log(self, station: str, since_date: str) -> list[dict]:
        """Return forecast logs for *station* on or after *since_date*, ordered by date ascending.

        Returns all rows (all lead_hours). Callers that need a specific lead-time bin
        should use get_forecast_log_by_lead().
        """
        cur = self._conn.execute(
            "SELECT * FROM model_forecast_log WHERE station=? AND date>=? ORDER BY date ASC",
            (station, since_date),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_forecast_log_for_date(self, station: str, date: str) -> list[dict]:
        """Return all model_forecast_log rows for *station* on the exact *date*.

        Unlike get_forecast_log(), this is an exact-date match (not >=). Used by
        get_ensemble_distribution() (issue #511) to build the per-model snapshot
        for a single trading day. Returns all lead_hours rows; callers that need
        the closest-to-valid capture should group by model and keep the lowest
        lead_hours.
        """
        cur = self._conn.execute(
            "SELECT * FROM model_forecast_log WHERE station=? AND date=? ORDER BY model ASC",
            (station, date),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_forecast_log_by_lead(
        self, station: str, since_date: str, lead_hours: int
    ) -> list[dict]:
        """Return forecast logs filtered to a specific lead-time bin.

        Args:
            station:    METAR station code.
            since_date: Earliest date (YYYY-MM-DD) inclusive.
            lead_hours: Lead-time bin to filter on (e.g. 24).

        Returns:
            List of row dicts ordered by date ascending.
        """
        cur = self._conn.execute(
            "SELECT * FROM model_forecast_log "
            "WHERE station=? AND date>=? AND lead_hours=? ORDER BY date ASC",
            (station, since_date, lead_hours),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # intraday_corrections
    # ------------------------------------------------------------------

    def upsert_intraday_correction(
        self,
        *,
        city: str,
        station: str = "",
        source: str = "",
        date: str,
        obs_time: str,
        obs_temp_f: float,
        model_temp_f: float,
        delta_f: float,
        corrected_mu_f: float,
        decay_factor: float,
        basis_weights: str = '{"open_meteo": 1.0}',
    ) -> None:
        """Upsert an intraday correction record (unique on city, station, source, date, obs_time).

        Args:
            basis_weights: JSON-encoded snapshot of the DEB weights dict used to
                build the intraday consensus basis for this delta (issue #572).
                Defaults to the legacy open_meteo-only basis for callers that
                don't pass one.
        """
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO intraday_corrections"
                    "(city,station,source,date,obs_time,"
                    "obs_temp_f,model_temp_f,delta_f,corrected_mu_f,decay_factor,"
                    "basis_weights) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (city, station, source, date, obs_time,
                     obs_temp_f, model_temp_f, delta_f, corrected_mu_f, decay_factor,
                     basis_weights),
                )

    def get_intraday_corrections(self, city: str, date: str) -> list[dict]:
        """Return intraday corrections for city on date, ordered by obs_time ascending."""
        cur = self._conn.execute(
            "SELECT * FROM intraday_corrections WHERE city=? AND date=? ORDER BY obs_time ASC",
            (city, date),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_trailing_deltas(
        self,
        city: str,
        window_days: int,
        *,
        station: "str | None" = None,
        source: "str | None" = None,
    ) -> list[float]:
        """Return delta_f values for city over the trailing window_days calendar days.

        Optional *station* and *source* filters narrow the query to a specific
        (station, source) pair.  When both are None the query is city-wide (legacy
        behaviour).
        """
        since_date = (date.today() - timedelta(days=window_days)).isoformat()
        if station is not None and source is not None:
            cur = self._conn.execute(
                "SELECT delta_f FROM intraday_corrections "
                "WHERE city=? AND station=? AND source=? AND date>=? "
                "ORDER BY date ASC, obs_time ASC",
                (city, station, source, since_date),
            )
        else:
            cur = self._conn.execute(
                "SELECT delta_f FROM intraday_corrections "
                "WHERE city=? AND date>=? ORDER BY date ASC, obs_time ASC",
                (city, since_date),
            )
        return [float(row[0]) for row in cur.fetchall()]

    def get_distinct_pairs(self, city: str, since_date: str) -> "list[tuple[str, str]]":
        """Return distinct (station, source) pairs from intraday_corrections since since_date."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT station, source "
                "FROM intraday_corrections "
                "WHERE city=? AND date>=?",
                (city, since_date),
            )
            return [(row[0], row[1]) for row in cur.fetchall()]

    def get_intraday_corrections_for_pair(
        self,
        city: str,
        station: str,
        source: str,
        since_date: str,
    ) -> list[dict]:
        """Return intraday correction rows for a specific (city, station, source) pair.

        Args:
            city:       City name.
            station:    Station identifier (e.g. "Busan", "RKPK").
            source:     Data source name (e.g. "amos", "metar").
            since_date: Lower bound on date (inclusive, YYYY-MM-DD).

        Returns:
            List of row dicts ordered by date, obs_time ascending.
        """
        cur = self._conn.execute(
            "SELECT * FROM intraday_corrections "
            "WHERE city=? AND station=? AND source=? AND date>=? "
            "ORDER BY date ASC, obs_time ASC",
            (city, station, source, since_date),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # emos_calibration
    # ------------------------------------------------------------------

    def upsert_emos_coefficients(
        self, *, city: str, model_mode: str,
        a: float, b: float, c: float, d: float,
        crps_score: "float | None" = None,
        trained_at: "str | None" = None,
        ready_for_promotion: int = 0,
        forecast_source: str = "nws_open_meteo",
    ) -> None:
        """Insert or replace EMOS calibration coefficients for a (city, mode, source) triple."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO emos_calibration
                   (city, model_mode, forecast_source,
                    a, b, c, d, crps_score, ready_for_promotion, trained_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (city, model_mode, forecast_source,
                 a, b, c, d, crps_score, ready_for_promotion, trained_at),
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
                SUM(CASE WHEN outcome IN ('filled','sold') AND pnl IS NOT NULL THEN 1 ELSE 0 END)         AS filled_count,
                SUM(CASE WHEN outcome IN ('filled','sold') AND pnl > 0 THEN 1 ELSE 0 END)                  AS win_count,
                SUM(COALESCE(pnl, 0))                                                           AS total_pnl,
                MAX(CASE WHEN outcome IN ('filled','sold') THEN ts END)                         AS last_trade_ts
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

    def get_emos_coefficients(
        self, city: str, model_mode: str, forecast_source: str = "nws_open_meteo"
    ) -> "dict | None":
        """Return EMOS coefficients dict for (city, model_mode, forecast_source), or None."""
        cur = self._conn.execute(
            "SELECT a, b, c, d, crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration WHERE city=? AND model_mode=? AND forecast_source=?",
            (city, model_mode, forecast_source),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "a": row[0], "b": row[1], "c": row[2], "d": row[3],
            "crps_score": row[4], "ready_for_promotion": row[5], "trained_at": row[6],
        }

    def get_all_emos_calibration(self) -> list[dict]:
        """Return all rows from emos_calibration as dicts."""
        cur = self._conn.execute(
            "SELECT city, model_mode, forecast_source, a, b, c, d, "
            "crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration ORDER BY city, model_mode, forecast_source"
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
    # guardrail_events
    # ------------------------------------------------------------------

    def log_guardrail_event(
        self,
        ts: str,
        station: str,
        event_type: str,
        raw_value: float,
        adj_value: float,
        ticker: "str | None" = None,
    ) -> None:
        """Insert a guardrail event row."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO guardrail_events(ts, station, event_type, raw_value, adj_value, delta, ticker) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (ts, station, event_type, raw_value, adj_value, adj_value - raw_value, ticker),
            )
            self._conn.commit()

    def get_guardrail_stats(self) -> dict:
        """Return summary counts and averages for each guardrail event type.

        Returns:
            Dict with keys 'cap_events', 'correction_events', each containing
            {'total': int, 'last_7d': int, 'avg_delta': float}.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        def _query(event_type: str) -> dict:
            row_total = self._conn.execute(
                "SELECT COUNT(*), AVG(delta) FROM guardrail_events WHERE event_type=?",
                (event_type,),
            ).fetchone()
            row_7d = self._conn.execute(
                "SELECT COUNT(*) FROM guardrail_events WHERE event_type=? AND ts>=?",
                (event_type, cutoff),
            ).fetchone()
            return {
                "total": int(row_total[0] or 0),
                "last_7d": int(row_7d[0] or 0),
                "avg_delta": round(float(row_total[1] or 0.0), 4),
            }

        return {
            "cap_events": _query("cap_applied"),
            "correction_events": _query("correction_applied"),
        }

    def get_forced_exit_stats(self) -> dict:
        """Return forced-exit trade counts: total, last_7d, and by_station dict."""
        cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        total_row = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE close_reason='forced_exit'"
        ).fetchone()
        seven_day_row = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE close_reason='forced_exit' AND ts>=?",
            (cutoff_7d,),
        ).fetchone()
        by_station_rows = self._conn.execute(
            "SELECT station, COUNT(*) FROM trades WHERE close_reason='forced_exit' GROUP BY station"
        ).fetchall()
        return {
            "total": int(total_row[0] or 0),
            "last_7d": int(seven_day_row[0] or 0),
            "by_station": {r[0]: int(r[1]) for r in by_station_rows},
        }

    def get_emos_shadow_city_status(self, city: str) -> dict:
        """Return EMOS shadow status for a single city: mean_crps and latest deb_weights."""
        crps_row = self._conn.execute(
            "SELECT AVG(crps_score) FROM emos_crps_log WHERE city=?", (city,)
        ).fetchone()
        deb_row = self._conn.execute(
            "SELECT weights_json FROM deb_weight_log WHERE city=? ORDER BY logged_at DESC LIMIT 1",
            (city,),
        ).fetchone()
        return {
            "mean_crps": float(crps_row[0]) if crps_row and crps_row[0] is not None else None,
            "deb_weights_snapshot": deb_row[0] if deb_row else None,
        }

    def get_trades_missing_fee_costs(self) -> list:
        """Return live closed trades where actual_fee_cents is NULL.

        Used by backfill_trade_costs.py. Each row has: id, order_id, actual_price, size_eur.
        """
        cur = self._conn.execute(
            """
            SELECT id, order_id, actual_price, size_eur
            FROM trades
            WHERE mode = 'live'
              AND outcome = 'sold'
              AND actual_fee_cents IS NULL
              AND order_id IS NOT NULL
            ORDER BY ts ASC
            """
        )
        return [dict(row) for row in cur.fetchall()]

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
        """Return {yes_enabled, no_enabled, low_no_enabled} for *station*, or None if absent."""
        cur = self._conn.execute(
            "SELECT yes_enabled, no_enabled, low_no_enabled "
            "FROM station_overrides WHERE station=?",
            (station,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "yes_enabled": bool(row[0]),
            "no_enabled": bool(row[1]),
            "low_no_enabled": bool(row[2]),
        }

    def set_station_override(
        self,
        station: str,
        yes_enabled: bool,
        no_enabled: bool,
        low_no_enabled: bool = False,
    ) -> None:
        """Upsert yes_enabled, no_enabled, and low_no_enabled for *station*."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO station_overrides"
                "(station, yes_enabled, no_enabled, low_no_enabled, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(station) DO UPDATE SET "
                "yes_enabled=excluded.yes_enabled, no_enabled=excluded.no_enabled, "
                "low_no_enabled=excluded.low_no_enabled, updated_at=excluded.updated_at",
                (station, int(yes_enabled), int(no_enabled), int(low_no_enabled), self._now()),
            )
            self._conn.commit()

    def get_all_station_overrides(self) -> "dict[str, dict]":
        """Return all station_overrides rows as {station: {yes_enabled, no_enabled, low_no_enabled}}."""
        cur = self._conn.execute(
            "SELECT station, yes_enabled, no_enabled, low_no_enabled FROM station_overrides"
        )
        return {
            row[0]: {
                "yes_enabled": bool(row[1]),
                "no_enabled": bool(row[2]),
                "low_no_enabled": bool(row[3]),
            }
            for row in cur.fetchall()
        }

    def get_hourly_obs_for_climb(self, station: str) -> list[dict]:
        """Return all observations for *station* with date and local hour.

        Used by build_climb_lookup.py --from-db to derive p95 climb rates from
        real collected observations. Returns rows with keys: date, hour_local, temp_f.
        Hour is in UTC (caller converts to local time using station timezone config).
        """
        cur = self._conn.execute(
            "SELECT DATE(ts) AS date, CAST(strftime('%H', ts) AS INTEGER) AS hour_utc, "
            "temp_f FROM observations WHERE station=? AND temp_f IS NOT NULL ORDER BY ts ASC",
            (station,),
        )
        return [{"date": row[0], "hour_local": row[1], "temp_f": float(row[2])} for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # emos_crps_log / deb_weight_log
    # ------------------------------------------------------------------

    def get_daily_obs_high(self, station: str, date: str) -> "float | None":
        """Return MAX(temp_f) from observations for *station* on *date* (YYYY-MM-DD)."""
        cur = self._conn.execute(
            "SELECT MAX(temp_f) FROM observations WHERE station=? AND DATE(ts)=?",
            (station, date),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def get_obs_highs_range(self, station: str, since_date: str) -> dict:
        """Return {date_str: max_temp_f} for all dates >= since_date for *station*.

        Used by DEB weight computation to pair model forecasts against observed
        daily highs without depending on the settlements table (which only
        populates from resolved live trades).
        """
        cur = self._conn.execute(
            "SELECT DATE(ts) AS d, MAX(temp_f) AS high_f "
            "FROM observations "
            "WHERE station=? AND DATE(ts) >= ? AND temp_f IS NOT NULL "
            "GROUP BY DATE(ts)",
            (station, since_date),
        )
        return {row[0]: float(row[1]) for row in cur.fetchall()}

    def log_crps(self, city: str, date: str, crps_score: float, model_mode: str = "emos_shadow") -> None:
        """Insert a CRPS score record for *city* on *date*."""
        logged_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO emos_crps_log(city,date,crps_score,model_mode,logged_at) VALUES(?,?,?,?,?)",
                    (city, date, crps_score, model_mode, logged_at),
                )

    def get_emos_crps_count(self, city: str) -> int:
        """Return the number of CRPS log entries for *city*."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM emos_crps_log WHERE city=?", (city,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def emos_crps_logged_for_date(
        self, city: str, date: str, model_mode: str = "emos_shadow"
    ) -> bool:
        """Return True if a CRPS row already exists for (city, date, model_mode).

        Used by the daily shadow runner to avoid double-counting samples when
        the calibration runs more than once on the same calendar day (e.g. after
        a process restart resets the in-memory once-per-day gate).
        """
        cur = self._conn.execute(
            "SELECT 1 FROM emos_crps_log WHERE city=? AND date=? AND model_mode=? LIMIT 1",
            (city, date, model_mode),
        )
        return cur.fetchone() is not None

    def log_deb_weights(self, city: str, date: str, weights_json: str) -> None:
        """Insert a DEB weights snapshot for *city* on *date*."""
        logged_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO deb_weight_log(city,date,weights_json,logged_at) VALUES(?,?,?,?)",
                    (city, date, weights_json, logged_at),
                )

    def get_latest_deb_weights(self, city: str) -> "str | None":
        """Return the most recent weights_json snapshot for *city*, or None.

        Used by get_ensemble_distribution() (issue #511) to weight the active
        FORECAST_STACK models by DEB weight when computing ensemble_mean.
        """
        cur = self._conn.execute(
            "SELECT weights_json FROM deb_weight_log WHERE city=? ORDER BY logged_at DESC LIMIT 1",
            (city,),
        )
        row = cur.fetchone()
        return row[0] if row else None
