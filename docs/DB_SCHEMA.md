# Database Schema Reference

MeteoEdge uses SQLite for persistence. All data is stored in `data/meteoedge.db`. This document describes every table, column, data type, units, and component ownership.

## Overview

The database is organized into these functional areas:

1. **Weather Observations** — METAR, forecasts, and derived metrics
2. **Trading** — Candidates, trades, and positions
3. **Settlements** — Market outcomes and P&L
4. **Risk State** — Daily P&L tracking
5. **Model State** — Forecast logs, model weights, corrections
6. **Market Metadata** — TAF windows for forecasting
7. **Configuration** — DB-backed bot parameter store

---

## Table Schemas

### observations

**Purpose:** Persists all weather observations from METAR, NWS, JMA AMEDAS, AMOS, MSS, and other sources.

**Writer:** Data collectors (src/data/metar.py, src/data/collectors/\*, etc.)  
**Reader:** Weather state builder (_build_weather in src/scripts/run.py), intraday correction model, climbing rate calculations

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Observation timestamp (e.g., "2024-06-03T14:30:00+00:00") |
| `station` | TEXT NOT NULL | METAR code | No | METAR station code (KORD, KMIA, RKSI, etc.) |
| `temp_f` | REAL NOT NULL | °F | No | Temperature in Fahrenheit (normalized unit across the bot) |
| `temp_native` | REAL NOT NULL | °F or °C depending on unit | No | Temperature in the original unit from the data source |
| `unit` | TEXT NOT NULL CHECK(unit IN ('F','C')) | categorical | No | Original unit: 'F' for US stations, 'C' for international |
| `current_high` | REAL | °F | Yes | The bot's computed daily high for the station as of this observation (running maximum) |
| `source` | TEXT NOT NULL | categorical | No | Data source name: "metar", "nws", "jma_amedas", "amos", "mss", "open_meteo", etc. |
| `raw_json` | TEXT | JSON | Yes | Raw API response (for debugging/audit trail) |
| `cadence_min` | INTEGER | minutes | Yes | Expected cadence/frequency of this data source (e.g., 10 for 10-minute updates) |
| `is_official` | INTEGER DEFAULT 1 | boolean (0/1) | Yes | Whether this is an official/validated observation (vs. preliminary) |

**Indexes:**
```sql
CREATE INDEX idx_obs_station_ts ON observations(station, ts);
```

**Notes:**
- All temperatures are stored in °F internally, but `temp_native` and `unit` preserve the original for audit.
- `current_high` is updated as new observations arrive; it is the running daily high for the station.
- Multiple sources may report the same station; the scanner prioritizes by freshness and configured priority.

---

### candidates

**Purpose:** Trade candidates evaluated during each poll cycle. Logged for analysis and backtesting.

**Writer:** Market scanner (src/strategy/scanner.py)  
**Reader:** Analysis, candidate review, backtesting

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Poll timestamp when the candidate was evaluated |
| `station` | TEXT NOT NULL | METAR code | No | Station (KORD, KMIA, etc.) |
| `ticker` | TEXT NOT NULL | Polymarket ticker | No | Polymarket market ticker (e.g., "0x123abc...") |
| `bracket_low` | REAL NOT NULL | °F | No | Lower bracket boundary (e.g., 75.0 for 75-76°F) |
| `bracket_high` | REAL NOT NULL | °F | No | Upper bracket boundary (e.g., 76.0 for 75-76°F) |
| `side` | TEXT NOT NULL CHECK(side IN ('YES','NO')) | categorical | No | 'YES' if betting on bracket hit, 'NO' if betting on miss |
| `predicted_price` | INTEGER NOT NULL | ¢ (cents) | No | Model's fair value estimate (0-100) |
| `predicted_edge` | REAL NOT NULL | ¢ (cents) | No | Model price minus market price (positive = favorable) |
| `market_price` | INTEGER NOT NULL | ¢ (cents) | No | Market mid-price from Polymarket at evaluation time |
| `confidence` | REAL NOT NULL | probability [0,1] | No | Model confidence: p(YES) for YES side, p(NO) for NO side |
| `minutes_to_settlement` | REAL NOT NULL | minutes | No | Time remaining until market resolution |
| `flagged_first` | INTEGER NOT NULL DEFAULT 1 | boolean (0/1) | No | Whether this candidate was flagged on first appearance (1) or re-evaluated (0) |
| `direction` | TEXT NOT NULL DEFAULT 'high' | categorical | No | Market direction: `'high'` for daily-high markets, `'low'` for daily-low markets (Epic C). Legacy rows default to `'high'`. |

**Indexes:**
```sql
CREATE INDEX idx_cand_station_ts ON candidates(station, ts);
CREATE INDEX idx_cand_ticker ON candidates(ticker);
CREATE INDEX idx_cand_station_direction ON candidates(station, direction);
```

**Notes:**
- A candidate is flagged when edge, confidence, and price pass filters.
- `flagged_first = 1` means the edge appeared on the first poll cycle for this bracket; 0 means it was already flagged earlier.
- Candidates are logged but not automatically traded; the risk manager applies additional filters (position count, capital, liquidity).

---

### trades

**Purpose:** Record of all executed trades (paper or live). Core audit trail and P&L tracking.

**Writer:** Live trader (src/execution/live_trader.py), paper trader (src/scripts/run.py)  
**Reader:** Dashboard, settlement, P&L analysis

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Trade execution timestamp |
| `station` | TEXT NOT NULL | METAR code | No | Station (KORD, KMIA, etc.) |
| `ticker` | TEXT NOT NULL | Polymarket ticker | No | Market ticker |
| `bracket_low` | REAL NOT NULL | °F | No | Bracket lower boundary |
| `bracket_high` | REAL NOT NULL | °F | No | Bracket upper boundary |
| `side` | TEXT NOT NULL CHECK(side IN ('YES','NO')) | categorical | No | Side traded: 'YES' or 'NO' |
| `predicted_price` | INTEGER NOT NULL | ¢ | No | Fair value at entry (model snapshot) |
| `actual_price` | INTEGER NOT NULL | ¢ | No | Actual fill price on the exchange |
| `slippage` | INTEGER | ¢ | Yes | Difference between predicted and actual (may be negative) |
| `predicted_edge` | REAL NOT NULL | ¢ | No | Edge at entry (fair_value - market_price) |
| `mode` | TEXT NOT NULL CHECK(mode IN ('paper','live','shadow')) | categorical | No | 'paper' for simulated, 'live' for real executed, 'shadow' for YES candidates logged when ENABLE_YES_TRADES=False |
| `order_id` | TEXT | Polymarket order ID | Yes | Exchange order ID (only set for live trades) |
| `outcome` | TEXT | categorical | Yes | Trade exit reason: 'filled' (held to expiry), 'sold' (early exit), 'cancelled', 'timeout' |
| `pnl` | REAL | € | Yes | Realized P&L in euros (set after settlement or early exit) |
| `capital_before` | REAL NOT NULL | € | No | Capital/position size at entry |
| `capital_after` | REAL | € | Yes | Capital after exit (for paper trades, = capital_before + pnl) |
| `settled_at` | TEXT | ISO 8601 timestamp (UTC) | Yes | Timestamp when settlement P&L was written |
| `close_reason` | TEXT | categorical | Yes | Why position was closed early: `take_profit`, `forced_exit`, `stop_loss`. NULL = held to settlement. |
| `minutes_to_settlement_at_close` | REAL | minutes | Yes | Minutes remaining until settlement when position was exited early |
| `bid_depth_at_close` | INTEGER | shares | Yes | Best-bid depth at the moment of early exit (for liquidity analysis) |
| `direction` | TEXT NOT NULL DEFAULT 'high' | categorical | No | Market direction: `'high'` for daily-high markets, `'low'` for daily-low markets. Legacy rows default to `'high'`. |
| `end_date` | TEXT | YYYY-MM-DD | Yes | Market resolution date, populated at insert time from the market's `endDate` (live trades only; see #609). NULL for rows written before this column existed ("legacy" rows) -- `settle.resolve_trade_date()` falls back to the station-local calendar date of `ts` (via `STATION_TZ`) for those. |

**Indexes:**
```sql
CREATE INDEX idx_trades_station_ts ON trades(station, ts);
CREATE INDEX idx_trades_mode ON trades(mode);
CREATE INDEX idx_trades_station_direction ON trades(station, direction);
```

**Notes:**
- `actual_price` is the fill price; `slippage = actual_price - predicted_price`.
- `pnl` is filled in by the settle script (if outcome='filled') or by the exit handler (if outcome='sold').
- For NO trades that are early-exited: `pnl = (sell_price - entry_price) / 100 * shares`.
- For filled trades: `pnl = (100 - entry_price) / 100 * shares` if YES bracket hit (or NO bracket miss), else `pnl = -(entry_price / 100) * shares`.
- **Live settlement is DB-driven (issue #609):** `settle_live_trades()` selects `mode='live' AND outcome='filled' AND settled_at IS NULL` via `db.get_unsettled_live_trades()`, matches each row to *target* via `end_date` (or the `ts` fallback above), and computes `shares = capital_before / (actual_price/100)` -- `capital_before` holds the EUR size committed at order placement for live trades (the DB equivalent of the old `live_trades.jsonl` `size_eur` field). A sold position never appears here twice: `order_manager._record_sell_in_db()` flips the *same* row (matched by `order_id`) from `outcome='filled'` to `outcome='sold'` and sets `settled_at` at sell time, so it's naturally excluded from the unsettled-live query rather than settled again. `live_trades.jsonl` is enrichment-only for this path (best-effort dashboard display), never the source of truth.
- **SELL records in live_trades.jsonl**: the `shares` field reflects the *remaining* shares sold in that specific exit attempt, not the full original position size. When a stop-loss IOC order partially fills across multiple poll cycles, each retry records only the unfilled remainder (see `_partial_fill_shares` tracking in `src/scripts/run.py`). The `size_eur` field still reflects the original position notional for context.
- **Shadow rows** (`mode='shadow'`): inserted when a YES candidate passes all selection gates but `ENABLE_YES_TRADES=False`. No order is placed; `capital_before=0.0`, `order_id=NULL`. `actual_price` holds the observed yes_ask_cents at logging time. Settlement uses a $1 notional stake: `pnl = (100 - actual_price) / 100` if YES bracket hit, else `pnl = -actual_price / 100`. These rows are excluded from live P&L accounting — they are an observational dataset for validating YES-side edge.

---

### settlements

**Purpose:** Record the actual market outcomes and resolution prices. One row per market.

**Writer:** Settlement script (src/scripts/settle.py)  
**Reader:** P&L reconciliation, dashboard

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Settlement timestamp (typically ~9h after market closes) |
| `station` | TEXT NOT NULL | METAR code | No | Station |
| `ticker` | TEXT NOT NULL UNIQUE | Polymarket ticker | No | Market ticker (UNIQUE — one outcome per market) |
| `bracket_low` | REAL NOT NULL | °F | No | Bracket lower boundary |
| `bracket_high` | REAL NOT NULL | °F | No | Bracket upper boundary |
| `actual_high_f` | REAL NOT NULL | °F | No | Actual daily high temperature at settlement (from METAR 48h history) |
| `resolved_yes` | INTEGER NOT NULL | boolean (0/1) | No | 1 if bracket was hit (YES wins), 0 if missed (NO wins) |
| `market_final_price` | INTEGER | ¢ | Yes | Final market price at settlement (may be 100 if YES, 0 if NO) |
| `source` | TEXT NOT NULL DEFAULT 'polymarket' | categorical | No | Source of settlement truth (currently 'polymarket') |
| `direction` | TEXT NOT NULL DEFAULT 'high' | categorical | No | Market direction: `'high'` for daily-high, `'low'` for daily-low markets. Legacy rows default to `'high'`. |

**Indexes:**
```sql
CREATE INDEX idx_settlements_station_ts ON settlements(station, ts);
CREATE INDEX idx_settlements_station_direction ON settlements(station, direction);
```

**Notes:**
- Unique on `ticker` to prevent duplicate settlements.
- Uses INSERT OR REPLACE semantics (upsert) in code.
- `actual_high_f` is fetched from NWS/METAR 48-hour history.

---

### open_positions

**Purpose:** Current open orders and positions. Used for position tracking, exit monitoring, and recovery on restart.

**Writer:** Live trader at entry (src/execution/live_trader.py), close_position() at exit  
**Reader:** Dashboard, take-profit/stop-loss monitors, wallet reconciliation

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `trade_id` | INTEGER NOT NULL REFERENCES trades(id) | foreign key | No | Links to the trades row for this position |
| `station` | TEXT NOT NULL | METAR code | No | Station |
| `ticker` | TEXT NOT NULL | Polymarket ticker | No | Market ticker |
| `token_id` | TEXT NOT NULL | Polymarket token ID | No | Token ID for this side (yes_token_id or no_token_id) |
| `side` | TEXT NOT NULL CHECK(side IN ('YES','NO')) | categorical | No | Side ('YES' or 'NO') |
| `order_id` | TEXT NOT NULL | Polymarket order ID | No | Order ID from exchange (not necessarily filled, may be GTC pending) |
| `entry_price` | INTEGER NOT NULL | ¢ | No | Entry fill price |
| `shares` | REAL NOT NULL | count | No | Number of shares held (fractional) |
| `entry_ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Entry/fill timestamp |
| `stop_loss_cents` | INTEGER | ¢ | Yes | Stop-loss threshold for NO positions (sell if bid <= this) |
| `take_profit_cents` | INTEGER | ¢ | Yes | Take-profit threshold (sell if bid >= this) |

**Unique Index:**
```sql
CREATE UNIQUE INDEX idx_open_positions_order ON open_positions(order_id);
```

**Notes:**
- One row per open order/position. Deleted on exit (take-profit, stop-loss, or market resolution).
- `stop_loss_cents` and `take_profit_cents` are optional but used by exit monitors.
- Recovering after restart: `_sync_open_orders()` rebuilds the dedup guard from this table.

---

### risk_state

**Purpose:** Daily accumulated P&L and position count tracking. Enforces daily loss limits.

**Writer:** RiskManager.upsert_daily_risk() (src/risk/manager.py)  
**Reader:** Risk manager, daily loss checks, dashboard

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `trade_date` | TEXT PRIMARY KEY | YYYY-MM-DD | No | Date (UTC) — acts as the row key |
| `daily_pnl` | REAL NOT NULL DEFAULT 0.0 | € | No | Cumulative P&L for the day (incremented on each trade exit) |
| `open_positions` | INTEGER NOT NULL DEFAULT 0 | count | No | Current number of open positions at end of day |
| `updated_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Last update timestamp |

**Notes:**
- Upserted via INSERT OR REPLACE with accumulated pnl_delta.
- Checked against `RISK_DAILY_LOSS_LIMIT_EUR` to halt trading for the day.
- One row per trading day; persists across restarts.

---

### taf_windows

**Purpose:** Terminal Aerodrome Forecast (TAF) data for wind and severe weather. Optional, used to enhance forecast reliability.

**Writer:** TAF collector daemon (src/data/taf_collector.py)  
**Reader:** Forecast model, weather state builder

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `city` | TEXT NOT NULL | city name | No | City name (e.g., "Chicago", "Seoul") |
| `issued_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | TAF issuance time |
| `valid_from` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | TAF validity start time |
| `valid_to` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | TAF validity end time |
| `group_type` | TEXT NOT NULL | categorical | No | Forecast group type: "TEMPO" (temporary), "BECMG" (becoming), base conditions |
| `temp` | REAL | °C | Yes | Forecast temperature (if present in TAF) |
| `wind_kt` | REAL | knots | Yes | Forecast wind speed |
| `sig_wx` | TEXT | code | Yes | Significant weather code (e.g., "TSRA" = thunderstorms with rain) |
| `raw_text` | TEXT | TAF text | Yes | Raw TAF text for parsing/audit |

**Indexes:**
```sql
CREATE INDEX idx_taf_city_from ON taf_windows(city, valid_from);
```

**Notes:**
- Populated by the TAF collector daemon; optional.
- Used to detect forecast degradation (wind, severe weather) during position holding.

---

### model_weights

**Purpose:** Store ensemble model weights per station/model/date. Used for adaptive weighting of NWS vs. Open Meteo forecasts.

**Writer:** DEB weighting engine (src/model/deb_weighting.py)  
**Reader:** Forecast blending, weight distribution checks

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `city` | TEXT NOT NULL | city name | No | City (e.g., "Chicago") |
| `model` | TEXT NOT NULL | categorical | No | Model name: "nws" or "open_meteo" |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | Date the weight applies to |
| `weight` | REAL NOT NULL | probability [0,1] | No | Weight for this model (e.g., 0.6 for NWS, 0.4 for Open Meteo) |
| `rmse` | REAL NOT NULL | °F | No | Root mean squared error of the model's forecast on this date |

**Primary Key:**
```sql
PRIMARY KEY (city, model, date)
```

**Indexes:**
```sql
CREATE INDEX idx_mw_city_date ON model_weights(city, date);
```

**Notes:**
- Recomputed daily using historical forecast accuracy.
- Blended forecast = `weight_nws * nws_forecast + weight_open_meteo * open_meteo_forecast`.

---

### model_forecast_log

**Purpose:** Log of model forecasts per station/model/date/lead_hours. Used for EMOS calibration training and DEB weight computation.

**Writer:** Cron capture worker (`src/scripts/capture_forecasts.py`) — the SOLE writer after the #422 migration. The scanner loop (`src/scripts/run.py`) no longer writes to this table.  
**Reader:** EMOS calibration (`src/model/emos_calibration.py`), DEB weighting (`src/model/deb_weighting.py`), promotion gate

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `station` | TEXT NOT NULL | METAR code | No | Station identifier (e.g. "KORD") |
| `model` | TEXT NOT NULL | categorical | No | Model: "nws", "open_meteo", or "gfs" |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | Forecast target date (what day does the forecast predict?) |
| `forecast_high_f` | REAL NOT NULL | °F | No | Forecasted daily high in Fahrenheit |
| `logged_at` | TEXT NOT NULL | ISO 8601 UTC | No | When the row was written to the database |
| `lead_hours` | INTEGER | hours | Yes | Lead time in hours (e.g. 3, 6, 12, 18, 24). NULL for legacy rows pre-#422. |
| `issued_at` | TEXT | ISO 8601 UTC | Yes | Wall-clock UTC time the capture was fetched. NULL for legacy rows. |
| `sigma_f` | REAL | °F | Yes | Ensemble spread (std-dev) at the time of capture. NULL when the source does not expose spread (falls back to `FORECAST_STDDEV_F` in EMOS). |

**Unique Index:**
```sql
CREATE UNIQUE INDEX idx_mfl_station_model_date_lead
    ON model_forecast_log(station, model, date, lead_hours);
```

**Notes:**
- One row per `(station, model, date, lead_hours)` combination.
- Multiple rows per `(station, model, date)` are expected once the cron worker runs: one row per scheduled lead-time bin (3h, 6h, 12h, 18h, 24h).
- Legacy rows pre-#422 migration have `lead_hours IS NULL` and are stored in `model_forecast_log_legacy_v1`. They are not used for EMOS training.
- `sigma_f` for NWS is a climatological approximation keyed on lead_hours (see `src/data/nws.py`). For Open-Meteo and GFS it is computed as the cross-model standard deviation.
- EMOS reader filters rows by `lead_hours` (default 24). DEB weighting also uses lead_hours=24 slice.

### model_forecast_log_legacy_v1

**Purpose:** Read-only audit copy of the pre-#422 `model_forecast_log` table. Contains nowcast snapshots (logged_at ≈ 23:54-23:59 UTC, last-write-wins). Not used for EMOS training.

**Writer:** None (created by the #422 migration, never written to again).  
**Reader:** Audit trail only.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER | Original row ID |
| `station` | TEXT | Station |
| `model` | TEXT | Model |
| `date` | TEXT | Forecast date |
| `forecast_high_f` | REAL | Forecasted daily high (°F) |
| `logged_at` | TEXT | When the nowcast was captured |

---

### intraday_corrections

**Purpose:** Store temperature corrections applied to the ensemble forecast during the day as new METAR observations arrive. Each row is scoped to the specific `(station, source)` pair that produced the observation.

**Writer:** Intraday correction model (src/model/intraday_correction.py)  
**Reader:** Weather state builder, envelope model, residual correction module

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `city` | TEXT NOT NULL | city name | No | City (e.g., "Chicago") |
| `station` | TEXT NOT NULL DEFAULT '' | station key | No | Station identifier used by the observation source (e.g., "Busan", "RKPK") |
| `source` | TEXT NOT NULL DEFAULT '' | source name | No | Data source name (e.g., "amos", "metar", "mss") |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | Forecast date (what day?) |
| `obs_time` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Observation time (when was the observation made?) |
| `obs_temp_f` | REAL NOT NULL | °F | No | Observed temperature (from METAR or other source) |
| `model_temp_f` | REAL NOT NULL | °F | No | Model's predicted temperature at obs_time |
| `delta_f` | REAL NOT NULL | °F | No | Observed minus model (bias) |
| `corrected_mu_f` | REAL NOT NULL | °F | No | Corrected forecast high after applying intraday learning |
| `decay_factor` | REAL NOT NULL | fraction [0,1] | No | Weight given to the bias (decays as temperature approaches daily high) |

**Primary Key:**
```sql
PRIMARY KEY (city, station, source, date, obs_time)
```

**Indexes:**
```sql
CREATE INDEX idx_ic_city_date ON intraday_corrections(city, date);
CREATE INDEX idx_ic_city_station_source_date ON intraday_corrections(city, station, source, date);
```

**Migration (issue #340):** Pre-existing databases with the old `(city, date, obs_time)` PK are automatically migrated on first `Database()` construction. Legacy rows receive `station=''` and `source=''` defaults.

**Notes:**
- Updated as new observations arrive during the day.
- `corrected_mu_f` converges to the actual daily high as the day progresses.
- Used to improve bracket probability estimates in real-time.
- The residual correction module queries per-(station, source) pair before falling back to city-wide aggregation.

---

### emos_calibration

**Purpose:** Store EMOS (Error Model Output Statistics) calibration coefficients for ensemble weather forecasts.

**Writer:** EMOS calibration training pipeline (e.g., `src/model/emos_trainer.py`)  
**Reader:** Forecast ensemble model for real-time probability adjustments

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `city` | TEXT NOT NULL | city name | No | City (e.g., "Chicago", "Seoul") |
| `model_mode` | TEXT NOT NULL | categorical | No | Deployment mode: `'legacy'` (existing Gaussian, default), `'emos_shadow'` (compute both, serve legacy), `'emos_primary'` (serve EMOS — requires `ready_for_promotion=1`) |
| `forecast_source` | TEXT NOT NULL DEFAULT 'nws_open_meteo' | categorical | No | Forecast stack this fit was trained on (e.g. `'nws_open_meteo'`, `'hrrr_nbm'`). Keys parallel per-stack calibration tracks so a retrain never overwrites another stack's row (issue #659). |
| `sigma_source` | TEXT NOT NULL DEFAULT 'fixed' | categorical | No | Sigma track this fit was trained on: `'fixed'` (constant `FORECAST_STDDEV_F`) or `'ensemble'` (persisted per-row `model_forecast_log.sigma_f`). Keys a parallel track the same way `forecast_source` does (issue #449). |
| `lead_hours` | INTEGER NOT NULL DEFAULT 24 | hours | No | Lead-time bin this fit was trained on (e.g. 3, 6, 12, 18, 24). Serving selects the row whose `lead_hours` is nearest to minutes-to-settlement at scan time (issue #665). |
| `a` | REAL NOT NULL | statistical coefficient | No | EMOS coefficient a (mu intercept) |
| `b` | REAL NOT NULL | statistical coefficient | No | EMOS coefficient b (mu slope) |
| `c` | REAL NOT NULL | statistical coefficient | No | EMOS coefficient c (sigma intercept) |
| `d` | REAL NOT NULL | statistical coefficient | No | EMOS coefficient d (sigma slope) |
| `crps_score` | REAL | continuous ranked probability | Yes | Continuous ranked probability skill score on validation set |
| `ready_for_promotion` | INTEGER DEFAULT 0 | boolean (0/1) | No | Whether calibration is ready to promote to production |
| `trained_at` | TEXT | ISO 8601 timestamp (UTC) | Yes | Timestamp when calibration was trained |

**Unique Constraint:**
```sql
UNIQUE(city, model_mode, forecast_source, sigma_source, lead_hours)
```

**Notes:**
- One row per (city, model_mode, forecast_source, sigma_source, lead_hours) tuple. Saving a new forecast_source/sigma_source/lead_hours combination for a city never overwrites another combination's row — only an exact key match is replaced.
- EMOS post-processing corrects systematic forecast bias and improves probability estimates.
- `crps_score` quantifies calibration quality; lower is better.
- `ready_for_promotion` gates whether this calibration is safe to use in live forecasts.

---

### emos_crps_log

**Purpose:** Per-city, per-day CRPS (Continuous Ranked Probability Score) evidence log. Backs the `emos_mode.get_city_mode` promotion guard — a city needs at least `EMOS_MIN_SAMPLES_PROMOTION` logged rows before it may serve `emos_primary` — and the EMOS-vs-legacy comparison surfaced on the dashboard (`/api/emos-shadow/status`).

**Writer:** `scripts/run_emos_shadow.py` (`_run_calibration`), once per city per day  
**Reader:** `emos_mode._primary_allowed` (promotion guard), `Database.get_emos_shadow_city_status` (dashboard)

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `city` | TEXT NOT NULL | city name | No | City (e.g., "Chicago") |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | Calendar date the CRPS score was computed for |
| `crps_score` | REAL NOT NULL | continuous ranked probability | No | Mean CRPS over that day's training triples for this `model_mode`/`forecast_source` |
| `model_mode` | TEXT NOT NULL DEFAULT 'emos_shadow' | categorical | No | `'emos_shadow'` (EMOS-transformed coefficients, what the promotion guard counts) or `'legacy'` (same triples scored with raw, uncorrected mu/sigma — the EMOS-vs-legacy comparison baseline, issue #667) |
| `forecast_source` | TEXT NOT NULL DEFAULT 'baseline' | categorical | No | Forecast stack this CRPS row was scored against (e.g. `'baseline'`, an expanded stack). Default `'baseline'` backfills every pre-#759 row. Keys the promotion counter and per-day dedup guard alongside `(city, model_mode)` so two different stacks calibrated the same day each accrue independent CRPS evidence instead of colliding on one shared daily slot (issue #759). |
| `logged_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | When the row was written |

**DDL:**
```sql
CREATE TABLE IF NOT EXISTS emos_crps_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    city            TEXT NOT NULL,
    date            TEXT NOT NULL,
    crps_score      REAL NOT NULL,
    model_mode      TEXT NOT NULL DEFAULT 'emos_shadow',
    forecast_source TEXT NOT NULL DEFAULT 'baseline',
    logged_at       TEXT NOT NULL
);
```

**Notes:**
- One `emos_shadow` row and one `legacy` row are written per city per calendar day (never more — `emos_crps_logged_for_date` dedups a same-day re-run, e.g. after a process restart).
- `get_emos_crps_count(city, model_mode='emos_shadow', forecast_source=None)` — the promotion counter — and `emos_crps_logged_for_date` both resolve `forecast_source=None` to the active `FORECAST_STACK` (via `Database._active_forecast_source`, same resolver `emos_calibration` reads/writes use), so callers that don't pass it explicitly automatically scope to whichever stack is currently active.
- The promotion guard in `emos_mode._primary_allowed` therefore counts CRPS samples for the active stack only — evidence logged under a different `forecast_source` never pools into another stack's promotion decision, even across a `FORECAST_STACK` switch.
- `Database.get_emos_shadow_city_status`'s `mean_crps`/`legacy_mean_crps` dashboard aggregates are scoped by `model_mode` only (not `forecast_source`) — out of scope for #759, tracked as a possible follow-up if per-stack dashboard display parity is needed.

---

### poll_runs

**Purpose:** Unconditional poll heartbeat — written once per poll cycle before any bracket evaluation (issue #914). Provides the daily health report with an accurate poll cadence and gap metric, decoupled from `scan_decisions` (which is only written when brackets are actually evaluated, a much rarer event). This is an append-only log; its only purpose is the health report's "Polls 24h" / gap calculation.

**Writer:** `Database.record_poll_run(poll_ts, mode)`, called at the top of `src.scripts.run.poll_once()`, before any bracket scanning/evaluation. The write is wrapped in try/except — a DB hiccup here must never block the rest of the poll cycle.

**Reader:** `src.scripts.daily_health_report._build_bot_health` — counts rows within the last 24h for the "Polls 24h" metric and computes the max inter-poll gap from the timestamp sequence.

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | — | No | Row ID |
| `poll_ts` | TEXT NOT NULL | ISO-8601 UTC | No | When the poll cycle started |
| `mode` | TEXT NOT NULL DEFAULT `'paper'` | — | No | `'live'` or `'paper'` — mirrors the poll's trading mode |

```sql
CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_ts     TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'paper'
);
CREATE INDEX IF NOT EXISTS idx_poll_runs_poll_ts ON poll_runs(poll_ts);
```

**Notes:**
- Created idempotently via `CREATE TABLE IF NOT EXISTS` in `Database.__init__` — no migration script needed.
- This table is intentionally decoupled from `scan_decisions`. A poll that evaluates zero brackets still writes a heartbeat row here, which is the whole point (defect #1).
- No retention policy currently applied — a week of 5-min polls is ~2,016 rows, so this table stays small indefinitely.

---

### scan_decisions

**Purpose:** The bot's-eye per-bracket decision view for the Edge tab (epic #754). One row per `(station, ticker, date)` holding the **most recent poll's** evaluated bracket — the same `p_yes`/`ev_yes`/`ev_no`/`emos_mode` numbers the scanner traded on (never a parallel recompute), plus the `gate_verdict` naming the exact reason that bracket did or didn't trade. Upserted every poll — this is a live "last known state" table, not an append-only log (`candidates`/`snapshots.jsonl` remain the historical record).

**Writer:** `src.scripts.run.poll_once`, once per evaluated high-side bracket per poll (mirrors the `db.insert_candidate` call site, issue #684). `src.strategy.scanner.scan_markets` attaches the pre-execution `gate_verdict` (+ every other column below) to each evaluated bracket's snapshot; `poll_once` upgrades the `traded_live` placeholder to `entry_guard` / `timeout_today` / (confirmed) `traded_live` once the entry-guard check and any live execution attempt for that bracket have resolved (the "verdict seam" — see issue #756's PR for the exact line-level seam between scanner.py and run.py). Low-side (shadow-only) brackets never produce a `snapshots.jsonl` row today and are therefore never written here either — out of scope for this table, same as `snapshots.jsonl`. Every row is also stamped with `execution_mode` (issue #780, `_persist_scan_decisions`) so a `traded_live` verdict can be trusted as a confirmed exchange fill (`execution_mode='live'`) vs. the scanner's unconfirmed paper-mode placeholder (`execution_mode='paper'`).
**Reader:** `Database.get_scan_decisions(station, date)`, consumed by `GET /api/analysis/{station}` (issue #757 — see `docs/OPERATIONS.md`'s "Edge Tab — Analysis API" section for the full response contract). The endpoint filters the returned rows by `is_next_day` to partition Today vs D+1.

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `station` | TEXT NOT NULL | METAR code | No | Station code (e.g., `KMIA`) — part of the primary key |
| `ticker` | TEXT NOT NULL | Polymarket condition ID | No | Bracket's market ticker — part of the primary key |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | The bracket's own settlement date (today for same-day evaluation, the market's own future date for a next-day evaluation, issue #687) — part of the primary key |
| `ts` / `poll_ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | When this poll ran. Two columns for parity with `snapshots.jsonl`'s `ts` field and the design spec's `poll_ts` freshness-indicator field — always written with the same value |
| `bracket_low` / `bracket_high` | REAL NOT NULL | °F | No | Bracket bounds (same convention as `candidates`) |
| `side` | TEXT | `'YES'` / `'NO'` / NULL | Yes | The traded/candidate side, from `Candidate.side` — NULL when this bracket never produced a `Candidate` at all (i.e. every rejection verdict except `mae_gate`, `shadow_only`, `next_day_shadow`, `entry_guard`, `timeout_today`, `traded_live`) |
| `yes_ask` / `no_ask` | INTEGER | cents | Yes | Market ask price on each side at scan time, clamped to `max(1, min(99, round(price * 100)))` (`_safe_price`/`parse_bracket_from_market`, `src/strategy/scanner.py`) — destroys sub-penny prices, which matters because Polymarket's own tick size tightens to $0.001 for price > 0.96 or < 0.04 (issue #1076). Retained unchanged because gates/sizing/fee estimation are all integer-cent-native; prefer `yes_price_raw`/`no_price_raw` below for true-price analysis |
| `yes_price_raw` / `no_price_raw` | REAL | probability [0,1] | Yes | Unclamped venue price on each side at scan time, alongside `yes_ask`/`no_ask` (issue #1076) — the field that answers the rail question `yes_ask`/`no_ask` cannot. NULL only for rows written before this migration; from this migration forward it mirrors `_safe_price`'s own 0.5 fallback when the venue price is missing/unparseable (see `Bracket` in `src/model/envelope.py`) |
| `current_high` / `latest_temp` / `forecast_high` | REAL | °F | Yes | Same-day observation/forecast context; NULL for a next-day row (no today-anchored observation exists yet — `forecast_high` instead holds the next-day mean fed into `p_yes`, see `scan_markets`) |
| `p_yes` / `raw_p_yes` / `capped_p_yes` | REAL | probability [0,1] | Yes | Model probability: capped (served/traded-on), raw (pre-`MODEL_PROB_CAP` clamp, issue #551), and capped again under its own name for `snapshots.jsonl` field parity |
| `ev_yes` / `ev_no` | REAL | cents | Yes | Expected value of buying YES / NO at the capped probability — the numbers the scanner actually gated on |
| `ev_yes_raw` / `ev_no_raw` | REAL | cents | Yes | Same, at the raw (uncapped) probability — logging/ranking only, never a gate input |
| `minutes_to_settlement` | REAL | minutes | Yes | Time to market close at scan time |
| `emos_mode` | TEXT | `'legacy'` / `'emos_shadow'` / `'emos_primary'` / `'next_day'` | Yes | Which forecast-serving path produced `p_yes` this poll |
| `is_next_day` | INTEGER NOT NULL DEFAULT 0 | boolean (0/1) | No | 1 when this bracket came from next-day evaluation (issue #687) |
| `gate_verdict` | TEXT NOT NULL | enum, 12 values | No | Exactly one of `traded_live`, `shadow_only`, `next_day_shadow`, `entry_guard`, `timeout_today`, `below_min_edge`, `above_max_edge`, `below_min_price`, `below_min_confidence`, `margin_gate`, `mae_gate`, `day_mismatch_shadow` — the single source of truth is `src.strategy.gate_verdicts.GATE_VERDICTS`, enforced by `Database.upsert_scan_decision` raising `ValueError` on any other value. **No DDL `CHECK` constraint** (issue #912 — see the DDL note below for why one existed briefly and was removed). Names locked with the design spec (`docs/design/edge-tab-bracket-decisions.md`) |
| `gate_actual` / `gate_threshold` / `gate_unit` | REAL / REAL / TEXT | varies (`cents`, `degrees_f`, `probability`) | Yes | The compared numbers for the six numeric-rejection verdicts (`below_min_edge`, `above_max_edge`, `below_min_price`, `below_min_confidence`, `margin_gate`, `mae_gate`) — the exact actual-vs-threshold pair `scan_markets` already computed. NULL for every other verdict |
| `gate_detail` | TEXT | prose | Yes | For `entry_guard`: the guard reason string verbatim from `run.py` (e.g. "open position already exists for this token") — passed through, never reconstructed by a reader. For `timeout_today`: the raw execution outcome (e.g. `execution outcome: timeout`). NULL otherwise |
| `execution_mode` | TEXT NOT NULL DEFAULT `'paper'` | `'live'` / `'paper'` | No | Issue #780: whether this poll had a live trader configured (`'live'`) or not (`'paper'`), stamped uniformly on every bracket row that poll by `_persist_scan_decisions`. Enforced by a `CHECK` constraint and by `Database.upsert_scan_decision` raising `ValueError` on any other value. Combined with `gate_verdict`, this is what distinguishes a confirmed live exchange fill (`traded_live` + `'live'`) from the scanner's unconfirmed "would trade live" placeholder (`traded_live` + `'paper'`) — every pre-#780 row defaults to `'paper'`, the conservative reading |
| `ensemble_mean` | REAL | °F | Yes | Weighted ensemble forecast mean for `(station, date)`, sourced read-only from `get_ensemble_distribution()` (same `model_forecast_log`-backed function the pre-#756 Edge tab used) — demoted into the "Forecast inputs" collapsed detail, not retired |
| `ensemble_members` | INTEGER | count | Yes | Number of distinct forecast models contributing to `ensemble_mean` |
| `ensemble_range_low` / `ensemble_range_high` | REAL | °F | Yes | Min/max forecast value across contributing models (labelled "5th–95th pct" in the UI, matching the pre-#756 KPI strip's existing min/max-as-range convention) |
| `direction` | TEXT NOT NULL DEFAULT `'high'` | `'high'` / `'low'` | No | Issue #900: daily-high vs daily-low market, mirroring `candidates`/`trades`/`settlements`' own `direction` column — stamped by `scan_markets` on every snapshot (`'high'` for the high-side scan, `'low'` for the shadow-only low-side scan, issue #733). Every pre-#900 row defaults to `'high'` — `scan_decisions` only started existing after the high-side scanner did, so the default is historically accurate, not just a placeholder |

**DDL:**
```sql
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
);
CREATE INDEX IF NOT EXISTS idx_scan_decisions_station_date ON scan_decisions(station, date);
```

Existing (pre-#780) installations pick up `execution_mode` via the idempotent
`ALTER TABLE ... ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'paper'`
migration in `Database._migrate` — same pattern as every other post-launch
column on this table. The `ALTER TABLE` omits the `CHECK` constraint (SQLite
allows it, but no other migration in this codebase adds one); validity is
instead enforced in Python by `Database.upsert_scan_decision`, same as
`gate_verdict`'s own `ValueError`-on-invalid-value guard.

Existing (pre-#900) installations pick up `direction` the same way, via
`ALTER TABLE ... ADD COLUMN direction TEXT NOT NULL DEFAULT 'high'` in
`Database._migrate`.

Existing (pre-#1076) installations pick up `yes_price_raw`/`no_price_raw` the
same way, via `ALTER TABLE ... ADD COLUMN yes_price_raw REAL` / `... ADD
COLUMN no_price_raw REAL` in `Database._migrate`.

**gate_verdict's dropped `CHECK` constraint (issue #912):** `gate_verdict`
briefly had a hand-written `CHECK(gate_verdict IN (...))` alongside this
table's two other allow-lists (`scanner.GATE_VERDICTS`,
`Database._SCAN_DECISION_GATE_VERDICTS`). Because that `CHECK` lived inside
`CREATE TABLE IF NOT EXISTS`, adding `'day_mismatch_shadow'` (issue #820) to
the other two allow-lists but not the DDL string left every *existing*
database rejecting that verdict with a raw `sqlite3.IntegrityError` — SQLite
cannot `ALTER` a `CHECK` constraint in place. The constraint has now been
removed entirely (not widened): `src.strategy.gate_verdicts.GATE_VERDICTS`
is the single source of truth, imported by both `scanner.py` and `db.py`,
and the only validation is `Database.upsert_scan_decision`'s
`ValueError`-on-invalid-value guard, which runs before the row ever reaches
SQLite and reports a clearer error than a `CHECK` failure would. Existing
databases that still carry the old `CHECK` are fixed by an idempotent
table-rebuild migration in `Database._migrate` (create → copy → drop →
rename, detected via `sqlite_master`'s stored `CREATE TABLE` text — same
pattern as the `trades.mode` migration above).

**Upsert-per-poll semantics:**
- `Database.upsert_scan_decision(...)` is an `INSERT ... ON CONFLICT(station, ticker, date) DO UPDATE SET ...` — a fresh poll for the same key **replaces** the prior row's every column in place. The table always reflects the single most recent scan for a bracket, never an accumulating history.
- This is a deliberate contrast with `candidates` (one row per candidate per poll, append-only) and `snapshots.jsonl` (append-only log) — `scan_decisions` exists specifically to answer "what did the bot see on its **last** poll," so old rows would only add noise.
- Every write is best-effort: a failed upsert is caught and logged (`log.warning`), never raised into the polling loop — a DB hiccup on this surfacing-only table must not affect trading.

**The verdict seam (scanner ↔ run.py):**
1. `scan_markets` computes the pre-execution verdict for every evaluated bracket from the existing gate branches it already takes — no new control flow, purely an attached label. A live (non-shadow, non-next-day) candidate that clears every gate gets the optimistic placeholder `traded_live`.
2. `run.py`'s entry-guard check (duplicate-candidate / open-position / same-day-reentry) downgrades that placeholder to `entry_guard` (with the real guard reason in `gate_detail`) if it fires before execution is even attempted — same for a risk-manager block or an insufficient-USDC-balance skip.
3. If the candidate reaches `_execute_live`, the real per-attempt outcome (`filled` / `timeout` / `cancelled` / `place_failed` / …) resolves the verdict to `traded_live` (only on `filled`) or `timeout_today` (everything else, with the raw outcome kept in `gate_detail`).
4. Any `traded_live` placeholder that a poll never got around to confirming one way or the other (e.g. the wallet-empty cooldown skipping the candidate loop entirely) is downgraded to `entry_guard` before the DB write, so the table never claims a live trade that did not happen. Paper-mode polls (no live trader configured) have no entry-guard/execution seam at all, so their placeholder is left as `traded_live` — a best-effort "would trade live" reading, not a confirmed exchange fill.

---

### copy_wallets_followed

**Purpose:** The subset of screened wallets (`copy_wallet_candidates`) actually being copied. Epic B (issue #1121, epic #1101) — signal detection and flat-stake paper execution reads this table to know which wallets to poll and at what stake.

**Writer:** `Database.insert_followed_wallet` / `Database.update_followed_wallet_status` / `Database.update_followed_wallet_last_seen`
**Reader:** Story B3's polling loop (not yet built), dashboard copy-trading tab (not yet built)

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `address` | TEXT PRIMARY KEY | wallet address | No | Followed wallet's proxy address — acts as the row key |
| `stake_per_trade` | REAL NOT NULL CHECK(stake_per_trade > 0) | USD | No | Flat stake used to size every copied trade for this wallet |
| `status` | TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')) | categorical | No | Whether polling/copying is currently active for this wallet |
| `paused_reason` | TEXT | free text | Yes | Why the wallet was paused; cleared (`NULL`) when returned to `'active'` |
| `added_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | When the wallet was first followed |
| `last_seen_trade_ts` | INTEGER | unix seconds | Yes | High-water-mark of the last trade this wallet's poll has processed; `NULL` until the first poll runs (story B3, not yet built) |

**Notes:**
- One row per wallet, **not** append-only (unlike `copy_wallet_candidates`) — `status`/`paused_reason`/`last_seen_trade_ts` are mutated in place via `UPDATE`.
- A wallet can't be followed twice: `insert_followed_wallet` is a plain `INSERT` and raises `sqlite3.IntegrityError` on a duplicate `address`; callers un-pause an existing row instead of re-inserting.
- Fully separate from `open_positions`/`trades` per the architecture doc's isolation decision (issue #1100) — no `strategy` discriminator column on the weather strategy's tables.

---

### copy_signals

**Purpose:** One row per detected BUY from a followed wallet, whether it was executed or skipped. The `copy_positions` row a signal produces (if any) is linked back via `position_id`.

**Writer:** Story B3's polling loop (not yet built)
**Reader:** Dashboard copy-trading tab (not yet built), story B3's dedup check on `source_trade_id`

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | | No | Auto-increment row ID |
| `address` | TEXT NOT NULL | wallet address | No | Followed wallet this signal came from |
| `market` | TEXT NOT NULL | Polymarket condition ID | No | Market the source trade was on |
| `outcome_index` | INTEGER CHECK(outcome_index IS NULL OR outcome_index IN (0,1)) | 0 or 1 | Yes | Positional outcome slot the source trade was on (see `src/data/polymarket_traders.py::normalize_trade`) |
| `source_price` | REAL NOT NULL CHECK(source_price >= 0 AND source_price <= 1) | 0–1 probability | No | The followed trader's own fill price |
| `source_trade_id` | TEXT | Data API `transactionHash` | Yes | Verified present on every live `/trades` record (2026-09-19); `normalize_trade()` does not yet surface it, so this stays `NULL` until story B3 adds that. **No `UNIQUE` constraint** — a single on-chain transaction can span multiple maker fills at different price levels, so the same `transactionHash` can legitimately appear on more than one row; story B3 must dedupe on `source_trade_id` first, falling back to the full `(address, market, source_price, detected_at)` tuple when it collides |
| `detected_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | When the bot detected this signal |
| `order_placed` | INTEGER NOT NULL DEFAULT 0 | boolean (0/1) | No | Whether a copy order was actually placed for this signal |
| `fill_price` | REAL CHECK(fill_price IS NULL OR (fill_price >= 0 AND fill_price <= 1)) | 0–1 probability | Yes | Only populated when `order_placed=1` |
| `size_usd` | REAL CHECK(size_usd IS NULL OR size_usd > 0) | USD | Yes | Only populated when `order_placed=1` |
| `position_id` | INTEGER REFERENCES copy_positions(id) | foreign key | Yes | Only populated when `order_placed=1` — links to the resulting `copy_positions` row |
| `skip_reason` | TEXT | free text | Yes | Only populated when `order_placed=0` (rate-limited, market already closed, risk limit hit, etc.) |

**Notes:**
- Append-only: one row per detected signal, executed or skipped, never overwritten.

---

### copy_positions

**Purpose:** Open, unsettled paper positions opened from `copy_signals`. Mirrors `open_positions`' shape conceptually, but rows are **not** deleted on settlement — epic C (settlement & P&L tracking, not yet built) needs to read settled rows for P&L history, so `status` flips `'open'` → `'settled'` in place instead.

**Writer:** Story B3's polling loop (open) / `Database.settle_copy_position` (settled — story C1, issue #1131; called by story C2's settlement script, not yet built)
**Reader:** `Database.get_open_copy_positions` (story B3's per-wallet and total exposure checks — summed over this query's result rather than an in-memory counter, so exposure state survives process restarts) / `Database.get_copy_realized_pnl_by_wallet` and `Database.get_copy_realized_pnl_total` (story C1's realized-P&L reads over settled rows, also consumed by story C3's `src/data/copy_backtest_comparison.py` — issue #1133 — via those same two methods), dashboard copy-trading tab (not yet built)

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | | No | Auto-increment row ID |
| `signal_id` | INTEGER NOT NULL REFERENCES copy_signals(id) | foreign key | No | The signal that opened this position |
| `address` | TEXT NOT NULL | wallet address | No | Followed wallet being copied |
| `market` | TEXT NOT NULL | Polymarket condition ID | No | Market this position is on |
| `outcome_index` | INTEGER NOT NULL CHECK(outcome_index IN (0,1)) | 0 or 1 | No | Outcome side this position is on |
| `entry_price` | REAL NOT NULL CHECK(entry_price >= 0 AND entry_price <= 1) | 0–1 probability | No | This position's own fill price (may differ from the signal's `source_price`/`fill_price` due to slippage) |
| `stake_usd` | REAL NOT NULL CHECK(stake_usd > 0) | USD | No | Amount staked on this position |
| `entry_ts` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Entry/fill timestamp |
| `status` | TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settled')) | categorical | No | Whether this position is still open or has settled |
| `settled_pnl_usd` | REAL | USD | Yes | Only populated once `status='settled'` |
| `settled_at` | TEXT | ISO 8601 timestamp (UTC) | Yes | Only populated once `status='settled'` |

**Notes:**
- Unlike `open_positions`, rows are retained (not deleted) after settlement — `status` flips in place so epic C's P&L history can read them later.
- Fully separate from `open_positions`/`trades` per the architecture doc's isolation decision (issue #1100).

---

### bot_config

**Purpose:** Persistent key-value store for operator-adjustable bot parameters. Values survive restarts and are authoritative over environment variables once seeded.

**Writer:** `seed_config()` (startup, first run only) and `PATCH /api/config` (dashboard)
**Reader:** `get_live_config()` (called each poll cycle), `GET /api/config` (dashboard)

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `key` | TEXT PRIMARY KEY | | No | Parameter name (e.g., `MIN_EDGE_CENTS`, `POLL_INTERVAL_SECONDS`) |
| `value` | TEXT NOT NULL | serialised string | No | Parameter value serialised as a string (cast to the correct type on read) |
| `updated_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Timestamp of the last write |

**DDL:**
```sql
CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

**Notes:**
- Seeded once at process start by `seed_config()` in `src/config.py`. Seeding only writes rows that do not yet exist — existing rows are never overwritten on restart.
- Env vars are only consulted during the very first seed. After that, DB values are authoritative.
- `STARTING_CAPITAL_EUR`, credentials, and API keys are NOT stored here.
- `EMOS_DEFAULT_MODE` is an enum — only `legacy`, `emos_shadow`, `emos_primary` are valid values.
- All 20 editable parameters are listed in `CONFIG_DEFAULTS` in `src/config.py`.
- The bot reads live values via `get_live_config(db)` on each poll cycle, so parameter changes take effect within one poll interval — no restart needed.

---

### station_overrides

**Purpose:** Per-station, per-side shadow/live flag overrides. Controls whether YES or NO candidates for a given station are executed live or logged as shadow trades.

**Writer:** `POST /api/stations/{metar}/toggle` (both sides), `POST /api/stations/{metar}/toggle/yes`, `POST /api/stations/{metar}/toggle/no` (dashboard API)
**Reader:** Market scanner (`src/strategy/scanner.py`) and stations overview (`GET /api/stations/overview`)

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `station` | TEXT PRIMARY KEY | METAR code | No | Station code (e.g., `KORD`, `RKSI`) |
| `enabled` | INTEGER NOT NULL DEFAULT 1 | boolean (0/1) | No | **Deprecated** — kept for back-compat. Legacy rows with `enabled=0` are migrated to `yes_enabled=0, no_enabled=0`. |
| `yes_enabled` | INTEGER NOT NULL DEFAULT 1 | boolean (0/1) | No | 1 = YES side is live (orders placed); 0 = YES side is shadow (logged at $1 notional, no order) |
| `no_enabled` | INTEGER NOT NULL DEFAULT 1 | boolean (0/1) | No | 1 = NO side is live (orders placed); 0 = NO side is shadow (logged at $1 notional, no order) |
| `updated_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Last update timestamp |

**DDL:**
```sql
CREATE TABLE IF NOT EXISTS station_overrides (
    station     TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    yes_enabled INTEGER NOT NULL DEFAULT 1,
    no_enabled  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);
```

**Four states:**

| `yes_enabled` | `no_enabled` | Behaviour |
|---|---|---|
| 1 | 1 | Fully live — both sides trade |
| 0 | 1 | YES shadow, NO live |
| 1 | 0 | YES live, NO shadow |
| 0 | 0 | Fully shadow — both sides logged only |

**Notes:**
- `enabled` is deprecated but retained for backwards compatibility. New code should read/write `yes_enabled` and `no_enabled` directly.
- Migration: existing rows with `enabled=0` are automatically back-populated to `yes_enabled=0, no_enabled=0` on first startup after upgrade.
- `ENABLE_YES_TRADES=False` (env var) still forces YES shadow on ALL stations regardless of `yes_enabled`.
- If no DB override exists for a station, the scanner falls back to the env vars `SHADOW_STATIONS`, `SHADOW_STATIONS_YES`, `SHADOW_STATIONS_NO`.

---

## Analytics Database (data/analytics.db)

**Purpose:** Durable, queryable archive of intra-day snapshot telemetry (EPIC #345). Isolated from the live trading database (data/meteoedge.db). Managed by `src/data/archive_db.py` and updated daily via `src/scripts/archive_snapshots.py`.

### snapshot_archive

Stores scanner snapshots at each poll cycle. One row per (ts, ticker) pair.

| Column | Type | Nullable | Description |
|--------|------|----------|---------|
| `ts` | TEXT | No | ISO 8601 timestamp (poll cycle time, UTC) |
| `station` | TEXT | No | METAR station code |
| `ticker` | TEXT | No | Polymarket market ticker |
| `bracket_low` | REAL | Yes | Bracket lower boundary (°F or °C) |
| `bracket_high` | REAL | Yes | Bracket upper boundary |
| `yes_ask` | INTEGER | Yes | Market ask price for YES side (cents) |
| `no_ask` | INTEGER | Yes | Market ask price for NO side (cents) |
| `current_high` | REAL | Yes | Running daily high temperature |
| `latest_temp` | REAL | Yes | Most recent observation temperature |
| `forecast_high` | REAL | Yes | Ensemble forecast high (blended) |
| `p_yes` | REAL | Yes | Model probability of bracket hit [0,1] |
| `raw_p_yes` | REAL | Yes | P(YES) before clipping |
| `capped_p_yes` | REAL | Yes | P(YES) after clipping |
| `ev_yes` | REAL | Yes | Expected value for YES entry (fair - market) |
| `ev_no` | REAL | Yes | Expected value for NO entry |
| `minutes_to_settlement` | REAL | Yes | Time until market closes |
| `emos_mode` | TEXT | Yes | EMOS calibration mode in effect |

**Unique Constraint:**
```sql
UNIQUE(ts, ticker)
```

**Indexes:**
```sql
CREATE INDEX idx_sa_station_ts ON snapshot_archive(station, ts);
CREATE INDEX idx_sa_ticker_ts ON snapshot_archive(ticker, ts);
```

**Writer:** ETL script (src/scripts/archive_snapshots.py)  
**Reader:** Dashboard, historical analysis, backtesting

**Notes:**
- Snapshots are logged by run.py every poll cycle to `logs/snapshots.jsonl` (JSONL files retained 30 days)
- ETL ingests to this table daily (scheduled at 12:30 UTC via systemd timer)
- Idempotent via INSERT OR IGNORE + high-water-mark filtering

### position_snapshot_archive

Stores snapshots of open positions at each poll cycle. One row per (ts, no_token_id) pair.

| Column | Type | Nullable | Description |
|--------|------|----------|---------|
| `ts` | TEXT | No | Poll timestamp (UTC) |
| `ticker` | TEXT | Yes | Market ticker |
| `no_token_id` | TEXT | No | Unique position identifier (NO token contract address) |
| `station` | TEXT | Yes | Station code |
| `bracket_low` | REAL | Yes | Bracket lower boundary |
| `bracket_high` | REAL | Yes | Bracket upper boundary |
| `entry_price` | INTEGER | Yes | Entry fill price (cents) |
| `predicted_price` | INTEGER | Yes | Fair value at snapshot time |
| `current_high` | REAL | Yes | Running daily high |
| `latest_temp` | REAL | Yes | Latest observation |
| `forecast_nws` | REAL | Yes | NWS forecast high |
| `forecast_secondary` | REAL | Yes | Secondary forecast (Open Meteo, etc.) |
| `no_best_bid` | INTEGER | Yes | Current best bid for NO side |
| `no_best_bid_size` | REAL | Yes | Bid-side depth (shares) |
| `no_best_ask` | INTEGER | Yes | Current best ask for NO side |
| `p_yes_now` | REAL | Yes | Model probability at snapshot time |
| `fair_value_now` | INTEGER | Yes | Fair value at snapshot time (cents) |
| `weather_missing` | INTEGER | Yes | Boolean: weather data missing? |

**Unique Constraint:**
```sql
UNIQUE(ts, no_token_id)
```

**Indexes:**
```sql
CREATE INDEX idx_psa_station_ts ON position_snapshot_archive(station, ts);
CREATE INDEX idx_psa_ticker_ts ON position_snapshot_archive(ticker, ts);
```

**Writer:** ETL script (src/scripts/archive_snapshots.py)  
**Reader:** Dashboard, position analysis, backtesting

**Notes:**
- Position snapshots are logged to `logs/position_snapshots.jsonl` on each poll cycle (retained 30 days)
- ETL ingests to this table daily
- Idempotent via INSERT OR IGNORE + high-water-mark filtering

---

## Data Flow Diagram

```
┌─────────────────────────┐
│ Data Collectors         │
│ - METAR                 │
│ - NWS, Open Meteo       │
│ - JMA AMEDAS, AMOS, MSS │
│ - TAF                   │
└────────┬────────────────┘
         │ write observations
         ▼
    ┌─────────────────┐
    │   observations  │
    └────────┬────────┘
             │ read to compute weather state
             ▼
    ┌──────────────────────┐
    │  Weather State Model  │
    │ - Envelope model     │
    │ - Climb rates        │
    │ - Intraday correc.   │
    └────────┬─────────────┘
             │ forecast + current high
             ▼
    ┌──────────────────────────┐
    │  Market Scanner          │
    │ - Fetch Polymarket data  │
    │ - Compute edge           │
    │ - Filter & flag          │
    └────────┬─────────────────┘
             │
             ├─→ write candidates
             │
             ├─→ Risk Manager (position count, capital)
             │
             └─→ Execution (Live Trader or Paper)
                     │
                     ├─→ write trades
                     ├─→ write open_positions
                     └─→ write risk_state
                             │
                             ▼ (at exit or settlement)
                        write settlements
```

---

## Query Examples

### Find all trades for a station on a date

```sql
SELECT ts, ticker, side, actual_price, pnl
FROM trades
WHERE station = 'KORD' AND DATE(ts) = '2024-06-03'
ORDER BY ts DESC;
```

### Compute daily P&L from settlements

```sql
SELECT 
    DATE(ts) as settlement_date,
    SUM(
        CASE 
            WHEN resolved_yes = 1 THEN 100 - entry_price
            ELSE -(entry_price)
        END
    ) as daily_pnl
FROM settlements
WHERE DATE(ts) = '2024-06-03'
GROUP BY DATE(ts);
```

### Check open positions

```sql
SELECT op.ticker, op.side, op.entry_price, op.shares, 
       t.bracket_low, t.bracket_high
FROM open_positions op
LEFT JOIN trades t ON op.trade_id = t.id
ORDER BY op.entry_ts DESC;
```

### Find candidates that weren't traded

```sql
SELECT ts, station, ticker, bracket_low, bracket_high, 
       predicted_price, market_price, predicted_edge
FROM candidates
WHERE ticker NOT IN (SELECT DISTINCT ticker FROM trades)
  AND DATE(ts) = '2024-06-03'
ORDER BY predicted_edge DESC;
```

### Analyze win rate

```sql
SELECT 
    COUNT(*) as total_trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_pct,
    SUM(pnl) as total_pnl,
    AVG(pnl) as avg_pnl
FROM trades
WHERE mode = 'live' AND outcome IN ('filled', 'sold')
  AND settled_at IS NOT NULL;
```

---

## Performance Considerations

- **observations**: Indexed by (station, ts) — queries filtered by date are fast.
- **trades**: Indexed by (station, ts) and (mode) — supports date-based and mode-based analysis.
- **settlements**: Unique on ticker — upsert operations are O(1).
- **open_positions**: Unique on order_id — fast dedup checks.
- **model_weights**: Small table (one row per model per date); queries are fast.

For large databases (>1GB), run periodic maintenance:

```sql
VACUUM;
PRAGMA optimize;
```

---

## Backup & Recovery

The SQLite database uses WAL (Write-Ahead Logging) for durability. On restart after crash:
- The WAL checkpoint automatically restores the database to the last committed state.
- No manual recovery needed.

To backup:

```bash
sqlite3 data/meteoedge.db ".backup data/meteoedge.db.backup"
# Or use file copy (safe during WAL mode)
cp data/meteoedge.db data/meteoedge.db.backup
cp data/meteoedge.db-wal data/meteoedge.db.backup-wal
```
