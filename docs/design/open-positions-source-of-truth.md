# Design Spec — DB `open_positions` as Single Source of Truth

**Epic:** #366 — Robust reconciliation and win-rate computation  
**Status:** Implementation complete; documented for operational clarity

---

## Overview

This document defines the architectural contract for position tracking, enrichment, and dashboard rendering across MeteoEdge after the completion of epic #366 (sprint 4). The key principle: **the `open_positions` database table is the single source of truth for active position state, enrichment (station, bracket, predicted_price), and performance metrics. The JSONL audit trail is write-only and is not consulted for the dashboard read path.**

---

## Architecture: Three-Layer Model

### Layer 1: Wallet (Polymarket CLOB)

The Polymarket Data API `/positions` endpoint returns the live orderbook state — token_id, shares, avg_price, redeemable flag, current_value.

**Properties:**
- Source of truth for share count and current mark-to-market value
- **Not** the source of truth for enrichment (station, bracket, predicted_price) — wallet has no knowledge of our bracket logic
- May be stale by seconds (eventual consistency with live orderbook)

### Layer 2: `open_positions` DB Table

SQLite table with schema:

```sql
CREATE TABLE open_positions (
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
```

**Properties:**
- Written by: order_manager.sync_open_orders() at start of each poll cycle
- Primary enrichment source for: station, bracket_low/bracket_high (inferred from ticker), side, entry_price
- Coupled to `trades` table for historical context
- Updated at each poll cycle; rows persist until position closes
- This table reflects the **canonical position state** the dashboard renders

**Invariants:**
1. Every token_id in open_positions has a corresponding row in the wallet
2. Every station/bracket pair in open_positions has a matching candidate from the signal generation step (or is a manual external trade)
3. The entry_price matches the actual_price from the corresponding trade record
4. shares value must not exceed the current wallet shares count (except during partial-fill tracking)

### Layer 3: JSONL Audit Trail

Two append-only JSONL files:

- **`logs/live_trades.YYYY-MM-DD.jsonl`**: One entry per trade execution (entry, partial fill, exit, reconciliation marker)
  - Records reconciliation checkpoints: sync_open_orders, reconcile_timeout_fills, reconcile_wallet_to_db
  - Enables replay and debugging of order lifecycle
  - Write-only; never read by the dashboard

- **`logs/snapshots.jsonl`**: One entry per model probability snapshot per bracket
  - Records p_yes probability for each (station, bracket_low, bracket_high) at poll time
  - Enables post-hoc analysis of edge quality
  - Used by the dashboard to populate `my_prob_now` field, but the enrichment (station, bracket) comes from open_positions

---

## Reconciliation Flow: Poll Sequence

The core poll loop in `run.py` executes this sequence once per 60-second cycle:

### Step 1: Sync Open Orders → DB

```python
order_manager.sync_open_orders(live_trader, db=db)
```

**What it does:**
- Fetches the current position wallet via Polymarket Data API
- For each active token in the wallet not yet in open_positions:
  - Inserts a new row into open_positions with token_id, shares, station, bracket, side, entry_price, entry_ts, stop_loss_cents, take_profit_cents
- JSONL checkpoint: records "sync_open_orders" with timestamp

**Invariants preserved:**
- Every wallet token is now in open_positions
- All enrichment (station, bracket) fields are populated from the signal or live_trades.jsonl history
- The open_positions count equals the wallet's active position count (post-sync)

### Step 2: Reconcile Timeout Fills → DB

```python
order_manager.reconcile_timeout_fills(ts, db=db)
```

**What it does:**
- Scans the trades table for orders marked `outcome='timeout'` (hung orders that never filled)
- For each timeout order where the token_id is now held in the wallet:
  - Updates the trade record: `outcome='filled'`; infers entry_price from wallet average cost
  - Updates the open_positions row: shares = wallet shares
- JSONL checkpoint: records "reconcile_timeout_fills" with count of patched orders

**Invariants preserved:**
- All timeout orders for held tokens are marked filled
- open_positions reflects the correct share count (wallet ground truth)
- No duplicate positions are created

### Step 3: Reconcile Wallet to DB → DB

```python
# Implicit in sync_open_orders; also triggered by manual close operations
# _reconcile_wallet_to_db() is called after any position closure
```

**What it does:**
- Compares wallet token list to open_positions table
- For each token in open_positions **not** in the wallet:
  - Marks the trade outcome (outcome='sold' if stop-loss/take-profit hit; outcome='abandoned' if it timed out)
  - Removes the row from open_positions
- For each token in the wallet not in open_positions:
  - Re-inserts via sync_open_orders (deduped)
- JSONL checkpoint: records "reconcile_wallet_to_db" with count of closed positions

**Invariants preserved:**
- open_positions contains **only** tokens that are currently in the wallet
- Every closed position has an outcome recorded in the trades table
- No ghost or orphaned positions remain

---

## Dashboard Read Path

### Endpoint: `GET /api/portfolio`

Returns `PositionOut` objects for each open position.

**Enrichment priority:**
1. **Station, bracket_low, bracket_high**: read from `open_positions` DB table
   - These are written at position entry and never change
2. **predicted_price (entry_price)**: read from `open_positions.entry_price`
3. **my_prob (current implied probability)**: read from the latest snapshots.jsonl entry for the (station, bracket_low, bracket_high) key
   - This is **optional** — may be None if no snapshot has been recorded yet
   - Do not fall back to JSONL live_trades enrichment for the dashboard
4. **Side (YES/NO)**: read from `open_positions.side`

**Fallback chain (do NOT use for the dashboard):**
- The internal function `_cached_live_trades()` provides a JSONL-sourced enrichment dict (station, bracket, predicted_price) for backward compatibility during **closed position reconciliation** only
- This fallback is **not** consulted by the dashboard's active position rendering

### Example PositionOut Structure

```json
{
  "token_id": "0x1234abc...",
  "question": "Will Tokyo's high on 2026-06-20 be between 80-81°F?",
  "station": "RJTT",
  "side": "NO",
  "bracket_low": 80.0,
  "bracket_high": 81.0,
  "entry_price": 45,
  "market_prob": 47,
  "my_prob": 45,
  "my_prob_now": 42,
  "edge": -2,
  "shares": 10.5,
  "invested": 4.73,
  "current_value": 4.97,
  "target_value": 10.5,
  "forecast_high_f": 80.5
}
```

---

## Win-Rate Computation

### Canonical Helper Function

Location: `src/dashboard/api.py::_compute_win_rate_canonical(filled: list[dict], wins: int) -> float | None`

**Definition:**
- Input: trades with outcome in ('filled', 'sold')
- Filter: exclude trades where pnl == 0.0 (neither win nor loss; settlement at entry point)
- Compute: wins / len(settled)
- Return: float in [0.0, 1.0] or None if settled list is empty

**Example:**
```python
settled = [
    {"outcome": "filled", "pnl": 1.5},    # win
    {"outcome": "filled", "pnl": 0.0},    # settled but not a win (pnl=0)
    {"outcome": "sold", "pnl": -0.8},     # loss
    {"outcome": "timeout", "pnl": -0.5},  # NOT settled (outcome != 'filled'/'sold')
]
# settled (after filter) = first 3 records
# wins = 1 (only the first record where pnl > 0)
# win_rate = 1 / 3 = 0.333
```

**Settled Definition:**
- outcome in ('filled', 'sold') **AND** pnl != 0.0
- pnl == 0.0 is considered settled (market resolved) but **not** a win (breakeven)

### Win-Rate Endpoints (Must All Agree)

Three endpoints must return the **same** win-rate number for the same filter criteria:

1. **`GET /api/status` → win_rate**
   - Computes over the last 50 settled trades (all stations, all modes except shadow)

2. **`GET /api/stations/{metar}/perf → {YES|NO}.win_rate**
   - Computes over all settled trades for the given station and side (all modes except shadow)

3. **`GET /api/stations/perf → {station}.{YES|NO}.win_rate**
   - Same as above, aggregated across all stations

**All three must call the same canonical helper and produce identical results for overlapping trade sets.**

---

## Edge Cases and Reconciliation Scenarios

### Edge Case 1: Manual External Trades

**Scenario:** User manually places a trade on Polymarket outside the bot (e.g., quick hedge via the mobile app).

**Behavior:**
- sync_open_orders detects a new token_id in the wallet not in open_positions
- Creates a new open_positions row with station='UNKNOWN', bracket_low=0, bracket_high=0 (or best-guess from question text)
- Dashboard renders "UNKNOWN" for the station and may omit bracket information
- Position is still tracked and can be closed via the API

**Invariant:** The position is not lost; enrichment may be degraded.

### Edge Case 2: Redemption-Window Ghosts

**Scenario:** A position's market resolves and the token becomes redeemable. The wallet still holds it (user has not yet clicked "redeem"), but reconcile_wallet_to_db does not remove it from open_positions because the token is still in the wallet.

**Behavior:**
- open_positions row persists until the token is removed from the wallet (manual redeem or auto-redemption event)
- The position is reported as open on the dashboard, even though the outcome is known
- Once redeemed, the token_id disappears from the wallet and the row is removed during the next reconcile_wallet_to_db cycle

**Invariant:** The position is not lost; dashboard shows it as open until redemption completes.

**Mitigation:** Operators should redeem positions promptly to keep the dashboard accurate.

### Edge Case 3: Partial Fills

**Scenario:** An order is placed for 10 shares; 7 shares fill immediately and 3 are held for later fill.

**Behavior:**
- First sync_open_orders: inserts open_positions row with shares=10, entry_ts=<initial>, entry_price=<avg of 7 fills>
- Subsequent poll cycles: if the held 3 shares are still in the wallet, reconcile_timeout_fills checks the trade outcome and may update shares if a fill was detected
- If the 3 shares expire (timeout), reconcile_timeout_fills still marks outcome='filled' (the 7 that did fill) and removes the row if wallet no longer holds the token

**Invariant:** The position is never left in a "partial" state in open_positions; it either reflects the current wallet holdings or is closed.

### Edge Case 4: Stop-Loss and Take-Profit Triggers

**Scenario:** A take-profit or stop-loss limit on an open_positions row triggers while the position is held.

**Behavior:**
- The order_manager or TP handler detects the trigger and submits a sell order
- The sell order fills (outcome='sold')
- reconcile_wallet_to_db removes the token from open_positions (no longer in wallet)
- Dashboard stops rendering the position; a closed position record is created with outcome='sold' and pnl calculated

**Invariant:** The position transition from open to closed is atomic from the dashboard's perspective.

---

## Operational Runbook: System Health Checks

### Check 1: Dashboard Renders Without Missing Enrichment

**Steps:**
1. Open the dashboard at http://localhost:8000/
2. Navigate to Portfolio tab
3. For each rendered open position:
   - Verify station is not empty (not "UNKNOWN")
   - Verify bracket_low and bracket_high are sensible (e.g., 75.0–85.0 for a temperature range)
   - Verify my_prob is between 1 and 99 (entry price)

**Success:** All positions show station, bracket, and entry_price.

**Failure modes:**
- Missing station → sync_open_orders did not enrich; check the trades table for the corresponding trade_id
- Missing bracket → ticker in open_positions is malformed; check live_trades.jsonl for the trade entry

### Check 2: `open_positions` Count Matches Wallet Count

**Steps:**
1. Query the database:
   ```bash
   sqlite3 data/meteoedge.db "SELECT COUNT(*) FROM open_positions WHERE shares > 0;"
   ```
   Let's call this DB_COUNT.

2. Fetch the wallet via Polymarket API:
   ```bash
   curl "https://data-api.polymarket.com/positions?user=$WALLET_ADDR&limit=100" | jq '[.[] | select(.size > 0.01)] | length'
   ```
   Let's call this WALLET_COUNT.

3. Compare:
   - DB_COUNT should equal WALLET_COUNT
   - If DB_COUNT > WALLET_COUNT: stale rows in open_positions (reconcile_wallet_to_db may have missed a closure)
   - If DB_COUNT < WALLET_COUNT: missing rows in open_positions (sync_open_orders may have failed)

**Success:** DB_COUNT == WALLET_COUNT.

**Recovery:**
- If DB_COUNT > WALLET_COUNT: delete stale rows manually:
  ```sql
  DELETE FROM open_positions WHERE token_id NOT IN (
    SELECT asset FROM <wallet_snapshot>
  );
  ```
- If DB_COUNT < WALLET_COUNT: run sync_open_orders again:
  ```bash
  python -c "
  from src.execution.order_manager import OrderManager
  from src.data.db import Database
  om = OrderManager(db=Database())
  from src.execution.live_trader import LiveTrader
  lt = LiveTrader(mode='live')
  om.sync_open_orders(lt, db=Database())
  "
  ```

### Check 3: Win-Rate Consistency

**Steps:**
1. Query `/api/status` for the portfolio win_rate
2. Query `/api/stations/{any-station}/perf` for the same station's win_rate
3. Manually compute from the trades table:
   ```sql
   SELECT
     COUNT(*) as total,
     SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
     ROUND(CAST(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*), 4) as win_rate
   FROM trades
   WHERE outcome IN ('filled', 'sold')
     AND pnl != 0.0
     AND mode != 'shadow';
   ```

**Success:** All three methods return the same win_rate (allowing for rounding to 4 decimals).

**Failure mode:**
- Endpoints return different values → canonical helper is not being used consistently; check dashboard/api.py for multiple win-rate computation paths

### Check 4: JSONL Audit Trail Is Complete

**Steps:**
1. Check that logs/live_trades.YYYY-MM-DD.jsonl contains recent entries:
   ```bash
   tail -n 5 logs/live_trades.$(date +%Y-%m-%d).jsonl | jq '.reconciled_at'
   ```
   You should see timestamps from the last few poll cycles.

2. For each open position, search the JSONL for a matching sync_open_orders entry:
   ```bash
   grep "\"token_id\": \"0x...\"" logs/live_trades.*.jsonl | tail -n 3
   ```
   You should see at least one entry per active position.

**Success:** Recent entries exist; reconciliation checkpoints are being logged.

**Failure mode:**
- No recent entries → logging is disabled or logs are being rotated too aggressively; check LOG_DIR and JSONL rotation config
- Missing sync_open_orders entries → order_manager is not writing to JSONL; check src/execution/order_manager.py for JSONL write calls

### Check 5: Freshness Monitor Is Not Emitting Excessive CRITICAL Logs

**Steps:**
1. Check the bot.log for CRITICAL lines in the last hour:
   ```bash
   grep CRITICAL logs/bot.log | tail -n 20 | grep -E "Stale observation|No observation data"
   ```

2. If more than 10 CRITICAL lines appeared in a 60-minute window, investigate:
   - Is the JMA AmeDAS API responding? (Check the first few lines for HTTP errors)
   - Has the fallback to Open-Meteo been triggered? (Check for "fallback" in logs)
   - Is the cadence correct? (Should be ~10-minute observations for JMA, ~60-minute for Open-Meteo)

**Success:** 0–3 CRITICAL lines per hour (expected: stale data on rare network glitches).

**Failure mode:**
- 10+ CRITICAL lines per hour → Either JMA is persistently 404'ing and Open-Meteo is slow, or the freshness thresholds are too aggressive; check src/data/freshness_monitor.py

---

## Summary: The Contract

| Component | Purpose | Source of Truth | Fallback | Invariant |
|---|---|---|---|---|
| Token ID, shares, current value | Wallet state | Polymarket CLOB | None | Always matches wallet count |
| Station, bracket, entry price | Position enrichment | `open_positions` DB table | JSONL (closed positions only) | Every active position is enriched |
| Win-rate (fraction of trades where pnl > 0) | Performance metric | trades table + canonical helper | Dashboard endpoints (all agree) | All three endpoints use the same helper |
| Audit trail (sync/reconcile checkpoints) | Replay & debugging | `logs/live_trades.*.jsonl` | None | Append-only; never read by dashboard |

**The golden rule:** The dashboard never reads from JSONL for active position enrichment. All enrichment comes from the `open_positions` DB table.
