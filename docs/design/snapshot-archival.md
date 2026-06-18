# Snapshot Archival Pipeline (EPIC #345)

## Problem

The intra-day scanner logs snapshot telemetry (market prices, model probabilities, weather state) to rotated JSONL files (`logs/snapshots.jsonl` and `logs/position_snapshots.jsonl`). These files are essential for:

- **Market × weather × model analysis**: understanding why the bot traded or didn't trade at specific times
- **Debugging and audit**: replaying poll cycles to validate decision logic
- **Historical backtesting**: correlating past weather observations with market outcomes

However, these logs are **deleted after 30 days** (controlled by `LOG_ROTATION_RETAIN_DAYS`), making multi-month historical analysis impossible. The solution is to **automatically archive selected fields to a durable SQLite analytics database** updated daily via systemd timer.

## Architecture

### Data Flow

```
┌─────────────────────────────────────────┐
│ src/scripts/run.py (hot path)           │
│ Logs snapshots on each poll cycle       │
│ (every 5 minutes by default)            │
└────────────────┬────────────────────────┘
                 │
                 ▼ append to rotated JSONL
       ┌─────────────────────────┐
       │ logs/snapshots.jsonl    │
       │ rotated daily, compressed after 1 day,
       │ deleted after 30 days
       │ (LOG_ROTATION_RETAIN_DAYS)
       │
       │ One row per poll cycle   │
       └────────────────┬────────┘
                 │
                 │ Scheduled daily ETL
                 │ via systemd timer
                 ▼
       ┌────────────────────────────────────┐
       │ deploy/systemd/meteoedge-archive.timer
       │ Fires daily at 12:30 UTC             │
       └────────────────┬─────────────────────┘
                 │
                 ▼ triggers service
       ┌────────────────────────────────────┐
       │ deploy/systemd/meteoedge-archive.service
       │ Runs:                                │
       │  python -m src.scripts.archive_snapshots
       └────────────────┬─────────────────────┘
                 │
                 ▼
       ┌────────────────────────────────────────────┐
       │ src/scripts/archive_snapshots.py (ETL)       │
       │ 1. Read all rotated JSONL files              │
       │ 2. Apply high-water-mark (HWM) filter        │
       │ 3. Batch-insert via ArchiveDatabase          │
       │    (INSERT OR IGNORE — idempotent)          │
       └────────────────┬─────────────────────────────┘
                 │
                 ▼
       ┌────────────────────────────────────┐
       │ data/analytics.db (durable store)    │
       │ SQLite with WAL mode                 │
       │ Isolated from live meteoedge.db      │
       │ Retained indefinitely                │
       │ Tables:                              │
       │ - snapshot_archive                   │
       │ - position_snapshot_archive          │
       └────────────────────────────────────┘
                 │
                 ▼
       ┌────────────────────────────────────┐
       │ Dashboard query helpers               │
       │ ArchiveDatabase.get_snapshot_series() │
       │ GET /analytics/intraday?station=KORD │
       └────────────────────────────────────┘
```

### Components

#### 1. Hot Path: `src/scripts/run.py`

Logs snapshots on each poll cycle via `src/utils/telemetry.py`. The scanner appends JSON records to:

- **`logs/snapshots.jsonl`**: scanner state at poll time
  - Fields: ts, station, ticker, bracket_low/high, prices, model probabilities, market prices, etc.
  - One row per (ts, ticker) pair per poll cycle
  - Rotated daily, retained 30 days

- **`logs/position_snapshots.jsonl`**: open position state at poll time
  - Fields: ts, ticker, no_token_id (unique identifier), prices, confidence, bid/ask, etc.
  - One row per (ts, no_token_id) pair per poll cycle
  - Rotated daily, retained 30 days

#### 2. Log Rotation: `src/utils/log_rotation.py`

Provides:
- **`rotated_path()`**: writes dated files (e.g., `snapshots.2026-06-16.jsonl`)
- **`housekeep()`**: compresses files after 1 day, deletes after N days
- **`iter_rotated_jsonl()`**: reads all retained JSONL files (including compressed .gz)
- **`SNAPSHOT_RETAIN_DAYS`**: override retention (default 365 days for snapshot files)

#### 3. Archive Database: `src/data/archive_db.py`

Provides `ArchiveDatabase` class for durable telemetry storage:

```python
from src.data.archive_db import ArchiveDatabase

db = ArchiveDatabase(path="data/analytics.db")
inserted = db.insert_snapshots(rows)  # returns count of rows actually inserted
inserted = db.insert_position_snapshots(rows)  # idempotent via INSERT OR IGNORE
hwm = db.get_max_archived_ts("snapshot_archive")  # high-water mark for incremental runs
```

- Opens/creates `data/analytics.db` on first construction (idempotent)
- Initializes schema (two tables + indexes) via DDL
- Uses WAL mode for durability
- Thread-safe via RLock

#### 4. ETL Script: `src/scripts/archive_snapshots.py`

Reads rotated JSONL files, filters by HWM, and batch-inserts to analytics.db:

```bash
# Incremental run (default behavior via systemd timer)
python -m src.scripts.archive_snapshots

# Dry-run preview
python -m src.scripts.archive_snapshots --dry-run

# Historical re-ingestion (ignores HWM, allows replaying a date range)
python -m src.scripts.archive_snapshots --from-date 2026-06-01 --to-date 2026-06-15
```

**Idempotency:** Uses HWM (`get_max_archived_ts()`) + `INSERT OR IGNORE` belt-and-suspenders to guarantee safe re-runs without duplication.

#### 5. Systemd Timer: `deploy/systemd/meteoedge-archive.timer`

Triggers ETL daily at **12:30 UTC** (well after US market close at 20:00 UTC the prior day, allowing 1 day's worth of log rotation):

```ini
[Timer]
OnCalendar=*-*-* 12:30:00 UTC
```

## Database Schema

### `snapshot_archive`

Stores intra-day scanner snapshots. One row per poll cycle per market.

| Column | Type | Nullable | Description |
|--------|------|----------|---------|
| `ts` | TEXT | No | ISO 8601 timestamp (poll cycle time, UTC) |
| `station` | TEXT | No | METAR station code (KORD, KMIA, etc.) |
| `ticker` | TEXT | No | Polymarket market ticker |
| `bracket_low` | REAL | Yes | Bracket lower boundary (°F or °C) |
| `bracket_high` | REAL | Yes | Bracket upper boundary |
| `yes_ask` | INTEGER | Yes | Market ask price for YES side (cents) |
| `no_ask` | INTEGER | Yes | Market ask price for NO side (cents) |
| `current_high` | REAL | Yes | Running daily high temperature as of poll |
| `latest_temp` | REAL | Yes | Most recent observation temperature |
| `forecast_high` | REAL | Yes | Ensemble forecast high (blended) |
| `p_yes` | REAL | Yes | Model probability of bracket hit [0,1] |
| `raw_p_yes` | REAL | Yes | P(YES) before any clipping |
| `capped_p_yes` | REAL | Yes | P(YES) after confidence clipping |
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

### `position_snapshot_archive`

Stores snapshots of open positions at poll time. One row per (ts, no_token_id) pair.

| Column | Type | Nullable | Description |
|--------|------|----------|---------|
| `ts` | TEXT | No | Poll timestamp |
| `ticker` | TEXT | Yes | Market ticker |
| `no_token_id` | TEXT | No | Unique position identifier (NO token contract address) |
| `station` | TEXT | Yes | Station code |
| `bracket_low` | REAL | Yes | Bracket boundary |
| `bracket_high` | REAL | Yes | Bracket boundary |
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
| `weather_missing` | INTEGER | Yes | Boolean: was weather data missing? |

**Unique Constraint:**
```sql
UNIQUE(ts, no_token_id)
```

**Indexes:**
```sql
CREATE INDEX idx_psa_station_ts ON position_snapshot_archive(station, ts);
CREATE INDEX idx_psa_ticker_ts ON position_snapshot_archive(ticker, ts);
```

## Idempotency and Resilience

**High-Water-Mark (HWM) Filter:**
- On each ETL run, fetch `MAX(ts)` from the archive table
- Skip any records where `ts <= hwm`, preventing double-insertion
- When `--from-date` is set, the HWM filter is bypassed to allow historical re-ingestion

**INSERT OR IGNORE:**
- Each batch insert uses `INSERT OR IGNORE` to catch duplicate keys (belt-and-suspenders)
- Duplicate keys increment the count of "skipped existing" but do not error
- Allows safe re-runs even if ETL crashes or systemd timer misfires

**Atomicity:**
- Each batch is a single transaction; either all records insert or none do
- SQLite WAL mode provides durability across restarts

## Retention Policy

| Source | Retention | Purpose |
|--------|-----------|----------|
| `logs/snapshots.jsonl` | 365 days (SNAPSHOT_RETAIN_DAYS) | Hot-path archive; daily deletion | 
| `logs/position_snapshots.jsonl` | 365 days | Hot-path archive; daily deletion |
| `data/analytics.db` | ∞ (indefinite) | Durable, queryable archive for multi-year analysis |

Old JSONL files are gzip-compressed after 1 day (`LOG_ROTATION_COMPRESS_AFTER_DAYS`), saving disk space while remaining readable to the ETL.

## Query Helpers

### Python API

```python
from src.data.archive_db import ArchiveDatabase

db = ArchiveDatabase()

# Fetch snapshots for a station on a specific date
rows = db._conn.execute("""
    SELECT ts, ticker, p_yes, forecast_high, latest_temp
    FROM snapshot_archive
    WHERE station = ? AND DATE(ts) = ?
    ORDER BY ts
""", ("KORD", "2026-06-15")).fetchall()

# Analyze model probability distribution over a date range
rows = db._conn.execute("""
    SELECT 
        DATE(ts) as day,
        COUNT(*) as snapshots,
        AVG(p_yes) as mean_p_yes,
        MIN(p_yes) as min_p_yes,
        MAX(p_yes) as max_p_yes
    FROM snapshot_archive
    WHERE station = ? AND ts >= ? AND ts <= ?
    GROUP BY DATE(ts)
""", ("KORD", "2026-06-01T00:00:00+00:00", "2026-06-15T23:59:59+00:00")).fetchall()
```

### Dashboard REST Endpoint

```
GET /analytics/intraday?station=KORD&date=2026-06-15

Returns:
{
  "station": "KORD",
  "date": "2026-06-15",
  "snapshots": [
    {
      "ts": "2026-06-15T14:35:00+00:00",
      "ticker": "HIGH-TEMP-KORD-2026-06-15-90-94",
      "p_yes": 0.42,
      "forecast_high": 91.5,
      "latest_temp": 82.3,
      "no_ask": 45,
      "current_high": 85.2
    },
    ...
  ]
}
```

## Example Queries

### Get a snapshot series for market analysis

```sql
SELECT ts, p_yes, latest_temp, forecast_high, no_ask
FROM snapshot_archive
WHERE station = 'KORD' AND ticker = 'HIGH-TEMP-KORD-2026-06-15-90-94'
ORDER BY ts;
```

Output (sample):
```
ts                            | p_yes | latest_temp | forecast_high | no_ask
------------------------------|-------|-------------|---------------|-------
2026-06-15T14:00:00+00:00     | 0.30  | 78.5        | 89.0          | 55
2026-06-15T14:05:00+00:00     | 0.35  | 79.2        | 89.0          | 52
2026-06-15T14:10:00+00:00     | 0.42  | 80.1        | 90.0          | 45
2026-06-15T14:15:00+00:00     | 0.48  | 81.3        | 90.5          | 40
...
```

### Hourly aggregation of model confidence

```sql
SELECT 
    substr(ts, 1, 13) || ':00:00+00:00' as hour,
    COUNT(*) as poll_count,
    AVG(p_yes) as mean_p_yes,
    AVG(latest_temp) as mean_temp
FROM snapshot_archive
WHERE station = 'KORD' AND DATE(ts) = '2026-06-15'
GROUP BY substr(ts, 1, 13)
ORDER BY hour;
```

### Positions at settlement time

```sql
SELECT ts, ticker, entry_price, predicted_price, no_best_bid, no_best_ask, weather_missing
FROM position_snapshot_archive
WHERE station = 'KORD' AND no_token_id = 'abc123def456'
ORDER BY ts DESC
LIMIT 10;
```

## Manual Operations

### Trigger archival manually

```bash
# Incremental (respects HWM, skips already-archived records)
python -m src.scripts.archive_snapshots

# Preview without writing
python -m src.scripts.archive_snapshots --dry-run

# Re-ingest a historical window (useful for recovery or backfill)
python -m src.scripts.archive_snapshots --from-date 2026-06-01 --to-date 2026-06-15

# Re-ingest in dry-run mode
ARCHIVE_DB_PATH=/tmp/test.db python -m src.scripts.archive_snapshots --from-date 2026-06-01 --dry-run
```

### Inspect the analytics database

```bash
sqlite3 data/analytics.db
> SELECT COUNT(*) FROM snapshot_archive;
> SELECT MAX(ts) FROM snapshot_archive;
> SELECT DISTINCT DATE(ts) FROM snapshot_archive ORDER BY DATE(ts) DESC LIMIT 10;
```

## Failure Modes and Recovery

| Scenario | Impact | Recovery |
|----------|--------|---------|
| ETL crashes mid-run | Partial batch inserted; rest lost | Re-run `archive_snapshots` — HWM + IGNORE handles dupes |
| JSONL file malformed | Individual malformed record skipped | Count increments; processing continues |
| Systemd timer misfires | ETL doesn't run on schedule | Manual `archive_snapshots` + next timer fires (24h later) |
| analytics.db gets corrupted | Unable to query analytics | Restore from backup; re-run ETL from `--from-date` |

---

## Summary

The snapshot archival pipeline is a **scheduled daily ETL** that:

1. Reads rotated JSONL snapshot files (retained 30 days)
2. Filters via HWM to avoid re-archiving
3. Batch-inserts into a durable SQLite analytics database
4. Runs automatically via systemd timer at 12:30 UTC daily
5. Supports manual re-ingestion for historical backfill or recovery

This enables **multi-year historical analysis** of market × weather × model state, critical for debugging, backtesting, and operational understanding.
