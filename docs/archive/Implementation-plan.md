# MeteoEdge — Implementation Plan

**Project:** Kalshi Intraday Weather Mean-Reversion Trading Bot  
**Author:** Tech Lead PM (André / MeteoEdge Team)  
**Date:** 2026-04-21  
**Status:** Ready for GitHub issue creation  
**Source specs:** `kalshi-weather-bot-spec.md`, `meteoedge-backtest-harness.md`, `meteoedge-mvp-spike.md`

---

## Complexity Legend

| Label | Model level | When to use |
|---|---|---|
| **Easy** | Haiku | Boilerplate, well-defined templates, scripting, config, docs |
| **Mid** | Sonnet | Moderate complexity — integrations, data pipelines, standard modules |

> **No Opus-level stories.** All formerly Hard stories have been decomposed into Mid or Easy sub-stories. The Tech Lead PM provides the exact interface, formula, pseudocode, and test cases in each story so the implementer executes rather than designs.

## Designer Involvement

The only frontend surface is the **local operator dashboard** (FastAPI + HTMX, `localhost:8080`). All dashboard stories require Designer review before implementation and Designer PR approval before merge. No other epics have a visual component.

---

## Critical Path & Epic Dependencies

```
Epic 0 (Spike)  ─────────────────────────────────────────────────────► GATE: proceed?
                                                                              │
Epic 1 (Infra)  ──────────────────────────────────────────────────────────────┤
Epic 9 (Security) ─► (parallel with Epic 1)                                   │
                                                                              │
Epic 2 (Ingestion) ─► depends on Epic 1 ──────────────────────────────────────┤
                                                                              │
Epic 3 (Strategy Engine) ─► depends on Epic 2                                 │
Epic 7 (Backtest) ─► depends on Epic 3 (injectable modules)                   │
                                                                              │
Epic 4 (Risk Management) ─► depends on Epic 3                                 │
Epic 5 (LLM Sanity Check) ─► depends on Epic 3 + Epic 4                       │
                                                                              │
Epic 6 (Order Execution) ─► depends on Epic 4 + Epic 5                        │
Epic 8 (Operational Tooling) ─► depends on Epic 6 (full P&L) ─────────────────┤
                                                                              │
Epic 10 (Rollout) ─► depends on Epic 7 Stage 1 gate pass ◄────────────────────┘
```

**Parallel streams possible from day 1:**
- Stream A: Epic 0 (Spike) → premise validation
- Stream B: Epic 1 + Epic 9 (Infra + Security) → platform foundation

**Critical path (longest chain blocking revenue):**
Epic 0 → Epic 1 → Epic 2 → Epic 3 → Epic 7 → Stage 1 gate → Epic 4 → Epic 5 → Epic 6 → Epic 8 → Epic 10

---

## Epic 0 — MVP Spike (Premise Validation)

**Goal:** Validate that the physical-envelope edge exists in live Kalshi data before committing to the full build. Observe-only, no orders, no database, no services.  
**Gate:** ≥55% hit rate on ≥30 unique flagged candidates over 5-10 trading days.  
**Blocks:** All epics (do not invest in the full build if the gate fails).  
**Designer needed:** No  
**Spike code:** Already provided in `meteoedge-mvp-spike.md` — implementation is largely complete.

---

### E0-S1: Bootstrap spike environment

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** None  
**Blocks:** E0-S2, E0-S3, E0-S4

**Description:**  
Set up the `meteoedge-spike/` directory structure, Python venv, and install dependencies. Create the directory layout and `config.py` with real credentials.

**Acceptance criteria:**
- [ ] `meteoedge-spike/` directory exists with layout from spec §4
- [ ] `.venv` created with all dependencies installed (`httpx`, `pandas`, `python-dateutil`, `pytz`, `astral`, `cryptography`)
- [ ] `config.py` populated with real `KALSHI_API_KEY_ID` and correct `KALSHI_PRIVATE_KEY_PATH`
- [ ] `keys/kalshi_private.pem` present with `chmod 600`
- [ ] `logs/` directory created
- [ ] Running `python spike.py` exits cleanly (no import errors) even if API calls fail

---

### E0-S2: Implement spike.py polling loop

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E0-S1  
**Blocks:** E0-S5

**Description:**  
Implement the main observe-only polling loop: every 5 minutes, fetch METAR + NWS forecast + Kalshi markets for all 5 stations, compute envelope and edge, flag and log candidates. All code is provided in `meteoedge-mvp-spike.md §5` — task is to harden error handling and validate against real data.

**Acceptance criteria:**
- [ ] All functions from `spike.py` (spec §5.4), `kalshi_client.py` (§5.2), `envelope.py` (§5.3) implemented
- [ ] `candidates.csv` and `snapshots.jsonl` correctly appended on each poll
- [ ] METAR, NWS, and Kalshi errors caught and logged; loop always continues
- [ ] `parse_bracket_from_market` regex heuristic tested against at least 5 real Kalshi market subtitles
- [ ] One manual end-to-end test run (real live data) documented in PR description

---

### E0-S3: Implement settle.py daily settlement checker

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E0-S2  
**Blocks:** E0-S4, E0-S5

**Description:**  
Implement `settle.py` per spec §5.5. Reads yesterday's candidates, fetches METAR 24h max as settlement proxy, records win/loss and P&L per candidate.

**Acceptance criteria:**
- [ ] Correctly identifies yesterday's candidates from `candidates.csv` by date prefix
- [ ] Correct win/loss logic for YES and NO sides
- [ ] P&L computed in cents, pre-fee
- [ ] Output appended to `settlements.csv` without duplicates
- [ ] Runs cleanly when `candidates.csv` is empty or missing

---

### E0-S4: Implement report.py hit-rate reporter

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E0-S3  
**Blocks:** E0-S5

**Description:**  
Implement `report.py` per spec §5.6. Deduplicates candidates, computes hit rate and P&L stats, prints per-station breakdown and Green/Yellow/Red decision per spec §7 table.

**Acceptance criteria:**
- [ ] Deduplication: same-day, same-ticker, same-side → take first occurrence
- [ ] Computes: n, wins, hit rate, total P&L, avg P&L, stdev P&L
- [ ] Per-station breakdown printed
- [ ] Green/Yellow/Red light logic applied per spec §7 decision table
- [ ] Handles empty `settlements.csv` gracefully

---

### E0-S5: Run spike for 5-10 trading days and produce retro

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Operator (André)  
**Depends on:** E0-S2, E0-S3, E0-S4  
**Blocks:** All remaining epics (gate decision)

**Description:**  
Operator runs the spike over 5-10 live US trading days. If green light (≥30 candidates, ≥55% hit rate): commit artifacts and proceed. If not: halt all work and revisit the spec.

**Acceptance criteria:**
- [ ] ≥30 unique flagged candidates accumulated
- [ ] `report.py` shows ≥55% hit rate → Green light to proceed
- [ ] Spike artifacts committed to `docs/spike-retro/`: `candidates.csv`, `settlements.csv`, written retro
- [ ] Retro covers: which stations/brackets/times worked, what surprised you
- [ ] **Decision gate:** hit rate < 55% on ≥30 candidates → halt all work, do not open Epic 1+ issues

---

## Epic 1 — Infrastructure & Data Platform

**Goal:** Production-grade host setup, storage, process management, logging, and dashboard skeleton.  
**Prerequisite:** Epic 0 green light (can start in parallel if operator is confident).  
**Blocks:** Epic 2, Epic 8.  
**Designer needed:** Yes (E1-S6 only).

---

### E1-S1: Ubuntu host hardening and user setup

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** None  
**Blocks:** E1-S2, E1-S4

**Description:**  
Create `meteoedge` system user, all required directories, and correct file permissions. Document as an idempotent setup script.

**Acceptance criteria:**
- [ ] `meteoedge` system user created (no login shell, no sudo)
- [ ] Directories created: `/opt/meteoedge/`, `/etc/meteoedge/`, `/var/log/meteoedge/`, `/var/run/meteoedge/`
- [ ] `/etc/meteoedge/` mode `0750` owner `root:meteoedge`; `/var/log/meteoedge/` mode `0755` owner `meteoedge`
- [ ] Python 3.11 venv created at `/opt/meteoedge/.venv`
- [ ] `scripts/setup_host.sh` committed, idempotent, and executable

---

### E1-S2: PostgreSQL 15 + Redis 7 installation and migration tooling

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S1  
**Blocks:** E1-S3

**Description:**  
Install PostgreSQL 15 and Redis 7, configure for localhost-only access, create `meteoedge` DB role, and set up Alembic migration tooling.

**Acceptance criteria:**
- [ ] PostgreSQL 15 running as systemd service; `meteoedge` role with no superuser rights
- [ ] `pg_hba.conf` restricts to localhost only
- [ ] Redis 7 running as systemd service, bound to `127.0.0.1` only
- [ ] Alembic configured; `scripts/migrate.py` and `make migrate` apply migrations successfully
- [ ] Python round-trip test: `psycopg2` + `redis-py` connect and execute a simple query

---

### E1-S3: Database schema — initial migration

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S2  
**Blocks:** E2-S1, E2-S2, E2-S3

**Description:**  
Create the Alembic migration implementing all 8 tables from spec §6.1: `metar_observation`, `daily_high`, `nws_forecast`, `market_snapshot`, `decision`, `trade_order`, `position`, `risk_event`.

**Acceptance criteria:**
- [ ] All 8 tables with exact schema from spec §6.1 (column names, types, nullability)
- [ ] Indexes: `idx_metar_station_time`, `idx_snapshot_ticker_time`
- [ ] Primary keys: `daily_high(station_code, trade_date)`, `position(ticker, side)`
- [ ] `trade_order.external_id` UNIQUE; `trade_order.decision_id` FK to `decision.id`
- [ ] Migration runs cleanly on fresh DB and is fully reversible (downgrade tested)

---

### E1-S4: systemd unit scaffolding

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E1-S1  
**Blocks:** E2-S1, E2-S2, E2-S3, E8-S7

**Description:**  
Create all 6 systemd unit files running as `meteoedge` user, restarting on failure, loading secrets from `/etc/meteoedge/env`.

**Acceptance criteria:**
- [ ] Units created: `meteoedge-metar.service` (10 min timer), `meteoedge-nws.service` (hourly), `meteoedge-kalshi-poller.service` (long-running), `meteoedge-engine.service` (long-running), `meteoedge-dashboard.service` (FastAPI), `meteoedge-daily-report.service` + timer (23:00 UTC)
- [ ] All units: `User=meteoedge`, `Restart=on-failure`, `RestartSec=30s`, `EnvironmentFile=/etc/meteoedge/env`
- [ ] `systemctl start meteoedge-metar.service` starts cleanly (stub binary acceptable at this stage)
- [ ] Unit files committed to `deploy/systemd/`

---

### E1-S5: Structured JSON logging framework

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E1-S1  
**Blocks:** E2-S1, E2-S2, E2-S3, E4-S1e

**Description:**  
Shared logging module writing structured JSON lines to `/var/log/meteoedge/`. Each entry includes timestamp, level, component, message, and structured data dict.

**Acceptance criteria:**
- [ ] `get_logger(component)` factory in `src/meteoedge/logging.py`
- [ ] JSON lines output; fields: `timestamp` (ISO 8601 UTC), `level`, `component`, `message`, `data`
- [ ] Log files routed by component: `poller.jsonl`, `engine.jsonl`, `execution.jsonl`, `risk.jsonl`
- [ ] Logrotate config committed: `deploy/logrotate/meteoedge` (30-day retention, weekly gzip)
- [ ] Unit tests: entry schema validated, file routing verified

---

### E1-S6: FastAPI dashboard skeleton with HTMX

**Complexity:** Mid  
**Designer needed:** **Yes** — Designer must deliver `docs/design/dashboard.md` layout spec before implementation begins  
**Assigned to:** Mid Dev  
**Depends on:** E1-S2, E1-S5  
**Blocks:** E8-S1, E8-S2, E8-S3, E8-S4

**Description:**  
FastAPI app bound to `localhost:8080`, HTMX for partial updates, base layout with navigation and health check. Stub data acceptable — no real data required at this stage.

**Acceptance criteria:**
- [ ] Designer spec at `docs/design/dashboard.md` approved before implementation starts
- [ ] FastAPI app starts via systemd unit, bound strictly to `127.0.0.1:8080`
- [ ] Base layout: navigation sidebar, main content area, status bar
- [ ] `/health` endpoint returns `{"status": "ok"}`
- [ ] HTMX partial update demonstrated on at least one stub widget
- [ ] No secrets in templates or endpoints
- [ ] Designer reviews and approves PR before merge

---

## Epic 2 — Data Ingestion

**Goal:** Three production-quality pollers feeding real-time data into PostgreSQL and Redis. Rate-limit compliance. Historical backfill tooling for backtest seeding.  
**Prerequisites:** Epic 1.  
**Blocks:** Epic 3.  
**Designer needed:** No.

---

### E2-S1: METAR poller

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S3, E1-S4, E1-S5  
**Blocks:** E2-S6, E3-S1a

**Description:**  
`src/meteoedge/pollers/metar.py` — timer-based service polling Aviation Weather Center every 10 minutes for all 5 stations. Upserts observations and running daily high.

**Acceptance criteria:**
- [ ] Fetches `https://aviationweather.gov/api/data/metar?ids=KNYC,KORD,KMIA,KAUS,KLAX&format=json&hours=2`
- [ ] Parses `temp` (°C→°F), `reportTime`/`obsTime` with full timezone handling
- [ ] Appends to `metar_observation`; upserts `daily_high` (running max per station/date)
- [ ] On fetch failure: uses cached response if within 90 min; on staleness >90 min writes `stale:metar:{station}` to Redis
- [ ] Unit tests: METAR parsing, daily-high upsert logic, stale-data detection

---

### E2-S2: NWS forecast poller

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S3, E1-S4, E1-S5  
**Blocks:** E2-S6, E3-S2b

**Description:**  
`src/meteoedge/pollers/nws.py` — hourly service using NWS points API two-step discovery, parsing today's forecast high per station.

**Acceptance criteria:**
- [ ] Two-step discovery: `GET /points/{lat},{lon}` → `forecastHourly` URL
- [ ] `User-Agent` set to `MeteoEdge/0.1 (contact: {ALERT_EMAIL_FROM})`
- [ ] Inserts snapshots to `nws_forecast` with `valid_from`, `valid_to`, `fetched_at`
- [ ] On failure: cache last forecast up to 3 hours
- [ ] DST-aware local-date filtering; unit tests include spring-forward/fall-back cases

---

### E2-S3: Kalshi market poller

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S3, E1-S4, E1-S5, E2-S4c  
**Blocks:** E2-S6, E3-S4b, E6-S1

**Description:**  
`src/meteoedge/pollers/kalshi.py` — long-running 30s loop snapshotting all open weather market order books into `market_snapshot` and Redis.

**Acceptance criteria:**
- [ ] Polls `GET /events?status=open&category=Climate` + nested market discovery
- [ ] Fetches `GET /markets/{ticker}/orderbook` for each relevant market
- [ ] Parses bracket bounds from market subtitle; persists full snapshot to `market_snapshot`
- [ ] Writes `price:{ticker}` to Redis (TTL 60s) for each snapshot
- [ ] Uses rate-limit decorator from E2-S4c; exponential backoff on failure, max 5 retries then halt
- [ ] Demo/prod switchable via `KALSHI_ENV`; unit tests cover bracket parsing heuristic

---

### E2-S4a: Redis token bucket for Kalshi rate limiting

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S2  
**Blocks:** E2-S4c

**Description:**  
Implement a Redis-backed token bucket as a reusable module. Uses a Lua script for atomicity so the bucket cannot be over-drawn under concurrent callers.

**Interface to implement:**
```python
class TokenBucket:
    def __init__(self, redis_client, key: str, max_per_minute: int): ...
    def acquire(self) -> bool:  # True if token granted, False if limit exceeded
        # Lua: INCR key, SET TTL 60s on first increment, return count
```

**Acceptance criteria:**
- [ ] `TokenBucket` class in `src/meteoedge/cache.py` with `acquire() -> bool`
- [ ] Redis key: `rate:kalshi:{endpoint}`, TTL 60s, counter resets each window
- [ ] Lua script used for atomic INCR+TTL — no WATCH/MULTI race condition
- [ ] `acquire()` returns `False` (never raises) when limit is reached
- [ ] Unit tests: first call grants, nth call at limit denies, TTL expiry resets counter
- [ ] Concurrent test: 10 threads acquiring simultaneously — total granted ≤ max_per_minute

---

### E2-S4b: Async retry decorator with exponential backoff

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** None  
**Blocks:** E2-S4c

**Description:**  
Generic `@retry` decorator for async functions. Backs off exponentially, raises `MaxRetriesExceeded` on exhaustion (never swallows the error silently).

**Interface to implement:**
```python
def retry(max_attempts: int = 5, base_delay: float = 1.0, backoff_factor: float = 2.0):
    # decorator: on exception, sleeps base_delay * backoff_factor^attempt, then retries
    # after max_attempts, raises MaxRetriesExceeded(last_exception)
```

**Acceptance criteria:**
- [ ] Decorator in `src/meteoedge/utils.py`
- [ ] Delays follow: 1s, 2s, 4s, 8s, 16s for defaults
- [ ] Raises `MaxRetriesExceeded` (custom exception) after exhaustion, wrapping original
- [ ] Unit tests: success on 3rd attempt, exhaustion raises correct exception, delay sequence verified with mock sleep

---

### E2-S4c: Apply rate limiting and retry to Kalshi HTTP calls

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E2-S4a, E2-S4b  
**Blocks:** E2-S3, E6-S1

**Description:**  
Wire the token bucket and retry decorator onto every Kalshi API call site. This is an integration task — no new logic, just application of the existing decorators.

**Acceptance criteria:**
- [ ] `@rate_limited(endpoint, max_per_minute)` decorator wraps all Kalshi HTTP functions
- [ ] `@retry(max_attempts=5)` applied to all Kalshi HTTP functions
- [ ] Rate limit exceeded: caller receives `False` from `acquire()`; logs WARN and skips that tick (never raises)
- [ ] `MaxRetriesExceeded` from retry propagates to caller; engine loop handles as halt
- [ ] Integration test: mock Kalshi API returning 429 — verify rate limiter fires and retry backoff occurs

---

### E2-S5: Historical data backfill tooling

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S3  
**Blocks:** E7-S7

**Description:**  
Bulk-download scripts for historical METAR (IEM ASOS) and Kalshi snapshots (3-month native window). Write to both PostgreSQL and Parquet for backtest use.

**Acceptance criteria:**
- [ ] `scripts/backfill_metar.py --station KNYC --start 2025-01-01 --end 2025-12-31` inserts IEM ASOS CSV into `metar_observation` and writes Parquet to `data/backtest/metar/`
- [ ] `scripts/backfill_kalshi.py --days 90` fetches Kalshi candle/trades history and writes to `market_snapshot` + `data/backtest/market_snapshots/`
- [ ] Both scripts idempotent (re-run doesn't duplicate)
- [ ] Progress logging: rows inserted, days processed, errors skipped

---

### E2-S6: Redis caching layer

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E1-S2, E2-S1, E2-S2, E2-S3  
**Blocks:** E3-S2b, E3-S4b

**Description:**  
Typed cache module wrapping all Redis key patterns from spec §6.2. All strategy modules consume from this cache — never from Redis directly.

**Acceptance criteria:**
- [ ] `src/meteoedge/cache.py` exposes typed read/write functions for: `price:{ticker}` (TTL 60s), `envelope:{station}:{date}` (TTL 900s), `halt:global` (no TTL), stale-data flags
- [ ] All reads return `None` on miss (never raise)
- [ ] Unit tests: TTL expiry, miss handling, halt flag persists across client reconnect

---

## Epic 3 — Strategy Engine

**Goal:** The core algorithmic brain — envelope computation, edge scanning, fee formula, position sizing. All modules must be clock-injectable (no direct `datetime.now()` calls).  
**Prerequisites:** Epic 2.  
**Blocks:** Epic 4, Epic 5, Epic 7.  
**Designer needed:** No.

---

### E3-S1a: Historical METAR grouping and daily-max extraction

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E2-S1  
**Blocks:** E3-S1b

**Description:**  
Script that reads `metar_observation` for all 5 stations and groups observations by `(station_code, month, local_hour_of_day)`, computing the daily high temperature up to each observation's time.

**Acceptance criteria:**
- [ ] `scripts/build_climb_rates.py` step 1: for each (station, date), compute running daily max at each observed hour
- [ ] Output: intermediate CSV `data/metar_daily_max_by_hour.csv` with columns `(station, date, month, local_hour, temp_f, daily_max_so_far_f, additional_rise_f)`
- [ ] `additional_rise_f = daily_final_high_f - daily_max_so_far_f` (how much more the high rose after this hour)
- [ ] Handles DST: converts UTC `observed_at` to station-local time via `pytz`
- [ ] Unit test: 3-observation synthetic day, verify `additional_rise_f` computed correctly

---

### E3-S1b: p95 climb-rate computation per (station, month, hour)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S1a  
**Blocks:** E3-S1c

**Description:**  
Reads the intermediate CSV from E3-S1a and computes the 95th-percentile additional rise for each `(station, month, local_hour)` cell using `numpy.percentile`. Outputs the lookup table.

**Acceptance criteria:**
- [ ] Groups by `(station, month, local_hour)`, computes `p95` of `additional_rise_f` across all days in that cell
- [ ] Output: `data/climb_rates_raw.json` — dict keyed `"{station}_{month}_{hour}"` → `p95_rise_f`
- [ ] Minimum sample size: cells with fewer than 20 observations flagged as `low_confidence: true`
- [ ] Monotone check: for each (station, month), p95 should be non-increasing from hour 10 → hour 22; log any violations as WARN
- [ ] Unit tests: p95 computation on known data, monotone check fires correctly

---

### E3-S1c: Climb-rate DB table, fallback, and validation plot

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S1b  
**Blocks:** E3-S2b

**Description:**  
Insert computed climb rates into the DB, implement fallback to `DEFAULT_CLIMB_LOOKUP` for low-confidence cells, and produce a validation plot.

**Acceptance criteria:**
- [ ] Alembic migration adds `climb_rate` table: `(station_code, month, local_hour, p95_rise_f, low_confidence BOOL)`
- [ ] Script inserts all rows from `climb_rates_raw.json`
- [ ] `get_expected_rise(station, month, hour, db) -> float` returns DB value; falls back to `DEFAULT_CLIMB_LOOKUP` for `low_confidence=True` cells or misses
- [ ] Validation plot saved to `data/climb_rate_curves.png`: one line per station per month showing p95 rise by hour
- [ ] Unit test: fallback fires for missing cell, returns correct lookup value

---

### E3-S2a: Clock protocol and SystemClock

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** None  
**Blocks:** E3-S2b, E7-S2a

**Description:**  
Define the `Clock` protocol used by all strategy and backtest modules. Implement `SystemClock` for production use. This single file enables swapping to `SimulationClock` in the backtest with zero production code changes.

**Interface to implement:**
```python
from typing import Protocol
from datetime import datetime

class Clock(Protocol):
    def now(self) -> datetime: ...  # always returns UTC-aware datetime

class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
```

**Acceptance criteria:**
- [ ] `Clock` protocol and `SystemClock` in `src/meteoedge/clock.py`
- [ ] `SystemClock.now()` returns UTC-aware `datetime`
- [ ] No other modules import `datetime.now()` directly (CI grep check added: `grep -r "datetime.now()" src/`)
- [ ] Unit test: `SystemClock.now()` is UTC-aware and within 1s of real time

---

### E3-S2b: Envelope min/max computation

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S1c, E3-S2a, E2-S2  
**Blocks:** E3-S2c

**Description:**  
Implement the envelope bounds calculation from spec §7.1. Accepts an injected `Clock` and DB-backed `get_expected_rise`. Returns `(min_achievable_high, max_achievable_high)`.

**Formula to implement exactly:**
```
min_achievable_high = current_high_f
max_achievable_high = max(
    current_high_f,
    latest_temp_f + get_expected_rise(station, month, local_hour, db)
)
# If hours_remaining == 0 (post-sunset): max_achievable_high = current_high_f
```

**Acceptance criteria:**
- [ ] `compute_envelope(state: WeatherState, clock: Clock, db) -> tuple[float, float]` in `src/meteoedge/engine/envelope.py`
- [ ] Post-sunset case: `max = min = current_high_f`
- [ ] `hours_remaining = max(0, sunset_time - clock.now())` — uses injected clock
- [ ] DST-safe: all local time via `pytz`, station timezone from config
- [ ] Envelope written to Redis cache: `envelope:{station}:{date}` TTL 900s; hit skips recompute
- [ ] Unit tests: normal case, post-sunset, cache hit skips DB call

---

### E3-S2c: P(Yes) probability branches

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S2b  
**Blocks:** E3-S4a, E7-S3b

**Description:**  
Implement the four `P(Yes)` cases from spec §7.1. The four cases are fully specified — implementer maps them to code exactly.

**Cases to implement (in priority order):**
```
1. hi < current_high_f                     → P = 0.0  (bracket below current high)
2. lo > max_achievable_high                → P = 0.0  (bracket above envelope ceiling)
3. lo <= current_high_f AND hi >= max_env  → P = 1.0  (bracket contains entire remaining range)
4. otherwise                               → P = p_normal_between(lo, hi, forecast_mean, stddev)
   where forecast_mean = clamp(forecast_high_f, min_env, max_env)
         stddev = station_season_stddev (1.5°F – 3.0°F, from config)
```

**Acceptance criteria:**
- [ ] `true_probability_yes(bracket, state, envelope) -> float` in `src/meteoedge/engine/envelope.py`
- [ ] `p_normal_between(lo, hi, mean, stddev)` using `math.erf` (no scipy dependency)
- [ ] All 4 branches covered by dedicated unit tests with hand-computed expected values
- [ ] 100% branch coverage on all probability cases
- [ ] Forecast mean clamped to envelope bounds before passing to normal distribution

---

### E3-S2d: DST and timezone edge-case test suite

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S2c  
**Blocks:** None

**Description:**  
Dedicated test file for DST edge cases in envelope computation. NWS Climate Report uses local standard time even during DST — this must not cause off-by-one-hour errors.

**Acceptance criteria:**
- [ ] Test: spring-forward day (2026-03-08, KNYC) — verify `now_local` is correct hour, envelope not miscalculated
- [ ] Test: fall-back day (2026-11-01, KNYC) — ambiguous hour 1:30am, verify no crash
- [ ] Test: KLAX (Pacific) vs KORD (Central) — different UTC offsets, verify both correct simultaneously
- [ ] Test: poll at exactly sunset time — `hours_remaining == 0`, `max_achievable = current_high`
- [ ] All tests pass with `SimulationClock` providing the test timestamp (no real wall-clock dependency)

---

### E3-S3: Kalshi fee formula (versioned)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** None  
**Blocks:** E3-S4a, E7-S8

**Description:**  
Implement `fee_cents` as a pure function using versioned fee schedule JSON. Unit-test against 20+ worked examples from Kalshi's published schedule.

**Formula:**
```python
def fee_cents(price_cents: int, quantity: int, schedule: FeeSchedule) -> int:
    p = price_cents / 100
    raw = 0.07 * quantity * p * (1 - p)
    return max(quantity, math.ceil(raw))  # $0.01 floor per contract
```

**Acceptance criteria:**
- [ ] `fee_cents(price_cents, quantity, schedule_version) -> int` in `src/meteoedge/engine/fee_model.py` — pure function, no side effects
- [ ] `FeeSchedule` loaded from `data/fee_schedule_v*.json`; version selected by `effective_from`/`effective_to` date
- [ ] At least 20 unit tests against known Kalshi examples covering: floor case (low price × small quantity), high-price symmetric case, large quantity, boundary prices (1¢, 50¢, 99¢)
- [ ] `scripts/verify_fee_schedule.py` stub committed (implementation in E9-S4)

---

### E3-S4a: EV computation pure function

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S2c, E3-S3  
**Blocks:** E3-S4b

**Description:**  
Pure function computing expected value per contract for both YES and NO sides after fees. No I/O, no side effects.

**Formula to implement exactly (spec §7.2):**
```python
def compute_ev(p_yes: float, yes_ask_cents: int, no_ask_cents: int,
               quantity: int, schedule: FeeSchedule) -> tuple[float, float]:
    fee_y = fee_cents(yes_ask_cents, quantity, schedule)
    fee_n = fee_cents(no_ask_cents, quantity, schedule)
    ev_yes = p_yes * 100 - yes_ask_cents - fee_y
    ev_no  = (1 - p_yes) * 100 - no_ask_cents - fee_n
    return ev_yes, ev_no
```

**Acceptance criteria:**
- [ ] `compute_ev(...)` in `src/meteoedge/engine/edge_scanner.py`
- [ ] Unit tests: 5 hand-computed cases covering YES win, NO win, both negative (no trade), fee dominating edge
- [ ] Pure function — no DB, no Redis, no clock

---

### E3-S4b: 4-filter candidate gate

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S4a, E3-S5  
**Blocks:** E4-S1a, E5-S6, E7-S3c

**Description:**  
Apply the four trade-candidate filters from spec §7.2. Return only qualifying `TradeCandidate` objects. Uses injected clock for settlement proximity check.

**Filters (all four must pass):**
```
1. edge >= MIN_EDGE_CENTS (configurable, default 3¢)
2. P(Yes) >= 0.80 for YES side  OR  P(Yes) <= 0.20 for NO side
3. time_to_settlement >= 15 minutes  (uses clock.now())
4. yes_ask_size >= quantity * 2  (liquidity: 2× depth required)
```

**Acceptance criteria:**
- [ ] `scan_edges(snapshots, weather_states, clock, config) -> list[TradeCandidate]` in `edge_scanner.py`
- [ ] Each filter tested independently with boundary-value unit tests (exactly at threshold → pass; one below → fail)
- [ ] Returns empty list when no candidates pass — never raises
- [ ] Logs count of evaluated vs passed candidates on each call (structured log, not print)

---

### E3-S4c: Decision logging to DB

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S4b, E1-S3  
**Blocks:** None

**Description:**  
Persist every evaluated bracket (passed or rejected) to the `decision` table. Enables post-hoc analysis of filter behavior.

**Acceptance criteria:**
- [ ] `log_decision(candidate, rules_approved, db) -> decision_id` writes to `decision` table
- [ ] Fields populated: `ticker`, `side`, `true_prob`, `market_price_cents`, `computed_edge_cents`, `rules_approved`, `decided_at`
- [ ] `claude_approved` and `claude_reason` left NULL at this stage (filled by Epic 5)
- [ ] Called for EVERY evaluated bracket, not just those that pass the filter
- [ ] Unit test: DB insert verified, fields match input values

---

### E3-S5: Position sizing (fractional Kelly)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S4b  
**Blocks:** E4-S1a, E6-S2b

**Description:**  
`fractional_kelly_size` pure function from spec §7.4. Three caps applied in order; always rounds down.

**Formula to implement:**
```python
def fractional_kelly_size(edge_cents, payout_ratio, bankroll_cents, ask_size,
                          fractional_kelly=0.25, max_trade_pct=0.02, liquidity_pct=0.20) -> int:
    kelly_f = (edge_cents / 100) / payout_ratio
    kelly_size = int(fractional_kelly * bankroll_cents * kelly_f / 100)  # in contracts
    cap1 = int(max_trade_pct * bankroll_cents / 100)
    cap2 = int(liquidity_pct * ask_size)
    return max(0, min(kelly_size, cap1, cap2))  # floor at 0
```

**Acceptance criteria:**
- [ ] `fractional_kelly_size(...)` in `src/meteoedge/engine/sizing.py` — pure function
- [ ] Unit tests: typical case, bankroll cap binding, liquidity cap binding, rounding down, zero result when all too small

---

### E3-S6: Strategy config and orchestration loop

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S4b, E3-S5, E2-S6  
**Blocks:** E4-S1c

**Description:**  
Typed env-var config and the 30s engine loop coordinating pollers → cache → envelope → edge scanner → risk → LLM → order router. Clock-injectable throughout.

**Acceptance criteria:**
- [ ] All env vars from spec §10.3 parsed and typed in `src/meteoedge/config.py`; startup fails fast with clear message on missing required vars
- [ ] `TRADING_ENABLED=false` respected — strategy evaluates but never places orders
- [ ] Engine loop checks `halt:global` Redis key and `STOP` file at the top of every 30s tick
- [ ] Loop uses injected `SystemClock` (production) — never calls `datetime.now()` directly
- [ ] Structured log on every tick: candidates evaluated, candidates passed, trades placed or blocked

---

## Epic 4 — Risk Management

**Goal:** Every rule from spec §7.5 enforced correctly, with independently tested unit tests per rule. Halt and resume logic must be rock-solid.  
**Prerequisites:** Epic 3.  
**Blocks:** Epic 5, Epic 6.  
**Designer needed:** No.

---

### E4-S1a: Exposure rules (per-ticker, total, max trades)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S4b  
**Blocks:** E4-S1e

**Description:**  
Implement the three exposure/count rules from spec §7.5. Pure functions operating on portfolio state — no DB writes here.

**Rules to implement:**
```
Rule 1: current_exposure_for_ticker / bankroll > 0.02  → REJECT
Rule 2: total_open_exposure / bankroll > 0.10          → REJECT
Rule 3: trades_placed_today >= 30                       → REJECT
```

**Acceptance criteria:**
- [ ] `check_exposure(candidate, portfolio, bankroll_cents) -> RuleResult` in `src/meteoedge/engine/risk_manager.py`
- [ ] Each rule has a dedicated unit test; boundary value (exactly at threshold) tested separately
- [ ] `RuleResult` is a dataclass: `{passed: bool, rule: str, reason: str}`
- [ ] Pure function — no side effects, no DB/Redis calls

---

### E4-S1b: Staleness rules (METAR and Kalshi freshness)

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E2-S6  
**Blocks:** E4-S1e

**Description:**  
Implement the two data-freshness rules. Reads stale-data flags from Redis (written by pollers in E2-S1 and E2-S3).

**Rules:**
```
Rule 6: stale:metar:{station} exists in Redis  → REJECT
Rule 7: stale:kalshi:{ticker} exists in Redis  → REJECT
```

**Acceptance criteria:**
- [ ] `check_staleness(candidate, cache) -> RuleResult` reads Redis flags
- [ ] Unit tests: flag set → REJECT, flag absent → pass, Redis miss (None) → pass
- [ ] Does not compute freshness itself — trusts the poller-written flags

---

### E4-S1c: Settlement proximity rule

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S6  
**Blocks:** E4-S1e

**Description:**  
Implement the 15-minute settlement proximity gate. Uses injected clock. Critically: on proximity trigger, do NOT cancel existing orders — only reject NEW ones.

**Rule:**
```
Rule 9: (market_close_time - clock.now()) < 15 minutes  → REJECT new trades
        (existing open orders left untouched)
```

**Acceptance criteria:**
- [ ] `check_settlement_proximity(candidate, clock) -> RuleResult`
- [ ] Uses `clock.now()` — never `datetime.now()`
- [ ] Unit tests: 14 min remaining → REJECT, 16 min → pass, exactly 15 min → pass (boundary)
- [ ] Confirmed by comment: existing orders must not be cancelled when this rule fires

---

### E4-S1d: HALT triggers (consecutive losses + daily P&L floor)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E4-S1a  
**Blocks:** E4-S1e

**Description:**  
Implement the two HALT-level rules. Unlike REJECT rules, these set `halt:global` in Redis and create a `risk_event` DB record. Manual restart required after HALT.

**Rules:**
```
Rule 4: consecutive_losing_days >= 3  → HALT (manual restart required)
Rule 5: daily_pnl_cents / bankroll_cents < -0.05  → HALT immediately
```

**Acceptance criteria:**
- [ ] `check_halt_conditions(portfolio, bankroll_cents, db, cache) -> RuleResult`
- [ ] On HALT: sets `halt:global` in Redis (no TTL), inserts `risk_event` with `severity="HALT"`
- [ ] Consecutive days counter: stored in Redis `losing_days:streak`, reset to 0 on winning day, incremented on losing day at EOD
- [ ] Unit tests: 3rd consecutive loss triggers HALT, winning day resets counter, daily P&L < -5% triggers HALT, exactly -5% does NOT trigger

---

### E4-S1e: RiskManager orchestrator

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E4-S1a, E4-S1b, E4-S1c, E4-S1d  
**Blocks:** E4-S2, E5-S6, E6-S2a

**Description:**  
`RiskManager` class wiring all rule functions together. Returns a single `RiskDecision` per candidate. This is the only entry point the engine loop uses.

**Acceptance criteria:**
- [ ] `RiskManager.check(candidate, portfolio, bankroll_cents, db, cache, clock) -> RiskDecision`
- [ ] `RiskDecision`: `{approved: bool, action: "REJECT"|"HALT", failing_rules: list[RuleResult]}`
- [ ] Rules evaluated in order: HALT rules first (so a HALT always beats a REJECT)
- [ ] First failing rule short-circuits: remaining rules skipped (efficiency, not correctness trade-off)
- [ ] Integration test: construct a scenario triggering each rule in turn, verify correct `action` returned each time

---

### E4-S2: Kill switch (STOP file watcher)

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E4-S1e  
**Blocks:** E4-S3

**Description:**  
Check for `/var/run/meteoedge/STOP` at the top of every engine tick. On detection trigger HALT via `RiskManager`.

**Acceptance criteria:**
- [ ] Engine loop checks `Path("/var/run/meteoedge/STOP").exists()` at start of each tick
- [ ] On detection: calls `risk_manager.halt(reason="manual_kill_switch")` → sets `halt:global`, writes `risk_event` with `event_type="KILL_SWITCH"`, `severity="HALT"`
- [ ] HALT logged at ERROR level with timestamp
- [ ] Unit test: file created mid-loop; next tick fires HALT correctly

---

### E4-S3: Halt and resume workflow

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E4-S1e, E4-S2  
**Blocks:** E6-S2a

**Description:**  
Clean halt (stops new orders, lets existing positions run to settlement) and manual resume workflow (operator deletes STOP file and clears Redis flag).

**Acceptance criteria:**
- [ ] On HALT: engine stops accepting new trades within 30s; existing open orders NOT cancelled
- [ ] `scripts/resume.py` wraps the two-step resume: `rm STOP` + `redis-cli DEL halt:global` with confirmation prompt
- [ ] Resume writes `acknowledged_at` on the triggering `risk_event` record + INFO log
- [ ] Integration test: trigger halt → verify orders blocked → simulate resume → verify normal operation resumes

---

### E4-S4: Daily P&L floor enforcement

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E4-S1d  
**Blocks:** E6-S4c

**Description:**  
Real-time daily P&L computation integrated into the risk manager, driving Rule 5 (daily floor) and Rule 4 (consecutive losing day counter).

**Acceptance criteria:**
- [ ] `daily_pnl_cents(db, clock) -> int` reads today's fills from `trade_order` and sums realized P&L
- [ ] Called on every engine tick; result passed to `check_halt_conditions`
- [ ] Consecutive losing days counter updated at midnight UTC roll-over (triggered by first tick of new day)
- [ ] Unit tests: floor breach, exactly at floor (no halt), EOD counter increment, reset on winning day

---

### E4-S5: Risk event logging

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E1-S3, E1-S5  
**Blocks:** E4-S1e

**Description:**  
`log_risk_event` writer used by the risk manager. All HALTs, WARNs, and anomalies persisted with enough context for reconstruction.

**Acceptance criteria:**
- [ ] `log_risk_event(event_type, severity, details: dict, db)` inserts into `risk_event` table
- [ ] `severity` validated against `INFO | WARN | HALT` enum; invalid value raises `ValueError`
- [ ] `details` stored as JSONB including at minimum: rule triggered, candidate ticker, portfolio state snapshot
- [ ] `acknowledged_at` is NULL on creation; set by resume workflow (E4-S3)
- [ ] Structured log entry also emitted at same severity level
- [ ] Unit test: DB insert, JSONB roundtrip, severity validation

---

## Epic 5 — LLM Sanity Check

**Goal:** Provider-agnostic LLM integration as an AND-gate pre-trade sanity layer.  
**Prerequisites:** Epic 3 + Epic 4.  
**Blocks:** Epic 6.  
**Designer needed:** No.

---

### E5-S1: LLMProvider protocol and factory

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S6  
**Blocks:** E5-S2, E5-S3, E5-S4, E5-S6

**Description:**  
Define the `LLMProvider` protocol (ABC), common request/response dataclasses, and the factory that instantiates the correct provider from `LLM_PROVIDER` env var.

**Acceptance criteria:**
- [ ] `LLMProvider` protocol in `src/meteoedge/llm/provider.py` with: `sanity_check(request) -> SanityCheckResponse`, `parse_text(prompt, schema) -> T`, `name: str`, `cost_per_call_estimate: float`
- [ ] `SanityCheckRequest` and `SanityCheckResponse` dataclasses with exact fields from spec §8.2/8.3
- [ ] `get_provider(config) -> LLMProvider` in `src/meteoedge/llm/factory.py`; raises clear `ValueError` on unknown provider
- [ ] Unit tests: factory returns correct type per env var; unknown value raises

---

### E5-S2: Anthropic provider implementation

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E5-S1  
**Blocks:** E5-S6

**Description:**  
`src/meteoedge/llm/anthropic_provider.py` using Messages API with tool use for structured JSON output. Prompt caching enabled for static system prompt.

**Acceptance criteria:**
- [ ] Uses `claude-sonnet-4-6` (sanity check) / `claude-haiku-4-5` (parsing); both overridable via env vars
- [ ] Structured output via tool use — not freeform text extraction
- [ ] Prompt caching on system prompt (`cache_control: {"type": "ephemeral"}`)
- [ ] Hard 10s timeout via `httpx` async; on timeout: returns `approve=False, reason="timeout"`
- [ ] On malformed output after 2 retries: returns `approve=False, reason="parse_error"` — never raises
- [ ] `cost_per_call_estimate = 0.015`
- [ ] Unit tests: approval, rejection, timeout, malformed response

---

### E5-S3: DeepSeek provider implementation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E5-S1  
**Blocks:** E5-S6

**Description:**  
`src/meteoedge/llm/deepseek_provider.py` using OpenAI-compatible endpoint with `response_format={"type": "json_object"}`.

**Acceptance criteria:**
- [ ] Uses `deepseek-chat` for both sanity check and parsing; overridable via env vars
- [ ] Same 10s timeout and fail-safe error handling as Anthropic provider
- [ ] `cost_per_call_estimate = 0.001`
- [ ] Unit tests mirror Anthropic tests exactly (parameterized test suite covers both providers)

---

### E5-S4: OpenAI provider implementation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E5-S1  
**Blocks:** E5-S6

**Description:**  
`src/meteoedge/llm/openai_provider.py` with structured output via function calling. Custom `OPENAI_API_BASE` supported for OpenAI-compatible endpoints.

**Acceptance criteria:**
- [ ] Uses `gpt-4o` (sanity check) / `gpt-4o-mini` (parsing); overridable
- [ ] `OPENAI_API_BASE` env var sets custom base URL (enables any OpenAI-compatible endpoint)
- [ ] Same timeout/error handling; `cost_per_call_estimate = 0.010`
- [ ] Parameterized unit tests shared with Anthropic and DeepSeek providers

---

### E5-S5: Provider factory — complete registration

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E5-S2, E5-S3, E5-S4  
**Blocks:** E5-S6

**Description:**  
Complete factory implementation registering all three providers. Adding a fourth provider requires only: implement protocol + add one line to the registry dict.

**Acceptance criteria:**
- [ ] Factory supports `anthropic`, `deepseek`, `openai` values
- [ ] Factory validates required env vars for selected provider on instantiation (fail fast)
- [ ] Unit test: each string → correct class; unknown string → `ValueError` with list of valid options

---

### E5-S6: Sanity check logic (AND gate)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E5-S1, E5-S5, E4-S1e  
**Blocks:** E6-S2a

**Description:**  
Provider-agnostic sanity check logic. Constructs the request, calls the provider, applies abort conditions, updates the `decision` table. LLM approval cannot override a rules engine rejection.

**Abort conditions (any → return False):**
- `approve=False`
- `approve=True` AND `confidence < 0.7`
- `warnings` contains any of: `"cold front"`, `"data error"`, `"anomaly"`
- API error or timeout

**Acceptance criteria:**
- [ ] `sanity_check(candidate, context, provider, db) -> bool` in `src/meteoedge/llm/sanity_check.py`
- [ ] AND gate enforced: `risk_decision.approved AND llm_approved` — LLM True cannot unlock a risk REJECT
- [ ] Updates `decision.claude_approved` and `decision.claude_reason` in DB on every call
- [ ] API error or timeout → returns `False`, logs error, never crashes engine loop
- [ ] Unit tests: each abort condition, successful approval, API error, confidence < 0.7

---

### E5-S7: Daily LLM call cap and cost tracking

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E5-S6  
**Blocks:** None

**Description:**  
Enforce `LLM_DAILY_CALL_CAP` (default 200) via Redis counter. Track per-provider cost. Alert when cap is hit.

**Acceptance criteria:**
- [ ] Redis counter `llm:calls:{date}:{provider}` incremented on each call
- [ ] If counter >= cap: subsequent calls return `False`, trigger alert email
- [ ] Daily cost estimate (`calls × cost_per_call_estimate`) logged per provider at end of day
- [ ] Unit test: cap enforcement, counter reset at midnight, alert triggered at exactly cap

---

### E5-S8: LLM rejection-reason logging

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E5-S6  
**Blocks:** None

**Description:**  
Persist LLM rejection reasons and warnings for post-hoc analysis. Weekly frequency report.

**Acceptance criteria:**
- [ ] Every sanity check outcome (approve/reject/error) updates `decision.claude_approved`, `decision.claude_reason`
- [ ] Warning list stored in structured engine log entry
- [ ] Weekly summary logged to `engine.jsonl` on Sundays: top 10 rejection reasons by frequency
- [ ] Unit test: all outcome types write correct DB fields

---

## Epic 6 — Order Execution

**Goal:** Correct, safe, traceable order lifecycle against the Kalshi API. Emergency liquidation available.  
**Prerequisites:** Epic 4, Epic 5.  
**Blocks:** Epic 8.  
**Designer needed:** No.

---

### E6-S1: Kalshi SDK integration with RSA-PSS auth

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E2-S4c  
**Blocks:** E6-S2a, E6-S2b, E6-S4a, E6-S5

**Description:**  
Production async Kalshi API client with RSA-PSS request signing. Loads private key once at startup. All endpoints from spec §9.3 implemented.

**Acceptance criteria:**
- [ ] RSA-PSS signing per Kalshi docs: message = `{timestamp_ms}{METHOD}{path}`, signed with SHA-256, base64-encoded
- [ ] Private key loaded once on client instantiation from `KALSHI_PRIVATE_KEY_PATH`
- [ ] All endpoints implemented: `GET /events`, `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook`, `POST /portfolio/orders`, `DELETE /portfolio/orders/{id}`, `GET /portfolio/fills`, `GET /portfolio/positions`
- [ ] `httpx.AsyncClient` with connection pool reuse; rate-limit decorator from E2-S4c applied
- [ ] Demo/prod URL switchable via `KALSHI_ENV`
- [ ] Integration tests against Kalshi demo API (marked `@pytest.mark.integration`)

---

### E6-S2a: Pre-flight validation chain

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S1, E4-S1e, E5-S6  
**Blocks:** E6-S2b

**Description:**  
All checks that must pass before an order is submitted. Re-checks `halt:global` immediately before placing (race condition guard) — not just at the top of the tick.

**Checks (in order, all must pass):**
```
1. halt:global not set in Redis
2. TRADING_ENABLED env var is "true"
3. decision.rules_approved == True
4. decision.claude_approved == True
5. computed size > 0 (from fractional_kelly_size)
```

**Acceptance criteria:**
- [ ] `pre_flight_check(decision, config, cache) -> PreFlightResult` in `src/meteoedge/execution/order_router.py`
- [ ] `PreFlightResult`: `{passed: bool, blocked_by: str | None}`
- [ ] Each check has a dedicated unit test; failure short-circuits remaining checks
- [ ] `halt:global` re-read directly from Redis (not from a cached value) to catch race conditions

---

### E6-S2b: Order submission to Kalshi API

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S2a  
**Blocks:** E6-S2c

**Description:**  
Construct and submit `POST /portfolio/orders` payload. Handle API success and rejection responses. All order payload details logged for audit.

**Acceptance criteria:**
- [ ] `submit_order(candidate, size, client) -> OrderSubmitResult` constructs correct payload and calls Kalshi
- [ ] Payload hash (SHA-256 of request body) logged with `decision_id` before API call
- [ ] On success: returns `external_order_id` from API response
- [ ] On API rejection (non-2xx): logs reason, returns `OrderSubmitResult(success=False, reason=...)`
- [ ] On network error: retries per E2-S4b config; on `MaxRetriesExceeded`: returns failure (does not raise)
- [ ] Unit tests: success path, API 400 rejection, network timeout hitting retry limit

---

### E6-S2c: Order DB record management

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E6-S2b  
**Blocks:** E6-S3a

**Description:**  
Create and update `trade_order` records around the API call. Record must exist before the API call (status=PENDING) so any crash is recoverable.

**Acceptance criteria:**
- [ ] `trade_order` row inserted with `status=PENDING` and `decision_id` BEFORE API call
- [ ] On success: row updated with `status=OPEN`, `external_id` from API response
- [ ] On rejection: row updated with `status=REJECTED`
- [ ] On network failure after max retries: row updated with `status=CANCELLED`
- [ ] Unit test: each status transition; DB state correct even if exception thrown during API call

---

### E6-S2d: Order cancellation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E6-S1  
**Blocks:** None

**Description:**  
`cancel_order` implementation: calls `DELETE /portfolio/orders/{id}`, updates `trade_order` status, logs reason.

**Acceptance criteria:**
- [ ] `cancel_order(order_id, reason, client, db)` calls Kalshi DELETE and updates `trade_order.status = "CANCELLED"`
- [ ] Idempotent: if order already cancelled on Kalshi, logs INFO and returns success
- [ ] Reason logged to `execution.jsonl` with `order_id` and `decision_id`
- [ ] Unit test: successful cancel, already-cancelled idempotency

---

### E6-S3a: Fill polling loop

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S2c  
**Blocks:** E6-S3b, E6-S3c

**Description:**  
Poll `GET /portfolio/fills` on each engine tick for fills since last poll. Match fills to open `trade_order` records.

**Acceptance criteria:**
- [ ] `poll_fills(client, db, last_poll_time) -> list[Fill]` fetches fills since `last_poll_time`
- [ ] Each fill matched to `trade_order` by `external_id`; unmatched fills logged as WARN
- [ ] `trade_order.filled_quantity` incremented by fill quantity
- [ ] All fill events logged to `execution.jsonl` with `decision_id`
- [ ] Unit tests: single fill, multiple fills same order (partial), fill for unknown order_id (WARN)

---

### E6-S3b: Position avg-cost updater

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E6-S3a  
**Blocks:** None

**Description:**  
Update `position` table on each fill: running weighted average cost, incremental realized P&L.

**Formula:**
```
new_avg_cost = (prev_qty * prev_avg_cost + fill_qty * fill_price) / (prev_qty + fill_qty)
```

**Acceptance criteria:**
- [ ] `update_position(fill, db)` upserts `position` row using weighted average formula
- [ ] Realized P&L computed on full position close: `(settlement_payout - avg_cost) * total_qty`
- [ ] Unit tests: first fill (create row), second fill same ticker (weighted avg), position close (P&L)

---

### E6-S3c: Order status state machine

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S3a  
**Blocks:** E6-S4b

**Description:**  
Enforce valid status transitions for `trade_order`. Prevents invalid states (e.g., CANCELLED → FILLED).

**Valid transitions:**
```
PENDING → OPEN | REJECTED | CANCELLED
OPEN    → FILLED | CANCELLED | OPEN (partial fill, stays open)
FILLED  → (terminal)
REJECTED → (terminal)
CANCELLED → (terminal)
```

**Acceptance criteria:**
- [ ] `transition_order_status(order_id, new_status, db)` validates transition before writing
- [ ] Invalid transition raises `InvalidStatusTransition` (logged as ERROR, not propagated)
- [ ] `finalized_at` set on transition to FILLED, REJECTED, or CANCELLED
- [ ] Unit tests: each valid transition, each invalid transition, terminal state idempotency

---

### E6-S4a: Kalshi authoritative position and fill fetcher

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E6-S1  
**Blocks:** E6-S4b

**Description:**  
Fetch Kalshi's authoritative positions and fills for a given trade date. Used by the reconciler as the source of truth.

**Acceptance criteria:**
- [ ] `fetch_kalshi_positions(client, trade_date) -> list[KalshiPosition]`
- [ ] `fetch_kalshi_fills(client, trade_date) -> list[KalshiFill]`
- [ ] Both handle pagination (follow `cursor` if present)
- [ ] Returns empty list (not error) if no activity for that date
- [ ] Unit test with mock API responses including paginated case

---

### E6-S4b: Local vs remote comparison logic

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S4a, E6-S3c  
**Blocks:** E6-S4c

**Description:**  
Compare local `position` and `trade_order` records against Kalshi's authoritative data. Compute discrepancies in quantity and P&L.

**Acceptance criteria:**
- [ ] `compare_positions(local: list[Position], remote: list[KalshiPosition]) -> list[Discrepancy]`
- [ ] `Discrepancy`: `{ticker, side, local_qty, remote_qty, local_pnl_cents, remote_pnl_cents}`
- [ ] Discrepancy detected if qty differs OR P&L differs by more than 1 cent (rounding tolerance)
- [ ] Unit tests: clean reconciliation (empty list), single qty mismatch, single P&L mismatch

---

### E6-S4c: Discrepancy handler (WARN/HALT thresholds)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S4b, E4-S4  
**Blocks:** E8-S1

**Description:**  
Apply thresholds to discrepancies and take appropriate action: WARN for small divergence, HALT for large. Reconciliation runs daily at settlement time.

**Thresholds:**
```
|P&L difference| > €0.50 → WARN risk event + email alert
|P&L difference| > €2.00 → HALT + email alert + manual investigation required
```

**Acceptance criteria:**
- [ ] `handle_discrepancies(discrepancies, db, cache, alerter)` applies threshold logic
- [ ] WARN: writes `risk_event` with `severity="WARN"`, sends alert email
- [ ] HALT: writes `risk_event` with `severity="HALT"`, sets `halt:global`, sends alert email
- [ ] Clean reconciliation: writes INFO `risk_event` (`acknowledged_at` set immediately for INFO)
- [ ] Scheduled daily at 09:00 local station time via systemd timer or daily-report service
- [ ] Unit tests: clean case, WARN threshold, HALT threshold, multiple discrepancies (one HALT is enough)

---

### E6-S5: Emergency liquidation script

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S2b  
**Blocks:** None

**Description:**  
`scripts/emergency_liquidate.py` — cancels all open orders and market-sells all positions. Requires explicit "YES" confirmation. Cannot be triggered accidentally.

**Acceptance criteria:**
- [ ] Without `--confirm`: prints dry-run summary and exits (no API calls)
- [ ] With `--confirm`: prompts `Type YES to proceed:` and aborts on anything else
- [ ] Cancels all open orders via `DELETE /portfolio/orders/{id}`; places market sell on each open position
- [ ] Each action logged to `execution.jsonl`; email alert sent on completion with summary
- [ ] Sets `halt:global` in Redis after completion
- [ ] Unit test: dry-run makes zero API calls; confirm path calls all endpoints in correct order

---

### E6-S6: Demo vs prod environment switching

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E6-S1  
**Blocks:** E10-S1, E10-S3

**Description:**  
Ensure all code routes to correct endpoint based on `KALSHI_ENV`. Smoke test script for demo connectivity verification.

**Acceptance criteria:**
- [ ] `KALSHI_ENV=demo` → `https://demo-api.kalshi.co/trade-api/v2` everywhere
- [ ] `KALSHI_ENV=prod` → `https://api.elections.kalshi.com/trade-api/v2` everywhere
- [ ] No hardcoded URLs in any source file (CI grep check)
- [ ] `scripts/smoke_test_demo.py` verifies: auth, market data fetch, positions fetch on demo API

---

## Epic 7 — Backtest Harness

**Goal:** Time-gated, determinism-guaranteed, future-leakage-proof replay engine. Stage 1 gate: all 8 exit criteria on held-out test set.  
**Prerequisites:** Epic 3 (clock-injectable strategy modules).  
**Blocks:** Epic 10.  
**Designer needed:** No.

---

### E7-S1: Event store skeleton (Parquet + DuckDB)

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** None  
**Blocks:** E7-S2b, E7-S7

**Description:**  
Parquet directory layout and DuckDB view schema. Works on empty dataset without error.

**Acceptance criteria:**
- [ ] `data/backtest/` with subdirs: `market_snapshots/`, `metar/`, `forecasts/`, `settlements/`; each partitioned by `date=YYYY-MM-DD/part-0.parquet`
- [ ] `EventStore.load(date_range) -> duckdb.Connection` creates all DuckDB VIEWs and returns connection with 0 rows on empty store
- [ ] `fee_schedule_versions.json` stub committed with schema defined
- [ ] Unit test: load empty store, query each view, get 0 rows (not error)

---

### E7-S2a: SimulationClock and Clock protocol

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S2a, E7-S1  
**Blocks:** E7-S2b

**Description:**  
`SimulationClock` for backtest use, satisfying the same `Clock` protocol as `SystemClock`. The clock is not a singleton — multiple instances can run concurrently with different times.

**Interface to implement (from spec §6.2):**
```python
@dataclass
class SimulationClock:
    start: datetime; end: datetime; step: timedelta = timedelta(seconds=30)
    _now: datetime = field(init=False)
    def __post_init__(self): self._now = self.start
    def now(self) -> datetime: return self._now
    def tick(self) -> bool:
        self._now += self.step
        return self._now <= self.end
```

**Acceptance criteria:**
- [ ] `SimulationClock` in `src/meteoedge/backtest/clock.py`
- [ ] `tick()` advances by `step` and returns `False` when past `end`
- [ ] Multiple independent instances can run simultaneously without interference
- [ ] Unit tests: tick sequence, end condition, step size verified

---

### E7-S2b: TimeGatedDataSource query methods

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S2a  
**Blocks:** E7-S2c, E7-S3b

**Description:**  
The temporal firewall of the backtest. Every query filters by `captured_at <= clock.now()`. Implementing this correctly is the most important correctness requirement in the entire harness.

**Methods to implement (from spec §6.3):**
```python
class TimeGatedDataSource:
    def latest_metars(self, station, lookback) -> list[MetarRow]
    def latest_forecast(self, station, target_date) -> ForecastRow | None
    def latest_orderbook(self, ticker, max_staleness) -> OrderbookRow | None
    def official_high(self, station, date) -> float | None
```

**Acceptance criteria:**
- [ ] ALL queries include `WHERE captured_at <= clock.now()` (or equivalent DuckDB filter)
- [ ] `latest_orderbook` also enforces `max_staleness`: rejects rows older than `clock.now() - max_staleness`
- [ ] Returns `None` / empty list on miss — never raises on empty result
- [ ] Unit tests: data at `t`, `t+1s`, `t-1s`; query at `t` returns only `t-1s` row

---

### E7-S2c: Leakage enforcement tests (CI gate)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S2b  
**Blocks:** E7-S4b

**Description:**  
The leakage tests are the most important tests in the project. They run on every CI build and are designed to catch future-data access if the firewall is ever accidentally removed.

**Three tests required:**
1. **Forward-query test:** insert data at `clock.now() + 30s`, query at `clock.now()` → assert 0 rows returned
2. **Missing-filter regression:** temporarily comment out the `captured_at <= ?` filter in one method, run leakage test → assert test FAILS (verifying the test itself is effective)
3. **Monotone clock test:** query at `t=T` ten times — assert all returned rows have `captured_at ≤ T`

**Acceptance criteria:**
- [ ] All three tests in `tests/backtest/test_leakage.py`
- [ ] Tests 1 and 3 pass on correct implementation; test 2 documents that removing filter breaks the suite
- [ ] `make leakage-check` added to CI pipeline (fails build if any leakage test fails)
- [ ] `GITHUB_CI_NOTE.md` updated: leakage tests must never be skipped

---

### E7-S3a: Audit and CI grep check for datetime.now()

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S2a  
**Blocks:** E7-S3b

**Description:**  
Enumerate all direct `datetime.now()` calls in `src/` and add a CI check that fails the build if any are found. This is a one-time audit + permanent guard.

**Acceptance criteria:**
- [ ] Run `grep -rn "datetime\.now()" src/` — document all findings in PR description
- [ ] Fix any found instances to use the injected `Clock`
- [ ] Add to `Makefile`: `check-clock-usage: grep -r "datetime\.now()" src/ && echo "FAIL" || echo "OK"`
- [ ] CI pipeline runs `make check-clock-usage`; build fails if output contains any match
- [ ] Unit test placeholder: `test_no_direct_datetime_calls` that invokes the grep check

---

### E7-S3b: Refactor envelope.py for clock injection

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S3a, E7-S2b  
**Blocks:** E7-S3c

**Description:**  
Refactor `compute_envelope` and `true_probability_yes` to accept a `Clock` and `DataSource` rather than calling `datetime.now()` or reading from Redis directly.

**Acceptance criteria:**
- [ ] `compute_envelope(state, clock, data_source)` signature updated
- [ ] `true_probability_yes(bracket, state, envelope)` unchanged (already pure)
- [ ] All existing unit tests still pass with `SystemClock` injected
- [ ] New test: `compute_envelope` called with `SimulationClock` at a fixed time → deterministic result
- [ ] No `datetime.now()` calls remain in `envelope.py` (CI check from E7-S3a catches violations)

---

### E7-S3c: Refactor edge_scanner.py for clock injection

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S3b  
**Blocks:** E7-S3d

**Description:**  
Update `scan_edges` to accept and pass through the injected `Clock` for the settlement proximity check (filter 3 in E3-S4b).

**Acceptance criteria:**
- [ ] `scan_edges(snapshots, weather_states, clock, config)` — clock already in signature from E3-S4b, confirm it's used (not replaced with `datetime.now()`)
- [ ] All existing edge scanner tests pass with `SimulationClock`
- [ ] No `datetime.now()` in `edge_scanner.py`

---

### E7-S3d: Refactor risk_manager.py for clock injection

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S3c  
**Blocks:** E7-S4b

**Description:**  
Update `RiskManager` to accept `clock` in `__init__` and use it for all time-based checks (staleness timestamps, settlement proximity).

**Acceptance criteria:**
- [ ] `RiskManager(clock, cache, db)` constructor
- [ ] All time comparisons (staleness, proximity) use `clock.now()`
- [ ] Existing risk manager unit tests pass with `SimulationClock` injected
- [ ] Integration test: `RiskManager` with `SimulationClock` at a fixed `t` → staleness rule fires correctly when METAR timestamp is `t - 91 minutes`

---

### E7-S3e: Regression test suite with SimulationClock

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S3d  
**Blocks:** None

**Description:**  
Run the full production unit test suite with `SimulationClock` injected everywhere. Any test failures indicate a wall-clock dependency that slipped through.

**Acceptance criteria:**
- [ ] `make test-with-sim-clock` runs full test suite with `SimulationClock` at a fixed timestamp
- [ ] Zero test failures
- [ ] Any found wall-clock dependencies fixed before this story is closed
- [ ] `make test-with-sim-clock` added to CI pipeline

---

### E7-S4a: FillResult dataclass and FillSimulator interface

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S2b  
**Blocks:** E7-S4b

**Description:**  
Define the `FillResult` dataclass and `FillSimulator` interface. No implementation logic — just the contract that the fill simulator must satisfy.

**Acceptance criteria:**
- [ ] `FillResult` in `src/meteoedge/backtest/fill_simulator.py`: `filled_quantity`, `avg_fill_price_cents`, `fees_cents`, `unfilled_quantity`, `fill_events: list[tuple[datetime, int, int]]`
- [ ] `FillSimulator` class stub with `simulate_market_order` and `simulate_limit_order` methods that raise `NotImplementedError`
- [ ] Type annotations complete; mypy clean

---

### E7-S4b: Taker order fill logic (ladder walk + partial fill)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S4a, E7-S3d  
**Blocks:** E7-S5a

**Description:**  
Implement `simulate_market_order`: walk the opposing order book ladder from best price, consuming levels until quantity is filled or ladder exhausted.

**Algorithm:**
```
1. Get orderbook at clock.now() from TimeGatedDataSource
2. Walk opposing side levels (price, size) from best to worst
3. For each level: fill min(remaining_qty, level_size) at level_price
4. If ladder exhausted before quantity: remainder is unfilled_quantity
5. Compute fees via fee_model.fee_cents on total filled quantity
```

**Acceptance criteria:**
- [ ] `simulate_market_order(ticker, side, action, quantity, clock) -> FillResult` implemented
- [ ] Partial fill: `unfilled_quantity > 0` when ladder insufficient
- [ ] `fill_events` list populated with `(clock.now(), qty, price)` per level consumed
- [ ] Fees computed on total filled quantity using E3-S3 fee model
- [ ] Unit tests: full fill, partial fill (ladder exhausted), zero liquidity (fill=0)

---

### E7-S5a: BacktestPortfolio data structure and record_fill

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S4b  
**Blocks:** E7-S5b

**Description:**  
`BacktestPortfolio` class tracking cash, positions, trade log, and daily snapshots. `record_fill` updates position avg cost via weighted average.

**Acceptance criteria:**
- [ ] `BacktestPortfolio(starting_cash_cents)` with: `cash`, `positions: dict[(ticker, side), Position]`, `trade_log: list[Trade]`, `daily_snapshots: list[DailySnapshot]`
- [ ] `record_fill(fill, ticker, side, action)` updates position using weighted avg cost formula
- [ ] Long trade (BUY): adds to position, increases qty, updates avg cost
- [ ] Unit tests: first fill (creates position), second fill same ticker (weighted avg verified to 4 decimal places), sell reduces position

---

### E7-S5b: settle_position (close position, realize P&L)

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S5a  
**Blocks:** E7-S5c

**Description:**  
Close a position at settlement: compute realized P&L based on whether the contract won or lost.

**Formula:**
```
if YES side won:  realized_pnl = (100 - avg_cost_cents) * quantity - fees_paid
if YES side lost: realized_pnl = -avg_cost_cents * quantity - fees_paid
(inverted for NO side)
```

**Acceptance criteria:**
- [ ] `settle_position(ticker, side, yes_won: bool, portfolio)` moves position to `trade_log` with P&L
- [ ] Removes settled position from `portfolio.positions`
- [ ] Unit tests: YES win, YES loss, NO win, NO loss — all P&L values verified by hand

---

### E7-S5c: settle_yesterday trigger

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S5b  
**Blocks:** E7-S5d

**Description:**  
`settle_yesterday` resolves all positions whose `trade_date` is yesterday against the official high from `TimeGatedDataSource`. Settlement modeled at 09:00 local on trade_date+1.

**Acceptance criteria:**
- [ ] `settle_yesterday(portfolio, data_source, settlement_date, clock)` as per spec §6.7
- [ ] Queries `data_source.official_high(station, settlement_date)` — time-gated, no future leakage
- [ ] Settlement time: 09:00 local on `trade_date + 1 day`; clock must be past that time
- [ ] Positions for non-yesterday dates untouched
- [ ] Unit test: two positions (yesterday + today); only yesterday's settled; P&L correct

---

### E7-S5d: End-to-end smoke test and reconciliation test

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S5c  
**Blocks:** E7-S6

**Description:**  
Two tests that together confirm the full harness pipeline works. These are the "harness is alive" milestone tests.

**Test 1 — Smoke test:** Run a 1-day backtest end-to-end: load Parquet → tick clock → engine decisions → fill → portfolio → settlement → produce non-empty summary. Zero exceptions.

**Test 2 — Reconciliation test:** Hand-craft 3 known trades (1 YES win, 1 YES loss, 1 NO win). Run through fill sim + portfolio + settlement. Assert final P&L to the cent.

**Acceptance criteria:**
- [ ] Smoke test produces ≥1 evaluated decision without error (any result acceptable at this stage)
- [ ] Reconciliation test: computed final P&L matches manually calculated expected P&L exactly
- [ ] Both tests committed to `tests/backtest/test_smoke.py`
- [ ] Both run under `make test` (no special flags required)

---

### E7-S6: Summary metrics and CSV/JSON output

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S5d  
**Blocks:** E7-S11, E7-S12

**Description:**  
Compute and output all metrics from spec §6.8. Implement Stage 1 exit criteria evaluation function.

**Acceptance criteria:**
- [ ] Per-trade CSV, per-day CSV, summary JSON with all metrics from spec §6.8
- [ ] `is_stage1_pass(summary) -> bool` evaluates all 8 exit criteria from spec §7
- [ ] Sharpe: `(daily_mean / daily_stdev) * sqrt(252)`; Sortino: uses downside deviation only
- [ ] Max drawdown: peak-to-trough as % of starting capital
- [ ] Per-station, per-bracket-distance, per-time-of-day attribution in summary JSON
- [ ] Unit tests: Sharpe formula, drawdown formula, all 8 criteria boundaries (pass/fail cases)

---

### E7-S7: Historical data ingestion (Parquet population)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S1, E2-S5  
**Blocks:** E7-S8

**Description:**  
Ingest 90+ days from all four sources into Parquet event store. `make ingest` runs all scripts.

**Acceptance criteria:**
- [ ] Kalshi: 90-day window → `market_snapshots/` Parquet
- [ ] METAR: IEM ASOS bulk CSV → `metar/` Parquet (all 5 stations)
- [ ] Forecasts: NWS NDFD or 06:00 daily snapshot → `forecasts/` Parquet
- [ ] Settlements: NWS F6 climate products → `settlements/` Parquet
- [ ] All scripts idempotent
- [ ] Coverage check: flag days with <90% expected snapshot coverage
- [ ] Cross-check: exclude settlement days where NWS official high and METAR 24h max disagree by >1°F

---

### E7-S8: Fee model — versioned historical application

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E3-S3, E7-S7  
**Blocks:** E7-S9a

**Description:**  
Apply versioned fee schedule to the backtest. `fee_cents(price, quantity, date)` selects the correct version by date.

**Acceptance criteria:**
- [ ] `fee_schedule_versions.json` populated with correct version(s) for the 90-day backtest window
- [ ] `fee_cents(price_cents, quantity, as_of_date)` selects version by `effective_from`/`effective_to`
- [ ] PR description documents % P&L change vs spike fee approximation
- [ ] Unit tests: version selection by date, boundary dates, gap between versions falls back to previous

---

### E7-S9a: Clock-advance execution latency

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S4b  
**Blocks:** E7-S9b

**Description:**  
Upgrade fill simulator: advance clock by `execution_latency` before fetching the fill book. Decision made at `t`; fill executed against book at `t + latency`.

**Acceptance criteria:**
- [ ] `FillSimulator.__init__` gains `execution_latency: timedelta = timedelta(milliseconds=250)`
- [ ] `simulate_market_order`: advances `clock._now` by `execution_latency` before calling `data_source.latest_orderbook`
- [ ] Clock advancement is local to the fill call — does not affect the global simulation clock
- [ ] Unit test: mock two orderbook snapshots at `t` and `t+250ms`; verify fill uses `t+250ms` book
- [ ] Configurable for parameter sweeps

---

### E7-S9b: Conservative limit-order fill (future book)

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S9a  
**Blocks:** E7-S9c

**Description:**  
`simulate_limit_order`: rest in book until opposing side reaches our price. Conservative: assume historical liquidity consumed before us — only fill if opposing size exceeds our quantity.

**Algorithm:**
```
1. Advance clock by execution_latency; fetch book
2. Walk forward in event stream until: (a) opposing side reaches our limit price, (b) market closes
3. At each future snapshot where opposing side reaches price: fill only if opposing_size > our_quantity
4. If market closes without fill: unfilled_quantity = quantity
```

**Acceptance criteria:**
- [ ] `simulate_limit_order(ticker, side, action, price_cents, quantity, ttl, clock) -> FillResult`
- [ ] Conservative fill: opposing size must strictly exceed our quantity to count as filled
- [ ] Market close without fill: returns `FillResult(filled_quantity=0, unfilled_quantity=quantity)`
- [ ] Unit tests: immediate cross (taker-equivalent), resting fill (walk forward), no fill (market closes)

---

### E7-S9c: Market order slippage model

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S9b  
**Blocks:** E7-S10

**Description:**  
Add configurable `slippage_bps` to market order fill: each ladder level price increased by `slippage_bps * price / 10000` before fill.

**Acceptance criteria:**
- [ ] `FillSimulator.__init__` gains `slippage_bps: int = 0`
- [ ] Applied per level: `effective_price = level_price * (1 + slippage_bps / 10000)`
- [ ] `slippage_bps=0` produces identical results to pre-slippage implementation
- [ ] Unit tests: `slippage_bps=100` on 50¢ level produces `50.5¢` effective price

---

### E7-S10: Slippage and partial fill handling in sweep

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S9c  
**Blocks:** E7-S11

**Description:**  
Confirm slippage model and partial fill handling integrate correctly with portfolio tracker. Validate across the sweep grid values.

**Acceptance criteria:**
- [ ] Partial fill: filled portion recorded as a trade; remainder marked cancelled in portfolio
- [ ] Slippage sweep: run backtests at `slippage_bps ∈ [0, 25, 50, 100]` on training set; results show Sharpe degradation with slippage
- [ ] Stage 1 robustness threshold: Sharpe > 1.0 at `slippage_bps=25`
- [ ] Unit test: partial fill + slippage combined — avg fill price correct

---

### E7-S11: Parameter sweep harness

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S6, E7-S10  
**Blocks:** E7-S12, E7-S13a

**Description:**  
Parallel parameter sweep on training set using `multiprocessing.Pool(3)`. Conservative config selection.

**Grid (from spec §6.9):**
```
MIN_EDGE_CENTS ∈ [2, 3, 4, 5, 7]
MIN_CONFIDENCE ∈ [0.70, 0.75, 0.80, 0.85, 0.90]
latency_ms ∈ [100, 250, 500, 1000]
```

**Acceptance criteria:**
- [ ] `run_single_backtest(config: StrategyConfig) -> SummaryMetrics` is a pure function (no shared state)
- [ ] `multiprocessing.Pool(3)` for parallelism
- [ ] Results written to `data/sweep_results.csv` (all config columns + all metric columns)
- [ ] `make sweep` runs on training set only
- [ ] `scripts/select_config.py`: picks config with best worst-case Sharpe across latency variants (not peak Sharpe at one setting)

---

### E7-S12: Reporting — plots and per-segment attribution

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S11  
**Blocks:** E7-S13a

**Description:**  
Matplotlib plots per spec §6.8 saved as PNG. `make report` generates all from latest backtest results.

**Acceptance criteria:**
- [ ] Plots saved to `data/backtest_reports/{run_date}/`: cumulative P&L, daily P&L histogram, rolling 7-day Sharpe, per-station cumulative P&L
- [ ] `make report` generates all plots
- [ ] Unit test: all plot functions run without error on synthetic 1-month 20-trade dataset

---

### E7-S13a: Future-data injection test

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S2c, E7-S11  
**Blocks:** E7-S13b

**Description:**  
Deliberately insert future data and verify `TimeGatedDataSource` returns zero rows. This test is the primary proof the firewall works.

**Test procedure:**
```
1. Set clock.now() = T
2. Insert a market snapshot with captured_at = T + 30s
3. Call data_source.latest_orderbook(ticker) at clock T
4. Assert: 0 rows returned (the future row is invisible)
```

**Acceptance criteria:**
- [ ] Test in `tests/backtest/test_leakage.py`
- [ ] Test covers all four `TimeGatedDataSource` methods (metars, forecast, orderbook, official_high)
- [ ] All four pass
- [ ] Runs in CI on every push

---

### E7-S13b: Missing-filter regression test

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E7-S13a  
**Blocks:** E7-S13c

**Description:**  
Verify the leakage tests themselves are effective: temporarily remove the `captured_at` filter from one method, confirm the leakage test now FAILS. This proves the tests aren't vacuously passing.

**Acceptance criteria:**
- [ ] A test marked `@pytest.mark.xfail` that removes the `captured_at` filter and asserts the future row IS returned — `xfail` because it's expected to fail on the broken implementation
- [ ] On correct implementation: `xfail` test is XPASS (unexpectedly passes) — which would indicate the leakage is not being caught, so it's also marked `strict=False`
- [ ] Documented in `docs/backtest-leakage-audit.md` with explanation of the methodology

---

### E7-S13c: Production datetime.now() CI check

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S3a  
**Blocks:** E7-S13d

**Description:**  
Confirm the CI grep check from E7-S3a is in the pipeline and blocks the build if `datetime.now()` appears in `src/`. This is already set up in E7-S3a — this story just validates and documents it.

**Acceptance criteria:**
- [ ] `make check-clock-usage` fails the build if any match found in `src/`
- [ ] CI config (GitHub Actions or equivalent) includes `make check-clock-usage` as a required step
- [ ] Documented in `docs/backtest-leakage-audit.md`

---

### E7-S13d: Leakage audit documentation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E7-S13b, E7-S13c  
**Blocks:** E7-S14

**Description:**  
Document the full leakage audit: what vectors were tested, how, results, and what would happen if each guard were removed.

**Acceptance criteria:**
- [ ] `docs/backtest-leakage-audit.md` covers: forward-query test, missing-filter regression, datetime.now() grep check
- [ ] Each section: what it tests, how it catches leakage, current status (passing/failing)
- [ ] "What done looks like" section mirrors spec §12 checklist

---

### E7-S14: Out-of-sample gate run (Stage 1 exit)

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Operator (André)  
**Depends on:** E7-S12, E7-S13d  
**Blocks:** Epic 10

**Description:**  
Run `make gate CONFIG=final.json` on the held-out 20% exactly once. Report against all 8 Stage 1 exit criteria. One-shot, no retuning after this.

**Acceptance criteria:**
- [ ] Results written to `data/stage1_gate_result.json`
- [ ] All 8 exit criteria evaluated: total trades ≥100, win rate >55%, avg EV >8¢, Sharpe >1.5, max drawdown <20%, max losing days ≤5, Sharpe@500ms >1.0, Sharpe@25bps_slippage >1.0
- [ ] **If ALL pass:** open PR changing `TRADING_ENABLED` comment to "Ready for Stage 2" in env template
- [ ] **If any fail:** close Epic 7, write `docs/stage1-gate-failure.md`, halt all work pending spec revision

---

## Epic 8 — Operational Tooling

**Goal:** Full operator visibility into positions, live edges, system health, and risk events. Daily P&L email. Automated alerting.  
**Prerequisites:** Epic 6, Epic 1-S6.  
**Designer needed:** Yes (E8-S1 through E8-S4).

---

### E8-S1: Dashboard — positions and live P&L view

**Complexity:** Mid  
**Designer needed:** **Yes**  
**Assigned to:** Mid Dev  
**Depends on:** E1-S6, E6-S4c  
**Blocks:** None

**Description:**  
Dashboard widget showing current open positions, avg cost, unrealized and realized P&L. HTMX auto-refresh every 30s.

**Acceptance criteria:**
- [ ] Designer spec in `docs/design/dashboard.md` includes positions widget layout
- [ ] Shows: ticker, side, quantity, avg cost, current market price, unrealized P&L
- [ ] Today's realized P&L shown as summary total
- [ ] Empty state: "No open positions" message
- [ ] Data served via FastAPI endpoint — no raw DB queries in templates
- [ ] Designer reviews and approves PR before merge

---

### E8-S2: Dashboard — live edges and opportunity feed

**Complexity:** Mid  
**Designer needed:** **Yes**  
**Assigned to:** Mid Dev  
**Depends on:** E1-S6, E3-S4b  
**Blocks:** None

**Description:**  
Table of recent edge scan results: all evaluated brackets, computed probabilities, edges, and filter outcomes.

**Acceptance criteria:**
- [ ] Table: ticker, bracket, P(Yes), edge_cents, filter_result (color-coded: green/amber/red)
- [ ] Sortable by edge (descending) and station
- [ ] Shows "last scan at: {timestamp}" to verify engine is alive
- [ ] Refreshes every 30s; Designer approves PR before merge

---

### E8-S3: Dashboard — system health view

**Complexity:** Mid  
**Designer needed:** **Yes**  
**Assigned to:** Mid Dev  
**Depends on:** E1-S6, E2-S1, E2-S2, E2-S3  
**Blocks:** None

**Description:**  
Panel showing poller freshness, engine status, Redis/DB connectivity, and halt status with prominent alert banner.

**Acceptance criteria:**
- [ ] Poller status: last fetched timestamps for METAR, NWS, Kalshi; staleness color indicators
- [ ] Engine status: last tick, trades placed today, LLM calls today
- [ ] Halt status: red banner displayed if `halt:global` set, including reason
- [ ] DB and Redis ping indicators; refreshes every 10s; Designer approves PR before merge

---

### E8-S4: Dashboard — risk events log

**Complexity:** Mid  
**Designer needed:** **Yes**  
**Assigned to:** Mid Dev  
**Depends on:** E1-S6, E4-S5  
**Blocks:** None

**Description:**  
Table of recent risk events with severity indicators and one-click acknowledge for HALTs.

**Acceptance criteria:**
- [ ] Table: occurred_at, event_type, severity, details summary, acknowledged status
- [ ] Unacknowledged HALT: red banner alert
- [ ] `POST /risk-events/{id}/acknowledge` sets `acknowledged_at`
- [ ] Filters: last 24h, unacknowledged only, all; refreshes every 30s; Designer approves PR before merge

---

### E8-S5: Daily P&L email report

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E6-S3b, E1-S4  
**Blocks:** None

**Description:**  
Daily P&L email at 23:00 UTC via `meteoedge-daily-report.service`. Covers: total trades, wins/losses, gross P&L, fees, net P&L, per-station breakdown.

**Acceptance criteria:**
- [ ] Template at `src/meteoedge/dashboard/templates/daily_report.txt`
- [ ] Sent via `smtplib` with TLS; errors logged but do not crash service
- [ ] Unit test: template rendering with sample data, SMTP mock

---

### E8-S6: Alerter (halts, anomalies, API errors)

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E4-S1e, E1-S5  
**Blocks:** E4-S3

**Description:**  
Email alerter for real-time critical events. Deduplicated (same event type alerts at most once per hour).

**Acceptance criteria:**
- [ ] `Alerter.send(subject, body, severity)` sends email immediately
- [ ] Triggers: HALT (any cause), daily floor breach, reconciliation discrepancy, API error rate >10%, LLM cap hit
- [ ] Redis TTL-based dedup: same `event_type` suppressed for 1 hour after first alert
- [ ] Alerter errors logged but never propagate; unit tests: dedup logic, SMTP mock

---

### E8-S7: Operator runbook documentation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E4-S3, E6-S5  
**Blocks:** Epic 10

**Description:**  
`docs/runbook.md` covering all operational procedures: startup, halt, resume, emergency liquidation, incident response, monthly fee reverification, quarterly re-backtest.

**Acceptance criteria:**
- [ ] Each procedure: pre-conditions, step-by-step commands, expected output, how to verify success
- [ ] Monthly fee reverification and quarterly re-backtest procedures documented
- [ ] Demo-prod divergence >20% response procedure documented
- [ ] Reviewed and signed off by operator before closing the story

---

## Epic 9 — Compliance & Security

**Goal:** Secrets management, network allowlist, audit logging, log retention. Parallel workstream with Epic 1.  
**Prerequisites:** Epic 1-S1.  
**Designer needed:** No.

---

### E9-S1: Secrets management and rotation procedure

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E1-S1  
**Blocks:** E9-S4

**Description:**  
`/etc/meteoedge/env` with correct permissions. Rotation procedure documented for each secret type.

**Acceptance criteria:**
- [ ] `/etc/meteoedge/env` mode `0600` owner `meteoedge`; `/etc/meteoedge/kalshi_private.pem` mode `0400`
- [ ] All env var templates from spec §10.3 present (values redacted in repo)
- [ ] `.gitignore` confirms `/etc/meteoedge/` never committed
- [ ] `docs/secrets-rotation.md`: rotation steps for Kalshi key, LLM API key, SMTP password
- [ ] Audit: `git log --all --full-history -- "*.pem" "*/env"` returns nothing

---

### E9-S2: Outbound network allowlist

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E1-S1  
**Blocks:** None

**Description:**  
UFW firewall configured to allow only the specific hosts required by the system. Provider-specific (only the active LLM provider's endpoint is opened).

**Acceptance criteria:**
- [ ] UFW enabled with default deny-outbound
- [ ] Allow: active LLM provider endpoint, `api.elections.kalshi.com`, `demo-api.kalshi.co`, `aviationweather.gov`, `api.weather.gov`, SMTP host
- [ ] Dashboard: `INPUT 127.0.0.1:8080` allowed; no external exposure
- [ ] Test: `curl https://google.com` from `meteoedge` user → blocked

---

### E9-S3: Audit logging for order placements

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S2b  
**Blocks:** None

**Description:**  
Every order placement attempt logged with enough information for a full end-to-end audit trail.

**Acceptance criteria:**
- [ ] Every `POST /portfolio/orders` attempt logged to `execution.jsonl`: `timestamp`, `decision_id`, `payload_hash` (SHA-256 of request body pre-signing), `api_response_status`, `external_order_id`, `outcome`
- [ ] Every cancellation logged: `timestamp`, `external_order_id`, `reason`
- [ ] `scripts/audit_sample.py`: randomly selects 10 trades, traces signal → settlement, prints summary
- [ ] Unit test: hash computation, log record completeness

---

### E9-S4: Monthly fee schedule reverification job

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E3-S3, E9-S1  
**Blocks:** None

**Description:**  
Monthly scheduled job comparing current Kalshi fee schedule against cached version. Alert on >10% divergence.

**Acceptance criteria:**
- [ ] `scripts/verify_fee_schedule.py` computes current fee formula and compares against `data/fee_schedule_v*.json`
- [ ] Divergence >10%: email alert + `risk_event` record; no divergence: INFO log + `last_verified_at` updated
- [ ] Monthly systemd timer: `meteoedge-fee-verify.service`
- [ ] Unit test: divergence detection logic

---

### E9-S5: Log rotation and archival

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E1-S5  
**Blocks:** None

**Description:**  
Logrotate for all MeteoEdge logs: 30-day local retention, weekly gzipped archive.

**Acceptance criteria:**
- [ ] `/etc/logrotate.d/meteoedge`: daily rotation, compress, delaycompress, 30-day retention
- [ ] `scripts/archive_logs.py` copies to `/var/log/meteoedge/archive/YYYY-WW/`; weekly systemd timer
- [ ] Test: `logrotate -f` force rotate, verify new files created and old files compressed

---

## Epic 10 — Production Rollout

**Goal:** Staged go-live through demo → micro-live → scale-up.  
**Prerequisites:** Epic 7 Stage 1 gate pass.  
**Designer needed:** No.

---

### E10-S1: Stage 2 demo run setup

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E6-S6, E8-S7, E7-S14  
**Blocks:** E10-S2

**Description:**  
Configure and launch full system against Kalshi demo API. Run 14 consecutive days. Deliberately trigger every risk rule at least once.

**Acceptance criteria:**
- [ ] `KALSHI_ENV=demo`, `TRADING_ENABLED=true` in demo env
- [ ] Stage 2 test plan documented: which risk rules to trigger and how
- [ ] All 6 systemd services healthy for 14 consecutive days
- [ ] Each risk rule triggered at least once with clean recovery documented
- [ ] Zero unhandled Python exceptions across 14 days

---

### E10-S2: Stage 2 exit criteria validation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Operator (André)  
**Depends on:** E10-S1  
**Blocks:** E10-S3

**Description:**  
Operator validates all Stage 2 exit criteria before proceeding to live trading.

**Acceptance criteria:**
- [ ] Zero unhandled exceptions in 14 consecutive days
- [ ] All risk halts triggered correctly and recovered cleanly
- [ ] Daily P&L attribution reconciles to within €0.50 of Kalshi's reported P&L per day
- [ ] Operator can answer "what happened yesterday?" from dashboard alone (no DB queries)
- [ ] Operator sign-off committed to `docs/stage2-signoff.md`

---

### E10-S3: Stage 3 micro-live setup

**Complexity:** Mid  
**Designer needed:** No  
**Assigned to:** Mid Dev  
**Depends on:** E10-S2  
**Blocks:** E10-S4

**Description:**  
Switch to production API with micro-live position limits. Run demo and prod in parallel on identical signals.

**Acceptance criteria:**
- [ ] `KALSHI_ENV=prod`; position size capped at 5-10 contracts; total bankroll on platform ≤ €100
- [ ] Separate demo instance running same code and signals for comparison
- [ ] Weekly comparison report: live P&L vs demo P&L (target: within ±15%)
- [ ] At least 20 live trades manually reviewed end-to-end by operator

---

### E10-S4: Stage 3 exit criteria validation

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Operator (André)  
**Depends on:** E10-S3  
**Blocks:** E10-S5

**Description:**  
Operator validates Stage 3 exit criteria after 4 weeks.

**Acceptance criteria:**
- [ ] Live P&L tracks demo P&L within ±15% over 4-week period
- [ ] No unexpected order rejections or API edge cases
- [ ] ≥20 live trades manually reviewed end-to-end
- [ ] Operator sign-off committed to `docs/stage3-signoff.md`

---

### E10-S5: Stage 4 scale-up checklist and weekly attribution

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E10-S4  
**Blocks:** None

**Description:**  
Stage 4 graduation rules documented. Weekly P&L attribution script identifying bottom-quintile sub-strategies.

**Acceptance criteria:**
- [ ] `docs/stage4-rules.md` with all graduation rules from spec §11 Stage 4
- [ ] `scripts/weekly_attribution.py` computes P&L by city, bracket range, time-of-day; flags bottom quintile
- [ ] Monthly re-backtest checklist in `docs/monthly-revalidation.md`

---

### E10-S6: Post-mortem and retrospective templates

**Complexity:** Easy  
**Designer needed:** No  
**Assigned to:** Junior Dev  
**Depends on:** E10-S4  
**Blocks:** None

**Description:**  
Templates for incident post-mortems and quarterly strategy retrospectives.

**Acceptance criteria:**
- [ ] `docs/templates/incident-postmortem.md`: what happened, timeline, root cause, fix, prevention
- [ ] `docs/templates/quarterly-retro.md`: metrics review, top/bottom sub-strategies, parameter retune results, go/no-go
- [ ] Both referenced in the runbook (E8-S7)

---

## Summary Table

| Epic | Title | Stories | Designer | Blocks |
|---|---|---|---|---|
| E0 | MVP Spike | 5 | No | All epics |
| E1 | Infrastructure | 6 | Yes (E1-S6) | E2, E8 |
| E2 | Data Ingestion | 8 | No | E3 |
| E3 | Strategy Engine | 13 | No | E4, E5, E7 |
| E4 | Risk Management | 9 | No | E5, E6 |
| E5 | LLM Sanity Check | 8 | No | E6 |
| E6 | Order Execution | 13 | No | E8 |
| E7 | Backtest Harness | 30 | No | E10 |
| E8 | Operational Tooling | 7 | Yes (E8-S1–S4) | — |
| E9 | Security | 5 | No | E10 |
| E10 | Rollout | 6 | No | — |
| **Total** | | **110 stories** | | |

---

## Decomposition rationale

The 14 originally Hard stories were each split into 2–5 Mid/Easy sub-stories using three techniques:

1. **Separate pure functions from I/O.** Algorithmic cores (EV formula, Kelly formula, p95 computation, P(Yes) branches) are tiny pure functions easy to spec completely. I/O, caching, and DB writes are separate stories.

2. **Separate interface from implementation.** Protocol/dataclass definitions (FillResult, LLMProvider, Clock) are Easy stories. Implementing them is Mid.

3. **Pre-specify the design.** When the Tech Lead writes the exact interface signature, formula, algorithm pseudocode, and test cases into the story, a Sonnet agent executes rather than designs. The "Hard" label was reflecting design work embedded in implementation stories — extracting that design into the story text eliminates the need for Opus-level reasoning during implementation.

---

## Open Questions (from spec §18 — must resolve before associated stories begin)

| # | Question | Blocks |
|---|---|---|
| 1 | Fee schedule canonical source (HTML, PDF, or reverse-engineer from fills?) | E3-S3, E9-S4 |
| 2 | Historical Kalshi data: 3-month native window vs DeltaBase vs self-scraping? | E7-S7 |
| 3 | Kalshi international tier: weather markets available to Portugal accounts? | E10-S1 |
| 4 | DST / local time: NWS Climate Report uses LST even during DST — dedicated test suite | E3-S2d |
| 5 | LLM model version pinning: schedule for deliberate model upgrades per provider | E5-S2, E5-S3, E5-S4 |

---

*End of implementation plan. 110 stories, 0 Opus-level tasks. Ready for GitHub issue creation.*
