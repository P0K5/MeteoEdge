# FundingEdge — Autonomous Binance Funding-Rate Arbitrage Bot

An autonomous trading agent that collects the funding-rate premium on Binance USDⓈ-M perpetual futures by running a delta-neutral cash-and-carry hedge: long spot, short perpetual, equal notional.

## Quick Start

**Status:** Ready for epic decomposition (Stage 0 spike phase)

- **Primary docs:** [/docs/funding-edge-spec.md](docs/funding-edge-spec.md) — full technical specification
- **MVP spike:** [/docs/funding-edge-mvp-spike.md](docs/funding-edge-mvp-spike.md) — observe-only premise validation (runs first)
- **Backtest harness:** [/docs/funding-edge-backtest-harness.md](docs/funding-edge-backtest-harness.md) — Stage 1 gate design
- **Implementation plan:** [/docs/Implementation-plan.md](docs/Implementation-plan.md) — epic + story breakdown
- **Target environment:** Ubuntu 20.04+ on Mac mini (same host MeteoEdge used)
- **Target venue:** Binance Spot + USDⓈ-M Futures (legal and bot-friendly for Portugal-resident operators)

## Project History

FundingEdge is the successor to **MeteoEdge**, a Kalshi weather-market trading bot. MeteoEdge's physical-envelope strategy was **validated in live data** — the observe-only spike confirmed the edge existed. The project was retired only because **Kalshi ceased operations for Portuguese residents**, eliminating the venue. All the archived design patterns — staged testing, LLM sanity check, risk manager, multi-agent development workflow — are reused here.

- MeteoEdge docs: [docs/archive/](docs/archive/)
- MeteoEdge spike code: [archive/meteoedge-spike/](archive/meteoedge-spike/)

## The Idea

On Binance perpetual futures, **funding payments** are exchanged every 8 hours between longs and shorts to keep the perpetual price anchored to spot. In typical bull-market conditions, longs pay shorts (funding rate > 0). By holding matched long-spot and short-perp positions, FundingEdge collects these payments with near-zero directional exposure.

The edge is **structural, not predictive.** We do not forecast price. The rate is already paid; we capture it.

- **Binance Funding API** — real-time funding rate and predicted next-funding
- **Binance Spot + Futures API** — paired leg execution
- **Historical funding archive** — free via Binance public endpoints, powers the backtest
- **LLM sanity check** — provider-agnostic (DeepSeek / Claude / OpenAI) — validates entries/exits, not decides

## System Architecture

```
Mac mini (Ubuntu)
├─ Funding Poller (60s)
├─ Spot + Perp Price Poller (10s)
├─ Account/Margin Poller (30s)
└─ Strategy Engine
   ├─ Funding Scorer
   ├─ Basis Monitor
   ├─ Risk Manager (kill switch, margin, weekly withdrawal)
   └─ LLM Sanity Check (provider-agnostic)
      └─ Hedge Executor (paired spot + perp)
         ├─ Reconciler
         └─ Dashboard + Alerts (FastAPI + email)
```

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
