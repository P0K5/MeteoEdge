# Operations Runbook

This document covers deployment, configuration, runtime modes, settlement processes, recovery procedures, and logging for MeteoEdge.

## Deployment

### Prerequisites

- Linux/Unix system with systemd
- Python 3.10+
- Virtual environment with dependencies installed (see `requirements.txt`)
- API credentials for Polymarket (L1 wallet key + L2 derived credentials for live mode)

### Installation

Run `deploy/systemd/install.sh` as root to install systemd units:

```bash
sudo deploy/systemd/install.sh
```

This script:
1. Disables the old meteoedge.timer if present
2. Installs three service units and one timer to `/etc/systemd/system/`
3. Reloads systemd and enables the bot, dashboard, and settlement timer
4. Displays current status

### Systemd Units

#### meteoedge.service
Runs the main polling loop in live or paper trading mode.

```ini
[Unit]
Description=MeteoEdge live trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=p0k5
WorkingDirectory=/home/p0k5/MeteoEdge
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/p0k5/MeteoEdge/.env
ExecStart=/home/p0k5/MeteoEdge/.venv/bin/python -u -m src.scripts.run --live
Restart=always
RestartSec=10s
StandardOutput=append:/home/p0k5/MeteoEdge/logs/bot.log
StandardError=append:/home/p0k5/MeteoEdge/logs/bot.log

[Install]
WantedBy=multi-user.target
```

**Key settings:**
- **User**: p0k5 (change to match your deployment user)
- **WorkingDirectory**: /home/p0k5/MeteoEdge (change to match your install path)
- **EnvironmentFile**: Points to `.env` for credentials and configuration
- **ExecStart**: Runs with `--live` for real trading (change to `--paper` for simulation)
- **Restart**: Always restarts on failure (10-second delay)
- **Logging**: Appends to `/home/p0k5/MeteoEdge/logs/bot.log`

**Operational commands:**
```bash
# Start the service
sudo systemctl start meteoedge.service

# Stop the service
sudo systemctl stop meteoedge.service

# Check status
sudo systemctl status meteoedge.service

# View logs in real time
sudo journalctl -u meteoedge.service -f

# Or view the appended log file
tail -f /home/p0k5/MeteoEdge/logs/bot.log
```

#### meteoedge-dashboard.service
Runs the web dashboard (displays open positions, P&L, trading stats).

```ini
[Unit]
Description=MeteoEdge portfolio dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=p0k5
WorkingDirectory=/home/p0k5/MeteoEdge
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/p0k5/MeteoEdge/.env
ExecStart=/home/p0k5/MeteoEdge/.venv/bin/python -u run_dashboard.py
Restart=always
RestartSec=10s
StandardOutput=append:/home/p0k5/MeteoEdge/logs/dashboard.log
StandardError=append:/home/p0k5/MeteoEdge/logs/dashboard.log

[Install]
WantedBy=multi-user.target
```

**Operational commands:**
```bash
sudo systemctl start meteoedge-dashboard.service
sudo systemctl status meteoedge-dashboard.service
sudo journalctl -u meteoedge-dashboard.service -f
```

**Access the dashboard:**
- Navigate to `http://<machine-ip>:8000` on the same network
- Shows live positions, cash balance, mark-to-market P&L, and trading statistics

#### meteoedge-settle.service
One-shot service that runs daily settlement (updates DB with market outcomes, calculates final P&L). Triggered by meteoedge-settle.timer.

```ini
[Unit]
Description=MeteoEdge daily settlement
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=p0k5
WorkingDirectory=/home/p0k5/MeteoEdge
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/p0k5/MeteoEdge/.env
ExecStart=/home/p0k5/MeteoEdge/.venv/bin/python -u -m src.scripts.settle
StandardOutput=append:/home/p0k5/MeteoEdge/logs/settle.log
StandardError=append:/home/p0k5/MeteoEdge/logs/settle.log
```

#### meteoedge-settle.timer
Systemd timer that runs the settlement service every day at 12:00 UTC (typically ~9 hours after US markets close, allowing NWS Daily Climate Reports to publish).

```ini
[Unit]
Description=Run MeteoEdge settlement daily at 12:00 UTC

[Timer]
OnCalendar=*-*-* 12:00:00 UTC
AccuracySec=1m
Persistent=true

[Install]
WantedBy=timers.target
```

**Operational commands:**
```bash
# Check next scheduled run
sudo systemctl list-timers meteoedge-settle.timer

# Manually trigger settlement (useful for re-settling a past date)
sudo systemctl start meteoedge-settle.service

# View settlement logs
sudo journalctl -u meteoedge-settle.service -f
```

---

## Configuration

All configuration is controlled via environment variables (defaults in `src/config.py`). Load from `.env` file:

### Strategy Configuration

| Variable | Default | Unit | Description | Requires Credentials |
|----------|---------|------|-------------|----------------------|
| `MIN_EDGE_CENTS` | 15.0 | ¢ | Minimum edge in cents to flag a candidate | No |
| `MAX_EDGE_CENTS` | 20.0 | ¢ | Maximum edge; higher edges may indicate adverse selection | No |
| `MIN_PRICE_CENTS` | 60 | ¢ | Reject trades below this price; below 60¢ ROI is negative | No |
| `ENABLE_YES_TRADES` | false | boolean | Enable YES-side live/paper trades. When false, YES candidates that pass all gates are still logged as `mode='shadow'` rows in the trades table (no order placed, no capital at risk) so outcomes can be tracked for later validation. | No |
| `MAX_CONFIDENCE_YES_FOR_NO` | 0.05 | probability | Confidence threshold for NO-side trades; only enter when p(YES) ≤ this | No |
| `MAX_MINUTES_TO_SETTLEMENT` | 1440 | minutes | Reject markets further than this from resolution | No |
| `POLL_INTERVAL_SECONDS` | 300 | seconds | How often to poll Polymarket for new markets (5 min default) | No |
| `FORECAST_STDDEV_F` | 2.0 | °F | Forecast uncertainty (stddev) for Bayesian prior on undetermined brackets | No |

### Shadow YES Gate Thresholds (issue #284)

The YES gate is intentionally strict for live trading but uses looser thresholds on the shadow path so the shadow loop can collect outcome data without loosening live-order gates.

These three parameters apply **only when `shadow_yes=True`** (station is in `SHADOW_STATIONS`, `SHADOW_STATIONS_YES`, or `ENABLE_YES_TRADES=false`). They have **no effect on live YES orders or the NO side**.

| Key | Default | Unit | Description |
|-----|---------|------|-------------|
| `SHADOW_MIN_EDGE_CENTS_YES` | 3.0 | ¢ | Minimum EV to emit a shadow YES candidate (live YES uses `MIN_EDGE_CENTS=15`) |
| `SHADOW_MIN_CONFIDENCE_YES` | 0.55 | probability | Minimum p(YES) for a shadow YES candidate (live YES uses `MIN_CONFIDENCE_YES=0.85`) |
| `SHADOW_MIN_PRICE_CENTS_YES` | 20 | ¢ | Minimum YES ask price for a shadow candidate (live YES uses `MIN_PRICE_CENTS=60`) |

All three keys are DB-backed (editable via the dashboard Config tab or `PATCH /api/config`) and support live reload without a bot restart.

**NO side guarantee:** The NO branch in `src/strategy/scanner.py` uses `MIN_EDGE_CENTS`, `MAX_CONFIDENCE_YES_FOR_NO`, and `MIN_PRICE_CENTS` unconditionally. None of the `SHADOW_MIN_*_YES` keys affect NO candidate detection.

### Model Guardrails

| Variable | Default | Unit | Description | Requires Credentials |
|----------|---------|------|-------------|----------------------|
| `MODEL_PROB_CAP` | 0.95 | probability | Symmetric cap applied to `p_yes` after `true_probability_yes()`: clamps to `[1-cap, cap]`. Interim guard against overconfidence until EMOS (#70) is promoted. Set to `1.0` to disable. | No |

### Residual Bias Correction

Per-city rolling bias correction derived from `intraday_corrections.delta_f` (observed − model). Controlled entirely by env vars; DB-backed via `CONFIG_DEFAULTS`.

| Variable | Default | Unit | Description | Requires Credentials |
|----------|---------|------|-------------|----------------------|
| `RESIDUAL_CORRECTION_ENABLED` | true | boolean | Master on/off switch for per-city bias correction | No |
| `RESIDUAL_WINDOW_DAYS` | 30 | days | Trailing window of delta_f rows used for rolling bias estimate | No |
| `RESIDUAL_MIN_SAMPLES` | 10 | count | Minimum rows before bias is applied (too few = noise) | No |
| `RESIDUAL_MAX_CORRECTION_F` | 5.0 | °F | Hard clamp on applied bias — correction is clamped to ±this value | No |
| `MAX_RESIDUAL_MAE_F_FOR_LIVE` | 8.0 | °F | Rolling MAE above this suppresses live NO entries for that city (forces shadow) | No |

### Risk Management

| Variable | Default | Unit | Description | Requires Credentials |
|----------|---------|------|-------------|----------------------|
| `STARTING_CAPITAL_EUR` | 500.0 | € | Reference capital for drawdown calculation | No |
| `RISK_DAILY_LOSS_LIMIT_EUR` | 50.0 | € | Stop trading for the day if daily loss exceeds this | No |
| `RISK_MAX_OPEN_POSITIONS` | 15 | count | Maximum simultaneous open positions | No |
| `RISK_DRAWDOWN_STOP_PCT` | 0.15 | fraction | Stop all trading if drawdown exceeds this fraction of starting capital | No |
| `RISK_MIN_LIQUIDITY` | 50 | contracts | Minimum combined order book size to accept a trade | No |

### Position Management

| Variable | Default | Unit | Description | Requires Credentials |
|----------|---------|------|-------------|----------------------|
| `POSITION_SIZE_EUR` | 5.0 | € | EUR staked per trade | No |
| `TAKE_PROFIT_BUFFER_CENTS` | 2 | ¢ | Exit when market bid reaches (predicted_price - buffer) | No |
| `TAKE_PROFIT_BUFFER_CENTS_{STATION}` | (uses default) | ¢ | Per-station take-profit override, e.g. `TAKE_PROFIT_BUFFER_CENTS_KMIA=3` | No |
| `FORCE_EXIT_MINUTES_TO_SETTLEMENT` | 60 | min | Force-exit open positions this many minutes before settlement. Set to 0 to disable. | No |
| `STOP_LOSS_MIN_BID_CENTS` | 40 | ¢ | Model-confidence stop-loss only sells while the NO bid is at or above this floor | No |
| `STOP_LOSS_CONSECUTIVE_POLLS` | 2 | polls | Consecutive polls with model fair value below entry before the stop fires | No |
| `STOP_LOSS_MIN_DEPTH_SHARES` | 10 | shares | Minimum best-bid depth required before selling into it | No |
| `STOP_LOSS_SELL_AGGRESSION_CENTS` | 2 | ¢ | Sell limit priced through the best bid (immediate-or-cancel; never left resting) | No |
| `STOP_LOSS_MIN_BRACKET_PROXIMITY_F` | 0.5 | °F | Don't fire while running daily high is more than this many °F below bracket_low (set ≤0 to disable) | No |
| `STOP_LOSS_RESPECT_FORECAST_OVERSHOOT` | true | bool | Suppress stop-loss firing when forecast exceeds bracket_high (NO wins on overshoot) | No |
| `MIN_FORECAST_BRACKET_MARGIN_F` | 2.5 | °F | Skip NO entries whose bracket is closer than this to the expected daily high | No |
| `SHADOW_STATIONS` | RKSI | codes | Comma-separated station codes where BOTH sides are shadowed (orders logged at $1 notional, not placed). Alias: `DISABLED_STATIONS` (back-compat). | No |
| `DISABLED_STATIONS` | RKSI | codes | Alias for `SHADOW_STATIONS` — kept for backwards compatibility. Both env vars are equivalent. | No |
| `SHADOW_STATIONS_YES` | (unset) | codes | Comma-separated stations where only the YES side is shadowed; NO side trades normally. | No |
| `SHADOW_STATIONS_NO` | (unset) | codes | Comma-separated stations where only the NO side is shadowed; YES side trades normally. | No |

### Operational & API

| Variable | Default | Description | Requires Credentials |
|----------|---------|-------------|----------------------|
| `DB_PATH` | data/meteoedge.db | SQLite database file path | No |
| `POLYMARKET_HOST` | https://clob.polymarket.com | Polymarket CLOB endpoint | No |
| `ENABLE_CLOB_ENRICHMENT` | false | Enrich market prices with live CLOB orderbook data (~2 API calls per candidate) | No |
| `POLYMARKET_DEPOSIT_WALLET` | (unset) | Your ERC-1967 proxy address for live trading | **Yes (live mode)** |
| `POLYMARKET_API_KEY` | (unset) | Your wallet private key (L1) for live trading | **Yes (live mode)** |
| `POLYMARKET_L2_API_KEY` | (unset) | Derived L2 API key for live trading | **Yes (live mode)** |
| `POLYMARKET_L2_API_SECRET` | (unset) | Derived L2 API secret for live trading | **Yes (live mode)** |
| `POLYMARKET_L2_API_PASSPHRASE` | (unset) | Derived L2 API passphrase for live trading | **Yes (live mode)** |
| `POLYMARKET_CHAIN_ID` | 137 | 137 for mainnet (real), 80002 for Amoy testnet | No |

**Live mode requirements:**
- `POLYMARKET_DEPOSIT_WALLET`: Your funded L2 proxy address (see `.env.example`)
- `POLYMARKET_API_KEY`: Your L1 wallet private key (see `.env.example`)
- `POLYMARKET_L2_API_*`: Derived credentials (see `.env.example` for derivation command)

---

## Run Modes

### Paper Trading (Default)

Simulates order execution with realistic slippage and queue delays. **Safe for testing; no real funds at risk.**

```bash
python -m src.scripts.run --paper
# or (--paper is the default)
python -m src.scripts.run
```

**What it does:**
- Polls Polymarket every 5 minutes (configurable)
- Evaluates candidates against risk limits
- Simulates order fills with realistic slippage (0.5–3¢)
- Logs trading candidates to `logs/candidates.csv`
- Logs market snapshots to `logs/snapshots.jsonl`
- Logs simulated trades to `logs/live_trades.jsonl`
- Tracks positions and P&L in SQLite `data/meteoedge.db`
- Respects all risk limits (capital, position count, drawdown)
- Starts dashboard at `http://localhost:8000`

**When to use:** Validation before going live, continuous testing, backtesting.

### Live Trading

Executes real trades on Polymarket with real funds. **Requires full setup and careful monitoring.**

```bash
python -m src.scripts.run --live
```

**Prerequisites:**
1. Set `POLYMARKET_DEPOSIT_WALLET` (your funded L2 proxy address)
2. Set `POLYMARKET_API_KEY` (your L1 wallet private key)
3. Run L2 credential derivation command (see `.env.example`)
4. Set `POLYMARKET_L2_API_KEY`, `POLYMARKET_L2_API_SECRET`, `POLYMARKET_L2_API_PASSPHRASE`

**What it does:**
- Same as paper mode, but executes real orders on Polymarket CLOB
- Maintains open order dedup guard to prevent duplicate fills
- Reconciles timeout fills against wallet state (GTC orders that filled post-timeout)
- Implements take-profit exits: sell NO positions when market bid reaches (predicted_price - buffer)
- Tracks real P&L; settlement updates final outcomes after market resolution
- Logs real trades to `logs/live_trades.jsonl` with order IDs and execution details

**Risk safeguards:**
- All risk limits enforced (daily loss, open position count, drawdown)
- Position size fixed per config (`POSITION_SIZE_EUR`)
- Minimum price and minimum liquidity gates apply to all trades

**First deployment:**
1. Start with `STARTING_CAPITAL_EUR=100.0` and small position size (5–10 EUR)
2. Monitor the first 7 days continuously
3. Increase capital only after validating all systems

### Single Poll Mode

Run one cycle of data collection and candidate generation, then exit.

```bash
python -m src.scripts.run --once
```

**Useful for:**
- Testing a single poll cycle without waiting
- Cron job integration (run once per time period)
- Debugging specific market conditions

---

## Settlement

### Automatic Daily Settlement (Timer)

The systemd timer `meteoedge-settle.timer` runs settlement every day at 12:00 UTC:

```bash
sudo systemctl list-timers meteoedge-settle.timer
```

### Manual Settlement

To re-settle a specific past date (e.g., if NWS data became available late):

```bash
python -m src.scripts.settle 2024-06-03
```

### What Settlement Does

1. **Fetches actual daily high temperatures** from METAR history for each station (48-hour lookback)
2. **Reads candidates.csv** from the target date
3. **Determines outcomes** for each candidate:
   - For YES trades: YES wins if actual_high falls within bracket
   - For NO trades: NO wins if actual_high falls outside bracket
4. **Calculates P&L** per candidate:
   - Win: `100¢ - entry_price`
   - Loss: `-entry_price`
5. **Writes settlements.csv** with actual_high, yes_won, candidate_won, pnl_cents
6. **Updates live_trades.jsonl** for trades with outcome='filled':
   - Adds actual_high, yes_won, pnl fields
   - Preserves 'sold' outcome records (already exited mid-day)
7. **Updates open_positions in DB** if entry was in DB (marks as closed after settlement)

### Re-running Settlement

If settlement fails or incomplete for a date:

```bash
# Trigger manually
sudo systemctl start meteoedge-settle.service

# Or re-settle a past date
python -m src.scripts.settle 2024-06-01

# View logs
sudo journalctl -u meteoedge-settle.service -f
tail -f logs/settle.log
```

---

## Recovery & Restart

### In-Memory State Lost on Restart

The RiskManager maintains in-memory daily state (daily P&L, open position count). On restart:
- The `risk_state` table in the database preserves daily P&L per trade_date
- Open positions are recovered from the `open_positions` table in the database
- Risk manager is re-initialized from the database; the prior day's state is not reloaded

```python
# In src/scripts/run.py, main()
db = Database()
open_positions = db.get_open_positions()
if open_positions:
    print(f"[startup] recovered {len(open_positions)} open position(s) from DB")
```

### State in Database (Persisted)

The SQLite database (`data/meteoedge.db`) persists:

**observations** — All METAR observations and high-frequency weather data
- Indexed by station and timestamp
- Includes computed daily high temperatures
- Used for intraday correction and model input

**trades** — All executed trades (paper or live)
- Indexed by station, timestamp, and mode
- Includes entry price, actual fill price, slippage, predicted edge, outcome, P&L
- Used to compute statistics and validate settlement

**settlements** — Outcomes of resolved markets
- One row per market (unique on ticker)
- Includes actual daily high, resolved YES/NO, final market price
- Used for reconciliation and P&L verification

**open_positions** — Positions currently held
- One row per open order
- Includes entry price, shares, stop-loss, take-profit thresholds
- Cleared on exit (take-profit, stop-loss, or market resolution)

**risk_state** — Daily P&L and position counts
- One row per trade_date
- Stores accumulated daily_pnl and open_position_count
- Survives restart; used to enforce daily loss limit

**candidates** — Trade candidates evaluated each poll
- Includes all metrics: bracket, predicted_price, confidence, minutes_to_settlement, etc.
- Used for backtesting and trade analysis

**Other tables** — model_weights, model_forecast_log, intraday_corrections, taf_windows
- Support model training and forecast tracking

### Reconciliation on Restart (Live Mode)

On restart in live mode, `_sync_open_orders()` reconciles two sources:

1. **Exchange state**: Queries Polymarket CLOB for open GTC orders still pending
2. **DB state**: Reads today's filled positions from `open_positions` table

The bot rebuilds `_open_orders` set from both sources to prevent duplicate fills.

### Order Timeout Reconciliation

GTC (Good-Till-Canceled) limit orders sometimes fill after the 5-minute wait window expires. The `_reconcile_timeout_fills()` function:
1. Reads `live_trades.jsonl` for records with outcome='timeout'
2. Fetches the wallet's current holdings via Polymarket data API
3. Patches records where the token is in the wallet to outcome='filled'
4. Rewrites the file, restoring visibility to take-profit and stop-loss monitors

---

## Logging

### Log Files (Appended)

Systemd appends logs to files in `logs/`:

| File | Source | Contents |
|------|--------|----------|
| `logs/bot.log` | meteoedge.service | Polling loop, market scans, trade execution |
| `logs/dashboard.log` | meteoedge-dashboard.service | Web server startup, request handling |
| `logs/settle.log` | meteoedge-settle.service | Daily settlement (outcomes, P&L) |

**View live:**
```bash
tail -f logs/bot.log
tail -f logs/settle.log
```

### Journald Logs (Systemd)

Logs are also captured by journald (systemd's journal):

```bash
# View bot logs in real time
sudo journalctl -u meteoedge.service -f

# View last 50 lines
sudo journalctl -u meteoedge.service -n 50

# View logs since last boot
sudo journalctl -u meteoedge.service -b

# View logs for the past hour
sudo journalctl -u meteoedge.service --since "1 hour ago"
```

### Structured Log Files (JSONL)

In addition to appended log files, the bot writes structured data to JSONL files in `logs/`:

| File | Contents | Columns |
|------|----------|---------|
| `logs/candidates.csv` | Trade candidates per poll | ts, station, ticker, bracket_low/high, side, predicted_price, market_price, confidence, minutes_to_settlement, flagged_first |
| `logs/snapshots.jsonl` | Market state snapshots during evaluation | ts, station, ticker, bracket, p_yes, ev_yes, ev_no, and orderbook state |
| `logs/live_trades.jsonl` | All live (real) trades executed | ts, order_id, station, ticker, side, entry price, actual fill price, outcome, pnl, and exit reason if sold early |
| `logs/position_snapshots.jsonl` | Intraday position monitoring | ts, ticker, no_token_id, bracket, current entry/bid/ask, model re-evaluation, fair value |
| `logs/settlements.csv` | Settlement outcomes (from settle.py) | candidate row + actual_high, yes_won, candidate_won, pnl_cents |

### Diagnosing Issues

**First three things to check when the bot misbehaves:**

1. **Is the bot running?**
   ```bash
   sudo systemctl status meteoedge.service
   sudo journalctl -u meteoedge.service -n 20
   ```

2. **Are there API errors?**
   ```bash
   tail -f logs/bot.log | grep -i error
   tail -f logs/bot.log | grep -i "polymarket\|clob\|timeout"
   ```

3. **Is the database locked or corrupted?**
   ```bash
   ls -lh data/meteoedge.db data/meteoedge.db-wal data/meteoedge.db-shm
   sqlite3 data/meteoedge.db "SELECT COUNT(*) FROM trades;"
   ```

### Log Levels

The bot uses standard Python logging via `logging` module. The main output is printed to stdout/stderr (captured by journald and appended to `logs/bot.log`).

**Common log prefixes:**
- `[run]` — Main polling loop
- `[<STATION>]` — Station-specific data (METAR, forecasts, scans)
- `[polymarket]` — Polymarket API calls
- `[live]` — Live trading execution
- `[paper]` — Paper trading simulation
- `[risk]` — Risk manager decisions
- `[orders]` — Order state tracking
- `[settle]` — Settlement process
- `[snap]` — Position snapshots
- `[tp]` — Take-profit exits
- `[exit]` — METAR stop-loss exits (currently disabled)
- `[balance]` — USDC balance checks
- `[reconcile]` — Timeout fill reconciliation

---

## Monitoring & Alerts

### Dashboard

Access the web dashboard at `http://<machine-ip>:8000` to view:
- Open positions (symbol, entry price, current bid/ask)
- Cash balance
- Mark-to-market P&L
- Trading statistics (win rate, ROI, P&L over time)

### Email Alerts

AlertManager sends email notifications on these conditions:
- Daily loss exceeds `RISK_DAILY_LOSS_LIMIT_EUR`
- Drawdown exceeds `RISK_DRAWDOWN_STOP_PCT`
- No poll has run in the past 30 minutes (poll-missed alert)

Configure email via env vars (see `.env.example`).

### Manual Monitoring

Check the bot's health continuously:

```bash
# Is the service running?
sudo systemctl is-active meteoedge.service

# Last 10 polls (look for error lines)
tail -f logs/bot.log | grep "=== Poll"

# Check open orders on the exchange
# (Requires Polymarket API credentials)

# Is the dashboard responding?
curl http://localhost:8000/status
```

---

## Troubleshooting

### Bot Exits or Restarts Repeatedly

```bash
# Check the error
sudo journalctl -u meteoedge.service -n 50

# Common causes:
# 1. API key invalid: POLYMARKET_API_KEY or L2 credentials missing/wrong
# 2. Database locked: Another process has data/meteoedge.db open
# 3. Network: Cannot reach Polymarket or weather APIs

# Restart the bot
sudo systemctl restart meteoedge.service
```

### Settlement Fails

```bash
# Check logs
tail -f logs/settle.log

# Common causes:
# 1. NWS API unreachable: METAR fetch failed (transient, retry later)
# 2. Missing candidates.csv: No trades on the target date
# 3. Database error: Check "Database locked" above

# Retry settlement manually
python -m src.scripts.settle 2024-06-03
```

### Dashboard Unreachable

```bash
# Check if service is running
sudo systemctl status meteoedge-dashboard.service

# Check the port
sudo netstat -tlnp | grep 8000

# Restart the dashboard
sudo systemctl restart meteoedge-dashboard.service
```

### No Candidates Generated

```bash
# Check bot logs
tail logs/bot.log | grep "\[polymarket\]"

# Check that at least one station is in active hours
# (See STATION_ACTIVE_HOURS in src/config.py)
python3 -c "from src.config import STATION_ACTIVE_HOURS; import datetime; print(datetime.datetime.now().hour)"

# Manually run one poll to debug
python -m src.scripts.run --once 2>&1 | head -50
```

### Position Not Closed After Settlement

1. Check if the market actually resolved:
   ```bash
   sqlite3 data/meteoedge.db "SELECT ticker FROM settlements WHERE DATE(ts) = '2024-06-03';"
   ```

2. Check if the trade was in the database:
   ```bash
   sqlite3 data/meteoedge.db "SELECT ticker, outcome FROM trades WHERE DATE(ts) = '2024-06-03';"
   ```

3. Manually re-settle:
   ```bash
   python -m src.scripts.settle 2024-06-03
   ```

---

## Maintenance

### Rotating Logs

Systemd appended logs grow indefinitely. Set up log rotation:

```bash
# Create /etc/logrotate.d/meteoedge
sudo tee /etc/logrotate.d/meteoedge <<EOF
/home/p0k5/MeteoEdge/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    notifempty
    create 0644 p0k5 p0k5
}
EOF

# Test it
sudo logrotate -f /etc/logrotate.d/meteoedge

# Verify rotation happened
ls -la logs/bot.log*
```

### Database Maintenance

The SQLite database uses WAL (Write-Ahead Logging) for performance. Periodically checkpoint the WAL:

```bash
sqlite3 data/meteoedge.db "PRAGMA optimize; VACUUM;"
```

### Archive Old Data

Export and archive old trades/candidates/settlements:

```bash
# Export trades older than 30 days
sqlite3 data/meteoedge.db ".mode csv" ".output archive_trades_$(date +%Y%m%d).csv" \
  "SELECT * FROM trades WHERE DATE(ts) < DATE('now', '-30 days');"

# Delete archived records from DB
sqlite3 data/meteoedge.db "DELETE FROM trades WHERE DATE(ts) < DATE('now', '-30 days');"
```

---

## Deployment Checklist

Before going live, verify:

- [ ] `.env` file present with all required credentials
- [ ] `POLYMARKET_API_KEY`, `POLYMARKET_L2_API_*`, `POLYMARKET_DEPOSIT_WALLET` set
- [ ] `STARTING_CAPITAL_EUR` set to a safe amount (start with 100-500 EUR)
- [ ] `POSITION_SIZE_EUR` set appropriately (recommend 5-10 EUR per position)
- [ ] `RISK_DAILY_LOSS_LIMIT_EUR` set (recommend 10-20% of capital)
- [ ] Systemd units installed via `deploy/systemd/install.sh`
- [ ] Services enabled: `sudo systemctl enable meteoedge.service meteoedge-dashboard.service meteoedge-settle.timer`
- [ ] All services running: `sudo systemctl start meteoedge.service meteoedge-dashboard.service`
- [ ] Dashboard accessible at `http://localhost:8000`
- [ ] Settlement timer scheduled: `sudo systemctl list-timers meteoedge-settle.timer`
- [ ] Logs flowing: `tail -f logs/bot.log`
- [ ] Monitor first 7 days continuously before increasing capital

---

## Support & Escalation

For issues beyond this runbook, escalate to:
- Architecture questions: Tech Lead PM
- Bug reports: Include full logs (bot.log, settle.log) and database state (trades/settlements from the error date)
- Operational changes: Discuss with Tech Lead PM before modifying systemd units or core config
