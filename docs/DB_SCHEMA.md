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

**Indexes:**
```sql
CREATE INDEX idx_cand_station_ts ON candidates(station, ts);
CREATE INDEX idx_cand_ticker ON candidates(ticker);
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
| `mode` | TEXT NOT NULL CHECK(mode IN ('paper','live')) | categorical | No | 'paper' for simulated, 'live' for real executed |
| `order_id` | TEXT | Polymarket order ID | Yes | Exchange order ID (only set for live trades) |
| `outcome` | TEXT | categorical | Yes | Trade exit reason: 'filled' (held to expiry), 'sold' (early exit), 'cancelled', 'timeout' |
| `pnl` | REAL | € | Yes | Realized P&L in euros (set after settlement or early exit) |
| `capital_before` | REAL NOT NULL | € | No | Capital/position size at entry |
| `capital_after` | REAL | € | Yes | Capital after exit (for paper trades, = capital_before + pnl) |
| `settled_at` | TEXT | ISO 8601 timestamp (UTC) | Yes | Timestamp when settlement P&L was written |

**Indexes:**
```sql
CREATE INDEX idx_trades_station_ts ON trades(station, ts);
CREATE INDEX idx_trades_mode ON trades(mode);
```

**Notes:**
- `actual_price` is the fill price; `slippage = actual_price - predicted_price`.
- `pnl` is filled in by the settle script (if outcome='filled') or by the exit handler (if outcome='sold').
- For NO trades that are early-exited: `pnl = (sell_price - entry_price) / 100 * shares`.
- For filled trades: `pnl = (100 - entry_price) / 100 * shares` if YES bracket hit (or NO bracket miss), else `pnl = -(entry_price / 100) * shares`.

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

**Indexes:**
```sql
CREATE INDEX idx_settlements_station_ts ON settlements(station, ts);
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

**Purpose:** Log of model forecasts per station/model/date. Used for historical validation and weight computation.

**Writer:** Forecast logger (src/model/deb_weighting.py)  
**Reader:** Weight computation, forecast audit trail

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `id` | INTEGER PRIMARY KEY | | No | Auto-increment row ID |
| `station` | TEXT NOT NULL | METAR code | No | Station |
| `model` | TEXT NOT NULL | categorical | No | Model: "nws" or "open_meteo" |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | Forecast date (what day does the forecast predict?) |
| `forecast_high_f` | REAL NOT NULL | °F | No | Forecasted daily high in Fahrenheit |
| `logged_at` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | When the forecast was logged |

**Unique Index:**
```sql
CREATE UNIQUE INDEX idx_mfl_station_model_date ON model_forecast_log(station, model, date);
```

**Notes:**
- One row per (station, model, date) combination.
- Multiple rows for the same date allowed if forecast is updated intra-day (uses upsert).

---

### intraday_corrections

**Purpose:** Store temperature corrections applied to the ensemble forecast during the day as new METAR observations arrive.

**Writer:** Intraday correction model (src/model/intraday_correction.py)  
**Reader:** Weather state builder, envelope model

| Column | Type | Units | Nullable | Description |
|--------|------|-------|----------|-------------|
| `city` | TEXT NOT NULL | city name | No | City (e.g., "Chicago") |
| `date` | TEXT NOT NULL | YYYY-MM-DD | No | Forecast date (what day?) |
| `obs_time` | TEXT NOT NULL | ISO 8601 timestamp (UTC) | No | Observation time (when was the observation made?) |
| `obs_temp_f` | REAL NOT NULL | °F | No | Observed temperature (from METAR or other source) |
| `model_temp_f` | REAL NOT NULL | °F | No | Model's predicted temperature at obs_time |
| `delta_f` | REAL NOT NULL | °F | No | Observed minus model (bias) |
| `corrected_mu_f` | REAL NOT NULL | °F | No | Corrected forecast high after applying intraday learning |
| `decay_factor` | REAL NOT NULL | fraction [0,1] | No | Weight given to the bias (decays as temperature approaches daily high) |

**Primary Key:**
```sql
PRIMARY KEY (city, date, obs_time)
```

**Indexes:**
```sql
CREATE INDEX idx_ic_city_date ON intraday_corrections(city, date);
```

**Notes:**
- Updated as new observations arrive during the day.
- `corrected_mu_f` converges to the actual daily high as the day progresses.
- Used to improve bracket probability estimates in real-time.

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
