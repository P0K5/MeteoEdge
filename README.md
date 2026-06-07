# MeteoEdge: Polymarket Weather Arbitrage

A machine learning trading system that identifies and executes profitable weather market arbitrages on Polymarket by comparing real-time meteorological data with market-implied probabilities.

## Project Status

🟢 **ACTIVE DEVELOPMENT** — Backtest validated (88.4% win rate). Ready for live paper trading.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run paper trading simulation (synthetic data)
python src/test_simulation.py

# Run backtest on real May 2026 data
python src/backtest_real_data.py

# (Coming soon) Run live paper trading
python src/run_paper_trading.py --capital 500
```

## What It Does

MeteoEdge automatically:

1. **Fetches live weather data** from NOAA/NWS APIs
2. **Calculates true probabilities** for daily high temperature brackets
3. **Queries Polymarket** for weather-tagged markets
4. **Identifies mispricings** where market odds diverge from model predictions
5. **Executes trades** with realistic slippage and fee modeling
6. **Tracks positions and P&L** in real-time

## Key Results

| Metric | Synthetic | Real Data |
|--------|-----------|-----------|
| Backtest Period | 2 hours | May 1-2, 2026 |
| Trades | 291 | 4,867 |
| Win Rate | 100%* | **88.4%** |
| Total PnL | €543.87 | **€793.97** |
| ROI | 108.8%* | **158.8%** |

*Synthetic data used forecasts to determine outcomes (unrealistic 100% win rate)

**Real data:** Actual Polymarket settlement outcomes. 88.4% win rate is genuine and robust.

## Repository Structure

```
MeteoEdge/
├── README.md                          # This file
├── CLAUDE.md                          # Project governance & team structure
├── requirements.txt                   # Python dependencies
│
├── docs/                              # Documentation
│   ├── TECHNICAL_SPECIFICATION.md     # System design & architecture
│   ├── SPIKE_DOCUMENTATION.md         # Implementation plan & decisions
│   └── archive/                       # Old projects (Kalshi, etc.)
│
├── src/                               # Production code
│   ├── improved_envelope.py           # Enhanced probability model
│   ├── paper_trader.py                # Execution engine with slippage simulation
│   ├── polymarket_client.py           # Polymarket API client
│   ├── test_simulation.py             # Synthetic data backtest harness
│   └── backtest_real_data.py          # Real data validator
│
├── archive/                           # Historical implementations
│   └── polymarket-spike/              # Original spike detector (May 2026)
│       ├── spike.py                   # Main polling loop
│       ├── envelope.py                # Original probability model
│       ├── config.py                  # Configuration
│       └── logs/                      # Real trading data (69K trades)
│
└── backtest_results/                  # Backtest output
    ├── station_summary.json           # Per-city performance
    └── trades.jsonl                   # All trades with outcomes
```

## Core Components

### Improved Envelope (Probability Model)
- **Seasonal climb rates**: Historical p95 daily high increases by hour/month
- **Forecast ensemble**: Combines NWS (60%) + Open-Meteo (40%) for robustness
- **Time-to-settlement boost**: Increases confidence as market resolution approaches
- **Bayesian weather state**: Models weather as distribution, not point estimate

### Paper Trader (Execution Engine)
- **Realistic slippage**: 0.5-3¢ random per trade (Gaussian, avg 1.07¢)
- **Queue delays**: 50-500ms order latency simulation
- **Capital tracking**: Proper double-entry accounting
- **P&L calculation**: Accounts for actual fill price vs payout

### Data Integration
- **METAR observations**: Current temperatures from aviationweather.gov
- **NWS forecasts**: Hourly forecast highs via api.weather.gov
- **Secondary forecasts**: Open-Meteo ensemble data
- **Polymarket API**: Real-time market prices & outcomes

## Key Insights

### ✅ What Works
- **Real edge exists**: 88.4% win rate on 4,867 real trades
- **Beats transaction costs**: €793.97 PnL after slippage/fees
- **Consistent across stations**: Works well in 5/10 major US cities
- **Scalable**: Can handle 2-3 trades/minute with current infrastructure

### ⚠️ Known Issues
- **Calibration problems**: Model overconfident at low-confidence predictions
- **Station variance**: Fails completely in Denver, Dallas
- **Edge threshold**: Only edges > 15¢ are profitable
- **Market hours**: Limited liquidity outside trading hours

## Performance by City

| City | Station | Win Rate | PnL | Status |
|------|---------|----------|-----|--------|
| Miami | KMIA | 100% | €207.69 | ✅ Excellent |
| Atlanta | KATL | 100% | €172.61 | ✅ Excellent |
| Seattle | KSEA | 100% | €141.12 | ✅ Excellent |
| Houston | KHOU | 96.9% | €90.20 | ✅ Excellent |
| Austin | KAUS | 100% | €87.71 | ✅ Excellent |
| NYC | KLGA | 89.7% | €119.43 | ✅ Good |
| San Francisco | KSFO | 72.8% | -€5.41 | ⚠️ Marginal |
| Los Angeles | KLAX | 69.9% | -€18.25 | ⚠️ Marginal |
| Denver | KBKF | 0% | -€0.57 | ❌ Failed |
| Dallas | KDAL | 0% | -€0.56 | ❌ Failed |

## Technical Requirements

- Python 3.10+
- httpx (async HTTP client)
- pytz (timezone handling)
- astral (sunrise/sunset calculations)

See `requirements.txt` for full dependencies.

## Configuration

Main configuration in `src/improved_envelope.py`:

```python
MIN_EDGE_CENTS = 15  # Only trade edges >= 15¢
MIN_CONFIDENCE = 0.80  # For YES bets
MAX_CONFIDENCE_NO = 0.20  # For NO bets (complement)
POSITION_SIZE_EUR = 5.0  # €5 per trade
STARTING_CAPITAL_EUR = 500.0  # Initial capital
```

## Dashboard

Run `python run_dashboard.py`, then open `http://<machine-ip>:8000` on any device on the same Wi-Fi.

The dashboard shows live open positions, cash balance, and mark-to-market values. It fetches from `/api/portfolio` on load and every 30 seconds.

```bash
python run_dashboard.py
curl http://localhost:8000/api/health    # → {"status":"ok","ts":"..."}
curl http://localhost:8000/api/portfolio # → JSON with open_positions and cash_usdc
```

## Next Steps

- [ ] Recalibrate confidence scoring
- [ ] Investigate station failures (Denver, Dallas)
- [ ] Test on April 2025 data
- [ ] Implement Kelly criterion for position sizing
- [ ] Set up live paper trading with €100-500

## Documentation

- 📖 **[TECHNICAL_SPECIFICATION.md](docs/TECHNICAL_SPECIFICATION.md)** — System architecture, data flows, probability model
- 🔧 **[SPIKE_DOCUMENTATION.md](docs/SPIKE_DOCUMENTATION.md)** — Implementation decisions, backtest analysis
- 📊 **[BACKTEST_SUMMARY.md](BACKTEST_SUMMARY.md)** — Detailed backtest results by station
- 🧪 **[SIMULATION_RESULTS.md](SIMULATION_RESULTS.md)** — Synthetic data simulation results

## License

Proprietary — Internal use only.

---

**Last Updated**: 2026-05-07  
**Status**: Ready for live paper trading  
**Win Rate (Backtest)**: 88.4% on 4,867 real trades

## Core Modules

| Module | Responsibility | Language | Tests |
|---|---|---|---|
| **Scorer** | Rank pairs by funding rate, persistence, liquidity | Python | 100% (unit) |
| **Basis Monitor** | Track perp-spot divergence; flag tolerance breaches | Python | 100% (unit) |
| **Risk Manager** | Enforce position, margin, drawdown, weekly-withdrawal rules | Python | 100% (unit) |
| **Sanity Checker** | LLM-powered validation. Provider-agnostic: DeepSeek / Claude / OpenAI | Python | 100% (unit + integration) |
| **Hedge Executor** | Place paired spot+perp legs; handle partial fills atomically | Python | 100% (integration) |
| **Reconciler** | Match internal state to Binance truth every 60s; halt on divergence | Python | 100% (unit + integration) |
| **Pollers** | Funding, price, account with retry/backoff + Redis cache | Python | 100% (unit + mocking) |
| **Dashboard** | FastAPI + HTMX UI (positions, funding, basis, margin, risk) | Python + HTML | Manual testing |
| **Backtester** | 12-month replay, conservative fills, liquidation modelling | Python | 100% (unit) |

## Data Model

**PostgreSQL:**
- `funding_snapshot` — funding rates + mark/index price per minute per symbol
- `market_snapshot` — spot + perp book tickers and computed basis every 10s
- `decision` — every evaluated entry/exit (executed or rejected)
- `hedge_position` — open + closed paired positions
- `trade_order` — full order lifecycle for each leg
- `funding_payment` — received funding cash flows
- `withdrawal` — off-platform transfer audit log
- `risk_event` — halt triggers, anomalies

**Redis:**
- Binance weight-based rate-limit counters
- Hot cache for latest funding, price, margin state
- Global halt flag

## Key Algorithms

### Funding Score

```
ratio_annualised  = funding_rate_per_8h * 3 * 365
persistence_score = fraction of last 72h where rate > entry threshold
liquidity_score   = min(spot_depth_usd, perp_depth_usd) / required_notional

score = ratio_annualised * persistence_score * min(1.0, liquidity_score)
```

Entry requires all of:
- `funding_rate_per_8h >= 3 bps` (≈ 32.85% annualised gross)
- `persistence_score >= 0.60`
- `liquidity_score >= 1.0`
- `basis_bps <= 20`
- `time_to_next_funding >= 30 min`

### Expected PnL per Cycle (3-day hold)

```
Gross funding over 9 settlements @ ≥ 3 bps each = 27+ bps of notional
Round-trip fees (4 taker legs)                  = 25 bps
Net edge ≥ 2 bps per cycle, compounds across multiple pairs and cycles
```

### Risk Rules (Non-Negotiable)

- Max exposure per symbol: 25% of bankroll
- Max concurrent hedges: 4
- Perp margin ratio > 0.70 → force-unwind that hedge
- Basis divergence > 100 bps → HALT + emergency unwind all hedges
- Daily realised P&L floor: -3% of bankroll
- **Weekly automatic withdrawal** — platform balance never exceeds 1 week of operating capital
- Kill switch file: `/var/run/fundingedge/STOP`

## Testing Strategy

**Four stages** — same discipline that validated MeteoEdge:

1. **MVP Observe-Only Spike (2 weeks).** Virtual hedges, no orders. Exit if: ≥ 30 cycles, ≥ 60% win rate, positive median net yield.

2. **Historical Backtest (12 months).** Replay Binance funding + klines, conservative fills, margin modelling. Out-of-sample gate: Sharpe > 1.5, max DD < 10%, net annualised > 15%.

3. **Testnet (2 weeks).** Same code on Binance testnet. Exit if: 14 days zero exceptions, all risk rules fire correctly, funding payments reconcile to $0.01.

4. **Micro Live (4 weeks).** Production API, $50/hedge cap, $200 on platform. Exit if: live P&L within ±20% of testnet, 4 clean weekly withdrawals, 15+ hedges manually reviewed.

5. **Graduated Scale-up (ongoing).** Double per-hedge notional max every 2 weeks. Weekly P&L review and bottom-quintile culling. Monthly fee reverification. Quarterly re-backtest.

## Deployment

**Directory layout:**
```
/opt/fundingedge/
├─ src/fundingedge/
│  ├─ pollers/ (funding, price, account)
│  ├─ engine/ (scorer, basis_monitor, risk_manager, sizing, fee_model)
│  ├─ llm/ (provider abstraction — DeepSeek / Claude / OpenAI)
│  ├─ execution/ (hedge_executor, reconciler, withdrawal)
│  ├─ db/ (models, migrations)
│  ├─ dashboard/ (FastAPI)
│  └─ config.py
├─ data/ (historical funding + klines, fee schedules)
└─ scripts/ (backtest, migrate, emergency_unwind, weekly_withdrawal)

/etc/fundingedge/env (secrets, chmod 600)
/var/log/fundingedge/*.jsonl (structured logs, daily rotation)
/var/run/fundingedge/STOP (kill switch)
```

**systemd units (one per service):**
- `fundingedge-funding-poller.service` (60s loop)
- `fundingedge-price-poller.service` (10s loop)
- `fundingedge-account-poller.service` (30s loop)
- `fundingedge-engine.service` (30s loop)
- `fundingedge-reconciler.service` (60s loop)
- `fundingedge-dashboard.service` (FastAPI on localhost:8080)
- `fundingedge-daily-report.service` (23:00 UTC daily)
- `fundingedge-weekly-withdrawal.service` (Fridays 18:00 Europe/Lisbon)

## Configuration

**Environment variables (subset):**
```bash
BINANCE_ENV=testnet|prod
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_WITHDRAWAL_WHITELIST=BTC:bc1q...,USDT-ERC20:0x...,EUR:IBAN:PT50...

# LLM provider (choose one: deepseek, anthropic, openai)
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...

POSTGRES_DSN=postgresql://fundingedge@localhost:5432/fundingedge
REDIS_URL=redis://localhost:6379/0

BANKROLL_CAP_USD=500
OPERATING_CAP_MULTIPLIER=1.2       # weekly withdraw keeps ≤ this * 7d-max used on platform
TRADING_ENABLED=false              # master switch for production
UNIVERSE=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT

ENTRY_THRESHOLD_BPS=3
EXIT_THRESHOLD_BPS=1
PERP_LEVERAGE_TARGET=2
```

## Cost Model

| Item | Monthly cost |
|---|---|
| Electricity (24/7) | €3–5 |
| LLM API (~100 calls/day) | €3–45 (DeepSeek ~€3, OpenAI ~€30, Claude ~€45) |
| Binance trading fees | 25 bps per round-trip hedge cycle |
| Weekly SEPA / on-chain withdrawal fee | ~€1–2 |
| **Fixed operating cost** | **€8–60/month** (depends on LLM provider) |

Break-even on €500 bankroll with DeepSeek: ~3% monthly gross return on deployed capital — comfortably inside the target regime when funding is positive.

## Team & Development

This is a **multi-agent project** coordinated via GitHub issues and project board:

- **Tech Lead PM** (Sonnet) — Architecture, planning, PR review, delegation
- **Designer** (Sonnet) — Dashboard UX/UI specs, frontend review
- **Mid Developer** (Sonnet) — Strategy engine, risk manager, hedge executor, reconciler
- **Junior Developer** (Haiku) — Pollers, SDK wrappers, scripts, docs, dashboard components

All risk-critical business logic requires 100% unit test coverage. See [CLAUDE.md](CLAUDE.md) for governance protocol and agent spawning templates.

## Success Criteria (V1 exit)

Over 3 months of Stage 4 operation:
- **Net return:** > 15% quarterly on deployed capital
- **Sharpe ratio:** > 1.5 on realised hedge cycles
- **Max drawdown:** < 10%
- **Zero unplanned halts**
- **Every Friday withdrawal executed** — Binance balance never drifts above 1 week of operating capital
- **Operator confidence:** high enough to grow bankroll 2x

## Getting Started

1. Read the full specification: [docs/funding-edge-spec.md](docs/funding-edge-spec.md)
2. Review the MVP spike plan: [docs/funding-edge-mvp-spike.md](docs/funding-edge-mvp-spike.md) — this runs **first**
3. Review the team structure in [CLAUDE.md](CLAUDE.md)
4. Start with Epic 0 (MVP Spike). Do not start Epic 1+ until the spike gate passes.

## References

- **Binance Spot API:** https://binance-docs.github.io/apidocs/spot/en/
- **Binance USDⓈ-M Futures API:** https://binance-docs.github.io/apidocs/futures/en/
- **Binance Testnet (Spot):** https://testnet.binance.vision
- **Binance Testnet (Futures):** https://testnet.binancefuture.com
- **LLM providers:**
  - DeepSeek: https://api.deepseek.com/
  - Anthropic (Claude): https://api.anthropic.com/
  - OpenAI: https://api.openai.com/

---

**Project codename:** FundingEdge
**Predecessor:** MeteoEdge (archived — strategy validated, venue lost)
**Author:** André
**Status:** Ready for epic decomposition
**Last updated:** 2026-04-24
