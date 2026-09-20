"""SQLite persistence layer for MeteoEdge."""
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytz
from dateutil import parser as dtparse

from src.config import (
    CONFIG_DEFAULTS,
    STATION_TZ,
    STATIONS,
    get_canonical_station_feeds,
    get_training_eligible_since,
    is_training_eligible,
)
from src.strategy.gate_verdicts import GATE_VERDICTS

log = logging.getLogger(__name__)

_DEFAULT_PATH = os.getenv("DB_PATH", "data/meteoedge.db")

# Issue #552: deb_weight_log rows logged before this date predate commit
# 195f08c (the per-region DEB routing fix) and may attribute "nws" weight to
# non-US cities. Purged on every startup (idempotent — no-op once purged).
_DEB_WEIGHT_LOG_PURGE_CUTOFF = "2026-06-30"

# ICAO -> Polymarket city name, built once from STATIONS (tuple index 0 = ICAO,
# index 3 = city). Same mapping pattern as config.get_canonical_station_feeds();
# duplicated here (rather than imported) to keep this a plain module-level dict
# lookup on the hot training-data read path.
_ICAO_TO_CITY: "dict[str, str]" = {s[0]: s[3] for s in STATIONS}


def _icao_to_city(station: str) -> "str | None":
    """Resolve an ICAO station code to its Polymarket city name, or None if unknown."""
    return _ICAO_TO_CITY.get(station)


def _normalize_iso_ts(ts: str) -> str:
    """Normalize an ISO-8601 UTC timestamp to a single ``...Z`` convention.

    Issue #977: different open_positions write paths produced different UTC
    suffixes for the same column -- live_trader.place_order() writes
    ``datetime.utcnow().isoformat() + "Z"`` while reconciliation paths write
    ``datetime.now(timezone.utc).isoformat()`` (``+00:00`` suffix). Both are
    valid ISO-8601 but the inconsistency makes the column harder to reason
    about / sort lexicographically. Normalizes any parseable timestamp to the
    ``...Z`` form; unparseable input is returned unchanged (never raises --
    this must not block a position write).
    """
    if not ts:
        return ts
    try:
        dt = dtparse.isoparse(ts) if hasattr(dtparse, "isoparse") else dtparse.parse(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return ts


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
    direction       TEXT NOT NULL DEFAULT 'high',
    is_next_day     INTEGER NOT NULL DEFAULT 0,
    today_position_open INTEGER NOT NULL DEFAULT 0
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
    direction       TEXT NOT NULL DEFAULT 'high',
    is_next_day     INTEGER NOT NULL DEFAULT 0
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
    sigma_source        TEXT NOT NULL DEFAULT 'fixed',
    lead_hours          INTEGER NOT NULL DEFAULT 24,
    a                   REAL NOT NULL,
    b                   REAL NOT NULL,
    c                   REAL NOT NULL,
    d                   REAL NOT NULL,
    crps_score          REAL,
    ready_for_promotion INTEGER DEFAULT 0,
    trained_at          TEXT,
    UNIQUE(city, model_mode, forecast_source, sigma_source, lead_hours)
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    city            TEXT NOT NULL,
    date            TEXT NOT NULL,
    crps_score      REAL NOT NULL,
    model_mode      TEXT NOT NULL DEFAULT 'emos_shadow',
    forecast_source TEXT NOT NULL DEFAULT 'baseline',
    sigma_source    TEXT NOT NULL DEFAULT 'fixed',
    logged_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deb_weight_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    city        TEXT NOT NULL,
    date        TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    logged_at   TEXT NOT NULL
);

-- Unconditional poll heartbeat (issue #914). Written once per poll cycle in
-- poll_once(), regardless of whether any brackets were evaluated that poll.
-- This is the source for the daily health report's "Polls 24h" / gap metrics
-- -- scan_decisions is NOT, because it is only written when brackets are
-- actually evaluated (a much rarer event than a poll).
CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_ts     TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'paper'
);
CREATE INDEX IF NOT EXISTS idx_poll_runs_poll_ts ON poll_runs(poll_ts);

CREATE TABLE IF NOT EXISTS scan_decisions (
    station               TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    date                  TEXT NOT NULL,
    ts                    TEXT NOT NULL,
    poll_ts               TEXT NOT NULL,
    bracket_low           REAL NOT NULL,
    bracket_high          REAL NOT NULL,
    side                  TEXT CHECK(side IN ('YES','NO') OR side IS NULL),
    yes_ask               INTEGER,
    no_ask                INTEGER,
    -- Unclamped float prices (0 to 1, not cents) alongside yes_ask/no_ask
    -- above -- issue #1076. yes_ask/no_ask keep clamping to 1..99 -- these
    -- are additive and read by nothing that gates/trades.
    yes_price_raw         REAL,
    no_price_raw          REAL,
    current_high          REAL,
    latest_temp           REAL,
    forecast_high         REAL,
    p_yes                 REAL,
    raw_p_yes             REAL,
    capped_p_yes          REAL,
    ev_yes                REAL,
    ev_no                 REAL,
    ev_yes_raw            REAL,
    ev_no_raw             REAL,
    minutes_to_settlement REAL,
    emos_mode             TEXT,
    is_next_day           INTEGER NOT NULL DEFAULT 0,
    -- gate_verdict intentionally has NO CHECK constraint (issue #912): SQLite
    -- cannot ALTER a CHECK in place, so every past addition to the allow-list
    -- required a table-rebuild migration and one such addition ('day_mismatch_
    -- shadow', issue #820) shipped without one, breaking every existing DB.
    -- Validation now lives solely in Database.upsert_scan_decision, which
    -- checks against src.strategy.gate_verdicts.GATE_VERDICTS (the single
    -- source of truth) and raises a clear ValueError before the row ever
    -- reaches SQLite.
    gate_verdict          TEXT NOT NULL,
    gate_actual           REAL,
    gate_threshold        REAL,
    gate_unit             TEXT,
    gate_detail           TEXT,
    execution_mode        TEXT NOT NULL DEFAULT 'paper' CHECK(execution_mode IN ('live','paper')),
    ensemble_mean         REAL,
    ensemble_members      INTEGER,
    ensemble_range_low    REAL,
    ensemble_range_high   REAL,
    direction             TEXT NOT NULL DEFAULT 'high',
    PRIMARY KEY (station, ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_scan_decisions_station_date ON scan_decisions(station, date);

-- Copy-trading wallet screening runs (issue #1108, epic #1099). Append-only:
-- every screening run gets its own row, never overwritten -- unlike
-- scan_decisions above, which upserts per key. The stability check (epic #1099
-- story 2) needs to diff a wallet's last two runs (e.g. the 0xd3b034d7
-- reversal: resolved trades 7,498 -> 2,271, median ROI +33.4% -> -100% across
-- two runs 15 hours apart), which is only possible if prior runs are kept.
CREATE TABLE IF NOT EXISTS copy_wallet_candidates (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    address              TEXT NOT NULL,
    window               TEXT NOT NULL,
    screened_at          TEXT NOT NULL,
    n_buy_trades         INTEGER NOT NULL,
    n_resolved           INTEGER NOT NULL,
    win_rate             REAL,
    mean_roi             REAL,
    median_roi           REAL,
    mirrored_dollar_pnl  REAL,
    flat_dollar_pnl      REAL,
    flat_stake           REAL,
    slippage_bps         REAL NOT NULL,
    eligible_to_follow   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_copy_wallet_candidates_address_screened
    ON copy_wallet_candidates(address, screened_at);

-- Copy-trading followed wallets (issue #1121, epic #1101 story B1 -- signal
-- detection & flat-stake paper execution). The subset of
-- copy_wallet_candidates actually being copied. Unlike that table, this is
-- NOT append-only: one row per wallet, mutated in place via UPDATE
-- (status/paused_reason/last_seen_trade_ts) rather than a new row per
-- change -- a wallet can't be followed twice, callers un-pause instead of
-- re-inserting. last_seen_trade_ts is the high-water-mark story B3's
-- polling will use to detect new trades since the last poll -- NULL until
-- the first poll runs for a wallet.
CREATE TABLE IF NOT EXISTS copy_wallets_followed (
    address              TEXT PRIMARY KEY,
    stake_per_trade      REAL NOT NULL CHECK(stake_per_trade > 0),
    status               TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
    paused_reason        TEXT,
    added_at             TEXT NOT NULL,
    last_seen_trade_ts   INTEGER
);

-- Copy-trading detected signals (issue #1121). One row per detected BUY
-- from a followed wallet, executed or skipped: source_price is the
-- followed trader's own fill price -- fill_price/size_usd/position_id are
-- only populated once order_placed=1, skip_reason only when it stays 0.
--
-- source_trade_id: verified live against data-api.polymarket.com/trades
-- 2026-09-19 (see src.data.polymarket_traders module docstring for the
-- verification note on other fields) -- every raw trade record carries a
-- stable `transactionHash`, so this column exists per the acceptance
-- criteria. normalize_trade() (src/data/polymarket_traders.py) does not
-- yet surface it -- story B3's polling code must add that before this
-- column can be populated -- until then it stays NULL. No UNIQUE constraint: a
-- single on-chain transaction can span multiple maker fills at different
-- price levels, so the same transactionHash can legitimately appear on
-- more than one BUY row. Story B3 must therefore dedupe on
-- source_trade_id first but fall back to the full (address, market,
-- source_price, detected_at) tuple when it collides -- documented here
-- as the known limitation this story hands off, per the acceptance
-- criteria.
CREATE TABLE IF NOT EXISTS copy_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    address          TEXT NOT NULL,
    market           TEXT NOT NULL,
    outcome_index    INTEGER CHECK(outcome_index IS NULL OR outcome_index IN (0,1)),
    source_price     REAL NOT NULL CHECK(source_price >= 0 AND source_price <= 1),
    source_trade_id  TEXT,
    detected_at      TEXT NOT NULL,
    order_placed     INTEGER NOT NULL DEFAULT 0,
    fill_price       REAL CHECK(fill_price IS NULL OR (fill_price >= 0 AND fill_price <= 1)),
    size_usd         REAL CHECK(size_usd IS NULL OR size_usd > 0),
    position_id      INTEGER REFERENCES copy_positions(id),
    skip_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_copy_signals_address_detected
    ON copy_signals(address, detected_at);
CREATE INDEX IF NOT EXISTS idx_copy_signals_source_trade_id
    ON copy_signals(source_trade_id);

-- Copy-trading open paper positions (issue #1121). Mirrors open_positions'
-- shape (docs/DB_SCHEMA.md) conceptually, but is NOT deleted on
-- settlement the way open_positions is -- epic C needs to read settled
-- rows later for P&L history, so status flips 'open' -> 'settled' in
-- place instead. get_open_copy_positions() sums stake_usd over this
-- table's 'open' rows (rather than an in-memory counter) so story B3's
-- per-wallet and total exposure checks survive process restarts.
CREATE TABLE IF NOT EXISTS copy_positions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL REFERENCES copy_signals(id),
    address          TEXT NOT NULL,
    market           TEXT NOT NULL,
    outcome_index    INTEGER NOT NULL CHECK(outcome_index IN (0,1)),
    entry_price      REAL NOT NULL CHECK(entry_price >= 0 AND entry_price <= 1),
    stake_usd        REAL NOT NULL CHECK(stake_usd > 0),
    entry_ts         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settled')),
    settled_pnl_usd  REAL,
    settled_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_copy_positions_address_status
    ON copy_positions(address, status);
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
            ("trades", "estimated_fee_cents", "REAL"),
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
            # Issue #609: market end date (YYYY-MM-DD), populated at live-trade
            # insert time from the market's endDate so settle_live_trades() can
            # match DB rows to a settlement date without depending on
            # live_trades.jsonl. NULL for rows written before this migration
            # ("legacy" rows) -- settle.py falls back to the station-local date
            # of `ts` for those.
            ("trades", "end_date", "TEXT"),
            # Issue #622: track whether settlement outcome came from Gamma market
            # resolution or METAR weather truth. NULL for rows written before this
            # migration (legacy rows without a known source).
            ("settlements", "resolution_source", "TEXT"),
            # Issue #449: sigma-source axis for EMOS retraining ('fixed' | 'ensemble'),
            # keyed alongside forecast_source so a sigma_source='ensemble' retrain is
            # stored as an independent row and never overwrites the legacy
            # sigma_source='fixed' calibration (same non-overwrite pattern as #659's
            # forecast_source column). Default 'fixed' matches every row written
            # before this migration -- readers that don't pass sigma_source keep
            # resolving to the same rows they always have (see _active_sigma_source).
            ("emos_calibration", "sigma_source", "TEXT NOT NULL DEFAULT 'fixed'"),
            # Issue #665: per-lead-bin EMOS coefficients. Default 24 matches the
            # implicit lead bin every pre-#665 row was trained/served at
            # (fetch_training_data's own default), so existing rows keep serving
            # identically until a caller explicitly fits/reads a different bin.
            ("emos_calibration", "lead_hours", "INTEGER NOT NULL DEFAULT 24"),
            # Issue #687: discriminator for next-day (forecast-only, shadow-only)
            # candidates vs. same-day candidates -- next-day rows flow into the
            # same candidates/snapshot_archive populations feeding the prob-cap
            # report and saturation baselines, so they must be explicitly
            # filterable rather than inferred from minutes_to_settlement.
            # Default 0 matches every pre-#687 row (all same-day).
            ("candidates", "is_next_day", "INTEGER NOT NULL DEFAULT 0"),
            # Issue #704 (Gap 1): same discriminator as candidates.is_next_day,
            # but on trades -- upsert_shadow_trade() (the next-day shadow write
            # path) had no way to tag its own rows, so once NEXT_DAY_EVALUATION
            # is on, next-day shadow trades are indistinguishable from same-day
            # shadow trades in every trades-based consumer (promotion gate,
            # prob-cap report, shadow-health calibration). Default 0 matches
            # every pre-#704 row (all same-day).
            ("trades", "is_next_day", "INTEGER NOT NULL DEFAULT 0"),
            # Issue #704 (Gap 2): records whether a same-station LIVE position
            # was open at next-day-evaluation time, so the ≥7-day shadow window
            # can quantify how often cross-day exposure would actually occur
            # (the approved #687 design's deferred-question-1 data need).
            # Always 0 for same-day candidates -- only populated by scanner.py's
            # next-day-eval branch. Default 0 matches every pre-#704 row.
            ("candidates", "today_position_open", "INTEGER NOT NULL DEFAULT 0"),
            # Issue #759: forecast-stack axis for the CRPS promotion counter.
            # Without this, get_emos_crps_count/emos_crps_logged_for_date key
            # on (city, model_mode) only, so two stacks' shadow runs on the
            # same day collide on the one-sample-per-city-per-day dedup guard
            # and the promotion count silently pools CRPS from whichever
            # stack happens to log first. Default 'baseline' backfills every
            # pre-#759 row (all logged before forecast-stack expansion
            # existed) and matches _active_forecast_source's own fallback, so
            # the already-accumulated baseline promotion evidence keeps
            # counting unchanged.
            ("emos_crps_log", "forecast_source", "TEXT NOT NULL DEFAULT 'baseline'"),
            # Issue #851: sigma-source axis for the CRPS promotion counter,
            # mirroring #759's forecast_source fix. Without this,
            # get_emos_crps_count/emos_crps_logged_for_date key on
            # (city, model_mode, forecast_source) only, so a sigma_source
            # switch (#799, fixed -> ensemble) does NOT reset the promotion
            # clock: shadow-day evidence logged under the old fixed-sigma
            # coefficients keeps counting toward the newly-retrained
            # ensemble-sigma lineage's promotion decision -- the exact
            # #658-style train/serve-evidence skew #799 closed for the
            # coefficients themselves. Default 'fixed' backfills every
            # pre-#851 row -- every CRPS row logged before this migration was
            # scored against a sigma_source='fixed' (or unresolved,
            # equivalently pre-#449) coefficient fit, so this is the accurate
            # historical tag, not merely a matching-default placeholder.
            ("emos_crps_log", "sigma_source", "TEXT NOT NULL DEFAULT 'fixed'"),
            # Issue #780: distinguishes a confirmed live fill from the
            # scanner's paper-mode "would trade live" placeholder for
            # traded_live rows -- see _persist_scan_decisions in run.py for
            # where it's stamped. Default 'paper' backfills every pre-#780
            # row conservatively (never claim a legacy row as a confirmed
            # live fill it can't prove).
            ("scan_decisions", "execution_mode", "TEXT NOT NULL DEFAULT 'paper'"),
            # Issue #900: scan_decisions never carried the high/low market
            # discriminator that candidates/trades/settlements already have
            # (see the direction migrations above) -- the scanner has stamped
            # every snapshot dict with a direction key since #876 (high-side
            # "high", low-side "low"), but upsert_scan_decision() had no
            # direction parameter at all, so every call raised TypeError:
            # unexpected keyword argument 'direction'. Adding the column here
            # alongside the new parameter (see upsert_scan_decision below) so
            # both fresh and existing DBs accept it in this same PR. Default
            # 'high' matches every row written before this migration --
            # scan_decisions only started existing after the high-side
            # scanner did, so every legacy row is genuinely high-side.
            ("scan_decisions", "direction", "TEXT NOT NULL DEFAULT 'high'"),
            # Issue #1076: unclamped float ask prices ([0,1], not cents)
            # alongside the existing yes_ask/no_ask integer-cent columns,
            # which clamp to [1, 99] and destroy sub-penny prices in
            # exactly the region (>0.96 or <0.04) where 74% of brackets sit
            # -- see Bracket's own docstring in src/model/envelope.py for
            # the full rationale. NULL for every row written before this
            # migration (the clamp already discarded that information, so
            # there is nothing to backfill). Additive only -- yes_ask/no_ask
            # and every downstream gate/trading consumer are unchanged.
            ("scan_decisions", "yes_price_raw", "REAL"),
            ("scan_decisions", "no_price_raw", "REAL"),
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

        # Migration: widen emos_calibration's UNIQUE constraint to (city, model_mode,
        # forecast_source, sigma_source, lead_hours) — issues #449/#665. The ALTER
        # TABLE ADD COLUMN above gives existing DBs the sigma_source/lead_hours
        # columns, but SQLite cannot widen a UNIQUE constraint in place; a DB whose
        # table was created before this migration still has an older UNIQUE index
        # (just city+model_mode, or city+model_mode+forecast_source), which would
        # silently collide a sigma_source='ensemble' or non-default lead_hours
        # retrain into the 'fixed'/lead_hours=24 legacy row via INSERT OR REPLACE.
        # Detect the narrow constraint via SQLite's own index catalog (PRAGMA
        # index_list/index_info) rather than matching sqlite_master's free-form SQL
        # text, which formatting changes could silently desync from.
        _ec_unique_cols: set = set()
        for _idx in self._conn.execute("PRAGMA index_list(emos_calibration)").fetchall():
            if not _idx[2]:  # idx[2] = unique flag
                continue
            _ec_unique_cols |= {
                col[2] for col in self._conn.execute(f"PRAGMA index_info({_idx[1]})").fetchall()
            }
        if not {"sigma_source", "lead_hours"}.issubset(_ec_unique_cols):
            with self._conn:
                self._conn.execute("DROP TABLE IF EXISTS emos_calibration_new")
                self._conn.execute(
                    """
                    CREATE TABLE emos_calibration_new (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        city                TEXT NOT NULL,
                        model_mode          TEXT NOT NULL,
                        forecast_source     TEXT NOT NULL DEFAULT 'nws_open_meteo',
                        sigma_source        TEXT NOT NULL DEFAULT 'fixed',
                        lead_hours          INTEGER NOT NULL DEFAULT 24,
                        a                   REAL NOT NULL,
                        b                   REAL NOT NULL,
                        c                   REAL NOT NULL,
                        d                   REAL NOT NULL,
                        crps_score          REAL,
                        ready_for_promotion INTEGER DEFAULT 0,
                        trained_at          TEXT,
                        UNIQUE(city, model_mode, forecast_source, sigma_source, lead_hours)
                    )
                    """
                )
                self._conn.execute(
                    """
                    INSERT INTO emos_calibration_new
                        (id, city, model_mode, forecast_source, sigma_source, lead_hours,
                         a, b, c, d, crps_score, ready_for_promotion, trained_at)
                    SELECT id, city, model_mode, forecast_source,
                           COALESCE(sigma_source, 'fixed'), COALESCE(lead_hours, 24),
                           a, b, c, d, crps_score, ready_for_promotion, trained_at
                    FROM emos_calibration
                    """
                )
                self._conn.execute("DROP TABLE emos_calibration")
                self._conn.execute(
                    "ALTER TABLE emos_calibration_new RENAME TO emos_calibration"
                )
            self._conn.commit()

        # Migration: add/update partial UNIQUE index for shadow-trade dedup (issue #376, #613).
        # When the index definition changes (e.g., adding direction column), we must drop
        # and recreate to ensure the new definition is used.
        self._conn.execute("DROP INDEX IF EXISTS idx_trades_shadow_unique")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_shadow_unique "
            "ON trades(station, bracket_low, bracket_high, side, direction, substr(ts,1,10)) "
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
                            estimated_fee_cents REAL,
                            size_eur        REAL,
                            direction       TEXT NOT NULL DEFAULT 'high',
                            p_yes_raw       REAL,
                            end_date        TEXT
                        )
                        """
                    )
                    # direction/p_yes_raw/end_date/estimated_fee_cents may not exist in old table — coalesce
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
                    end_date_expr = (
                        "end_date" if "end_date" in old_col_names else "NULL"
                    )
                    # Migration: rename actual_fee_cents → estimated_fee_cents if old table has it
                    if "estimated_fee_cents" in old_col_names:
                        fee_expr = "estimated_fee_cents"
                    elif "actual_fee_cents" in old_col_names:
                        fee_expr = "actual_fee_cents"
                    else:
                        fee_expr = "NULL"
                    self._conn.execute(
                        "INSERT INTO trades_new SELECT "
                        "id,ts,station,ticker,bracket_low,bracket_high,side,"
                        "predicted_price,actual_price,slippage,predicted_edge,mode,"
                        "order_id,outcome,pnl,capital_before,capital_after,settled_at,"
                        f"{fee_expr},size_eur,{direction_expr},{p_yes_raw_expr},{end_date_expr} "
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

        # Migration: drop the scan_decisions.gate_verdict CHECK constraint
        # (issue #912). SQLite cannot ALTER a CHECK in place -- requires a
        # table rebuild. This constraint was hand-maintained separately from
        # scanner.GATE_VERDICTS/Database._SCAN_DECISION_GATE_VERDICTS and
        # fell out of sync when 'day_mismatch_shadow' (issue #820) was added
        # to those two but not here: every existing database kept rejecting
        # that verdict with a raw sqlite3.IntegrityError even after #820
        # shipped, because CREATE TABLE IF NOT EXISTS is a no-op against a
        # table that already exists. The constraint is now removed entirely
        # (not widened) -- validation lives solely in the Python validator
        # at upsert_scan_decision, which checks against the single
        # src.strategy.gate_verdicts.GATE_VERDICTS source of truth and
        # raises a clear ValueError instead of an opaque CHECK failure.
        # Detect the old constraint via sqlite_master's stored CREATE TABLE
        # text -- same pattern as the trades.mode migration above.
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='scan_decisions'"
        ).fetchone()
        if row and row[0] and "CHECK(gate_verdict" in row[0]:
            with self._conn:
                self._conn.execute("DROP TABLE IF EXISTS scan_decisions_new")
                self._conn.execute(
                    """
                    CREATE TABLE scan_decisions_new (
                        station               TEXT NOT NULL,
                        ticker                TEXT NOT NULL,
                        date                  TEXT NOT NULL,
                        ts                    TEXT NOT NULL,
                        poll_ts               TEXT NOT NULL,
                        bracket_low           REAL NOT NULL,
                        bracket_high          REAL NOT NULL,
                        side                  TEXT CHECK(side IN ('YES','NO') OR side IS NULL),
                        yes_ask               INTEGER,
                        no_ask                INTEGER,
                        yes_price_raw         REAL,
                        no_price_raw          REAL,
                        current_high          REAL,
                        latest_temp           REAL,
                        forecast_high         REAL,
                        p_yes                 REAL,
                        raw_p_yes             REAL,
                        capped_p_yes          REAL,
                        ev_yes                REAL,
                        ev_no                 REAL,
                        ev_yes_raw            REAL,
                        ev_no_raw             REAL,
                        minutes_to_settlement REAL,
                        emos_mode             TEXT,
                        is_next_day           INTEGER NOT NULL DEFAULT 0,
                        gate_verdict          TEXT NOT NULL,
                        gate_actual           REAL,
                        gate_threshold        REAL,
                        gate_unit             TEXT,
                        gate_detail           TEXT,
                        execution_mode        TEXT NOT NULL DEFAULT 'paper' CHECK(execution_mode IN ('live','paper')),
                        ensemble_mean         REAL,
                        ensemble_members      INTEGER,
                        ensemble_range_low    REAL,
                        ensemble_range_high   REAL,
                        direction             TEXT NOT NULL DEFAULT 'high',
                        PRIMARY KEY (station, ticker, date)
                    )
                    """
                )
                old_cols = {
                    r[1] for r in self._conn.execute(
                        "PRAGMA table_info(scan_decisions)"
                    ).fetchall()
                }
                new_cols = {
                    r[1] for r in self._conn.execute(
                        "PRAGMA table_info(scan_decisions_new)"
                    ).fetchall()
                }
                common_cols = [c for c in (
                    "station", "ticker", "date", "ts", "poll_ts", "bracket_low",
                    "bracket_high", "side", "yes_ask", "no_ask",
                    "yes_price_raw", "no_price_raw", "current_high",
                    "latest_temp", "forecast_high", "p_yes", "raw_p_yes",
                    "capped_p_yes", "ev_yes", "ev_no", "ev_yes_raw", "ev_no_raw",
                    "minutes_to_settlement", "emos_mode", "is_next_day",
                    "gate_verdict", "gate_actual", "gate_threshold", "gate_unit",
                    "gate_detail", "execution_mode", "ensemble_mean",
                    "ensemble_members", "ensemble_range_low", "ensemble_range_high",
                    "direction",
                ) if c in old_cols and c in new_cols]
                cols_csv = ",".join(common_cols)
                self._conn.execute(
                    f"INSERT INTO scan_decisions_new ({cols_csv}) "
                    f"SELECT {cols_csv} FROM scan_decisions"
                )
                self._conn.execute("DROP TABLE scan_decisions")
                self._conn.execute(
                    "ALTER TABLE scan_decisions_new RENAME TO scan_decisions"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scan_decisions_station_date "
                    "ON scan_decisions(station, date)"
                )

        # Migration: rename actual_fee_cents → estimated_fee_cents (issue #1075).
        # This column has always held estimated fees (from estimate_fee_cents model),
        # never observed fill costs. The rename is idempotent.
        # If actual_fee_cents has data and estimated_fee_cents is empty, copy the data.
        old_cols = {
            r[1] for r in self._conn.execute(
                "PRAGMA table_info(trades)"
            ).fetchall()
        }
        if "actual_fee_cents" in old_cols:
            # Copy data from actual_fee_cents to estimated_fee_cents if needed
            if "estimated_fee_cents" in old_cols:
                # Both columns exist — copy data and drop the old column
                self._conn.execute(
                    "UPDATE trades SET estimated_fee_cents = actual_fee_cents "
                    "WHERE estimated_fee_cents IS NULL AND actual_fee_cents IS NOT NULL"
                )
                # Now rebuild the table to drop actual_fee_cents
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
                                estimated_fee_cents REAL,
                                size_eur        REAL,
                                close_reason    TEXT,
                                minutes_to_settlement_at_close REAL,
                                bid_depth_at_close INTEGER,
                                direction       TEXT NOT NULL DEFAULT 'high',
                                p_yes_raw       REAL,
                                end_date        TEXT,
                                is_next_day     INTEGER NOT NULL DEFAULT 0
                            )
                            """
                        )
                        self._conn.execute(
                            "INSERT INTO trades_new SELECT "
                            "id,ts,station,ticker,bracket_low,bracket_high,side,"
                            "predicted_price,actual_price,slippage,predicted_edge,mode,"
                            "order_id,outcome,pnl,capital_before,capital_after,settled_at,"
                            "estimated_fee_cents,size_eur,close_reason,minutes_to_settlement_at_close,"
                            "bid_depth_at_close,direction,p_yes_raw,end_date,is_next_day "
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
            else:
                # Only actual_fee_cents exists (very old DB) — just rename via ADD
                self._conn.execute("ALTER TABLE trades ADD COLUMN estimated_fee_cents REAL")
                self._conn.execute(
                    "UPDATE trades SET estimated_fee_cents = actual_fee_cents"
                )

        self._purge_stale_deb_weight_log()

        # Migration: purge intraday_corrections rows poisoned by the build_consensus
        # weight-scaling bug (issue #615). Rows recorded while the bug was active have
        # inflated delta_f values (~30-45°F for US cities) that corrupt the MAE gate
        # and residual bias window. Rows matching the poisoned signature:
        #   date >= '2026-07-02'  — #576 deploy date (when live DEB weights switched on)
        #   basis_weights != '{"open_meteo": 1.0}'  — non-fallback weights = US stations
        # are deleted so the MAE gate and residual correction window recover immediately.
        self._conn.execute(
            "DELETE FROM intraday_corrections "
            "WHERE date >= '2026-07-02' "
            "AND basis_weights != '{\"open_meteo\": 1.0}'"
        )
        self._conn.commit()

    def _purge_stale_deb_weight_log(self) -> None:
        """One-time idempotent cleanup for issue #552.

        deb_weight_log's writer (the once-per-day logging side effect inside
        deb_weighting.get_weights()) was silently removed in commit 94a2ece
        (2026-06-26) and was never restored — ensemble_distribution.py now
        reads model_weights directly instead (single source of truth, see
        #552). Every row still in deb_weight_log therefore predates 195f08c
        (2026-06-30, the per-region DEB routing fix) and may attribute "nws"
        weight to non-US cities. get_emos_shadow_city_status() still surfaces
        the latest deb_weight_log row for EMOS shadow monitoring, so purge the
        contaminated backlog rather than leaving it to be served stale
        indefinitely. No-op once the backlog has been purged.
        """
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM deb_weight_log WHERE logged_at < ?",
                    (_DEB_WEIGHT_LOG_PURGE_CUTOFF,),
                )

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
        is_next_day: int = 0,
        today_position_open: int = 0,
    ) -> int:
        """Insert a trade candidate; returns the new row id.

        Args:
            is_next_day: 1 when this candidate came from next-day evaluation
                (issue #687) -- a forecast-only, shadow-only evaluation of a
                station's next market. Defaults to 0 so every existing
                same-day call site is unaffected.
            today_position_open: 1 when a same-station LIVE position was open
                at next-day-evaluation time (issue #704, Gap 2). Only
                meaningful when is_next_day=1 -- same-day candidates always
                pass 0 (there is no "today position" concept relative to a
                same-day candidate). Read-only telemetry: never gates or
                alters the live entry decision.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO candidates"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,predicted_edge,market_price,confidence,"
                "minutes_to_settlement,flagged_first,direction,p_yes_raw,is_next_day,"
                "today_position_open) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, predicted_edge, market_price, confidence,
                    minutes_to_settlement, flagged_first, direction, p_yes_raw,
                    int(is_next_day), int(today_position_open),
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
    # scan_decisions (issue #756 -- Edge tab bot's-eye per-bracket view)
    # ------------------------------------------------------------------

    # Single source of truth: src.strategy.gate_verdicts.GATE_VERDICTS (issue
    # #912). Kept as a class attribute (rather than referencing the module
    # constant directly at call sites) so tests/callers that patch
    # Database._SCAN_DECISION_GATE_VERDICTS keep working unchanged.
    _SCAN_DECISION_GATE_VERDICTS = GATE_VERDICTS

    # Issue #780: 'live' only when the verdict seam confirmed a real exchange
    # fill this poll; 'paper' for every other row, including the scanner's
    # unconfirmed traded_live placeholder from a poll with no live_trader.
    _SCAN_DECISION_EXECUTION_MODES = frozenset({"live", "paper"})

    def upsert_scan_decision(
        self,
        *,
        ts: str,
        station: str,
        ticker: str,
        date: str,
        bracket_low: float,
        bracket_high: float,
        gate_verdict: str,
        poll_ts: "str | None" = None,
        side: "str | None" = None,
        yes_ask: "int | None" = None,
        no_ask: "int | None" = None,
        yes_price_raw: "float | None" = None,
        no_price_raw: "float | None" = None,
        current_high: "float | None" = None,
        latest_temp: "float | None" = None,
        forecast_high: "float | None" = None,
        p_yes: "float | None" = None,
        raw_p_yes: "float | None" = None,
        capped_p_yes: "float | None" = None,
        ev_yes: "float | None" = None,
        ev_no: "float | None" = None,
        ev_yes_raw: "float | None" = None,
        ev_no_raw: "float | None" = None,
        minutes_to_settlement: "float | None" = None,
        emos_mode: "str | None" = None,
        is_next_day: int = 0,
        gate_actual: "float | None" = None,
        gate_threshold: "float | None" = None,
        gate_unit: "str | None" = None,
        gate_detail: "str | None" = None,
        execution_mode: str = "paper",
        ensemble_mean: "float | None" = None,
        ensemble_members: "int | None" = None,
        ensemble_range_low: "float | None" = None,
        ensemble_range_high: "float | None" = None,
        direction: str = "high",
    ) -> None:
        """Upsert the latest poll's evaluated-bracket decision row.

        Idempotent per poll: a fresh call for the same ``(station, ticker,
        date)`` key replaces the prior row in place, so this table always
        reflects the most recent scan, not an ever-growing history -- unlike
        ``candidates``/``snapshots.jsonl``, which are append-only logs.

        Writer: ``src.scripts.run.poll_once`` (mirrors the ``insert_candidate``
        call site, issue #684), once per evaluated bracket per poll -- the
        scanner attaches ``gate_verdict`` per bracket (src.strategy.scanner.
        scan_markets) and run.py upgrades the ``traded_live`` placeholder to
        ``entry_guard``/``timeout_today``/(confirmed) ``traded_live`` once the
        entry-guard check and any live execution attempt have resolved (the
        "verdict seam" -- see the PR for #756).

        Args:
            yes_price_raw: Unclamped YES ask price ([0,1], not cents), alongside
                the clamped ``yes_ask`` integer-cent column -- issue #1076.
                Prefer this over ``yes_ask`` for true-price analysis (e.g. the
                rail question); every gate/trading path still reads ``yes_ask``
                unchanged.
            no_price_raw: Same as ``yes_price_raw``, for the NO side.
            gate_verdict: one of the 11 canonical verdicts (see
                ``_SCAN_DECISION_GATE_VERDICTS``); raises ``ValueError`` on
                any other value so a typo never silently reaches the DB.
            execution_mode: ``'live'`` or ``'paper'`` (see
                ``_SCAN_DECISION_EXECUTION_MODES``; issue #780) -- whether
                this poll had a live trader configured, and therefore
                whether a ``traded_live`` verdict is a confirmed exchange
                fill (``'live'``) or the scanner's unconfirmed placeholder
                (``'paper'``). Raises ``ValueError`` on any other value.
        """
        if gate_verdict not in self._SCAN_DECISION_GATE_VERDICTS:
            raise ValueError(f"invalid gate_verdict: {gate_verdict!r}")
        if execution_mode not in self._SCAN_DECISION_EXECUTION_MODES:
            raise ValueError(f"invalid execution_mode: {execution_mode!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO scan_decisions("
                "station,ticker,date,ts,poll_ts,bracket_low,bracket_high,side,"
                "yes_ask,no_ask,yes_price_raw,no_price_raw,current_high,latest_temp,forecast_high,"
                "p_yes,raw_p_yes,capped_p_yes,ev_yes,ev_no,ev_yes_raw,ev_no_raw,"
                "minutes_to_settlement,emos_mode,is_next_day,gate_verdict,"
                "gate_actual,gate_threshold,gate_unit,gate_detail,execution_mode,"
                "ensemble_mean,ensemble_members,ensemble_range_low,ensemble_range_high,direction) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(station, ticker, date) DO UPDATE SET "
                "ts=excluded.ts, poll_ts=excluded.poll_ts, "
                "bracket_low=excluded.bracket_low, bracket_high=excluded.bracket_high, "
                "side=excluded.side, yes_ask=excluded.yes_ask, no_ask=excluded.no_ask, "
                "yes_price_raw=excluded.yes_price_raw, no_price_raw=excluded.no_price_raw, "
                "current_high=excluded.current_high, latest_temp=excluded.latest_temp, "
                "forecast_high=excluded.forecast_high, p_yes=excluded.p_yes, "
                "raw_p_yes=excluded.raw_p_yes, capped_p_yes=excluded.capped_p_yes, "
                "ev_yes=excluded.ev_yes, ev_no=excluded.ev_no, "
                "ev_yes_raw=excluded.ev_yes_raw, ev_no_raw=excluded.ev_no_raw, "
                "minutes_to_settlement=excluded.minutes_to_settlement, "
                "emos_mode=excluded.emos_mode, is_next_day=excluded.is_next_day, "
                "gate_verdict=excluded.gate_verdict, gate_actual=excluded.gate_actual, "
                "gate_threshold=excluded.gate_threshold, gate_unit=excluded.gate_unit, "
                "gate_detail=excluded.gate_detail, execution_mode=excluded.execution_mode, "
                "ensemble_mean=excluded.ensemble_mean, "
                "ensemble_members=excluded.ensemble_members, "
                "ensemble_range_low=excluded.ensemble_range_low, "
                "ensemble_range_high=excluded.ensemble_range_high, direction=excluded.direction",
                (
                    station, ticker, date, ts, poll_ts or ts, bracket_low, bracket_high, side,
                    yes_ask, no_ask, yes_price_raw, no_price_raw, current_high, latest_temp, forecast_high,
                    p_yes, raw_p_yes, capped_p_yes, ev_yes, ev_no, ev_yes_raw, ev_no_raw,
                    minutes_to_settlement, emos_mode, int(is_next_day), gate_verdict,
                    gate_actual, gate_threshold, gate_unit, gate_detail, execution_mode,
                    ensemble_mean, ensemble_members, ensemble_range_low, ensemble_range_high, direction,
                ),
            )
            self._conn.commit()

    def get_scan_decisions(self, station: str, date: str) -> list[dict]:
        """Return every evaluated bracket's latest-poll decision row for (station, date).

        Ordered ascending by bracket_low, matching the Edge tab's per-bracket
        table row order (design spec docs/design/edge-tab-bracket-decisions.md §4).
        """
        cur = self._conn.execute(
            "SELECT station,ticker,date,ts,poll_ts,bracket_low,bracket_high,side,"
            "yes_ask,no_ask,yes_price_raw,no_price_raw,current_high,latest_temp,forecast_high,"
            "p_yes,raw_p_yes,capped_p_yes,ev_yes,ev_no,ev_yes_raw,ev_no_raw,"
            "minutes_to_settlement,emos_mode,is_next_day,gate_verdict,"
            "gate_actual,gate_threshold,gate_unit,gate_detail,execution_mode,"
            "ensemble_mean,ensemble_members,ensemble_range_low,ensemble_range_high,direction "
            "FROM scan_decisions WHERE station=? AND date=? ORDER BY bracket_low ASC",
            (station, date),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # copy_wallet_candidates (issue #1108, epic #1099 -- copy-trading wallet
    # screening pipeline). Isolated from scan_decisions/candidates above:
    # this table is copy-trading-only, per the architecture doc's isolation
    # requirement.
    # ------------------------------------------------------------------

    def insert_wallet_screening(
        self,
        *,
        address: str,
        window: str,
        screened_at: str,
        n_buy_trades: int,
        n_resolved: int,
        slippage_bps: float,
        win_rate: "float | None" = None,
        mean_roi: "float | None" = None,
        median_roi: "float | None" = None,
        mirrored_dollar_pnl: "float | None" = None,
        flat_dollar_pnl: "float | None" = None,
        flat_stake: "float | None" = None,
        eligible_to_follow: int = 0,
    ) -> int:
        """Insert one wallet-screening-run row; returns the new row id.

        Always a plain INSERT (never INSERT OR REPLACE / upsert) -- this
        table is append-only so the stability check (epic #1099 story 2) can
        diff a wallet's last two runs. Never overwrites a prior run.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO copy_wallet_candidates"
                "(address,window,screened_at,n_buy_trades,n_resolved,win_rate,"
                "mean_roi,median_roi,mirrored_dollar_pnl,flat_dollar_pnl,"
                "flat_stake,slippage_bps,eligible_to_follow) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    address, window, screened_at, n_buy_trades, n_resolved, win_rate,
                    mean_roi, median_roi, mirrored_dollar_pnl, flat_dollar_pnl,
                    flat_stake, slippage_bps, int(eligible_to_follow),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_recent_wallet_screenings(self, address: str, limit: int = 2) -> list[dict]:
        """Return *address*'s most recent *limit* screening runs, newest first.

        Ordered by ``id DESC`` (not ``screened_at DESC``) -- two rows can
        share a timestamp, and ``id`` is the only guaranteed-monotonic
        tiebreak. Returns ``[]`` for an unknown address, never raises.
        """
        cur = self._conn.execute(
            "SELECT * FROM copy_wallet_candidates WHERE address=? "
            "ORDER BY id DESC LIMIT ?",
            (address, int(limit)),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_latest_wallet_screenings(self) -> list[dict]:
        """Return every screened address's single most-recent screening row.

        One row per distinct ``address`` -- the "latest run" that
        ``copy_wallet_promotion.py``'s advisory report (issue #1122) and
        ``--follow`` eligibility check are both defined against. Tiebreak is
        ``id DESC`` per address, matching ``get_recent_wallet_screenings``.
        Returns ``[]`` if no wallet has ever been screened.
        """
        cur = self._conn.execute(
            "SELECT c.* FROM copy_wallet_candidates c "
            "INNER JOIN ("
            "  SELECT address, MAX(id) AS max_id FROM copy_wallet_candidates "
            "  GROUP BY address"
            ") latest ON c.address = latest.address AND c.id = latest.max_id"
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # copy_wallets_followed / copy_signals / copy_positions (issue #1121,
    # epic #1101 story B1 -- copy-trading signal detection & flat-stake
    # paper execution). Fully separate tables from open_positions/trades
    # per the architecture doc's isolation decision (issue #1100), not a
    # `strategy` discriminator column on the weather strategy's tables.
    # ------------------------------------------------------------------

    def insert_followed_wallet(
        self,
        *,
        address: str,
        stake_per_trade: float,
        added_at: str,
        status: str = "active",
    ) -> None:
        """Insert one row for a newly-followed wallet.

        Plain INSERT -- raises the underlying ``sqlite3.IntegrityError`` on
        a duplicate ``address`` (the primary key). A wallet can't be
        followed twice; callers un-pause an existing row via
        ``update_followed_wallet_status`` instead of re-inserting.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO copy_wallets_followed"
                "(address,stake_per_trade,status,added_at) VALUES(?,?,?,?)",
                (address, stake_per_trade, status, added_at),
            )
            self._conn.commit()

    def get_followed_wallets(self, status: "str | None" = None) -> list[dict]:
        """Return all followed wallets, or only those matching *status* if given."""
        if status is not None:
            cur = self._conn.execute(
                "SELECT * FROM copy_wallets_followed WHERE status=?", (status,)
            )
        else:
            cur = self._conn.execute("SELECT * FROM copy_wallets_followed")
        return [dict(row) for row in cur.fetchall()]

    def update_followed_wallet_status(
        self, address: str, status: str, paused_reason: "str | None" = None
    ) -> None:
        """Set *address*'s status in place -- does not touch any other column
        (``stake_per_trade``, ``added_at``, ``last_seen_trade_ts``).

        ``paused_reason`` is written as given, including ``None`` -- e.g.
        un-pausing back to ``'active'`` with the default clears any prior
        reason rather than leaving it stale.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE copy_wallets_followed SET status=?, paused_reason=? WHERE address=?",
                (status, paused_reason, address),
            )
            self._conn.commit()

    def update_followed_wallet_last_seen(self, address: str, last_seen_trade_ts: int) -> None:
        """Set *address*'s high-water-mark trade timestamp in place.

        Story B3's polling loop uses this to only scan trades newer than
        the last-seen one on each pass.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE copy_wallets_followed SET last_seen_trade_ts=? WHERE address=?",
                (int(last_seen_trade_ts), address),
            )
            self._conn.commit()

    def insert_copy_signal(
        self,
        *,
        address: str,
        market: str,
        source_price: float,
        detected_at: str,
        outcome_index: "int | None" = None,
        source_trade_id: "str | None" = None,
        order_placed: int = 0,
        fill_price: "float | None" = None,
        size_usd: "float | None" = None,
        position_id: "int | None" = None,
        skip_reason: "str | None" = None,
    ) -> int:
        """Insert one detected-signal row; returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO copy_signals"
                "(address,market,outcome_index,source_price,source_trade_id,"
                "detected_at,order_placed,fill_price,size_usd,position_id,skip_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    address, market, outcome_index, source_price, source_trade_id,
                    detected_at, int(order_placed), fill_price, size_usd, position_id,
                    skip_reason,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def insert_copy_position(
        self,
        *,
        signal_id: int,
        address: str,
        market: str,
        outcome_index: int,
        entry_price: float,
        stake_usd: float,
        entry_ts: str,
        status: str = "open",
    ) -> int:
        """Insert one open copy-trading position row; returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO copy_positions"
                "(signal_id,address,market,outcome_index,entry_price,stake_usd,"
                "entry_ts,status) VALUES(?,?,?,?,?,?,?,?)",
                (signal_id, address, market, outcome_index, entry_price, stake_usd,
                 entry_ts, status),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_open_copy_positions(self, address: "str | None" = None) -> list[dict]:
        """Return ``status='open'`` copy-trading positions, optionally
        filtered to one wallet.

        Story B3's exposure checks sum ``stake_usd`` over this query's
        result to compute current per-wallet and total exposure -- reading
        the DB rather than an in-memory counter, so exposure state
        survives process restarts.
        """
        if address is not None:
            cur = self._conn.execute(
                "SELECT * FROM copy_positions WHERE status='open' AND address=?",
                (address,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM copy_positions WHERE status='open'")
        return [dict(row) for row in cur.fetchall()]

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
        end_date: "str | None" = None,
    ) -> int:
        """Insert a trade record; returns the new row id.

        ``end_date`` (YYYY-MM-DD) is the market's resolution date, used by
        settle_live_trades() (#609) to match a row to a settlement date
        without depending on live_trades.jsonl. Optional -- rows without it
        fall back to the station-local date of ``ts`` at settlement time.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,actual_price,slippage,predicted_edge,mode,order_id,"
                "outcome,pnl,capital_before,capital_after,settled_at,size_eur,direction,"
                "p_yes_raw,end_date) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, actual_price, slippage, predicted_edge, mode,
                    order_id, outcome, pnl, capital_before, capital_after, settled_at,
                    size_eur, direction, p_yes_raw, end_date,
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
        estimated_fee_cents: "float | None" = None,
        size_eur: "float | None" = None,
    ) -> int:
        """Backfill cost accounting columns on the trade row matching *order_id*.

        Only non-None values are written; returns rows updated (0 = no match).

        Note: estimated_fee_cents stores the result of estimate_fee_cents(sell_price),
        not an observed fee from fill receipts. No observed fee is recorded anywhere
        in this project. For validation of the fee model, see scripts/fee_calibration.py.
        """
        fields = {"estimated_fee_cents": estimated_fee_cents, "size_eur": size_eur}
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
                SUM(CASE WHEN estimated_fee_cents IS NOT NULL
                         THEN 1 ELSE 0 END)                      AS fee_populated_count,
                ROUND(SUM(COALESCE(estimated_fee_cents, 0)) / 100.0, 4)
                                                                  AS total_fee_eur,
                ROUND(AVG(COALESCE(estimated_fee_cents, 0)) / 100.0, 4)
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
        is_next_day: "int | None" = None,
    ) -> list:
        """Return trades ordered by most-recent-first.

        Args:
            limit:       Maximum rows to return (``None`` = no limit).
            mode:        Filter to ``'paper'`` or ``'live'`` if provided.
            direction:   Filter to ``'high'`` or ``'low'`` if provided.
            is_next_day: Filter to 0 (same-day) or 1 (next-day shadow eval,
                issue #704) if provided. ``None`` (default) returns both --
                callers that read shadow trades for win-rate/calibration
                statistics should pass ``is_next_day=0`` explicitly so
                next-day rows (different sigma/lead-time regime) never
                silently contaminate the same-day population.
        """
        conditions = []
        params: list = []
        if mode is not None:
            conditions.append("mode=?")
            params.append(mode)
        if direction is not None:
            conditions.append("direction=?")
            params.append(direction)
        if is_next_day is not None:
            conditions.append("is_next_day=?")
            params.append(int(is_next_day))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM trades{where} ORDER BY ts DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cur = self._conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def get_unsettled_shadow_trades(self, target_date: str, lookback_days: int = 0) -> list:
        """Return unsettled shadow trades for *target_date* (YYYY-MM-DD).

        With *lookback_days* > 0, rows from up to that many days BEFORE
        target_date are included too, so markets that had not resolved on
        Polymarket by an earlier settle run get retried (issue #644).
        """
        cur = self._conn.execute(
            "SELECT * FROM trades "
            "WHERE mode='shadow' AND settled_at IS NULL "
            "AND DATE(ts) BETWEEN DATE(?, ?) AND ?",
            (target_date, f"-{int(lookback_days)} days", target_date),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_settled_live_trades(self) -> list:
        """Return held-to-expiry settled live trades (issue #617).

        Selects ``mode='live' AND outcome='filled' AND settled_at IS NOT NULL``
        -- these are markets that resolved via ``settle_live_trades()`` (#609),
        which writes ``pnl``/``settled_at`` directly onto the trades row.

        Early exits (``outcome='sold'``, stop-loss/take-profit/manual) are
        deliberately excluded here even though they also carry a non-NULL
        ``settled_at`` (set at sell time by order_manager) -- the dashboard's
        closed-positions panel still reads those from live_trades.jsonl, which
        order_manager writes directly and independently of settle.py, so they
        are unaffected by the removal of the settle.py JSONL write-back.
        """
        cur = self._conn.execute(
            "SELECT * FROM trades "
            "WHERE mode='live' AND outcome='filled' AND settled_at IS NOT NULL "
            "ORDER BY settled_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_unsettled_live_trades(self) -> list:
        """Return ALL live held-to-expiry trades not yet settled (issue #609).

        Mirrors get_unsettled_shadow_trades() but does NOT filter by date in
        SQL: live rows only have a target settlement date via the (nullable)
        ``end_date`` column or a station-local fallback derived from ``ts``,
        neither of which SQLite can resolve without STATION_TZ. Callers
        (settle.settle_live_trades) filter to the target date in Python.

        A row is "unsettled" when it was actually filled and is still
        awaiting a resolution: mode='live', outcome='filled', settled_at IS
        NULL. Rows that were sold early (outcome flips to 'sold' via
        update_trade_by_order at exit time, see order_manager._record_sell_in_db)
        are naturally excluded and never double-settled here.
        """
        cur = self._conn.execute(
            "SELECT * FROM trades "
            "WHERE mode='live' AND outcome='filled' AND settled_at IS NULL"
        )
        return [dict(row) for row in cur.fetchall()]

    def has_live_trade_today(self, station: str, ticker: str, side: str, day: str) -> bool:
        """Return True if a live trade already exists for (station, ticker, side, day).

        Used by the run.py entry gate (issue #611) to stop the bot re-entering
        a bracket it already holds or has already tried today. ANY outcome
        counts -- filled, sold, and timeout attempts all block re-entry; this
        is deliberate because repeated timeout retries were part of the
        observed stacking (7x KATL, 3x WMKK on one bracket in a single day).

        ``day`` is matched the same way resolve_trade_date() (settle.py, #609)
        prefers a live row's target date: the ``end_date`` column when
        present, falling back to the UTC calendar date of ``ts`` for legacy
        rows without one. Callers should pass the *candidate's* end_date
        (falling back to today's UTC date) so re-entry is blocked for the
        whole life of the market day, not just the wall-clock day the poll
        happens to run in.
        """
        cur = self._conn.execute(
            "SELECT 1 FROM trades "
            "WHERE mode='live' AND station=? AND ticker=? AND side=? "
            "AND COALESCE(substr(end_date,1,10), substr(ts,1,10))=? "
            "LIMIT 1",
            (station, ticker, side, day),
        )
        return cur.fetchone() is not None

    def has_open_live_position(self, station: str) -> bool:
        """Return True if *station* has a LIVE position currently open.

        "Open" mirrors get_unsettled_live_trades()'s definition: mode='live',
        outcome='filled' (actually entered, not a timeout/cancel), settled_at
        IS NULL (not yet resolved) -- i.e. capital is presently at risk on
        this station's today market.

        Used by scanner.py's next-day evaluation branch (issue #704, Gap 2)
        to record whether a same-station today position was open at
        next-day-evaluation time, so the shadow window can quantify how often
        this cross-day overlap would actually occur. Read-only telemetry
        only -- never used to gate or alter the live entry decision.
        """
        cur = self._conn.execute(
            "SELECT 1 FROM trades "
            "WHERE mode='live' AND station=? AND outcome='filled' AND settled_at IS NULL "
            "LIMIT 1",
            (station,),
        )
        return cur.fetchone() is not None

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
        is_next_day: int = 0,
    ) -> tuple[int, bool]:
        """Insert a shadow trade row, or update actual_price if one already exists today.

        Dedup key: (station, bracket_low, bracket_high, side, direction, day).

        Args:
            is_next_day: 1 when this shadow trade came from next-day
                evaluation (issue #687/#704) -- mirrors Candidate.is_next_day
                so trades-based consumers (promotion gate, prob-cap report,
                shadow-health) can filter/segment next-day rows out of the
                same-day population. Only used on insert -- the dedup-hit
                UPDATE path only refreshes actual_price (same as it already
                does for p_yes_raw), since the dedup key does not include
                is_next_day and an existing row's classification should not
                change underneath it.

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
                "outcome,pnl,capital_before,capital_after,settled_at,direction,p_yes_raw,"
                "is_next_day) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high, side,
                    predicted_price, actual_price, None, predicted_edge, "shadow",
                    None, None, None, capital_before, None, None, direction, p_yes_raw,
                    int(is_next_day),
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
        resolution_source: "str | None" = None,
    ) -> None:
        """Upsert a settlement record (unique on ticker)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settlements"
                "(ts,station,ticker,bracket_low,bracket_high,"
                "actual_high_f,resolved_yes,market_final_price,source,direction,"
                "resolution_source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, station, ticker, bracket_low, bracket_high,
                    actual_high_f, resolved_yes, market_final_price, source, direction,
                    resolution_source,
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

    def get_all_settlements(
        self,
        since: str,
        direction: "str | None" = None,
    ) -> list:
        """Return settlements for ALL stations at or after *since*, oldest first.

        Mirrors ``get_trades(mode=..., limit=None)``'s no-station-filter style —
        use this instead of looping ``get_settlements()`` per station when a
        caller needs to scan every station in one pass (e.g. the promotion bar
        in ``src/model/promotion_gate.py``), to avoid N+1 DB reads.

        Args:
            direction: Optional filter — ``'high'`` or ``'low'``.
        """
        if direction is not None:
            cur = self._conn.execute(
                "SELECT * FROM settlements WHERE ts>=? AND direction=? ORDER BY ts ASC",
                (since, direction),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM settlements WHERE ts>=? ORDER BY ts ASC",
                (since,),
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
        """Atomically insert an open position.

        Logs a loud warning if a row for the same ``token_id`` already exists
        (issue #611 observed 3 duplicate open_positions rows stacking for one
        WMKK token). This method stays policy-free -- it always inserts; the
        entry gate in src/scripts/run.py (has_live_trade_today() +
        get_open_position_by_token()) is the enforcement point that decides
        whether a live candidate should ever reach this call.

        ``entry_ts`` is normalized to a single ``...Z`` UTC convention
        (issue #977) so every write path produces a consistent format
        regardless of how the caller formatted its own timestamp.
        """
        entry_ts = _normalize_iso_ts(entry_ts)
        with self._lock:
            with self._conn:
                existing = self._conn.execute(
                    "SELECT COUNT(*) FROM open_positions WHERE token_id=?", (token_id,)
                ).fetchone()[0]
                if existing:
                    log.warning(
                        "[open_positions] token_id=%s... already has %d open row(s) -- "
                        "inserting another (order_id=%s). The run.py entry gate should "
                        "have blocked this before order placement; investigate if it did not.",
                        token_id[:14], existing, order_id,
                    )
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

    def get_last_forecast_capture_ts(self) -> "str | None":
        """Return MAX(logged_at) across all of model_forecast_log, or None if empty.

        Used by the forecast-capture staleness watchdog (issue #717) to detect
        a silently-dead capture job (src/scripts/capture_forecasts.py, run by
        the separate meteoedge-capture-forecasts.service/.timer). Read-only --
        does not touch the capture process itself.
        """
        cur = self._conn.execute("SELECT MAX(logged_at) FROM model_forecast_log")
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

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
        min_date: "str | None" = None,
    ) -> list[float]:
        """Return delta_f values for city over the trailing window_days calendar days.

        Optional *station* and *source* filters narrow the query to a specific
        (station, source) pair.  When both are None the query is city-wide (legacy
        behaviour).

        Optional *min_date* (YYYY-MM-DD) raises the effective lower bound of the
        window when it is more recent than ``today - window_days`` — used by
        callers (see ``src.model.residual_correction``) to exclude rows recorded
        under a prior consensus basis regime without shortening the window for
        callers that don't care (issue #586).
        """
        since_date = (date.today() - timedelta(days=window_days)).isoformat()
        if min_date is not None and min_date > since_date:
            since_date = min_date
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

    def _active_forecast_source(self) -> str:
        """Resolve the forecast_source key for emos_calibration rows.

        The active FORECAST_STACK is the semantic key — writers (run_emos_shadow,
        auto_retrain) save per stack, and consumers must read the same key.
        Before this resolver, readers defaulted to the literal 'nws_open_meteo'
        while the shadow runner saved under the stack name, so shadow rows were
        invisible to get_city_mode/apply_emos (issue #659).
        """
        return self.get_config("FORECAST_STACK") or "baseline"

    def _active_sigma_source(self) -> str:
        """Resolve the sigma_source key for emos_calibration rows (issue #799).

        Derives directly from the single ``USE_ENSEMBLE_SIGMA`` bot_config
        flag: 'ensemble' when it resolves true, 'fixed' otherwise. This is
        the SAME flag ``resolve_sigma_raw``/``true_probability_yes`` gate the
        live sigma_raw INPUT on (src/model/emos_mode.py, src/model/envelope.py)
        -- one flag now drives both "which coefficient row do we read/write"
        and "what raw sigma value do we feed it", so the two can never point
        at different tracks.

        Before #799 this read a SEPARATE bot_config key, EMOS_SIGMA_SOURCE
        ('fixed' | 'ensemble'), independently settable from USE_ENSEMBLE_SIGMA.
        That dual-flag design is exactly how a #658-style train/serve skew can
        reappear: an operator (or a retrain script that forgot to thread the
        parameter -- the actual bug this fixed, see run_emos_shadow.py) could
        save/read the 'ensemble' row while USE_ENSEMBLE_SIGMA stayed False, so
        serving fed the constant FORECAST_STDDEV_F into coefficients that were
        fit against real per-row spread (or vice versa). EMOS_SIGMA_SOURCE
        remains a legacy bot_config key (kept for schema/back-compat -- some
        already-deployed DBs have a row for it) but is no longer read here;
        setting it has no effect. Use USE_ENSEMBLE_SIGMA instead.

        Defaults to 'fixed' when USE_ENSEMBLE_SIGMA is unset/unseeded/false —
        the value every row written before #449 is migrated to (see the
        emos_calibration UNIQUE-widening migration in _migrate()).
        """
        raw = self.get_config("USE_ENSEMBLE_SIGMA")
        if raw is None:
            raw = str(CONFIG_DEFAULTS.get("USE_ENSEMBLE_SIGMA", False))
        use_ensemble = raw.lower() in ("true", "1", "yes")
        return "ensemble" if use_ensemble else "fixed"

    def upsert_emos_coefficients(
        self, *, city: str, model_mode: str,
        a: float, b: float, c: float, d: float,
        crps_score: "float | None" = None,
        trained_at: "str | None" = None,
        ready_for_promotion: int = 0,
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
        lead_hours: int = 24,
    ) -> None:
        """Insert or replace EMOS calibration coefficients for a (city, mode, source,
        sigma_source, lead_hours) tuple.

        forecast_source=None resolves to the active FORECAST_STACK (see
        _active_forecast_source); sigma_source=None resolves to the active
        EMOS_SIGMA_SOURCE (see _active_sigma_source) — both so writers and
        readers key consistently without every call site threading them
        explicitly. lead_hours defaults to 24 (issue #665), matching the only
        lead bin ever trained/served before per-lead-bin storage existed, so a
        caller that never mentions it lands on exactly the row pre-#665 code
        reads and writes.

        Coefficients for a different forecast_source, sigma_source, or
        lead_hours are stored as independent rows (UNIQUE(city, model_mode,
        forecast_source, sigma_source, lead_hours)) — saving one never
        overwrites another's row, same non-overwrite guarantee #659 already
        provides across forecast_source values.
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO emos_calibration
                   (city, model_mode, forecast_source, sigma_source, lead_hours,
                    a, b, c, d, crps_score, ready_for_promotion, trained_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (city, model_mode, forecast_source, sigma_source, lead_hours,
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
        self, city: str, model_mode: str,
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
        lead_hours: int = 24,
    ) -> "dict | None":
        """Return EMOS coefficients dict for (city, model_mode, forecast_source,
        sigma_source, lead_hours), or None.

        forecast_source=None resolves to the active FORECAST_STACK, sigma_source=None
        to the active USE_ENSEMBLE_SIGMA-derived track (see _active_forecast_source /
        _active_sigma_source), so consumers (get_city_mode, apply_emos, promotion
        checks) read the same rows a retrain writes (issues #659, #449, #799).
        lead_hours defaults to 24, the lead bin every pre-#665 row lives at, so
        callers that don't care about per-lead-bin serving see unchanged behaviour.
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        cur = self._conn.execute(
            "SELECT a, b, c, d, crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration WHERE city=? AND model_mode=? AND forecast_source=? "
            "AND sigma_source=? AND lead_hours=?",
            (city, model_mode, forecast_source, sigma_source, lead_hours),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "a": row[0], "b": row[1], "c": row[2], "d": row[3],
            "crps_score": row[4], "ready_for_promotion": row[5], "trained_at": row[6],
        }

    def get_emos_coefficients_by_lead(
        self, city: str, model_mode: str,
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
    ) -> "dict[int, dict]":
        """Return {lead_hours: coefficients} for every lead bin fitted for this
        (city, model_mode, forecast_source, sigma_source) key (issue #665).

        Used by the per-lead-bin serving path to pick the bin nearest to
        minutes-to-settlement at scan time — see
        src/model/emos_mode.py:_nearest_lead_hours. Empty dict when nothing has
        been fitted for this key (including the common case where only the
        legacy lead_hours=24 row exists, for city/mode combos with no row at
        all).

        Rows are returned ordered by lead_hours ASC so a dict built from them
        iterates lowest-lead-first — _nearest_lead_hours' tie-break (Python's
        min() keeps the first-encountered candidate) then consistently prefers
        the shorter lead bin on an exact tie, matching this codebase's existing
        "lowest lead_hours wins" convention (see ensemble_distribution.py).
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        cur = self._conn.execute(
            "SELECT lead_hours, a, b, c, d, crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration WHERE city=? AND model_mode=? AND forecast_source=? "
            "AND sigma_source=? ORDER BY lead_hours ASC",
            (city, model_mode, forecast_source, sigma_source),
        )
        return {
            row[0]: {
                "a": row[1], "b": row[2], "c": row[3], "d": row[4],
                "crps_score": row[5], "ready_for_promotion": row[6], "trained_at": row[7],
            }
            for row in cur.fetchall()
        }

    def get_all_emos_calibration(self) -> list[dict]:
        """Return all rows from emos_calibration as dicts."""
        cur = self._conn.execute(
            "SELECT city, model_mode, forecast_source, sigma_source, lead_hours, "
            "a, b, c, d, crps_score, ready_for_promotion, trained_at "
            "FROM emos_calibration "
            "ORDER BY city, model_mode, forecast_source, sigma_source, lead_hours"
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

    def toggle_emos_ready_for_promotion(
        self,
        city: str,
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
        lead_hours: int = 24,
        all_tracks: bool = False,
    ) -> "int | None":
        """Toggle ready_for_promotion (0 ↔ 1) on the emos_shadow row for city.

        Issue #696: emos_calibration's UNIQUE key grew to (city, model_mode,
        forecast_source, sigma_source, lead_hours) across #659/#449/#665, so a
        toggle keyed only on (city, model_mode) can flip rows outside the
        track actually being served. Scopes to the ACTIVE track by default:
        forecast_source=None/sigma_source=None resolve via
        _active_forecast_source/_active_sigma_source (the same resolvers
        get_emos_coefficients uses), and lead_hours defaults to 24 — the exact
        (forecast_source, sigma_source, lead_hours) triple
        _check_ready_for_promotion reads via
        db.get_emos_coefficients(city, "emos_primary") with no lead_hours
        argument. The per-lead-bin serving path (_select_emos_row /
        _nearest_lead_hours, #665) only picks which mu/sigma coefficients
        apply at scan time; the promotion gate itself is single-bin, so that
        is what this toggle targets by default.

        Pass all_tracks=True to reproduce the pre-#696 city-wide behavior:
        every row for (city, model_mode='emos_shadow') flips together,
        regardless of forecast_source/sigma_source/lead_hours — an explicit
        operator escape hatch, not the default.

        Returns the new value (0 or 1), or None if no matching shadow row
        exists.
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        with self._lock:
            if all_tracks:
                where = "city=? AND model_mode='emos_shadow'"
                params: tuple = (city,)
            else:
                where = (
                    "city=? AND model_mode='emos_shadow' AND forecast_source=? "
                    "AND sigma_source=? AND lead_hours=?"
                )
                params = (city, forecast_source, sigma_source, lead_hours)
            cur = self._conn.execute(
                f"SELECT ready_for_promotion FROM emos_calibration WHERE {where}",
                params,
            )
            row = cur.fetchone()
            if row is None:
                return None
            new_val = 0 if row[0] else 1
            self._conn.execute(
                f"UPDATE emos_calibration SET ready_for_promotion=? WHERE {where}",
                (new_val, *params),
            )
            self._conn.commit()
        return new_val

    # ------------------------------------------------------------------
    # guardrail_events
    # ------------------------------------------------------------------

    def record_poll_run(self, poll_ts: str, mode: str = "paper") -> None:
        """Record an unconditional poll heartbeat (issue #914).

        Call once per poll cycle from poll_once(), before any bracket
        evaluation happens. Used by the daily health report to measure real
        poll cadence/gaps -- NOT scan_decisions, which is only written when
        brackets are actually evaluated and therefore undercounts polls.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO poll_runs(poll_ts, mode) VALUES(?, ?)",
                (poll_ts, mode),
            )
            self._conn.commit()

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
            Dict with keys 'cap_events', 'correction_events', and
            'entry_guard_blocks' (issue #611), each containing
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
            "entry_guard_blocks": _query("entry_guard_block"),
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
        """Return EMOS shadow status for a single city.

        Includes ``mean_crps`` (model_mode='emos_shadow'), ``legacy_mean_crps``
        (model_mode='legacy' — the uncalibrated equal-weight baseline scored on
        the same training triples, see issue #667), ``crps_delta`` =
        legacy_mean_crps - mean_crps (positive means EMOS is beating legacy,
        since lower CRPS is better), and ``model_weights_snapshot`` — a dict
        {model: weight} for the most recent date in model_weights, or None if
        no model_weights rows exist for this city (see issue #649).

        Both CRPS averages are scoped to their own model_mode explicitly —
        an unscoped AVG() over the whole table would silently mix 'legacy'
        rows into the EMOS score once they start accumulating alongside
        'emos_shadow' rows for the same city/date.

        Both averages are also scoped to the active sigma_source (issue
        #851) — otherwise, after a sigma_source switch (issue #799), this
        display metric would silently blend shadow-day CRPS scored against
        the old sigma track's coefficients into the average, understating or
        overstating how the CURRENTLY active (newly-retrained) lineage is
        actually performing.

        NOTE: unlike get_emos_crps_count, this is not additionally scoped by
        forecast_source — a pre-existing gap from before issue #759 that is
        out of scope for #851's sigma_source fix; tracked separately.
        """
        sigma_source = self._active_sigma_source()
        crps_row = self._conn.execute(
            "SELECT AVG(crps_score) FROM emos_crps_log "
            "WHERE city=? AND model_mode='emos_shadow' AND sigma_source=?",
            (city, sigma_source),
        ).fetchone()
        legacy_row = self._conn.execute(
            "SELECT AVG(crps_score) FROM emos_crps_log "
            "WHERE city=? AND model_mode='legacy' AND sigma_source=?",
            (city, sigma_source),
        ).fetchone()

        # Get all model weights for this city, ordered by date DESC
        model_weights = self.get_model_weights(city)

        # Extract the snapshot for the most recent date
        model_weights_snapshot = None
        if model_weights:
            # Get the most recent date (first row since ordered DESC)
            most_recent_date = model_weights[0]["date"]
            # Build dict of {model: weight} for this date
            snapshot = {}
            for row in model_weights:
                if row["date"] == most_recent_date:
                    snapshot[row["model"]] = row["weight"]
                else:
                    # Since ordered by date DESC, we can stop when date changes
                    break
            model_weights_snapshot = snapshot if snapshot else None

        mean_crps = float(crps_row[0]) if crps_row and crps_row[0] is not None else None
        legacy_mean_crps = (
            float(legacy_row[0]) if legacy_row and legacy_row[0] is not None else None
        )
        crps_delta = (
            legacy_mean_crps - mean_crps
            if mean_crps is not None and legacy_mean_crps is not None
            else None
        )
        return {
            "mean_crps": mean_crps,
            "legacy_mean_crps": legacy_mean_crps,
            "crps_delta": crps_delta,
            "model_weights_snapshot": model_weights_snapshot,
        }

    def get_trades_missing_fee_costs(self) -> list:
        """Return live closed trades where estimated_fee_cents is NULL.

        Used by backfill_trade_costs.py. Each row has: id, order_id, actual_price, size_eur.
        """
        cur = self._conn.execute(
            """
            SELECT id, order_id, actual_price, size_eur
            FROM trades
            WHERE mode = 'live'
              AND outcome = 'sold'
              AND estimated_fee_cents IS NULL
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
        """Return all observations for *station* as raw timestamps + temps.

        Used by build_climb_lookup.py --from-db to derive p95 climb rates from
        real collected observations. Returns rows with keys: ts, temp_f. The
        timestamp is returned verbatim (ISO 8601, UTC — naive values are treated
        as UTC by the caller); the caller localizes to the station timezone
        before binning, since the climb table is indexed by *local* month/hour
        (issue #587 — a previous version truncated to UTC date/hour here, which
        made local binning impossible and mislabeled the column as hour_local).
        """
        cur = self._conn.execute(
            "SELECT ts, temp_f FROM observations "
            "WHERE station=? AND temp_f IS NOT NULL ORDER BY ts ASC",
            (station,),
        )
        return [{"ts": row[0], "temp_f": float(row[1])} for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # emos_crps_log / deb_weight_log
    # ------------------------------------------------------------------

    def get_daily_obs_high(self, station: str, date: str) -> "float | None":
        """Return MAX(temp_f) from observations for *station* on *date* (YYYY-MM-DD).

        Groups observations by the station's LOCAL calendar day (not UTC).
        Returns None if no observations found, station has no known timezone,
        the station's city is marked ``training_eligible: false`` in
        ``config/source_priority.yaml`` (issue #558 — bad-label stations must
        not feed training data), or *date* falls before the city's
        ``training_eligible_since`` cutover date, if any (issue #766 — a city
        can become eligible only from a known date onward, e.g. Shenzhen/ZGSZ
        after its 2026-07-14 obs-cadence upgrade).

        Rows are unioned across every DB key returned by
        ``config.get_canonical_station_feeds(station)`` (e.g. both the
        city-keyed high-cadence feed and the ICAO-keyed METAR feed for WSSS),
        so verification uses the same source-of-truth as scan-time nowcasting.
        """
        if station not in STATION_TZ:
            return None

        city = _icao_to_city(station)
        if city is not None and not is_training_eligible(city):
            return None

        try:
            target_date = dtparse.parse(date).date()
        except (ValueError, TypeError):
            return None

        if city is not None:
            eligible_since = get_training_eligible_since(city)
            if eligible_since is not None and target_date < eligible_since:
                return None

        tz = pytz.timezone(STATION_TZ[station])

        # Fetch observations in a ±1 day window around the target date to avoid
        # missing observations that fall on the target local day but different UTC day.
        date_minus_1 = (target_date - timedelta(days=1)).isoformat()
        date_plus_2 = (target_date + timedelta(days=2)).isoformat()

        feed_keys = get_canonical_station_feeds(station)
        placeholders = ",".join("?" * len(feed_keys))
        # Issue #731: exclude is_official=0 (Open-Meteo fallback) rows from the
        # daily-high TRUTH. The canonical feed union pools a city-keyed
        # high-cadence feed with the ICAO-keyed METAR feed and takes the MAX;
        # a city feed sourced from modelled Open-Meteo data (is_official=0)
        # could otherwise override the real METAR reading as the "observed"
        # high that EMOS/DEB train against (circular truth). Historically this
        # applied to Seoul/Busan (amos, retired -- issue #740) and could apply
        # to any future city-keyed feed that falls back to modelled data.
        # NULL is treated as official (legacy rows predate the column default).
        # Issue #741: also exclude via raw_json LIKE '%source_fallback%' as a
        # second, independent signal -- every fallback writer (e.g. the former
        # amos Open-Meteo fallback) tags raw_json with "source_fallback" AND
        # sets is_official=0, so this OR condition is redundant by design and
        # only catches a row where one of the two markers was set incorrectly.
        # NULL raw_json is treated as non-fallback (most rows have no raw_json
        # at all and must not be excluded).
        cur = self._conn.execute(
            f"SELECT ts, temp_f FROM observations "
            f"WHERE station IN ({placeholders}) AND ts >= ? AND ts < ? AND temp_f IS NOT NULL "
            f"AND (is_official IS NULL OR is_official = 1) "
            f"AND (raw_json IS NULL OR raw_json NOT LIKE '%source_fallback%') "
            f"ORDER BY ts",
            (*feed_keys, date_minus_1, date_plus_2),
        )

        best = None
        for row in cur.fetchall():
            ts_str, temp_f = row[0], row[1]
            try:
                t = dtparse.parse(ts_str)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=pytz.UTC)
                local_date = t.astimezone(tz).date()
                if local_date == target_date:
                    temp_f = float(temp_f)
                    if best is None or temp_f > best:
                        best = temp_f
            except (ValueError, OverflowError):
                continue

        return best

    def get_obs_highs_range(self, station: str, since_date: str) -> dict:
        """Return {date_str: max_temp_f} for all dates >= since_date for *station*.

        Groups observations by the station's LOCAL calendar day (not UTC).
        Used by DEB weight computation to pair model forecasts against observed
        daily highs without depending on the settlements table (which only
        populates from resolved live trades).

        Returns ``{}`` immediately (no query) if the station's city is marked
        ``training_eligible: false`` in ``config/source_priority.yaml`` (issue
        #558). Dates before the city's ``training_eligible_since`` cutover, if
        any, are silently dropped from the result (issue #766). Rows are
        unioned across every DB key returned by
        ``config.get_canonical_station_feeds(station)``, same as
        ``get_daily_obs_high``.
        """
        if station not in STATION_TZ:
            return {}

        city = _icao_to_city(station)
        if city is not None and not is_training_eligible(city):
            return {}

        try:
            since_date_obj = dtparse.parse(since_date).date()
        except (ValueError, TypeError):
            return {}

        eligible_since = get_training_eligible_since(city) if city is not None else None
        if eligible_since is not None and eligible_since > since_date_obj:
            since_date_obj = eligible_since

        tz = pytz.timezone(STATION_TZ[station])

        # Fetch all observations (across every canonical feed key) with temp_f
        # IS NOT NULL. Issue #731: exclude is_official=0 (Open-Meteo fallback)
        # rows so modelled data can't override real METAR as the observed daily
        # high the DEB weights train against -- same rationale as
        # get_daily_obs_high(). NULL is treated as official (legacy rows).
        # Issue #741: same raw_json LIKE '%source_fallback%' second signal as
        # get_daily_obs_high() -- see the comment there for the full rationale.
        feed_keys = get_canonical_station_feeds(station)
        placeholders = ",".join("?" * len(feed_keys))
        cur = self._conn.execute(
            f"SELECT ts, temp_f FROM observations "
            f"WHERE station IN ({placeholders}) AND temp_f IS NOT NULL "
            f"AND (is_official IS NULL OR is_official = 1) "
            f"AND (raw_json IS NULL OR raw_json NOT LIKE '%source_fallback%') "
            f"ORDER BY ts",
            (*feed_keys,),
        )

        result: dict[str, float] = {}
        for row in cur.fetchall():
            ts_str, temp_f = row[0], row[1]
            try:
                t = dtparse.parse(ts_str)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=pytz.UTC)
                local_date = t.astimezone(tz).date()
                if local_date >= since_date_obj:
                    date_str = local_date.isoformat()
                    temp_f = float(temp_f)
                    if date_str not in result or temp_f > result[date_str]:
                        result[date_str] = temp_f
            except (ValueError, OverflowError):
                continue

        return result

    def log_crps(
        self,
        city: str,
        date: str,
        crps_score: float,
        model_mode: str = "emos_shadow",
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
    ) -> None:
        """Insert a CRPS score record for *city* on *date*.

        forecast_source=None resolves to the active FORECAST_STACK (see
        _active_forecast_source) — same resolution pattern as the
        emos_calibration read/write helpers (issue #659). The shadow runner
        passes it explicitly so two different stacks' runs never collide on
        the same (city, date, model_mode) row (issue #759).

        sigma_source=None resolves to the active USE_ENSEMBLE_SIGMA-derived
        track (see _active_sigma_source) — same resolution pattern, so a
        sigma_source switch (issue #799) tags new rows with the lineage they
        were actually scored under instead of leaving them indistinguishable
        from rows logged under the previous sigma track (issue #851).
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        logged_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO emos_crps_log"
                    "(city,date,crps_score,model_mode,forecast_source,sigma_source,logged_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (city, date, crps_score, model_mode, forecast_source, sigma_source, logged_at),
                )

    def get_emos_crps_count(
        self,
        city: str,
        model_mode: str = "emos_shadow",
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
    ) -> int:
        """Return the number of CRPS log entries for *city* under *model_mode*.

        Defaults to 'emos_shadow' — this is what emos_mode.get_city_mode's
        promotion guard counts against EMOS_MIN_SAMPLES. Scoped explicitly
        (rather than counting all model_mode rows for the city) so the
        'legacy' baseline row logged alongside each 'emos_shadow' row
        (issue #667) does not silently double the promotion sample count.

        forecast_source=None resolves to the active FORECAST_STACK, so the
        promotion guard counts CRPS samples for the currently-served stack
        only, never pooling evidence accrued under a different stack across
        a FORECAST_STACK switch (issue #759).

        sigma_source=None resolves to the active USE_ENSEMBLE_SIGMA-derived
        track (see _active_sigma_source), so the promotion guard counts CRPS
        samples scored under the currently-active sigma lineage only, never
        pooling shadow-day evidence accrued under a different sigma_source
        (e.g. pre-#799 'fixed' coefficients) across a sigma_source switch
        (issue #851) — mirroring the #759 forecast_source guard above.
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM emos_crps_log "
            "WHERE city=? AND model_mode=? AND forecast_source=? AND sigma_source=?",
            (city, model_mode, forecast_source, sigma_source),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def emos_crps_logged_for_date(
        self,
        city: str,
        date: str,
        model_mode: str = "emos_shadow",
        forecast_source: "str | None" = None,
        sigma_source: "str | None" = None,
    ) -> bool:
        """Return True if a CRPS row already exists for (city, date, model_mode,
        forecast_source, sigma_source).

        Used by the daily shadow runner to avoid double-counting samples when
        the calibration runs more than once on the same calendar day (e.g. after
        a process restart resets the in-memory once-per-day gate).

        forecast_source=None resolves to the active FORECAST_STACK, so two
        different stacks calibrated on the same day each get their own row
        instead of the second stack silently skipping because the first
        already claimed that day's (city, date, model_mode) slot
        (issue #759).

        sigma_source=None resolves to the active USE_ENSEMBLE_SIGMA-derived
        track, so a sigma_source switch on the same calendar day (e.g. the
        day #799 flips USE_ENSEMBLE_SIGMA) does not silently skip logging
        the new lineage's first row because the old lineage already claimed
        that day's (city, date, model_mode, forecast_source) slot
        (issue #851).
        """
        if forecast_source is None:
            forecast_source = self._active_forecast_source()
        if sigma_source is None:
            sigma_source = self._active_sigma_source()
        cur = self._conn.execute(
            "SELECT 1 FROM emos_crps_log WHERE city=? AND date=? AND model_mode=? "
            "AND forecast_source=? AND sigma_source=? LIMIT 1",
            (city, date, model_mode, forecast_source, sigma_source),
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
