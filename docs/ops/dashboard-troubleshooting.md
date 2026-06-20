# Dashboard Troubleshooting Guide

This guide helps operators diagnose and recover from missing or incorrect position rendering on the MeteoEdge dashboard.

## How to Diagnose a Missing Position

Follow these steps in order to identify why a position is not appearing on the dashboard.

### Step 1: Check the Wallet

**Objective:** Confirm that the position exists in Polymarket and the bot's wallet.

**Steps:**

1. Open the Polymarket web UI at https://polymarket.com
2. Log in to the wallet account (same account configured in the bot)
3. Go to **Portfolio** or **Open Orders**
4. Search for the expected question (e.g., "Chicago high 80-81°F")
5. Verify the position exists with the expected:
   - Token ID (visible in the advanced details or transaction view)
   - Number of shares held
   - Side (YES or NO)

**Expected outcome:** The token appears in the wallet with the expected shares and side.

**If missing from wallet:**
- The position was closed (sold, abandoned, or redeemed) before the dashboard query
- Check the market status: is it resolved? Has redemption closed?
- Go to Step 2 below to confirm the DB record

**If present in wallet:**
- Proceed to Step 2

---

### Step 2: Check the Database

**Objective:** Verify that the position is recorded in the `open_positions` table with correct enrichment.

**Steps:**

1. SSH to the production server and access the database:
   ```bash
   sqlite3 data/meteoedge.db
   ```

2. Query for the position by token_id:
   ```sql
   SELECT id, station, ticker, token_id, side, shares, entry_ts
   FROM open_positions
   WHERE token_id = '0x<token_id_from_wallet>';
   ```
   (Replace `0x<token_id_from_wallet>` with the actual token ID from Step 1)

3. Verify the result:
   - **Row exists:** Check that station, ticker, and side are correct
   - **No row found:** The position was not synced; proceed to Step 3

4. If a row exists, verify enrichment:
   ```sql
   SELECT station, bracket_low, bracket_high, side, entry_price
   FROM open_positions
   WHERE token_id = '0x<token_id>';
   ```

**Expected outcome:**
- station is not empty (e.g., "KORD", "RJTT", not "UNKNOWN")
- bracket_low and bracket_high are sensible temperature ranges (e.g., 80.0, 81.0)
- side is YES or NO
- entry_price is between 1 and 99 (cents)

**If enrichment is missing or wrong:**
- The position was inserted but not enriched correctly
- The `ticker` or `trades` table entry may be malformed
- Go to Step 4 below

**If the row doesn't exist:**
- sync_open_orders did not insert the position into the DB
- The wallet has the token, but the bot doesn't know about it
- Proceed to Step 3

---

### Step 3: Check the JSONL Audit Trail

**Objective:** Verify that the position was recorded in the live_trades JSONL for historical context and replay.

**Steps:**

1. Check the live_trades JSONL for today's date:
   ```bash
   grep "\"token_id\": \"0x<token_id>\"" logs/live_trades.$(date +%Y-%m-%d).jsonl
   ```

2. Look for a `sync_open_orders` entry with the token_id:
   ```bash
   tail -n 50 logs/live_trades.$(date +%Y-%m-%d).jsonl | grep -A 5 -B 5 "0x<token_id>"
   ```

3. Verify the entry contains:
   - `"event": "sync_open_orders"` (or `"sync"`, depending on the logging version)
   - `"token_id": "0x<token_id>"`
   - `"station": "<STATION>"` (e.g., "KORD")
   - `"shares": <number>`
   - `"reconciled_at": "<timestamp>"` (indicates the sync completed)

**Expected outcome:** One or more entries for the token_id in the JSONL, with enrichment data.

**If the entry exists in JSONL but not in the DB:**
- The JSONL was written but the DB insert failed
- This usually indicates a database constraint violation or a schema mismatch
- Check the bot.log for error messages:
  ```bash
  grep -i "error\|exception\|insert" logs/bot.log | tail -n 50
  ```

**If the entry is missing from JSONL:**
- sync_open_orders did not run or did not discover the token
- Check if the bot is running:
  ```bash
  systemctl status meteoedge.service
  ```
- Check if the polling cycle is executing:
  ```bash
  tail -n 100 logs/bot.log | grep "poll\|cycle"
  ```

---

### Step 4: Check the Trades Table

**Objective:** Verify that the position has a corresponding trade record with correct enrichment.

**Steps:**

1. Find the trade that created the position:
   ```bash
   sqlite3 data/meteoedge.db <<EOF
   SELECT id, station, ticker, side, outcome, pnl, ts
   FROM trades
   WHERE ticker LIKE '%<part_of_question_name>%'
     AND ts > datetime('now', '-1 day')
   ORDER BY ts DESC
   LIMIT 5;
   EOF
   ```
   (Look for the most recent trade for this question)

2. Or, if you know the open_positions.trade_id, query directly:
   ```bash
   sqlite3 data/meteoedge.db <<EOF
   SELECT t.id, t.station, t.ticker, t.side, t.outcome, t.actual_price, op.shares
   FROM open_positions op
   JOIN trades t ON op.trade_id = t.id
   WHERE op.token_id = '0x<token_id>';
   EOF
   ```

3. Verify:
   - **station** is not empty
   - **ticker** matches the question text (or can be inferred from it)
   - **outcome** is empty or 'live' (not 'abandoned', 'timeout', or 'closed')
   - **actual_price** is between 1 and 99

**Expected outcome:** A single trade record with enrichment, linked to the open_positions row.

**If the trade record is missing:**
- The trade was never recorded in the DB
- The order_manager did not execute the trade, or the trade was placed manually outside the bot
- If manually placed: use Step 1 to confirm it exists in the wallet; the dashboard will render it with `station='UNKNOWN'`

**If enrichment is missing from the trade:**
- The trade was recorded but the station/ticker fields are empty
- This happens when a manual external trade is executed (not through the bot's signal pipeline)
- The dashboard will render the position with `station='UNKNOWN'` and empty brackets

---

## Common Failure Modes and Recovery

### Failure Mode 1: Position in Wallet but Not in DB

**Symptom:** The wallet shows 10 shares of a token, but `SELECT COUNT(*) FROM open_positions` shows 9 rows (one is missing).

**Root cause:** sync_open_orders failed or was not called.

**Recovery:**
1. Verify the bot is running:
   ```bash
   systemctl status meteoedge.service
   ```
2. If not running, start it:
   ```bash
   sudo systemctl start meteoedge.service
   ```
3. Wait 2 poll cycles (120 seconds)
4. Verify the position was synced:
   ```bash
   sqlite3 data/meteoedge.db "SELECT COUNT(*) FROM open_positions;"
   ```

**If still missing after 2 cycles:**
1. Check the bot.log for errors:
   ```bash
   tail -n 200 logs/bot.log | grep -i "error\|exception\|sync_open_orders"
   ```
2. If the error is a database lock or constraint violation, restart the bot:
   ```bash
   sudo systemctl restart meteoedge.service
   ```
3. If the error persists, contact the tech lead

---

### Failure Mode 2: Position in DB But Not on Dashboard

**Symptom:** The DB has the row, but the dashboard `/api/portfolio` endpoint does not list it.

**Root cause:** The dashboard's wallet fetch is stale or the enrichment is missing.

**Recovery:**
1. Clear the dashboard cache:
   ```bash
   # If the dashboard is running in a separate process
   pkill -f "uvicorn src.dashboard.api"
   sleep 2
   uvicorn src.dashboard.api:app --port 8000 &
   ```
2. Refresh the dashboard in your browser (hard refresh: Ctrl+Shift+R or Cmd+Shift+R)
3. Check the dashboard logs for errors:
   ```bash
   # If logged to a file
   tail -n 50 logs/dashboard.log | grep -i "error\|exception\|portfolio"
   ```

**If still missing:**
1. Verify the Polymarket API is accessible:
   ```bash
   curl -s "https://data-api.polymarket.com/positions?user=$POLYMARKET_DEPOSIT_WALLET&limit=1" | jq '.' | head -20
   ```
2. If the API returns an error, the Polymarket service may be down
3. Check the dashboard code for the enrichment fallback:
   - The dashboard reads enrichment from `open_positions` (primary) and `live_state.json` (optional fallback)
   - If `live_state.json` is stale, enrichment may be missing; check the file's mtime:
     ```bash
     ls -la logs/live_state.json
     ```

---

### Failure Mode 3: Stale Position (In DB But Closed in Wallet)

**Symptom:** The DB has a row, the wallet is empty (position was redeemed or sold), but the dashboard still shows it.

**Root cause:** reconcile_wallet_to_db did not run or did not detect the closure.

**Recovery:**
1. Manually remove the stale row:
   ```bash
   sqlite3 data/meteoedge.db <<EOF
   DELETE FROM open_positions
   WHERE token_id = '0x<token_id>';
   EOF
   ```
2. Wait for the next poll cycle (60 seconds)
3. Verify the position is gone from the dashboard

**To prevent in future:**
- Ensure the bot is running continuously
- Check systemd service status daily:
  ```bash
  systemctl status meteoedge.service
  ```

---

### Failure Mode 4: Wallet Position with `station='UNKNOWN'`

**Symptom:** The dashboard shows a position with `station='UNKNOWN'`, `bracket_low=0.0`, `bracket_high=0.0`.

**Root cause:** The position was placed manually outside the bot (e.g., via the Polymarket mobile app), or the enrichment was not found in the trades table.

**Recovery:**
1. This is expected behavior for manual external trades
2. To avoid this, use the bot's signal pipeline to place trades
3. If enrichment is needed:
   - Manually insert a record into the trades table with the correct station/bracket, or
   - Mark the position for closure via the `/api/positions/{token_id}/sell` endpoint and re-trade via the bot

**Example: Manually add enrichment**
```bash
sqlite3 data/meteoedge.db <<EOF
UPDATE open_positions
SET station = 'KORD', bracket_low = 80.0, bracket_high = 81.0
WHERE token_id = '0x<token_id>';
EOF
```

---

## System Health Checks (Daily)

Run these checks daily to ensure the system is operating correctly:

### Check 1: Bot is Running

```bash
systemctl is-active meteoedge.service
# Expected: active
```

If not active, start it:
```bash
sudo systemctl start meteoedge.service
```

### Check 2: Dashboard is Running

```bash
curl -s http://localhost:8000/health | jq '.status'
# Expected: "ok"
```

If not responding, restart:
```bash
pkill -f "uvicorn src.dashboard.api" || true
sleep 2
nohup uvicorn src.dashboard.api:app --port 8000 > logs/dashboard.log 2>&1 &
```

### Check 3: Open Position Count is Consistent

```bash
# DB count
DB_COUNT=$(sqlite3 data/meteoedge.db "SELECT COUNT(*) FROM open_positions WHERE shares > 0.01;")
echo "DB open positions: $DB_COUNT"

# Wallet count (requires POLYMARKET_DEPOSIT_WALLET env var)
WALLET_COUNT=$(curl -s "https://data-api.polymarket.com/positions?user=$POLYMARKET_DEPOSIT_WALLET&limit=100" | jq '[.[] | select(.size > 0.01)] | length')
echo "Wallet open positions: $WALLET_COUNT"

# They should match
if [ "$DB_COUNT" -eq "$WALLET_COUNT" ]; then
    echo "✓ Counts match"
else
    echo "✗ Mismatch! DB=$DB_COUNT, Wallet=$WALLET_COUNT"
fi
```

### Check 4: Recent Bot Log Entries

```bash
tail -n 20 logs/bot.log | grep -E "poll|cycle|sync_open_orders|reconcile"
# Expected: Recent timestamps and cycle messages
```

### Check 5: No Excessive CRITICAL Logs

```bash
grep "CRITICAL" logs/bot.log | wc -l
# Expected: < 10 per hour
```

If excessive CRITICAL logs, check for stale observations:
```bash
grep "CRITICAL" logs/bot.log | head -n 5
```

---

## Emergency Recovery

### If the Bot Has Crashed

1. Restart the service:
   ```bash
   sudo systemctl restart meteoedge.service
   ```

2. Monitor the restart:
   ```bash
   journalctl -u meteoedge.service -f
   ```

3. If it fails to start, check the error:
   ```bash
   sudo systemctl status meteoedge.service
   ```

### If the Database Is Corrupted

1. Restore from backup (if available):
   ```bash
   cp data/meteoedge.db data/meteoedge.db.corrupted
   cp /backup/meteoedge.db.backup data/meteoedge.db
   ```

2. Restart the bot:
   ```bash
   sudo systemctl restart meteoedge.service
   ```

3. If no backup exists, reinitialize:
   ```bash
   rm data/meteoedge.db
   # Bot will recreate schema on next run
   sudo systemctl restart meteoedge.service
   ```

### If the Polymarket API Is Down

1. The bot will fail to fetch new positions and will log errors
2. Wait for Polymarket to recover (check their status page)
3. The bot will resume automatically

### If the JSONL Log Is Full

1. Check the disk space:
   ```bash
   df -h logs/
   ```

2. If nearly full, compress old JSONL files:
   ```bash
   gzip logs/live_trades.2026-06-*.jsonl
   ```

3. Monitoring will continue; the bot rotates logs automatically

---

## Contact and Escalation

- **Bot operational questions:** Check logs/bot.log and this guide
- **Persistent position sync failures:** Escalate to the Tech Lead PM with the output of Steps 1–4 above
- **Dashboard rendering issues:** Restart the dashboard and check logs/dashboard.log
- **Database corruption:** Contact the Tech Lead PM immediately

---

## References

- **Position lifecycle:** docs/design/open-positions-source-of-truth.md
- **DB schema:** docs/DB_SCHEMA.md
- **Deployment guide:** docs/OPERATIONS.md
- **API reference:** docs/API.md (if available)
