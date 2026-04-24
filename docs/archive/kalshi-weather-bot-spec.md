# Technical Specification: Kalshi Intraday Weather Mean-Reversion Bot

**Project codename:** `MeteoEdge`
**Author:** André
**Version:** 0.1 (initial draft)
**Status:** Ready for epic decomposition
**Target platform:** Mac mini 2012 (i7, 16 GB DDR3) running Ubuntu
**Operator jurisdiction:** Portugal (Kalshi international tier)

---

## 1. Executive Summary

MeteoEdge is an autonomous trading agent that identifies and trades mispriced intraday temperature bracket contracts on Kalshi. Its structural edge is that the day's actual high temperature is often physically locked in hours before the market fully reprices, and the settlement data source (NOAA METAR / NWS Daily Climate Report) is public and free.

The system is a deterministic rules engine in Python with an LLM (Claude) acting as a sanity-check layer before every order — not as the decision-maker. The bot runs on the operator's existing Ubuntu Mac mini, polls public data, executes trades via the Kalshi API, and enforces strict risk limits.

**Non-goals.** This is not a generalized prediction-market bot. It does not trade politics, economics, or sports. It does not do cross-venue arbitrage. It does not use the LLM to pick winners.

## 2. Glossary

| Term | Meaning |
|---|---|
| Bracket | A temperature range contract, e.g. "NYC high in 82-84°F today" |
| METAR | Standardized hourly airport weather report |
| NWS | US National Weather Service |
| NOAA | US National Oceanic and Atmospheric Administration |
| Edge | True probability × $1 payout − (ask price + fee) |
| Envelope | The physically achievable range of remaining daily high given current observations |
| Settlement | Contract resolution based on the NWS Daily Climate Report |
| Maker / Taker | Limit order that rests / market order that crosses the spread |

## 3. Business Case

**Problem.** Retail prediction-market traders lose money by betting on news-driven markets (CPI, Fed, elections) where they have no informational edge against institutional players.

**Opportunity.** Weather markets are dominated by semi-pro retail who do not run METAR polling or ensemble forecast ingestion. Bracket prices frequently lag physical reality in the final 2-4 hours of the trading day, producing trades with bounded downside and calculable EV.

**Expected outcome.** After validation, 5-20% monthly return on deployed capital with Sharpe > 1.5 and max drawdown < 15%. These targets are aspirational and conditional on passing the four-stage testing gate (Section 11).

**Capital requirement.** €200-500 initial, scaling to €2,000-5,000 if Stage 3 validates. Never more than 30 days of trading capital held on platform.

## 4. Scope

### 4.1 In scope (V1)

- Five US cities: New York (KNYC), Chicago (KORD), Miami (KMIA), Austin (KAUS), Los Angeles (KLAX)
- Daily high temperature bracket markets only
- Intraday trading window: 12:00 local time through close of trading for that day's market
- Taker and maker order placement via Kalshi REST API
- Claude-as-sanity-checker pre-trade validation
- Full order lifecycle management (place, amend, cancel, reconcile)
- Operator dashboard and daily P&L email
- Kill switch and automated risk halts

### 4.2 Out of scope (V1)

- Rainfall, hurricane, tornado, or severe weather markets
- Non-US cities
- Multi-day weather markets
- Cross-market arbitrage with Polymarket
- Websocket streaming (V2 candidate)
- FIX connectivity
- Mobile or web UI beyond a local dashboard

### 4.3 Deferred to V2

- Additional cities (Denver, Seattle, Phoenix, Dallas)
- Rain/precipitation markets
- WebSocket order book feed for tighter entries
- Ensemble model forecast ingestion (GFS 31-member) for multi-day markets
- Multi-account / multi-operator support

## 5. System Architecture

### 5.1 High-level diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    Mac mini 2012 / Ubuntu                       │
│                                                                 │
│  ┌───────────────┐   ┌──────────────┐   ┌──────────────────┐    │
│  │ METAR Poller  │   │ NWS Forecast │   │ Kalshi Poller    │    │
│  │ (10 min)      │   │ Poller (1h)  │   │ (30 sec)         │    │
│  └──────┬────────┘   └──────┬───────┘   └────────┬─────────┘    │
│         │                   │                    │              │
│         └───────────────────┼────────────────────┘              │
│                             ▼                                   │
│                    ┌────────────────┐                           │
│                    │  PostgreSQL    │◄────┐                     │
│                    │  + Redis cache │     │                     │
│                    └────────┬───────┘     │                     │
│                             │             │                     │
│                             ▼             │                     │
│                 ┌───────────────────────┐ │                     │
│                 │  Strategy Engine      │ │                     │
│                 │  1. Envelope calc     │ │                     │
│                 │  2. Edge scanner      │ │                     │
│                 │  3. Risk manager      │ │                     │
│                 └───────────┬───────────┘ │                     │
│                             ▼             │                     │
│                 ┌───────────────────────┐ │                     │
│                 │  LLM Sanity Check    │ │                     │
│                 │  (provider-agnostic) │ │                     │
│                 └───────────┬───────────┘ │                     │
│                             ▼             │                     │
│                 ┌───────────────────────┐ │                     │
│                 │  Order Router        ─┼─┘                     │
│                 │  (Kalshi API)         │                       │
│                 └───────────┬───────────┘                       │
│                             ▼                                   │
│                 ┌───────────────────────┐                       │
│                 │  Dashboard + Alerts   │                       │
│                 │  (FastAPI, email)     │                       │
│                 └───────────────────────┘                       │
└─────────────────────────────────────────────────────────────────┘
         │                          │                  │
         ▼                          ▼                  ▼
   aviationweather.gov         api.weather.gov    api.elections.kalshi.com
   (METAR)                     (NWS forecast)     (market data + orders)
```

### 5.2 Component responsibilities

| Component | Responsibility | Deliverable |
|---|---|---|
| METAR Poller | Fetch hourly airport observations, maintain running daily max per station | systemd service |
| NWS Forecast Poller | Pull zone forecast text and parse temperature ceiling for next N hours | systemd service |
| Kalshi Poller | Snapshot all open weather markets every 30s, persist order book | systemd service |
| Envelope Engine | Given METAR + forecast + time-of-day, compute achievable daily high range | Python module |
| Edge Scanner | For each bracket, compute true probability and edge vs. ask | Python module |
| Risk Manager | Enforce position, exposure, and drawdown limits; own the kill switch | Python module |
| Sanity Checker | Pass proposed trade + context to configured LLM provider; require JSON approval | Python module |
| Order Router | Sign Kalshi requests, place/cancel/reconcile orders | Python module |
| Dashboard | Local FastAPI UI showing positions, P&L, live edges, system health | Web service |
| Alerter | Email on halts, anomalies, daily P&L summary | Python module |

### 5.3 Technology choices

- **Language:** Python 3.11 in a single venv
- **Storage:** PostgreSQL 15 (time-series data, trades, P&L), Redis 7 (rate-limit counters, hot cache)
- **HTTP:** `httpx` (async) for all external calls
- **Kalshi SDK:** `kalshi-python-sync` with RSA-PSS request signing
- **Process management:** `systemd` units, one per service
- **Secrets:** environment variables loaded from `/etc/meteoedge/env` with `chmod 600`
- **LLM:** Provider-agnostic via abstraction layer. Default: Anthropic (`claude-sonnet-4-6` sanity checks, `claude-haiku-4-5` parsing). Alternatives: DeepSeek, OpenAI, or any provider exposing a chat-completions-style API with structured JSON output
- **Observability:** structured JSON logs to `/var/log/meteoedge/*.jsonl`, rotated daily
- **Dashboard:** FastAPI + HTMX, served on `localhost:8080` only (no external exposure)

## 6. Data Model

### 6.1 Core tables (PostgreSQL)

```sql
-- Raw METAR observations (append-only)
CREATE TABLE metar_observation (
  id              BIGSERIAL PRIMARY KEY,
  station_code    VARCHAR(4) NOT NULL,       -- KNYC, KORD, etc.
  observed_at     TIMESTAMPTZ NOT NULL,
  temperature_f   NUMERIC(5,2) NOT NULL,
  raw_report      TEXT NOT NULL,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_metar_station_time ON metar_observation (station_code, observed_at DESC);

-- Running daily high per station (upsert)
CREATE TABLE daily_high (
  station_code    VARCHAR(4) NOT NULL,
  trade_date      DATE NOT NULL,
  current_high_f  NUMERIC(5,2) NOT NULL,
  high_time       TIMESTAMPTZ NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (station_code, trade_date)
);

-- NWS forecast snapshots
CREATE TABLE nws_forecast (
  id              BIGSERIAL PRIMARY KEY,
  station_code    VARCHAR(4) NOT NULL,
  valid_from      TIMESTAMPTZ NOT NULL,
  valid_to        TIMESTAMPTZ NOT NULL,
  forecast_high_f NUMERIC(5,2),
  forecast_text   TEXT,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Kalshi market snapshots
CREATE TABLE market_snapshot (
  id              BIGSERIAL PRIMARY KEY,
  ticker          VARCHAR(64) NOT NULL,
  station_code    VARCHAR(4) NOT NULL,
  trade_date      DATE NOT NULL,
  bracket_low_f   NUMERIC(5,2),
  bracket_high_f  NUMERIC(5,2),
  yes_ask_cents   INTEGER,
  yes_bid_cents   INTEGER,
  no_ask_cents    INTEGER,
  no_bid_cents    INTEGER,
  yes_ask_size    INTEGER,
  volume          INTEGER,
  snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_snapshot_ticker_time ON market_snapshot (ticker, snapshot_at DESC);

-- Decisions (every evaluated opportunity, traded or not)
CREATE TABLE decision (
  id                  BIGSERIAL PRIMARY KEY,
  ticker              VARCHAR(64) NOT NULL,
  side                VARCHAR(3) NOT NULL,   -- 'YES' or 'NO'
  true_prob           NUMERIC(5,4) NOT NULL,
  market_price_cents  INTEGER NOT NULL,
  computed_edge_cents NUMERIC(6,2) NOT NULL,
  rules_approved      BOOLEAN NOT NULL,
  claude_approved     BOOLEAN,
  claude_reason       TEXT,
  executed            BOOLEAN NOT NULL DEFAULT FALSE,
  decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Orders and fills
CREATE TABLE trade_order (
  id              BIGSERIAL PRIMARY KEY,
  external_id     VARCHAR(64) UNIQUE,
  ticker          VARCHAR(64) NOT NULL,
  side            VARCHAR(3) NOT NULL,
  action          VARCHAR(4) NOT NULL,        -- 'BUY' or 'SELL'
  order_type      VARCHAR(6) NOT NULL,        -- 'LIMIT' or 'MARKET'
  price_cents     INTEGER,
  quantity        INTEGER NOT NULL,
  filled_quantity INTEGER NOT NULL DEFAULT 0,
  status          VARCHAR(16) NOT NULL,       -- PENDING|OPEN|FILLED|CANCELLED|REJECTED
  decision_id     BIGINT REFERENCES decision(id),
  placed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finalized_at    TIMESTAMPTZ
);

-- Positions (derived, recalculated on each fill)
CREATE TABLE position (
  ticker          VARCHAR(64) NOT NULL,
  side            VARCHAR(3) NOT NULL,
  quantity        INTEGER NOT NULL,
  avg_cost_cents  NUMERIC(6,2) NOT NULL,
  realized_pnl    NUMERIC(10,2) NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ticker, side)
);

-- Risk events (halt triggers, anomalies)
CREATE TABLE risk_event (
  id              BIGSERIAL PRIMARY KEY,
  event_type      VARCHAR(32) NOT NULL,
  severity        VARCHAR(8) NOT NULL,        -- INFO|WARN|HALT
  details         JSONB,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  acknowledged_at TIMESTAMPTZ
);
```

### 6.2 Redis keys

```
rate:kalshi:{endpoint}        → counter, TTL 60s
price:{ticker}                → latest snapshot JSON, TTL 60s
envelope:{station}:{date}     → envelope JSON, TTL 900s
halt:global                   → "1" if halted, no TTL (manual reset)
```

## 7. Core Algorithms

### 7.1 Envelope calculation

Given for a station on a given date:
- `current_high_f` (running max since midnight local)
- `latest_temp_f` and `latest_temp_time`
- `sunset_time` and current local time `t_now`
- `hours_remaining = max(0, sunset_time - t_now)` (temps very rarely rise after sunset)
- `forecast_high_f` from NWS

Compute:

```
min_achievable_high = current_high_f
max_achievable_high = max(
    current_high_f,
    latest_temp_f + expected_rise(hours_remaining, station, month)
)
```

where `expected_rise` is a lookup from a 5-year historical table of hourly temperature climbs by station and month (95th percentile climb from time-of-day t to end-of-day).

For each bracket `[low, high]`:

```
P(Yes) =
    1.0  if low <= current_high_f AND high is reachable by envelope
    0.0  if low > max_achievable_high
    0.0  if high < current_high_f (bracket entirely below current high is impossible as daily high)
    bayesian_estimate(forecast_high, forecast_stddev, bracket)  otherwise
```

The bayesian_estimate uses a normal distribution around the forecast with a station-and-season-tuned stddev (typical values 1.5°F to 3°F).

### 7.2 Edge calculation

For each bracket and each side:

```
ev_yes_per_contract = P(Yes) * 100 - yes_ask_cents        (in cents)
ev_no_per_contract  = (1 - P(Yes)) * 100 - no_ask_cents

fee_per_contract = kalshi_fee_formula(price, quantity)    (see Section 7.3)

edge = max(ev_yes - fee, ev_no - fee)
```

Trade candidates must satisfy:
- `edge >= 3 cents` (minimum 3¢ expected value per contract after fees)
- `P(Yes) >= 0.80` (for YES side) or `P(Yes) <= 0.20` (for NO side) — only high-confidence trades
- `time_to_settlement >= 15 minutes` — never trade into the final 15 minutes
- `bracket liquidity (yes_ask_size) >= quantity * 2` — require 2x depth

### 7.3 Fee model

Kalshi fees are variable by contract price and quantity. Implement the fee schedule as a pure function:

```python
def kalshi_fee_cents(price_cents: int, quantity: int) -> int:
    # Reference: kalshi.com/fee-schedule (fetched and cached monthly)
    # Formula is approximately: fee = ceil(0.07 * quantity * price * (1 - price))
    # with a $0.01 floor per contract and tiered caps by category
    ...
```

Unit-test against 20+ worked examples from Kalshi's published schedule. Refetch and re-verify the schedule monthly; alert on any divergence.

### 7.4 Position sizing (Kelly, fractional)

```
kelly_fraction = (edge_per_dollar) / (payout_ratio)
position_size = min(
    fractional_kelly * bankroll * kelly_fraction,   # fractional_kelly = 0.25
    max_per_trade_cap,                              # 2% of bankroll
    liquidity_cap                                    # 20% of visible ask size
)
```

Always round down to integer contracts.

### 7.5 Risk manager rules (non-negotiable)

| Rule | Threshold | Action |
|---|---|---|
| Max exposure per ticker | 2% of bankroll | Reject trade |
| Max total exposure | 10% of bankroll | Reject trade |
| Max trades per day | 30 | Reject trade |
| Consecutive losing days | 3 | HALT, manual restart required |
| Daily P&L floor | -5% of bankroll | HALT immediately |
| Stale data (METAR > 90 min old) | true | Reject trade |
| Stale data (Kalshi > 90s old) | true | Reject trade |
| Manual kill switch | `/var/run/meteoedge/STOP` file exists | HALT immediately |
| Settlement proximity | < 15 min to market close | Reject new trades; do not cancel existing |

## 8. LLM Sanity-Check Contract

Before any order is placed, the strategy engine calls the configured LLM provider via an abstraction layer. The provider is interchangeable — Claude, DeepSeek, OpenAI, or any model that supports structured JSON output.

### 8.1 Provider abstraction

```
src/meteoedge/llm/
├── provider.py          # LLMProvider protocol (ABC)
├── anthropic_provider.py  # Anthropic (Claude) implementation
├── deepseek_provider.py   # DeepSeek implementation
├── openai_provider.py     # OpenAI-compatible implementation (covers any OpenAI-API-compatible endpoint)
└── sanity_check.py        # Sanity check logic — uses LLMProvider, provider-agnostic
```

The `LLMProvider` protocol defines:
```python
class LLMProvider(Protocol):
    def sanity_check(self, request: SanityCheckRequest) -> SanityCheckResponse: ...
    def parse_text(self, prompt: str, response_schema: type[T]) -> T: ...
    @property
    def name(self) -> str: ...
    @property
    def cost_per_call_estimate(self) -> float: ...
```

Each provider implementation handles its own:
- Authentication (API keys, signing)
- Request formatting (Messages API vs. chat completions vs. provider-specific)
- Response parsing into the common `SanityCheckResponse` schema
- Error handling and timeouts

Provider selection is via `LLM_PROVIDER` env var. The sanity-check module never imports a specific provider directly.

### 8.2 Input payload (provider-agnostic)

```json
{
  "proposed_trade": {
    "ticker": "KXHIGHNY-25APR18-B85",
    "side": "NO",
    "quantity": 20,
    "price_cents": 8,
    "bracket": "85-87°F",
    "station": "KNYC"
  },
  "context": {
    "current_local_time": "2026-04-18T16:45:00-04:00",
    "sunset_time": "2026-04-18T19:42:00-04:00",
    "running_daily_high_f": 81.2,
    "running_daily_high_time": "2026-04-18T14:20:00-04:00",
    "latest_temp_f": 78.5,
    "latest_temp_time": "2026-04-18T16:30:00-04:00",
    "nws_forecast_high_f": 82,
    "nws_forecast_text": "Partly sunny, high near 82. Breezy, with a southwest wind...",
    "computed_true_prob_yes": 0.06,
    "computed_edge_cents": 4.2
  },
  "question": "Is there any reason this trade is wrong? Consider frontal systems, unusual forecast divergence, data anomalies, DST issues, holiday resolution quirks, or anything else that would invalidate the computed edge."
}
```

### 8.3 Required response schema (all providers must produce this)

```json
{
  "approve": true,
  "confidence": 0.9,
  "reason": "Current temp 78.5°F with 3 hours to sunset cannot plausibly reach 85°F. Cooling curve begins after peak at 14:20.",
  "warnings": []
}
```

Each provider implementation is responsible for coercing its model's output into this exact schema. If the model returns malformed output after retries, treat as a rejection (fail-safe).

### 8.4 Handling logic

- `approve: false` → abort trade, log rationale, increment rejection counter
- `approve: true, confidence < 0.7` → abort trade (LLM uncertain is a halt signal)
- `warnings` non-empty and flagging any of ["cold front", "data error", "anomaly"] → abort
- LLM API error or timeout (> 10s) → abort trade, log, continue
- Under no circumstance does LLM approval override a rules engine rejection (AND gate, not OR)

### 8.5 Cost control

Cap at 200 LLM calls per day (configurable via `LLM_DAILY_CALL_CAP`). If exceeded, halt new trades and alert. Cost tracking is per-provider since pricing varies significantly (e.g., DeepSeek is ~10x cheaper than Claude Sonnet for equivalent context).

## 9. External Integrations

### 9.1 NOAA METAR (Aviation Weather Center)

- **URL:** `https://aviationweather.gov/api/data/metar?ids=KNYC,KORD,KMIA,KAUS,KLAX&format=json&hours=2`
- **Auth:** none
- **Rate limit:** self-imposed 10 min poll, well under their limits
- **Failure mode:** cache last successful response for up to 90 min, then flag data as stale

### 9.2 NWS Forecast API

- **URL:** `https://api.weather.gov/points/{lat},{lon}/forecast/hourly`
- **Auth:** User-Agent header required (`MeteoEdge/0.1 (contact email)`)
- **Rate limit:** no published limit, self-imposed hourly poll
- **Failure mode:** cache last forecast for up to 3 hours

### 9.3 Kalshi Trade API

- **Base URL (prod):** `https://api.elections.kalshi.com/trade-api/v2`
- **Base URL (demo):** `https://demo-api.kalshi.co/trade-api/v2`
- **Auth:** RSA-PSS request signing per Kalshi docs
- **Rate limit:** respect published limits, implement Redis-backed token bucket
- **Failure mode:** exponential backoff, max 5 retries, then halt
- **Endpoints used:**
  - `GET /events` and `GET /markets` — discovery
  - `GET /markets/{ticker}` — specific bracket data
  - `GET /markets/{ticker}/orderbook` — depth
  - `POST /portfolio/orders` — place order
  - `DELETE /portfolio/orders/{id}` — cancel
  - `GET /portfolio/fills` — reconciliation
  - `GET /portfolio/positions` — position check

### 9.4 LLM Provider API

The LLM integration is provider-agnostic. Configure via `LLM_PROVIDER` env var. Each provider has its own auth and endpoint config:

| Provider | Env vars | Primary model | Parsing model | Approx. cost/call |
|---|---|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` | `claude-haiku-4-5` | ~$0.015 |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` | `deepseek-chat` | ~$0.001 |
| `openai` | `OPENAI_API_KEY` | `gpt-4o` | `gpt-4o-mini` | ~$0.010 |

- **Failure mode (all providers):** abort trade, log, continue
- **Timeout:** 10s per call, non-negotiable
- **Provider-specific notes:**
  - Anthropic: Uses Messages API with structured JSON output via tool use
  - DeepSeek: Uses OpenAI-compatible chat completions endpoint with JSON mode
  - OpenAI: Uses chat completions with structured output / function calling
- **Adding a new provider:** Implement the `LLMProvider` protocol (see Section 8.1), register in provider factory, add env vars to config

## 10. Deployment

### 10.1 Directory layout

```
/opt/meteoedge/
├── .venv/
├── src/
│   ├── meteoedge/
│   │   ├── pollers/
│   │   │   ├── metar.py
│   │   │   ├── nws.py
│   │   │   └── kalshi.py
│   │   ├── engine/
│   │   │   ├── envelope.py
│   │   │   ├── edge_scanner.py
│   │   │   ├── risk_manager.py
│   │   │   └── sizing.py
│   │   ├── llm/
│   │   │   ├── provider.py           # LLMProvider protocol
│   │   │   ├── anthropic_provider.py  # Anthropic (Claude) implementation
│   │   │   ├── deepseek_provider.py   # DeepSeek implementation
│   │   │   ├── openai_provider.py     # OpenAI-compatible implementation
│   │   │   ├── factory.py            # Provider factory from config
│   │   │   └── sanity_check.py       # Provider-agnostic sanity check logic
│   │   ├── execution/
│   │   │   ├── order_router.py
│   │   │   └── reconciler.py
│   │   ├── db/
│   │   │   ├── models.py
│   │   │   └── migrations/
│   │   ├── dashboard/
│   │   │   └── app.py
│   │   └── config.py
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── backtest/
├── data/
│   ├── historical_metar/
│   ├── market_snapshots/
│   └── fee_schedule_v*.json
├── scripts/
│   ├── backtest.py
│   ├── migrate.py
│   └── emergency_liquidate.py
└── README.md

/etc/meteoedge/
└── env                       (chmod 600, private key path + API keys)

/var/log/meteoedge/
├── poller.jsonl
├── engine.jsonl
├── execution.jsonl
└── risk.jsonl

/var/run/meteoedge/
└── STOP                      (touch this file to halt immediately)
```

### 10.2 Systemd units

- `meteoedge-metar.service` — every 10 min via `OnCalendar=*:0/10`
- `meteoedge-nws.service` — hourly via `OnCalendar=hourly`
- `meteoedge-kalshi-poller.service` — long-running, 30s internal loop
- `meteoedge-engine.service` — long-running, 30s internal loop, depends on pollers
- `meteoedge-dashboard.service` — long-running, FastAPI on localhost:8080
- `meteoedge-daily-report.service` — once daily at 23:00 UTC, emails summary

All services run as user `meteoedge`, `Restart=on-failure`, `RestartSec=30s`.

### 10.3 Environment variables

```
KALSHI_ENV=demo|prod
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/etc/meteoedge/kalshi_private.pem

# LLM provider (choose one)
LLM_PROVIDER=anthropic          # anthropic | deepseek | openai
LLM_PRIMARY_MODEL=...           # override default primary model for the provider
LLM_PARSING_MODEL=...           # override default parsing model for the provider
LLM_DAILY_CALL_CAP=200          # max sanity-check calls per day

# Provider-specific keys (only the active provider's key is required)
ANTHROPIC_API_KEY=...            # required if LLM_PROVIDER=anthropic
DEEPSEEK_API_KEY=...             # required if LLM_PROVIDER=deepseek
OPENAI_API_KEY=...               # required if LLM_PROVIDER=openai
OPENAI_API_BASE=...              # optional: custom base URL for OpenAI-compatible endpoints

POSTGRES_DSN=postgresql://meteoedge@localhost:5432/meteoedge
REDIS_URL=redis://localhost:6379/0
ALERT_EMAIL_FROM=...
ALERT_EMAIL_TO=...
SMTP_HOST=...
SMTP_USER=...
SMTP_PASSWORD=...
BANKROLL_CAP_EUR=500
TRADING_ENABLED=false    # master switch, set to true after Stage 3
```

## 11. Testing Strategy

Four stages. Advance to the next only if the current stage passes.

### Stage 1 — Historical Backtest (2 weeks)

**Setup.**
- Scrape 90 days of Kalshi weather market snapshots (build a scraper or purchase from DeltaBase)
- Pull matching historical METAR from Iowa Environmental Mesonet (free bulk download)
- Pull matching NWS forecasts from archive.org or rebuild from the scraped historical forecast archives

**Execution.**
- Replay tick-by-tick through the Envelope, Edge Scanner, and Risk Manager modules
- Apply the fee formula to every simulated trade
- Simulate fills conservatively: assume you got the visible ask, not the mid

**Exit criteria (all required).**
- Win rate > 55%
- Average expected value per trade > 8 cents after fees
- Sharpe ratio > 1.5
- Max drawdown < 20%
- At least 100 trades in the backtest window

If any metric misses, the strategy as specified does not work. Do not proceed.

### Stage 2 — Demo Environment (2 weeks minimum)

**Setup.**
- Identical code, pointed at `demo-api.kalshi.co` with demo credentials
- Seed demo bankroll to match planned live bankroll (e.g. €500 equivalent)

**Execution.**
- Run the engine unmodified against demo market data
- Verify every code path end-to-end: place, fill, partial fill, cancel, reconcile
- Deliberately trigger each risk rule at least once (stale data, kill switch, daily loss limit)

**Exit criteria.**
- Zero unhandled exceptions in 14 consecutive days
- All risk halts trigger correctly and recover cleanly
- Daily P&L attribution reconciles to within €0.50 of Kalshi's reported P&L
- Operator (you) can confidently answer "what happened yesterday?" from the dashboard alone

### Stage 3 — Micro Live (4 weeks)

**Setup.**
- Production API credentials
- Position size capped at 5-10 contracts per trade (€3-7 exposure each)
- Total bankroll on platform capped at €100

**Execution.**
- Run identical code on production and demo in parallel, same signals
- Compare live P&L against demo P&L weekly

**Exit criteria.**
- Live P&L tracks demo P&L within ±15% over the month
- No unexpected order rejections or API edge cases
- Operator has manually reviewed at least 20 live trades end-to-end

### Stage 4 — Graduated Scale-up (ongoing)

**Rules.**
- Double max per-trade size no more often than every 2 weeks
- Never double total bankroll on platform in a single step
- Withdraw 50% of monthly profits off-platform weekly
- Weekly P&L attribution review: kill bottom-quintile sub-strategies (by city, bracket range, time-of-day)
- Monthly fee schedule reverification

### Continuous validation

- Demo and prod run identical code forever
- If demo-prod divergence exceeds 20% in any week, halt and investigate (edge decay or bug)
- Quarterly full re-backtest with the most recent 90 days

## 12. Security

- Kalshi private key stored in `/etc/meteoedge/kalshi_private.pem`, mode `0400`, owner `meteoedge`
- All secrets in `/etc/meteoedge/env`, mode `0600`, owner `meteoedge`, loaded via systemd `EnvironmentFile`
- No secrets in code, Git, logs, or dashboard
- Dashboard bound to `127.0.0.1` only; access via SSH tunnel
- PostgreSQL accepts localhost only; `meteoedge` role has no superuser rights
- Outbound network allowlist: active LLM provider endpoint only (`api.anthropic.com` / `api.deepseek.com` / `api.openai.com` based on `LLM_PROVIDER`), `api.elections.kalshi.com`, `demo-api.kalshi.co`, `aviationweather.gov`, `api.weather.gov`, SMTP host
- Daily logrotate, keep 30 days, weekly gzipped archive to external storage
- All order placements logged with decision ID, timestamp, payload hash, and response
- Monthly audit: operator reviews 10 random trades end-to-end from signal to settlement

## 13. Operational Runbook

**Normal startup.**
1. `systemctl start meteoedge-*`
2. Check dashboard at `http://localhost:8080`
3. Verify all 4 pollers green, engine green, recent METAR < 15 min old

**Manual halt.**
1. `touch /var/run/meteoedge/STOP`
2. Engine will stop placing new orders within 30s
3. Open positions continue to settlement (no panic exit)

**Resume after halt.**
1. Review risk event log and the `STOP` trigger
2. Document resolution in operator log
3. `rm /var/run/meteoedge/STOP`
4. Engine resumes on next cycle

**Emergency liquidation.**
1. `scripts/emergency_liquidate.py --confirm` cancels all open orders and places market sells on all positions
2. Operator-confirmed, logged, alerted

**Incident response.**
- Unexpected loss > 5% in a day → auto-halt, email alert, operator investigates within 24h
- API error rate > 10% → auto-halt, email alert
- Demo-prod divergence > 20% in a week → halt, full audit

## 14. Cost Model

| Item | Monthly cost |
|---|---|
| Hardware (sunk) | €0 |
| Electricity (Mac mini, 24/7) | €3-5 |
| PostgreSQL / Redis (self-hosted) | €0 |
| LLM API (~200 calls/day) — see provider comparison below | €6-90 |
| Kalshi trading fees | variable, ~2-5% of trade volume |
| Historical data (one-time, DeltaBase or similar) | €50-200 once |
| Deposit/withdrawal friction (debit card + FX) | ~2% per round trip |
| **Fixed operating cost** | **€10-100/month** (depends on LLM provider) |

**LLM provider cost comparison** (at ~200 calls/day, ~6,000/month):

| Provider | Model | Avg cost/call | Monthly estimate |
|---|---|---|---|
| Anthropic | Claude Sonnet | ~€0.015 | ~€90 |
| DeepSeek | DeepSeek Chat | ~€0.001 | ~€6 |
| OpenAI | GPT-4o | ~€0.010 | ~€60 |

Using DeepSeek drops the break-even to ~€10/month, making it viable even on a €500 bankroll (2% monthly gross return). With Anthropic, break-even requires ~€100/month — 20% monthly on €500 (aggressive) or 5% on €2,000 (realistic).

## 15. Success Criteria (V1 exit)

V1 is considered successful if, over 3 months of Stage 4 operation:

- Net return after all costs > 10% quarterly
- Sharpe ratio > 1.2 on realized trades
- Max drawdown < 15%
- Zero unplanned manual interventions (i.e., all halts were intended)
- Operator confidence high enough to grow bankroll by 2x

If any of these miss, V1 is paused and the strategy is re-evaluated.

## 16. Suggested Epic Decomposition

Below is a first-cut epic breakdown. Each epic should be estimated separately and prioritized to deliver a vertical slice to Stage 1 backtest as fast as possible.

### Epic 1 — Infrastructure & Data Platform
- Ubuntu host hardening and user setup
- PostgreSQL + Redis installation, schema migration tooling
- systemd unit scaffolding
- Structured logging framework
- Local dashboard skeleton (FastAPI + HTMX)

### Epic 2 — Data Ingestion
- METAR poller
- NWS forecast poller
- Kalshi market poller (demo + prod config)
- Rate-limit and retry framework
- Historical data backfill tooling

### Epic 3 — Strategy Engine
- Envelope calculation module
- Historical climb-rate lookup table builder
- Edge scanner
- Kalshi fee formula (with unit tests)
- Position sizing (fractional Kelly)

### Epic 4 — Risk Management
- Risk manager module with all rules from Section 7.5
- Kill switch (file-based)
- Halt / resume workflow
- Daily P&L limit enforcement

### Epic 5 — LLM Sanity Check
- LLM provider abstraction layer (protocol + factory)
- Provider implementations: Anthropic, DeepSeek, OpenAI-compatible
- Structured request/response schema (provider-agnostic)
- Cost tracking and daily cap (per-provider cost awareness)
- Rejection-reason logging

### Epic 6 — Order Execution
- Kalshi SDK integration with RSA-PSS auth
- Order router (place, cancel, amend)
- Reconciler (fills, positions)
- Emergency liquidation script

### Epic 7 — Backtest Harness
- Tick-level replay engine
- Fee simulation
- Conservative fill simulation
- Reporting: win rate, EV, Sharpe, drawdown

### Epic 8 — Operational Tooling
- Daily P&L email
- Dashboard: positions, live edges, system health, risk events
- Alerting (halts, anomalies, API errors)
- Operator runbook documentation

### Epic 9 — Compliance & Security
- Secrets management and rotation
- Outbound allowlist
- Audit logging
- Monthly fee schedule reverification job

### Epic 10 — Production Rollout
- Stage 2 demo run (2 weeks)
- Stage 3 micro-live run (4 weeks)
- Stage 4 scale-up checklist
- Post-mortem and retrospective template

## 17. Agent Development Workflow

Given the operator's existing Claude Code senior/junior agent architecture from CondoGest, reuse the same pattern:

- **Orchestrator (Opus):** Owns Epic-level planning, breaks epics into stories, arbitrates between senior and junior outputs.
- **Senior agent (Sonnet):** Owns the Strategy Engine, Risk Manager, and Order Execution modules — the code paths where correctness is critical.
- **Junior agents (Haiku):** Own pollers, SDK wrappers, dashboard UI, and documentation — boilerplate-heavy, lower-stakes code.
- **Reviewer (Haiku):** Independent review on every PR, different GitHub machine user per the operator's existing multi-agent setup.

All critical business logic (envelope, edge scanner, risk manager, fee formula) requires 100% unit test coverage before merge. LLM-generated code is never trusted for risk-critical paths without operator review.

## 18. Open Questions

1. **Fee schedule canonical source.** Need to decide whether to parse `kalshi.com/fee-schedule` HTML, the published PDF, or reverse-engineer from actual fill responses. Recommend: reverse-engineer + cross-check against published schedule monthly.
2. **Historical Kalshi data.** DeltaBase vs. self-scraping vs. Kalshi's own 3-month window. Recommend: start with the 3-month native window for Stage 1, evaluate DeltaBase if edge is confirmed.
3. **International tier specifics.** Confirm with Kalshi support whether weather markets are available to Portugal-resident accounts and whether any fee or market differences apply. Block Epic 10 on this answer.
4. **DST / local time edge cases.** The NWS Climate Report uses local standard time even during DST. Envelope logic must handle this — add a dedicated test suite.
5. **LLM model version pinning.** Pin specific model strings per provider (e.g. `claude-sonnet-4-6`, `deepseek-chat`) via `LLM_PRIMARY_MODEL` / `LLM_PARSING_MODEL` env vars. Refresh deliberately, never automatically. When switching providers, run at least 1 week of parallel observation (old provider + new provider on same trades) to verify comparable approval/rejection behavior before cutting over.

---

## Appendix A — Minimum Viable Spike

If the operator wants a 1-weekend spike to validate the premise before investing in the full build, here is the minimum viable version:

1. Single Python script, no database, no services
2. Hard-code 5 cities and poll METAR + Kalshi every 5 minutes for one trading day
3. Run envelope + edge scanner on each snapshot, print candidates to console
4. No order placement — observe only
5. Over 5-10 trading days, compare the candidates flagged against actual settlement outcomes

If the spike shows ≥55% of flagged high-confidence candidates resolved profitably, proceed with the full build. If not, the core premise is weaker than expected and the spec should be revisited before further investment.

---

*End of specification. Ready for epic estimation and story decomposition.*
