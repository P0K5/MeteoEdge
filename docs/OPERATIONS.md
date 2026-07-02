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

#### meteoedge-prob-cap-report.service / meteoedge-prob-cap-report.timer

One-shot service, run daily at **12:30 UTC** (after `meteoedge-settle.timer` at
12:00 UTC) by `meteoedge-prob-cap-report.timer`. Runs
`scripts/prob_cap_shadow_report.py`, the self-gating shadow report for the
`MODEL_PROB_CAP` decision tracked by issues #551/#570.

```ini
[Unit]
Description=MeteoEdge prob-cap shadow report (issue #570 / #551)
After=network-online.target meteoedge-settle.service
Wants=network-online.target

[Service]
Type=oneshot
User=p0k5
WorkingDirectory=/home/p0k5/MeteoEdge
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/p0k5/MeteoEdge/.env
ExecStart=/home/p0k5/MeteoEdge/.venv/bin/python -u scripts/prob_cap_shadow_report.py
StandardOutput=append:/home/p0k5/MeteoEdge/logs/prob_cap_report.log
StandardError=append:/home/p0k5/MeteoEdge/logs/prob_cap_report.log
```

```ini
[Unit]
Description=Run MeteoEdge prob-cap shadow report daily at 12:30 UTC (after settlement)

[Timer]
OnCalendar=*-*-* 12:30:00 UTC
AccuracySec=1m
Persistent=true

[Install]
WantedBy=timers.target
```

**What it does:**
- Self-gating: counts distinct dates with non-NULL `p_yes_raw` candidate data
  (from `logs/candidates.csv`) since PR #564 deployed. Below `--min-days`
  (default 7) it logs one line and exits 0 — safe to run every day from the
  moment the timer is installed, well before there is 7 days of data.
- At/above the threshold, writes `backtest_results/prob_cap_shadow_<date>.md`:
  clamp saturation rate, distribution of `p_yes_raw` among clamped candidates,
  a simulated `MODEL_PROB_CAP` comparison across 0.95/0.97/0.98 (NO side only
  — the protected side), a two-channel breakout (edge-driven vs
  gate-headroom-driven, see the script's module docstring) with
  `MAX_CONFIDENCE_YES_FOR_NO` held fixed per the binding PM spec on issue
  #570, a `RANK_ON_RAW_PROB` ordering simulation, and an explicit
  change/hold/extend-window recommendation.
- **No live gate changes**: this script only reads `logs/candidates.csv`,
  `logs/settlements.csv`, and `logs/snapshots.jsonl` and writes a markdown
  report. It never touches `MODEL_PROB_CAP`, `MAX_CONFIDENCE_YES_FOR_NO`, or
  any other live config.

**Operational commands:**
```bash
# Check next scheduled run
sudo systemctl list-timers meteoedge-prob-cap-report.timer

# Manually trigger a run (e.g. to check gating status early)
sudo systemctl start meteoedge-prob-cap-report.service

# Or run directly without systemd (prints instead of writing a file)
python scripts/prob_cap_shadow_report.py --dry-run

# View logs
sudo journalctl -u meteoedge-prob-cap-report.service -f
tail -f logs/prob_cap_report.log
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

Per-city rolling bias correction derived from `intraday_corrections.delta_f` (observed − model). As of issue #340, the residual correction module is **pair-scoped**: it first tries to compute stats from the top-priority `(station, source)` pair for the city (per `source_priority.yaml`). If that pair has fewer than `RESIDUAL_MIN_SAMPLES` rows in the trailing window, it falls back to city-wide aggregation. The `ResidualStats.scope` field indicates which path was taken (`"pair"` or `"city_fallback"`).

Controlled entirely by env vars; DB-backed via `CONFIG_DEFAULTS`.

| Variable | Default | Unit | Description | Requires Credentials |
|----------|---------|------|-------------|----------------------|
| `RESIDUAL_CORRECTION_ENABLED` | true | boolean | Master on/off switch for per-city bias correction | No |
| `RESIDUAL_WINDOW_DAYS` | 30 | days | Trailing window of delta_f rows used for rolling bias estimate | No |
| `RESIDUAL_MIN_SAMPLES` | 10 | count | Minimum rows before bias is applied (too few = noise) | No |
| `RESIDUAL_MAX_CORRECTION_F` | 5.0 | °F | Hard clamp on applied bias — correction is clamped to ±this value | No |
| `MAX_RESIDUAL_MAE_F_FOR_LIVE` | 8.0 | °F | Rolling MAE above this suppresses live NO entries for that city (forces shadow) | No |

#### Edge Tab — Analysis API

`GET /api/analysis/{station}?date=YYYY-MM-DD` returns the ensemble distribution and per-bracket Polymarket edge for a given station and date. The `date` parameter is optional and defaults to today (UTC).

**HTTP status codes:**

| Code | Condition |
|------|-----------|
| 200 | Success — data returned |
| 404 | Unknown station (not in STATIONS config) |
| 422 | Malformed date parameter |
| 503 | Ensemble data unavailable (no model forecast log rows or DB error) |

**Response schema (200):**

| Field | Type | Description |
|-------|------|-------------|
| `station` | string | METAR code (upper-cased) |
| `date` | string | YYYY-MM-DD date of the forecast |
| `ensemble_mean` | float \| null | Weighted ensemble mean high temp (°F or °C per station unit) |
| `bias_corrected` | float \| null | EMOS bias-corrected mean |
| `member_count` | int | Number of model members in the distribution |
| `range` | [float\|null, float\|null] | [min, max] of the distribution |
| `distribution` | object | Bucketed distribution — keys are integer temp strings, values are counts |
| `brackets` | array | Per-bracket edge analysis (see below) |

**Each `brackets` element:**

| Field | Type | Description |
|-------|------|-------------|
| `range` | string | Human-readable label (e.g. `"54–55°F"`) |
| `bracket_low` | float | Lower boundary of the bracket |
| `bracket_high` | float | Upper boundary of the bracket |
| `polymarket_prob` | float \| null | Current Polymarket YES probability (0–100) |
| `model_prob` | float \| null | Model probability for the bracket (0–100) |
| `edge` | float \| null | Signed edge in percentage points (model_prob − polymarket_prob); null when either side is missing |

**503 error body:**
```json
{"error": "ensemble data unavailable", "station": "KORD", "date": "2026-06-29"}
```

#### Per-Station Residual API

`GET /api/stations/{metar}/residual` returns a list of residual stats entries, one per distinct `(station, source)` pair that has qualified data (≥ `RESIDUAL_MIN_SAMPLES` rows) in the trailing `RESIDUAL_WINDOW_DAYS`. Each entry includes:

| Field | Description |
|-------|-------------|
| `station` | Station identifier (e.g. `"Busan"`, `"RKPK"`) |
| `source` | Data source (e.g. `"amos"`, `"metar"`) |
| `mean_signed_error` | Mean bias in °F (positive = warm bias) |
| `rolling_mae` | Rolling MAE in °F |
| `sample_count` | Number of delta_f rows used |
| `clamped_correction` | Correction actually applied, clamped to ±`RESIDUAL_MAX_CORRECTION_F` |
| `correction_applied` | True when clamped correction is non-zero |
| `live_suppressed` | True when MAE exceeds `MAX_RESIDUAL_MAE_F_FOR_LIVE` |
| `last_obs_time` | ISO timestamp of the most recent obs for this pair (last 30 days) |
| `scope` | Always `"pair"` for this endpoint |

Returns 404 when the METAR is not in the STATIONS config.

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

## Guardrail Telemetry

Three Stage-1 guardrails are instrumented and queryable via `GET /api/guardrail-events`:

### Forced exits (`close_reason = 'forced_exit'`)

Fires when a NO position is closed early because settlement is within
`FORCE_EXIT_MINUTES_TO_SETTLEMENT` minutes and bid depth is adequate.

- Queried from the `trades` table directly.
- **Alert threshold**: >5 forced exits per day suggests the exit window
  (`FORCE_EXIT_MINUTES_TO_SETTLEMENT`) may be too wide, or that markets are
  regularly held too close to settlement. Investigate position entry timing.

### Cap events (`event_type = 'cap_applied'`)

Fires when the model probability `p_yes` is clamped to the `[1−MODEL_PROB_CAP,
MODEL_PROB_CAP]` interval (default 5%–95%).

- Stored in `guardrail_events`.
- **Alert threshold**: >5 cap events per day per station suggests the model is
  frequently overconfident, which may indicate data quality issues or that
  `MODEL_PROB_CAP` is too tight for current market conditions.
  Consider raising `MODEL_PROB_CAP` or investigating the model inputs.

### Correction events (`event_type = 'correction_applied'`)

Fires when the rolling residual bias correction (`apply_residual_correction`)
shifts the ensemble mean by more than 0°F.

- Stored in `guardrail_events`.
- **Alert threshold**: `avg_delta_f` (average °F shift) persistently above ±3°F
  suggests the model has a systematic bias for that station. This is expected
  early in deployment; if it persists after 30+ settled days, review the
  forecast source weights via the DEB panel.

### Example query

```bash
curl http://localhost:8000/api/guardrail-events | python3 -m json.tool
```

```json
{
  "forced_exits": {"total": 12, "last_7d": 3, "by_station": {"KLAX": 5, "KORD": 7}},
  "cap_events":   {"total": 8,  "last_7d": 2, "avg_delta_p": -0.0312},
  "correction_events": {"total": 41, "last_7d": 9, "avg_delta_f": -1.4}
}
```

---

## EMOS Data Reset

### Background (Issues #422, #423, #424, #425)

Prior to 2026-06-25, the `model_forecast_log` table accumulated "nowcast snapshots" — end-of-day forecasts at a single hardcoded σ — which could not be used for EMOS calibration. The EMOS algorithm (`src/model/emos_calibration.py`) requires:

1. Forecast-vs-actual pairs at **fixed lead times** (e.g., 24-hour leads)
2. Realistic ensemble spread (σ) from source models (NWS, Open-Meteo, GFS)
3. At least **MIN_SAMPLES=60 triples** per city to fit meaningful calibration coefficients

### Reset Timestamp

When the new capture pipeline (#425) was deployed and confirmed healthy (24h of valid data), the pre-migration rows were archived to `model_forecast_log_legacy_v1` (migration #422) and a reset marker was inserted into the `bot_config` table:

```sql
-- Query the reset timestamp
SELECT value FROM bot_config WHERE key = 'model_forecast_log_reset_at';
```

**Reset date (UTC):** 2026-06-25T15:00:14.400085+00:00

### Expected Timeline to EMOS Readiness

- **First 0–10 days**: New pipeline captures forecasts at multiple lead times (24h, 12h, 6h, 3h).
- **Day 10–30**: DEB weighting (`src/model/deb_weighting.py`) falls back to equal weights (0.333 each) because only ~10 settlement rows are available.
- **Day 30–60**: DEB gains traction as settlement count reaches `_MIN_SAMPLES=10`.
- **Day 60+**: EMOS calibration becomes available; first cities ready for promotion are those with high daily-forecast cadence:
  - **Singapore (WSSS)**: High-cadence mss station, typically ready first (~day 60–75)
  - **Korean stations (RKSI, RKPK, RKPB, RKTU, RKNN)**: AMOS data, typically ready ~day 60–90
  - **North America (KORD, KLAX, KMIA, KBOS)**: METAR only (once-daily), typically ready ~day 90–120

### Data Quality Checks

Use the provided script to monitor EMOS data readiness:

```bash
python scripts/check_emos_data_quality.py [--db /path/to/db]
```

This script:
- Counts rows per (city, lead_hours, model) from `model_forecast_log`
- Flags cities with < 60 rows at 24h lead as "at risk"
- Projects estimated readiness date: reset_date + (60 - current_rows) days

### Impact on Live Trading

- **Before reset (legacy nowcasts)**: EMOS disabled; DEB uses empirical RMSE weights
- **After reset (new pipeline, <60 days)**: EMOS still disabled; DEB uses equal weights (0.333)
- **After 60 days**: EMOS shadow runs; coefficients calibrate; ready for promotion to active
- **Current mode** (`EMOS_DEFAULT_MODE="legacy"`): Live envelope still uses the legacy method until manually promoted

No trading halt is required. The reset affects only EMOS calibration; all live trading, risk limits, and settlement continue unchanged.

---

## Architectural Decisions

### DB `open_positions` as Single Source of Truth (2026-06-20)

**Decision:** The dashboard reads open position enrichment (station, bracket, predicted_price) **exclusively** from the `open_positions` DB table via the existing endpoint `/api/portfolio`. The JSONL audit trail (`logs/live_trades.*.jsonl`) is write-only and is never consulted for the dashboard read path.

**Rationale:**
- The database table is the canonical state, written by `order_manager.sync_open_orders()` at poll start
- JSONL is for replay and debugging, not for live dashboard rendering
- Separating concerns (DB for state, JSONL for audit) simplifies reconciliation logic and prevents fallback cascades

**Impact on future agents:**
- Do not re-introduce a JSONL fallback for `station`, `bracket_low`, `bracket_high`, or `predicted_price` in the dashboard
- If enrichment is missing from open_positions, the root cause is in sync_open_orders or the trades table, not in JSONL
- Any changes to position enrichment logic must update both the DB write path and the dashboard read path consistently

**Reference:** See `docs/design/open-positions-source-of-truth.md` for the full architecture spec.

### Per-Channel `sigma_f` Sourcing Policy (issue #555)

**Decision:** `model_forecast_log.sigma_f` must either carry a genuine dispersion
signal or be an honestly-`NULL` "we don't have one" — never a silently-invented
number. Prior to #555, `compute_ensemble_sigma()` (which applies
`SIGMA_FLOOR_F=1.0°F`) was called directly at GEFS capture time and its
*floored* return value was persisted verbatim: 240 of 345 GEFS rows sat at
exactly the 1.00°F floor, destroying the real ensemble-spread signal EMOS
needs to learn its spread coefficient `d` (`σ_calibrated = c + d·σ_ensemble`).

The fix splits capture-time and consumption-time sigma:

- **Capture time** (`src/scripts/capture_forecasts.py`): for the `gefs`
  channel, `raw_member_sigma()` (`src/model/ensemble_sigma.py`) computes the
  **unfloored** sample stdev of the raw ensemble members and persists it
  as-is — including genuine near-zero spreads — or `NULL` when fewer than 2
  members are available.
- **Consumption time** (live probability/trading, EMOS shadow
  self-calibration, etc.): `compute_ensemble_sigma()` is unchanged — it still
  applies `SIGMA_FLOOR_F` (and the historical-calibration regression when
  enough history exists). Its behavior and output are identical to
  pre-#555 for the same inputs; no existing or future caller of that function
  sees any behavior change.

**Committed per-channel decision table:**

| Channel | Decision | Rationale |
|---|---|---|
| `nws` | Derive | Climatological σ keyed on `lead_hours` (see `src/data/nws.py:_nws_sigma_for_lead`) — a documented static per-lead dispersion table, not a raw ensemble, but a real signal. |
| `open_meteo` | Derive | Cross-model stdev across 4 constituent NWP runs (`ecmwf_ifs04`/`gfs_seamless`/`jma_seamless`/`best_match`) — genuine multi-model spread. |
| `gfs` | NULL | Single deterministic run (#548); no ensemble or multi-model spread exists to derive from. |
| `gefs` | Derive (raw, unfloored) | Raw stdev of the ~30 GEFS members — the one true ensemble-member spread captured. `SIGMA_FLOOR_F` is applied only at consumption (`src/model/ensemble_sigma.py::compute_ensemble_sigma`), never at capture. |
| `hrrr` | NULL | Single deterministic run, hourly grid only; no member spread. A lagged-run spread (diff between consecutive HRRR cycles) could be derived but requires additional cycle-history plumbing not yet built — left NULL rather than inventing a static number. Revisit if a future issue adds lagged-run ingestion. |
| `nbm` | NULL | NBM itself blends models internally but the open endpoint used here exposes only the point forecast, not its internal spread — left NULL rather than a fabricated constant. |
| `ecmwf` | NULL | Single deterministic run (open-data endpoint does not expose the ECMWF ensemble/EPS spread) — left NULL rather than a fabricated constant. |
| `icon` | NULL | Single deterministic run (open-data endpoint does not expose ICON-EPS spread) — left NULL rather than a fabricated constant. |

**Impact on future agents:**
- Do not call `compute_ensemble_sigma()` from `capture_forecasts.py` — it
  floors its return value, which would reintroduce the bug #555 fixed. Use
  `raw_member_sigma()` for anything persisted to `model_forecast_log.sigma_f`.
- Do not call `raw_member_sigma()` from live probability/trading code — it
  deliberately omits the floor and the historical-calibration regression;
  `compute_ensemble_sigma()` remains the sanctioned consumption-time entry
  point (integration tracked in #448, Week 3).
- EMOS trains per-channel (see `forecast_source`/`regime` filtering in
  `src/model/emos_calibration.py:fetch_training_data`), so a NULL `sigma_f`
  for a given channel only means that channel's EMOS fit falls back to
  `FORECAST_STDDEV_F` — it does not block other channels from training on
  their own real sigma.
- Use `scripts/check_emos_data_quality.py` (`print_sigma_quality_report()`)
  to monitor floor-saturation and NULL-sigma rates per channel going forward.

**Reference:** See `src/scripts/capture_forecasts.py` module docstring and
`src/model/ensemble_sigma.py` module docstring for the same table maintained
alongside the code.

---

## Support & Escalation

For issues beyond this runbook, escalate to:
- Architecture questions: Tech Lead PM
- Bug reports: Include full logs (bot.log, settle.log) and database state (trades/settlements from the error date)
- Operational changes: Discuss with Tech Lead PM before modifying systemd units or core config

---

---

## GRIB2 / HRRR Data Ingestion (Epic A1)

### Overview

`src/data/grib_cache.py` provides byte-range GRIB2 fetches from public NOAA AWS S3
buckets using **herbie** (MIT-licensed).  Only `TMP_2m` and `DPT_2m` fields are
fetched.  Full GRIB files (several hundred MB) are **never** downloaded — herbie
reads the `.idx` sidecar to determine exact byte ranges for the requested variable.

### AWS bucket paths

| Model | S3 path | Auth required |
|---|---|---|
| HRRR | `s3://noaa-hrrr-bdp-pds/` | None (public, free egress) |
| NBM  | `s3://noaa-nbm-grib2-pds/` | None (public, free egress) |

HRRR path layout example:
```
s3://noaa-hrrr-bdp-pds/hrrr.20240615/conus/hrrr.t18z.wrfnatf00.grib2
s3://noaa-hrrr-bdp-pds/hrrr.20240615/conus/hrrr.t18z.wrfnatf00.grib2.idx  ← index/sidecar
```

herbie resolves the correct path automatically given a cycle datetime and model name.
No AWS credentials are needed.

### Cache location

Sliced GRIB2 messages are cached on disk at:

```
.grib_cache/          ← default (relative to repo root)
  hrrr_TMP_2m_20240615T18Z_f000.grib2
  hrrr_DPT_2m_20240615T18Z_f000.grib2
  ...
```

Override via the `GRIB_CACHE_DIR` environment variable.

### Eviction policy

The cache uses a **time-to-live (TTL)** approach:

- Default TTL: **6 hours** (configurable via `GRIB_CACHE_TTL_HOURS` env var).
- Eviction is triggered opportunistically on every call to `fetch_hrrr_field()`.
- Any `.grib2` file older than the TTL is deleted silently.
- No manual cache flush is required in normal operation; a `rm -rf .grib_cache/`
  will force a full refresh on next run.

### Config parameters

All GRIB config params follow the standard env-var-override pattern used throughout
`src/config.py`:

| Env var | Default | Description |
|---|---|---|
| `GRIB_CACHE_TTL_HOURS` | `6.0` | On-disk cache TTL in hours |
| `GRIB_CACHE_DIR` | `.grib_cache` | Directory for cached GRIB2 slices |

Set these in the `.env` file or systemd `EnvironmentFile` as needed.

### Model cycle resolution

`_resolve_latest_cycle()` tries the current UTC hour and steps back up to 6 hours
to find the most recent HRRR cycle whose `.idx` file is published on S3.  HRRR
typically publishes within 45–60 minutes of cycle time; the 6-hour lookback ensures
the system always has a valid cycle even during NOAA upload delays.

### Supported fields

Only the following fields may be requested via `fetch_hrrr_field()`:

| Key | GRIB2 matcher | Units |
|---|---|---|
| `TMP_2m` | `:TMP:2 m above ground:` | Kelvin |
| `DPT_2m` | `:DPT:2 m above ground:` | Kelvin |

HRRR is US-only.  Use a US coordinate (e.g. Denver: `lat=39.73, lon=-104.99`) for
testing; international coordinates will produce a grid lookup outside the HRRR
domain.

### Smoke test

```python
from src.data.grib_cache import fetch_hrrr_field

# Denver, CO — should return a value in 273–318 K in summer
val = fetch_hrrr_field("TMP_2m", lat=39.73, lon=-104.99)
print(f"TMP_2m Denver: {val} K ({val - 273.15:.1f} °C)")
assert val is not None and 273.0 <= val <= 318.0
```

### Dependencies

```
herbie-data>=2024.3.0   # MIT — NOAA model-run resolution + S3 byte-range fetch
cfgrib>=0.9.10          # Read GRIB2 slices into numpy/xarray
numpy>=1.24.0           # Nearest-grid-point arithmetic
```

eccodes (the underlying C library for cfgrib) is installed automatically on most
systems via the `cfgrib` wheel.  If it is missing, `fetch_hrrr_field()` returns
`None` and logs a warning — it does not raise.


---

## Open-Meteo Usage

### Commercial Use Policy

Open-Meteo's free API tier permits non-commercial use without restriction. MeteoEdge's use of Open-Meteo qualifies as **commercial use** because:
- Live PnL is generated on Polymarket (a real-money prediction market)
- Forecasts directly inform trading decisions and capital allocation
- The service generates revenue (or would, if operational)

### Current Status

**Commercial use is permitted as-is.** Open-Meteo's free API explicitly allows commercial use under the following conditions:

1. **Attribution**: Required in your application or documentation
2. **Rate limits**: 10,000 requests per day (soft limit, higher usage negotiable)
3. **No commercial redistribution**: You cannot resell or republish Open-Meteo data

**Cost**: Free (no payment required). Open-Meteo is entirely free for both non-commercial and commercial use.

### Attribution Requirement

Open-Meteo's license requires attribution. The attribution is already present in the inline documentation at `/src/data/open_meteo.py` (module docstring). No additional file-level attribution comment is required.

### Paid Tier (Not Needed)

Open-Meteo offers a paid plan (~€29/mo) only for:
- **Custom SLAs** (service level agreements)
- **Higher rate limits** (>10k requests/day)
- **Priority support**

MeteoEdge's current usage is well within the free tier:
- ~288 requests per day (11 stations × 24-hour polling at 5-min intervals)
- Current rate limit: 10,000 requests/day (97% headroom)

### Self-Hosting

Open-Meteo publishes models and infrastructure as open source. Self-hosting is technically possible but **not required** for commercial use. The free API already permits commercial trading.

### Conclusion

No action required. MeteoEdge may continue using Open-Meteo's free API for live trading without payment, licensing changes, or self-hosting. Attribution is already documented in the code.

---

## EMOS Shadow Fit Threshold (Issue #556)

**Two-tier min_samples control: 35 days for shadow fits, 60 days for promotion eligibility.**

The `fetch_training_data()` function requires a minimum number of settled forecast-vs-actual pairs to train EMOS. This was previously hardcoded at 60 days, blocking shadow observation during early deployments (forecast log started June 24 → first fit ~August 23, too late for summer markets).

### Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `EMOS_MIN_SAMPLES_SHADOW` | 35 | Minimum samples for shadow-only fits (no live serving) |
| `EMOS_MIN_SAMPLES_PROMOTION` | 60 | Minimum samples for promotion-eligible fits |

Both are configurable via the dashboard **Config** tab under **EMOS Model Settings**.

### Behavior

- **Shadow fit (30–59 samples):** Runs daily on `scripts/run_emos_shadow.py`. Persists `ready_for_promotion=0` regardless of CRPS (hard guardrail, issue #556). CRPS rows logged to `emos_crps_log` for monitoring. Zero risk — nothing served live.
- **Promotion fit (≥60 samples):** Only after 60 settled days. Operator must manually promote via database update (see **§ Step 3 — Validate and promote** in **EMOS Retrain** below). Automatic promotion is never performed.

### First shadow fit timeline (June 24 deployment)

The forecast log started June 24. First cities cross the shadow threshold (~35 settled days) around **late July (July 28–31)**. First shadow CRPS rows begin appearing in the dashboard shortly after.

### Guardrails

1. **sample_count < 60 → ready_for_promotion=0 (enforced):** Even if CRPS is excellent, fits trained on <60 samples are shadow-only and cannot be promoted. Avoids overfitting risk (#226).
2. **GFS duplicate-era exclusion:** `fetch_training_data()` excludes "gfs" rows dated before 2026-07-02 (issue #548 duplicate-era rows are byte-identical copies of "open_meteo").

---

## EMOS Retrain for New Forecast Stack

**Rule: Whenever `FORECAST_STACK` changes (Epic A1/A2 promotion), retrain EMOS before switching live.**

EMOS coefficients correct the raw forecast mean and spread per city. When a new forecast stack
lands (e.g. HRRR/NBM for US, ECMWF/ICON for international), the existing coefficients are stale —
they were fitted on a different input distribution. Retrain per `(city, forecast_source)` before
promoting.

### When to retrain

- After ≥ 30 days of `model_forecast_log` rows exist for the new `forecast_source` identifier.
- Before `check_ready_for_promotion()` returns True for any city.
- Before calling any code that switches `FORECAST_STACK`.

### Step 1 — Check data availability

```bash
python - <<'EOF_SCRIPT'
from src.data.db import Database
from src.model.emos_calibration import fetch_training_data, InsufficientDataError

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"  # change to your new stack

with Database() as db:
    for city in CITIES:
        try:
            rows = fetch_training_data(city, db, min_samples=1, forecast_source=SOURCE)
            print(f"  {city}: {len(rows)} rows OK")
        except InsufficientDataError as e:
            print(f"  {city}: INSUFFICIENT — {e}")
EOF_SCRIPT
```

### Step 2 — Fit and save coefficients

```bash
python - <<'EOF_SCRIPT'
from src.data.db import Database
from src.model.emos_calibration import (
    fetch_training_data, fit_emos, save_coefficients, InsufficientDataError
)

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"

with Database() as db:
    for city in CITIES:
        try:
            data = fetch_training_data(city, db, forecast_source=SOURCE)
            a, b, c, d = fit_emos(data)
            from src.model.crps_score import crps_gaussian
            crps = sum(crps_gaussian(a + b*mu, max(c + d*sg, 1e-3), y)
                       for mu, sg, y in data) / len(data)
            save_coefficients(city, a, b, c, d, crps, db, forecast_source=SOURCE)
            print(f"  {city}: saved (a={a:.3f} b={b:.3f} c={c:.3f} d={d:.3f} CRPS={crps:.4f})")
        except InsufficientDataError as e:
            print(f"  {city}: SKIP — {e}")
EOF_SCRIPT
```

All rows are stored with `model_mode='emos_shadow'` and `ready_for_promotion=0`.

### Step 3 — Validate and promote (manual)

```bash
python - <<'EOF_SCRIPT'
from src.data.db import Database
from src.model.emos_calibration import check_ready_for_promotion

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"

with Database() as db:
    # Inspect CRPS scores
    for row in db.get_all_emos_calibration():
        if row["forecast_source"] == SOURCE:
            print(f"  {row['city']}: CRPS={row['crps_score']:.4f} ready={row['ready_for_promotion']}")

    # Gate check (should be False until you manually set ready_for_promotion=1)
    ready = check_ready_for_promotion(db, SOURCE, CITIES)
    print(f"\nPromotion gate: {'OPEN' if ready else 'BLOCKED'}")
EOF_SCRIPT
```

After verifying CRPS and win-rate, set `ready_for_promotion=1` manually:

```bash
python - <<'EOF_SCRIPT'
from src.data.db import Database

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"

with Database() as db:
    for city in CITIES:
        db._conn.execute(
            "UPDATE emos_calibration SET ready_for_promotion=1 "
            "WHERE city=? AND forecast_source=? AND model_mode='emos_shadow'",
            (city, SOURCE),
        )
    db._conn.commit()
    print("Done. Run check_ready_for_promotion() again to confirm gate is open.")
EOF_SCRIPT
```

### Notes

- Legacy `nws_open_meteo` coefficients are never deleted. The `(city, model_mode, forecast_source)`
  unique key ensures rows for different sources coexist.
- Do NOT run `fit_emos` in the live trading loop or CI — it is an offline, operator-run step.
- EMOS sigma retrain (#449) follows the same procedure.

- GEFS ensemble (30 members): ingested via capture-forecasts timer; `model='gefs'` rows written to `model_forecast_log` with `sigma_f` from `ensemble_sigma.py`.
- HRRR: hourly rows fetched via `src/data/hrrr.py`; mean temp stored as `model='hrrr'` with `sigma_f=None`. Built-in CONUS check — returns empty list outside the CONUS domain (no row written).
- NBM: daily-high forecast fetched via `src/data/nbm.py`; stored as `model='nbm'` with `sigma_f=None`. Built-in CONUS check — returns None outside the CONUS domain (no row written).
- ECMWF (Open): daily-high forecast fetched via `src/data/ecmwf_open.py`; stored as `model='ecmwf'` with `sigma_f=None`. Global domain — no domain restriction.
- ICON: hourly rows fetched via `src/data/icon.py`; mean temp stored as `model='icon'` with `sigma_f=None`. Built-in EU-domain check (lat 29–72, lon −25 to 45) — returns empty list outside EU (no row written).

All five shadow sources (GEFS, HRRR, NBM, ECMWF, ICON) are store/log only. They have zero effect on the live envelope, FORECAST_STACK, daily-high blend, or EMOS consumption. Each source is wrapped in an independent `try/except Exception` so a single source failure never affects the others.

### EMOS Retrain Scoping (issue #494)

EMOS retrain is always scoped to the active FORECAST_STACK. The mapping of stack → model tags is `src/config.py:FORECAST_STACK_MODELS`. Run `scripts/auto_retrain_probability_calibration.py` (reads FORECAST_STACK from DB automatically). Never call `fetch_training_data` without `regime` or `forecast_source` — it raises ValueError.

---

## DEB Channel Group Assignments (issue #550)

### Background

DEB (`src/model/deb_weighting.py`) computes inverse-RMSE weights per registered
forecast channel, then caps the combined weight of any `group_id` at
`GROUP_WEIGHT_CAP` (default 0.7) so that correlated channels can't jointly
dominate the ensemble even though each looks independently well-calibrated.
Prior to #550, `open_meteo` and `gfs` were registered with `group_id=None`
("uncapped") even though both are Open-Meteo-API-derived and `open_meteo`'s
multi-model mean literally includes a `gfs_seamless` constituent — the same
raw model the dedicated `gfs` channel fetches. As `ecmwf`/`gefs` accumulate
enough samples to leave cold-start, DEB would otherwise let three channels
sharing the same underlying GFS/ECMWF signal converge into what looks like a
diversified 3-4 channel ensemble but is actually a monoculture.

### Raw-model provenance table

| Channel | Raw model(s) | Fetcher | Notes |
|---|---|---|---|
| `nws` | NWS/NDFD gridpoint forecast | `src/data/nws.py::fetch_nws_with_spread` | US-only (`/gridpoints` API). |
| `open_meteo` | Mean of **ecmwf_ifs04, gfs_seamless, jma_seamless, best_match** | `src/data/open_meteo.py::fetch_open_meteo_with_spread` | 4-model blend proxied through the Open-Meteo API. `best_match` picks Open-Meteo's own "best regional model" per location and is opaque/variable. |
| `gfs` | NOAA GFS deterministic (`gfs_seamless`) | `src/data/open_meteo.py::fetch_gfs_with_spread` | Scoped to `models=gfs_seamless` only (issue #548 — previously a byte-identical duplicate of `open_meteo`). Same raw model as one of `open_meteo`'s four constituents. |
| `hrrr` | NOAA HRRR (High-Resolution Rapid Refresh) | `src/data/hrrr.py::fetch_hrrr_hourly` | Direct GRIB via herbie. CONUS-only. |
| `nbm` | NOAA National Blend of Models | `src/data/nbm.py::fetch_nbm_daily_high` | Direct GRIB via herbie. CONUS-only. NBM is itself a statistical blend of multiple NOAA/NCEP guidance products, ingested here as one opaque channel. |
| `ecmwf` | ECMWF HRES IFS (deterministic, `2t`) | `src/data/ecmwf_open.py::fetch_ecmwf_daily_high` | Direct fetch from ECMWF Open Data (AWS `s3://ecmwf-forecasts/`) via herbie (`model="ifs"`). Global domain. |
| `icon` | DWD ICON-EU (independent German model) | `src/data/icon.py::fetch_icon_hourly` | Direct GRIB2 download from DWD opendata (not ECMWF-derived). EU domain only (lat 29–72, lon -25 to 45). |
| `gefs` | NOAA GEFS — 31-member GFS ensemble (`gec00` + `gep01`…`gep30`) | `src/data/gefs.py::fetch_gefs_ensemble` | **Not currently registered in the DEB registry.** Ingestion-only per the module's own docstring; wiring into the trading/strategy layer (and therefore into DEB) is tracked separately by issue #448. Rows are already written to `model_forecast_log` under `model="gefs"` by `capture_forecasts.py`, but `compute_weights()` silently ignores them today since no `register_model("gefs", ...)` call exists. |

### group_id assignments

| Channel | `group_id` | Rationale |
|---|---|---|
| `nws`, `hrrr`, `nbm` | `noaa_us` | Unchanged. All three are NOAA/NWS-family CONUS sources; correlated by shared observational network and model lineage. |
| `gfs`, `open_meteo` | `gfs_family` | **Changed by #550 (was `None` for both).** `open_meteo` shares a literal `gfs_seamless` constituent with `gfs`, and pre-#548 `gfs` was a byte-identical duplicate of `open_meteo` — the strongest available evidence of how tightly coupled these two channels are. `open_meteo`'s other constituents (`ecmwf_ifs04`, `jma_seamless`) are **not** captured by this single group_id — the registry only supports one group per channel. This is a deliberate, documented trade-off: open_meteo's GFS overlap is the tightest and best-evidenced correlation, so it anchors the choice, but the residual ECMWF/JMA correlation against `ecmwf_intl` remains uncapped. A follow-up could split `open_meteo` into per-constituent channels or extend the registry to support multiple group memberships — out of scope for #550. |
| `ecmwf`, `icon` | `ecmwf_intl` | **Reassessed, unchanged.** ICON-EU is DWD's own independently developed model, not literally ECMWF-derived, so this is not strictly a "shared raw model" grouping the way `gfs_family` is. Kept because both are non-NOAA-US, high-cadence deterministic international sources whose errors correlate in practice over the European domain (overlapping synoptic-scale observations/boundary conditions), and no evidence was found to justify decoupling them. |
| `gefs` | *(recommended: `gfs_family`, not yet applied)* | Not currently registered in DEB (see provenance table above) — issue #448 tracks wiring it in. When it is registered, it should join `gfs_family`: GEFS members are GFS-core perturbations, sharing the same underlying NWP system as `gfs`/`open_meteo`. |

Every currently-*registered* channel has a deliberate, non-`None` `group_id` — the "documented reason for `None`" escape hatch in the #550 acceptance criteria is not exercised by any channel today.

### `_apply_group_cap` redistribution fix

Grouping `open_meteo`/`gfs` under `gfs_family` means that for EU/global-only
stations (`nws`/`hrrr`/`nbm` excluded), **every** applicable channel now has a
real `group_id` — `open_meteo`/`gfs` in `gfs_family`, `ecmwf`/`icon` in
`ecmwf_intl` — with zero models left with `group_id=None`. The pre-#550
`_apply_group_cap` implementation only redistributed a capped group's freed
excess weight to explicitly ungrouped models; with none available, the freed
weight was silently dropped and the returned weights summed to **less than
1.0** (confirmed by direct test: a naive redistribution left weights summing
to 0.9 instead of 1.0 for a 4-channel EU scenario). `_apply_group_cap` was
fixed to redistribute freed weight to any model **not** a member of an
over-cap group (ungrouped models, plus members of any other group that stayed
within cap) instead of only literally-ungrouped models. See
`src/model/deb_weighting.py::_apply_group_cap` docstring and
`src/tests/test_deb_weighting.py::TestGfsFamilyRegistry::test_redistribution_when_no_ungrouped_models_remain`
for the regression test. This is a minimal, additive fix — verified against
the full existing test suite with no other behavioral regressions.
