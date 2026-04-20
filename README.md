# MeteoEdge — Autonomous Weather Trading Bot

An autonomous trading agent that identifies and executes arbitrage trades on Kalshi intraday temperature bracket contracts using physical weather observations.

## Quick Start

**Status:** Ready for epic decomposition (Stage 0 planning phase)

- **Primary docs:** See [/docs/kalshi-weather-bot-spec.md](docs/kalshi-weather-bot-spec.md) for the full technical specification
- **Design specs:** See [/docs/design/](docs/design/) for component design
- **Target environment:** Ubuntu 20.04+ on Mac mini (or comparable)
- **Target market:** Kalshi weather brackets — 5 US cities, daily high temperature

## The Idea

MeteoEdge trades the mispricing that occurs when Kalshi's market prices lag behind physical reality. The structural edge is simple: the day's actual high temperature is often physically locked in hours before the market reprices it. We use:

- **NOAA METAR** — hourly airport observations to track the running daily high
- **NWS Forecast API** — zone forecasts with physics-grounded temperature ceilings
- **Kalshi API** — market snapshots and order execution
- **LLM sanity check** — provider-agnostic layer (Claude, DeepSeek, or OpenAI) — validates trades, not decides them

## System Architecture

```
Mac mini (Ubuntu)
├─ METAR Poller (10 min)
├─ NWS Forecast Poller (1 hour)
├─ Kalshi Poller (30 sec)
└─ Strategy Engine
   ├─ Envelope Calculator
   ├─ Edge Scanner
   ├─ Risk Manager (kill switch, position limits)
   └─ LLM Sanity Check (provider-agnostic)
      └─ Order Router (Kalshi API)
         └─ Dashboard + Alerts (FastAPI + email)
```

## Core Modules

| Module | Responsibility | Language | Tests |
|---|---|---|---|
| **Envelope Engine** | Calculate physical daily high range given current conditions | Python | 100% (unit + integration) |
| **Edge Scanner** | Compute true probability and EV vs. market prices | Python | 100% (unit + properties) |
| **Risk Manager** | Enforce position, exposure, and drawdown limits | Python | 100% (unit) |
| **Sanity Checker** | LLM-powered validation layer (approval required, not decision). Provider-agnostic: Claude, DeepSeek, or OpenAI | Python | 100% (unit + integration) |
| **Order Router** | Place, amend, cancel orders via Kalshi SDK | Python | 100% (integration) |
| **Pollers** | METAR, NWS, Kalshi data ingestion with retry/backoff | Python | 100% (unit + mocking) |
| **Dashboard** | FastAPI + HTMX UI (positions, P&L, system health, risk events) | Python + HTML/CSS | Manual testing |
| **Backtester** | Tick-level replay, conservative fill simulation, metrics | Python | 100% (unit) |

## Data Model

**PostgreSQL:**
- `metar_observation` — hourly airport temperature readings
- `daily_high` — running maximum per station per day
- `nws_forecast` — forecast snapshots with temp ceiling
- `market_snapshot` — order book snapshots every 30s
- `decision` — all evaluated opportunities (traded or rejected)
- `trade_order` — full order lifecycle (place, amend, fill, cancel)
- `position` — current holdings
- `risk_event` — halt triggers, anomalies, audit log

**Redis:**
- Rate limit counters (Kalshi API)
- Hot cache for latest prices/envelopes
- Global halt flag

## Key Algorithms

### Envelope Calculation
Given current observations and forecast, compute the physically achievable range of the daily high:

```
min_achievable = current_high_f
max_achievable = max(
    current_high_f,
    latest_temp + expected_rise_to_sunset(hours_remaining, station, month)
)
```

### Edge Calculation
For each bracket:

```
P(bracket resolves YES) = bayesian_estimate(forecast, envelope)
EV = P(YES) × 100 - ask_price - trading_fee
```

Trade candidates require:
- Edge ≥ 3¢ after fees
- High confidence: P(YES) ≥ 0.80 or ≤ 0.20
- ≥ 15 min to settlement
- 2× liquidity depth

### Risk Rules (Non-Negotiable)
- Max exposure per ticker: 2% of bankroll
- Max total exposure: 10% of bankroll
- Max 30 trades per day
- Daily loss floor: -5% of bankroll (auto-halt)
- Stale data (METAR > 90 min or Kalshi > 90s): reject
- Kill switch file: `/var/run/meteoedge/STOP`

## Testing Strategy

**Four stages:**

1. **Historical Backtest (2 weeks)** — Replay 90 days of Kalshi snapshots + historical METAR. Exit if: win rate > 55%, avg EV > 8¢, Sharpe > 1.5, max DD < 20%.

2. **Demo Environment (2 weeks)** — Same code on Kalshi demo API. Exit if: 14 consecutive days zero exceptions, all risk rules trigger correctly, P&L reconciles to ±€0.50.

3. **Micro Live (4 weeks)** — Production API, 5–10 contract max per trade, €100 on platform. Exit if: live P&L ±15% of demo, no unexpected API edge cases.

4. **Graduated Scale-up (ongoing)** — Double per-trade size max every 2 weeks. Weekly P&L review and bottom-quintile culling.

## Deployment

**Directory layout:**
```
/opt/meteoedge/
├─ src/meteoedge/
│  ├─ pollers/ (METAR, NWS, Kalshi)
│  ├─ engine/ (envelope, edge, risk, sizing)
│  ├─ llm/ (sanity check)
│  ├─ execution/ (order router, reconciler)
│  ├─ db/ (models, migrations)
│  ├─ dashboard/ (FastAPI)
│  └─ config.py
├─ data/ (historical feeds, fee schedules)
└─ scripts/ (backtest, migrate, emergency_liquidate)

/etc/meteoedge/env (secrets, chmod 600)
/var/log/meteoedge/*.jsonl (structured logs, daily rotation)
/var/run/meteoedge/STOP (kill switch)
```

**systemd units (one per service):**
- `meteoedge-metar.service` (every 10 min)
- `meteoedge-nws.service` (every 60 min)
- `meteoedge-kalshi-poller.service` (continuous, 30s loop)
- `meteoedge-engine.service` (continuous, 30s loop)
- `meteoedge-dashboard.service` (FastAPI on localhost:8080)
- `meteoedge-daily-report.service` (23:00 UTC daily)

## Configuration

**Environment variables:**
```bash
KALSHI_ENV=demo|prod
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/etc/meteoedge/kalshi_private.pem

# LLM provider (choose one: anthropic, deepseek, openai)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...     # if LLM_PROVIDER=anthropic
DEEPSEEK_API_KEY=...      # if LLM_PROVIDER=deepseek
OPENAI_API_KEY=...        # if LLM_PROVIDER=openai

POSTGRES_DSN=postgresql://meteoedge@localhost:5432/meteoedge
REDIS_URL=redis://localhost:6379/0
BANKROLL_CAP_EUR=500
TRADING_ENABLED=false    # master switch for production
```

## Cost Model

| Item | Monthly cost |
|---|---|
| Electricity (24/7) | €3–5 |
| LLM API (~200 calls/day) | €6–90 (DeepSeek ~€6, OpenAI ~€60, Claude ~€90) |
| Kalshi trading fees | 2–5% of volume |
| **Fixed** | **€10–100** (depends on LLM provider) |

Break-even depends on LLM provider. With DeepSeek (~€6/mo), a €500 bankroll needs only ~2% monthly gross return. With Claude (~€90/mo), it needs ~20% on €500 or ~5% on €2,000.

## Team & Development

This is a **multi-agent project** coordinated via GitHub issues and project board:

- **Tech Lead PM** (Opus) — Architecture, planning, PR review, delegation
- **Designer** (Sonnet) — UX/UI specs, component design, frontend review
- **Mid Developer** (Sonnet) — Engine, risk logic, SDK integration
- **Junior Developer** (Haiku) — Pollers, dashboard, scripts, docs

All critical business logic requires 100% unit test coverage. See [CLAUDE.md](CLAUDE.md) for governance protocol and agent spawning templates.

## Success Criteria (V1 exit)

Over 3 months of Stage 4 operation:
- **Net return:** > 10% quarterly
- **Sharpe ratio:** > 1.2
- **Max drawdown:** < 15%
- **Zero unplanned halts**
- **Operator confidence:** high enough to grow bankroll 2x

## Getting Started

1. Read the full specification: [docs/kalshi-weather-bot-spec.md](docs/kalshi-weather-bot-spec.md)
2. Review the team structure in [CLAUDE.md](CLAUDE.md)
3. Check [docs/design/](docs/design/) for component specs
4. Start with Epic 1 (Infrastructure & Data Platform)

## References

- **Kalshi API:** https://kalshi.com/api-docs
- **NOAA METAR:** https://aviationweather.gov/api/data/metar
- **NWS Forecast:** https://api.weather.gov/
- **LLM Providers:**
  - Anthropic (Claude): https://api.anthropic.com/
  - DeepSeek: https://api.deepseek.com/
  - OpenAI: https://api.openai.com/

---

**Project codename:** MeteoEdge  
**Author:** André  
**Status:** Ready for epic decomposition  
**Last updated:** 2026-04-19
