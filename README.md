# MeteoEdge: Polymarket Weather Arbitrage

![CI](https://github.com/P0K5/MeteoEdge/actions/workflows/ci.yml/badge.svg)

A machine learning trading system that identifies and executes profitable weather market arbitrages on Polymarket by comparing real-time meteorological data with market-implied probabilities.

## What MeteoEdge Does

MeteoEdge automatically:

1. **Collects live weather data** from multiple sources (METAR observations, NWS forecasts, JMA AMEDAS, AMOS, MSS, TAF)
2. **Computes daily high temperature predictions** using ensemble weather models (DEB weighting, envelope model, intraday correction)
3. **Queries Polymarket** for daily high temperature markets at supported stations
4. **Identifies mispricings** where market odds diverge from model predictions
5. **Executes trades** in shadow (observe-only), paper (simulated), or live (real money) modes
6. **Tracks positions and settlement outcomes** in real-time

## Live Stations and Data Sources

MeteoEdge trades on Polymarket daily high temperature markets at these airports:

| Station | City | Market Unit | Data Sources | Status |
|---------|------|-------------|--------------|--------|
| KORD | Chicago | °F | METAR, NWS | ✅ Active |
| KMIA | Miami | °F | METAR, NWS | ✅ Active |
| KLAX | Los Angeles | °F | METAR, NWS | ✅ Active |
| KATL | Atlanta | °F | METAR, NWS | ✅ Active |
| KHOU | Houston | °F | METAR, NWS | ✅ Active |
| RKSI | Seoul | °C | METAR, AMOS | ✅ Active |
| WMKK | Kuala Lumpur | °C | METAR, NWS | ✅ Active |
| RKPK | Busan | °C | METAR, AMOS | ✅ Active |
| ZGSZ | Shenzhen | °C | METAR, NWS | ✅ Active |
| WSSS | Singapore | °C | METAR, MSS | ✅ Active |
| MPMG | Panama City | °C | METAR, NWS | ✅ Active |

## Architecture Overview

```
┌─────────────────────────────────────┐
│  Data Collectors                    │
│  - METAR (aviation weather)         │
│  - NWS forecasts (US stations)      │
│  - JMA AMEDAS (Japan)               │
│  - AMOS (SE Asia)                   │
│  - MSS (China)                      │
│  - TAF (Terminal Aerodrome Forecast)│
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Weather State Computation          │
│  - Daily high temperature           │
│  - Forecast integration             │
│  - Timezone conversions             │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Probability Envelope Model         │
│  - Climb rates (historical p95)     │
│  - Ensemble forecast blending       │
│  - Intraday correction              │
│  - DEB hourly consensus weighting   │
│  - Decay functions (time-to-market) │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Market Scanner                     │
│  - Polymarket API queries           │
│  - Edge detection (15–20¢)          │
│  - Confidence filtering             │
│  - Price validation                 │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Risk Manager & Execution           │
│  - Position limits                  │
│  - Capital allocation               │
│  - Drawdown checks                  │
│  - Trade placement (CLOB)           │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Settlement & Monitoring            │
│  - Position tracking                │
│  - Market outcome recording         │
│  - P&L calculation                  │
│  - Alerts & dashboards              │
└─────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.10+
- Dependencies listed in `requirements.txt`
- (Optional) `.env` file with API keys for live trading

### Installation

```bash
git clone https://github.com/P0K5/MeteoEdge.git
cd MeteoEdge
pip install -r requirements.txt
cp .env.example .env  # For live mode, fill in POLYMARKET_API_KEY and L2 credentials
```

### Run Modes

MeteoEdge supports two main trading modes plus an observation-only option:

#### 1. Paper Trading Mode (Default)

Simulates order execution with realistic slippage and queue delays. This is the default mode and is safe to run at any time:

```bash
python -m src.scripts.run
# or explicitly:
python -m src.scripts.run --paper
```

This mode will:
- Poll Polymarket every 5 minutes (configurable via `POLL_INTERVAL_SECONDS`)
- Log trading candidates to `logs/candidates.csv`
- Log market snapshots to `logs/snapshots.jsonl`
- Simulate order execution with realistic slippage (0.5–3¢)
- Track positions and P&L in the local database
- Log all trades to `logs/live_trades.jsonl`
- Respect all risk limits (capital, position count, drawdown)
- Start an interactive dashboard at `http://localhost:8000`

Use paper mode to validate the system on your machine before going live.

#### 2. Live Trading Mode (Real Money)

Executes real trades on Polymarket against real funds. **Requires authentication and careful setup.**

```bash
python -m src.scripts.run --live
```

Before running live:
1. Set `POLYMARKET_API_KEY` (your L1 wallet private key) in `.env`
2. Run the L2 API credential derivation command (see `.env.example` for details)
3. Fill in `POLYMARKET_L2_API_KEY`, `POLYMARKET_L2_API_SECRET`, `POLYMARKET_L2_API_PASSPHRASE`
4. Set `POLYMARKET_DEPOSIT_WALLET` (your ERC-1967 proxy address)
5. Start with `STARTING_CAPITAL_EUR=100.0` and monitor the first 7 days

#### Single Poll Mode

Run one cycle of data collection and candidate generation, then exit:

```bash
python -m src.scripts.run --once
```

Useful for testing or cron jobs.

### Dashboard

View live positions, cash balance, and mark-to-market values via the web dashboard:

```bash
python run_dashboard.py
```

Then open `http://<machine-ip>:8000` on any device on the same network.

The dashboard shows:
- Open positions (symbol, entry price, current bid/ask)
- Cash balance
- Mark-to-market P&L
- Trading statistics

### Settlement

After markets resolve, settle outcomes and update records:

```bash
python -m src.scripts.settle
```

This command:
- Queries Polymarket for resolved market outcomes
- Updates position records with settlement results
- Logs final P&L for each trade
- Cleans up closed positions

## Environment Variables

All configuration is controlled via environment variables (or defaults in `src/config.py`). See `.env.example` for secrets. Here are the strategy and operational variables:

### Strategy Configuration

- `MIN_EDGE_CENTS` — Minimum edge in cents to flag a candidate (default: 15.0)
- `MAX_EDGE_CENTS` — Maximum edge; higher edges may indicate adverse selection (default: 20.0)
- `MIN_PRICE_CENTS` — Reject trades below this price; below 60¢ ROI is negative (default: 60)
- `ENABLE_YES_TRADES` — Enable YES-side trades; disabled by default until calibrated (default: false)
- `MAX_CONFIDENCE_YES_FOR_NO` — Confidence threshold for NO-side trades; only enter when model's p(YES) is ≤ this (default: 0.05)
- `MAX_MINUTES_TO_SETTLEMENT` — Reject markets further than this from resolution; prevents stale data (default: 1440 = 24 hours)

### Risk Management

- `STARTING_CAPITAL_EUR` — Reference capital for drawdown calculation (default: 500.0)
- `POSITION_SIZE_EUR` — EUR staked per trade (default: 5.0)
- `RISK_DAILY_LOSS_LIMIT_EUR` — Stop trading for the day if daily loss exceeds this (default: 50.0)
- `RISK_MAX_OPEN_POSITIONS` — Max simultaneous open positions (default: 15)
- `RISK_DRAWDOWN_STOP_PCT` — Stop all trading if drawdown exceeds this fraction of starting capital (default: 0.15)
- `RISK_MIN_LIQUIDITY` — Minimum combined order book size to accept a trade, in contracts (default: 50)

### Position Management

- `TAKE_PROFIT_BUFFER_CENTS` — Exit when market bid reaches (predicted_price - buffer) (default: 2)
- `STOP_LOSS_MIN_BID_CENTS` — Model-confidence stop-loss only sells while the NO bid is at or above this floor (default: 40)
- `STOP_LOSS_CONSECUTIVE_POLLS` — Polls in a row with model fair value below entry before the stop fires (default: 2)
- `STOP_LOSS_MIN_DEPTH_SHARES` — Minimum best-bid depth (shares) required before selling into it (default: 10)
- `STOP_LOSS_SELL_AGGRESSION_CENTS` — Stop-loss sell limit is priced this many cents through the best bid so it crosses immediately; unfilled orders are cancelled, never left resting (default: 2)
- `STOP_LOSS_MIN_BRACKET_PROXIMITY_F` — Don't fire stop-loss while the running daily high is more than this many °F below bracket_low; prevents firing on intraday model panics when the temp is still well away from the bracket (default: 0.5; set ≤0 to disable)
- `STOP_LOSS_RESPECT_FORECAST_OVERSHOOT` — When true, suppress stop-loss firing if forecast (NWS or secondary) exceeds bracket_high — NO wins on overshoot, so the dip is a false alarm (default: true)
- `MIN_FORECAST_BRACKET_MARGIN_F` — Skip NO entries whose bracket is closer than this (°F) to the expected daily high (default: 2.5)
- `DISABLED_STATIONS` — Comma-separated station codes excluded from new entries (default: RKSI)

### Operational

- `POLL_INTERVAL_SECONDS` — How often to poll Polymarket for new markets (default: 300 = 5 minutes)
- `ENABLE_CLOB_ENRICHMENT` — Enrich market prices with live CLOB orderbook data; adds ~2 API calls per candidate (default: false)
- `POLYMARKET_HOST` — Polymarket CLOB endpoint; leave at default unless testing (default: https://clob.polymarket.com)

### Live Execution (Required for --live mode)

See `.env.example` for:
- `POLYMARKET_API_KEY` — Your wallet private key (L1)
- `POLYMARKET_L2_API_KEY`, `POLYMARKET_L2_API_SECRET`, `POLYMARKET_L2_API_PASSPHRASE` — Derived L2 credentials
- `POLYMARKET_DEPOSIT_WALLET` — Your ERC-1967 proxy address
- `POLYMARKET_CHAIN_ID` — 137 for mainnet (real), 80002 for Amoy testnet

## Documentation

For deeper specifications and implementation details, see:

- **[OPERATIONS.md](docs/OPERATIONS.md)** — Deployment, run modes, settlement, recovery, and logging runbook
- **[DB_SCHEMA.md](docs/DB_SCHEMA.md)** — Complete database schema with column definitions, types, units, and ownership
- **[TECHNICAL_SPECIFICATION.md](docs/TECHNICAL_SPECIFICATION.md)** — System architecture, data flows, probability model
- **[SPIKE_DOCUMENTATION.md](docs/SPIKE_DOCUMENTATION.md)** — Implementation decisions, backtest analysis
- **[IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)** — Planned features and enhancements

## Historical Testing & Archives (Deprecated)

Early spike testing results from May 2026 have been deprecated as of 2026-06-16 (post-Shadow-Stations epic). These are archived in [`archive/early-spike-results-may-2026/`](archive/early-spike-results-may-2026/) for historical record only.

**⚠️ These results should NOT be used for current decision-making.** The model, risk parameters, station selection, and market conditions have all evolved significantly since then.

See [`archive/early-spike-results-may-2026/DEPRECATION.md`](archive/early-spike-results-may-2026/DEPRECATION.md) for historical context.

## Testing

Run the test suite to validate your installation:

```bash
pytest src/tests/
```

## Project Status

Sprint 1 — Core system and live trading: **Complete**

Sprint 2 — Monitoring, alerts, and operational hardening: **In Progress**

Recent validated improvements:
- TAF (Terminal Aerodrome Forecast) integration for forecast reliability
- Intraday temperature correction model
- DEB hourly consensus weighting
- Position snapshots for live trading validation

## License

Proprietary — Internal use only.

---

**Last Updated**: 2026-06-10  
**Status**: Live trading in progress  
**Supported Stations**: 11 (5 US, 6 international)
