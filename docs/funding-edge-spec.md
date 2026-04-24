# Technical Specification: Binance Funding-Rate Arbitrage Bot

**Project codename:** `FundingEdge`
**Author:** André
**Version:** 0.1 (initial draft)
**Status:** Ready for epic decomposition
**Target platform:** Mac mini 2012 (i7, 16 GB DDR3) running Ubuntu
**Operator jurisdiction:** Portugal
**Predecessor:** MeteoEdge (archived — see `archive/` for validated but venue-blocked weather strategy)

---

## 1. Executive Summary

FundingEdge is an autonomous trading agent that captures the funding-rate premium paid on Binance USDⓈ-M perpetual futures. The structural edge is that when funding is persistently positive, longs pay shorts every 8 hours to keep the perpetual anchored to spot. By holding **long spot** and **short perpetual** in equal notional, the bot collects funding payments with near-zero directional exposure.

The system is a deterministic rules engine in Python with an LLM (DeepSeek by default, Claude / OpenAI interchangeable) acting as a sanity-check layer before each position entry or exit — not as the decision-maker. The bot runs on the operator's existing Ubuntu Mac mini, polls Binance public data, executes hedge legs via the Binance API, and enforces strict risk limits including a mandatory weekly capital-withdrawal routine to contain platform risk.

**Why this strategy, why now.** The predecessor project MeteoEdge validated the operating pattern (structural edge, LLM sanity check, staged testing, Mac mini deployment) on Kalshi weather markets. Kalshi's exit from Portugal eliminated the venue, not the method. Funding-rate arbitrage is the closest analog: mechanical, not predictive; edge comes from market structure, not forecasting; downside is bounded when the hedge is intact.

**Non-goals.** This is not a directional trading bot. It does not predict price. It does not run momentum, mean-reversion on price, or pair trading between assets. It does not hold unhedged perpetual exposure. It does not use leverage beyond what is required to maintain the short-perp hedge against its spot collateral. It does not trade on Binance Options or non-USDⓈ-M instruments in V1.

## 2. Glossary

| Term | Meaning |
|---|---|
| Perpetual (perp) | A futures contract with no expiry, anchored to spot via funding payments |
| USDⓈ-M | USDT-margined perpetual contracts on Binance Futures |
| Funding rate | The periodic payment exchanged between longs and shorts (every 8h on Binance) |
| Funding interval | The 8-hour window between funding payments (00:00, 08:00, 16:00 UTC) |
| Basis | The price difference between the perpetual and its underlying spot |
| Cash-and-carry / funding arb | Long spot + short perp of equal notional, collecting funding |
| Hedge leg | One side (spot or perp) of the paired cash-and-carry position |
| Liquidation price | The perp price at which the short leg is force-closed by Binance |
| Maintenance margin | The minimum margin required to keep the perp position open |
| Premium index | The price premium of perp over spot, used in the funding rate formula |
| Edge | Expected funding-payment income per cycle minus expected costs |
| Maker / Taker | Limit order that rests in the book / order that crosses the spread |

## 3. Business Case

**Problem.** Retail crypto traders lose money by directionally trading price. The arms race against professional market makers with lower latency and cheaper capital is unwinnable from a Mac mini in Lisbon. What *is* winnable is the funding-rate carry — a structural premium that exists because leveraged longs outnumber leveraged shorts during crypto's default regime.

**Opportunity.** Binance's perpetual funding rate routinely exceeds 0.05% per 8 hours on large-cap pairs (BTC, ETH, and the top 20 altcoins) during bullish regimes. Annualized, that is >54% gross. The rate is published in real-time and paid deterministically at each funding time. A hedged position captures this payment with no directional exposure, subject to three known risks: basis blow-out, funding-rate flip, and platform risk.

**Expected outcome.** After validation, 15–40% annualised return on deployed capital when funding is positive, zero return during neutral or negative-funding periods. These are aspirational and conditional on passing the four-stage testing gate (Section 11).

**Capital requirement.** €250–500 initial, scaling to €2,000–5,000 if Stage 3 validates. **Hard platform cap: no more than one week of operating capital held on Binance at any time.** Profits above the operating cap withdraw automatically to a Portuguese bank account or a cold wallet every Friday.

## 4. Scope

### 4.1 In scope (V1)

- Binance USDⓈ-M perpetual futures paired with matching spot markets
- Initial universe: **BTCUSDT**, **ETHUSDT**, **SOLUSDT**, **BNBUSDT** (4 pairs — deep liquidity, reliable funding)
- Cash-and-carry funding arbitrage only (long spot, short perpetual, equal notional)
- Automated entry when realised funding rate exceeds threshold (default > 0.03% per 8h)
- Automated exit when rate normalises, flips negative, or basis widens beyond tolerance
- Mandatory weekly profit withdrawal off-platform
- LLM sanity-check pre-entry and pre-exit
- Full hedge-leg lifecycle (spot buy, perp sell, rebalance on fills, emergency unwind)
- Operator dashboard and daily P&L email
- Kill switch and automated risk halts

### 4.2 Out of scope (V1)

- Directional strategies (momentum, mean reversion on price)
- Cross-exchange arbitrage (Binance vs. OKX, Bybit, etc.)
- COIN-M (coin-margined) perpetuals
- Binance Options, Delivery Futures, Margin (spot margin), or Isolated Margin
- Altcoin pairs outside the V1 universe
- WebSocket order stream (V2 candidate — V1 uses REST polling)
- Mobile or web UI beyond a local dashboard

### 4.3 Deferred to V2

- Expanded universe (top 20 by funding-rate volume and depth)
- WebSocket-driven entry for tighter slippage control
- Multi-exchange arbitrage (Binance ↔ Bybit funding dispersion)
- Auto-rehypothecation: use spot holdings as cross-margin collateral for perp short (capital efficiency gain of ~2x; higher liquidation complexity)
- Basis-trade-only mode (fixed-expiry futures instead of perpetuals) when funding regime flips

## 5. System Architecture

### 5.1 High-level diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Mac mini 2012 / Ubuntu                            │
│                                                                      │
│  ┌────────────────┐  ┌─────────────────┐  ┌──────────────────────┐   │
│  │ Funding Poller │  │ Spot+Perp Price │  │ Account/Margin Poller│   │
│  │ (every 60s)    │  │ Poller (10s)    │  │ (30s)                │   │
│  └───────┬────────┘  └────────┬────────┘  └──────────┬───────────┘   │
│          │                    │                      │               │
│          └────────────────────┼──────────────────────┘               │
│                               ▼                                      │
│                      ┌────────────────┐                              │
│                      │  PostgreSQL    │◄────┐                        │
│                      │  + Redis cache │     │                        │
│                      └────────┬───────┘     │                        │
│                               │             │                        │
│                               ▼             │                        │
│                   ┌───────────────────────┐ │                        │
│                   │  Strategy Engine      │ │                        │
│                   │  1. Funding scorer    │ │                        │
│                   │  2. Basis monitor     │ │                        │
│                   │  3. Risk manager      │ │                        │
│                   └───────────┬───────────┘ │                        │
│                               ▼             │                        │
│                   ┌───────────────────────┐ │                        │
│                   │  LLM Sanity Check    │ │                        │
│                   │  (provider-agnostic) │ │                        │
│                   └───────────┬───────────┘ │                        │
│                               ▼             │                        │
│                   ┌───────────────────────┐ │                        │
│                   │  Hedge Executor      ─┼─┘                        │
│                   │  (paired spot+perp)   │                          │
│                   └───────────┬───────────┘                          │
│                               ▼                                      │
│                   ┌───────────────────────┐                          │
│                   │  Dashboard + Alerts   │                          │
│                   │  (FastAPI, email)     │                          │
│                   └───────────────────────┘                          │
│                               │                                      │
│                   ┌───────────────────────┐                          │
│                   │  Weekly Withdrawal    │                          │
│                   │  Job (Fridays 18:00)  │                          │
│                   └───────────────────────┘                          │
└──────────────────────────────────────────────────────────────────────┘
         │                          │                  │
         ▼                          ▼                  ▼
   api.binance.com            fapi.binance.com    (SMTP / off-platform)
   (spot markets)             (USDⓈ-M perps)
```

### 5.2 Component responsibilities

| Component | Responsibility | Deliverable |
|---|---|---|
| Funding Poller | Fetch current funding rate and predicted next-funding for each symbol | systemd service |
| Price Poller | Snapshot spot + perp bid/ask, compute basis every 10s | systemd service |
| Account Poller | Snapshot balances, positions, unrealised P&L, margin ratio every 30s | systemd service |
| Funding Scorer | Rank pairs by realised funding rate, expected persistence, and liquidity | Python module |
| Basis Monitor | Detect basis blow-outs that would force an emergency unwind | Python module |
| Risk Manager | Enforce position, exposure, margin, drawdown, and withdrawal rules | Python module |
| Sanity Checker | Pass proposed entry/exit + context to LLM; require JSON approval | Python module |
| Hedge Executor | Place paired spot+perp legs atomically; manage partial fills | Python module |
| Reconciler | Match internal state to Binance truth every 60s; halt on divergence | Python module |
| Withdrawal Job | Every Friday 18:00, withdraw profits above operating cap | systemd timer |
| Dashboard | Local FastAPI UI showing positions, funding schedule, basis, P&L, margin | Web service |
| Alerter | Email on halts, basis anomalies, liquidation warnings, daily P&L summary | Python module |

### 5.3 Technology choices

- **Language:** Python 3.11 in a single venv
- **Storage:** PostgreSQL 15 (time-series data, orders, positions, P&L), Redis 7 (hot cache, rate-limit counters)
- **HTTP:** `httpx` (async) for all external calls
- **Exchange SDK:** `python-binance` (maintained, well-documented, supports spot + futures) with HMAC-SHA256 request signing
- **Process management:** `systemd` units, one per service
- **Secrets:** environment variables loaded from `/etc/fundingedge/env` with `chmod 600`
- **LLM:** Provider-agnostic via abstraction layer. Default: **DeepSeek** (low cost, good enough for sanity-check schema). Alternatives: Anthropic Claude, OpenAI GPT-4o
- **Observability:** structured JSON logs to `/var/log/fundingedge/*.jsonl`, rotated daily
- **Dashboard:** FastAPI + HTMX, served on `localhost:8080` only (no external exposure)

## 6. Data Model

### 6.1 Core tables (PostgreSQL)

```sql
-- Funding-rate snapshots (append-only; every symbol every minute)
CREATE TABLE funding_snapshot (
  id                  BIGSERIAL PRIMARY KEY,
  symbol              VARCHAR(20) NOT NULL,           -- BTCUSDT, ETHUSDT, ...
  funding_rate        NUMERIC(10,8) NOT NULL,         -- realised last payment
  predicted_rate      NUMERIC(10,8),                  -- next-funding estimate (Binance's live estimate)
  funding_time        TIMESTAMPTZ NOT NULL,           -- next funding settlement time
  mark_price          NUMERIC(20,8) NOT NULL,
  index_price         NUMERIC(20,8) NOT NULL,
  snapshot_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_funding_symbol_time ON funding_snapshot (symbol, snapshot_at DESC);

-- Spot + perp price snapshots (append-only; every symbol every 10s)
CREATE TABLE market_snapshot (
  id              BIGSERIAL PRIMARY KEY,
  symbol          VARCHAR(20) NOT NULL,
  spot_bid        NUMERIC(20,8) NOT NULL,
  spot_ask        NUMERIC(20,8) NOT NULL,
  perp_bid        NUMERIC(20,8) NOT NULL,
  perp_ask        NUMERIC(20,8) NOT NULL,
  spot_bid_size   NUMERIC(20,8),
  perp_bid_size   NUMERIC(20,8),
  basis_bps       NUMERIC(10,4) NOT NULL,             -- (perp_mid - spot_mid) / spot_mid * 10000
  snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_market_symbol_time ON market_snapshot (symbol, snapshot_at DESC);

-- Decisions (every evaluated entry/exit candidate, traded or not)
CREATE TABLE decision (
  id                  BIGSERIAL PRIMARY KEY,
  symbol              VARCHAR(20) NOT NULL,
  action              VARCHAR(6) NOT NULL,            -- 'ENTER' or 'EXIT'
  funding_rate        NUMERIC(10,8) NOT NULL,
  basis_bps           NUMERIC(10,4),
  notional_usd        NUMERIC(14,2) NOT NULL,
  expected_pnl_bps    NUMERIC(10,4),                  -- per-cycle expected yield
  rules_approved      BOOLEAN NOT NULL,
  llm_approved        BOOLEAN,
  llm_reason          TEXT,
  executed            BOOLEAN NOT NULL DEFAULT FALSE,
  decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hedge positions (one row per active hedge; both legs tracked)
CREATE TABLE hedge_position (
  id                  BIGSERIAL PRIMARY KEY,
  symbol              VARCHAR(20) NOT NULL,
  spot_quantity       NUMERIC(20,8) NOT NULL,
  perp_quantity       NUMERIC(20,8) NOT NULL,         -- stored positive; side is always SHORT
  spot_entry_price    NUMERIC(20,8) NOT NULL,
  perp_entry_price    NUMERIC(20,8) NOT NULL,
  entry_basis_bps     NUMERIC(10,4) NOT NULL,
  opened_at           TIMESTAMPTZ NOT NULL,
  closed_at           TIMESTAMPTZ,
  realised_funding    NUMERIC(14,4) NOT NULL DEFAULT 0,
  realised_basis_pnl  NUMERIC(14,4) NOT NULL DEFAULT 0,
  fees_paid           NUMERIC(14,4) NOT NULL DEFAULT 0,
  status              VARCHAR(16) NOT NULL            -- OPEN | CLOSING | CLOSED | EMERGENCY_UNWOUND
);
CREATE INDEX idx_hedge_symbol_status ON hedge_position (symbol, status);

-- Orders and fills (one row per order leg; parent = hedge_position)
CREATE TABLE trade_order (
  id                  BIGSERIAL PRIMARY KEY,
  external_id         VARCHAR(64) UNIQUE,
  hedge_position_id   BIGINT REFERENCES hedge_position(id),
  venue               VARCHAR(10) NOT NULL,           -- 'SPOT' or 'PERP'
  symbol              VARCHAR(20) NOT NULL,
  side                VARCHAR(4) NOT NULL,            -- 'BUY' or 'SELL'
  order_type          VARCHAR(6) NOT NULL,            -- 'LIMIT' or 'MARKET'
  price               NUMERIC(20,8),
  quantity            NUMERIC(20,8) NOT NULL,
  filled_quantity     NUMERIC(20,8) NOT NULL DEFAULT 0,
  avg_fill_price      NUMERIC(20,8),
  status              VARCHAR(16) NOT NULL,           -- PENDING|OPEN|FILLED|CANCELLED|REJECTED
  fees_paid           NUMERIC(14,4) NOT NULL DEFAULT 0,
  decision_id         BIGINT REFERENCES decision(id),
  placed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finalized_at        TIMESTAMPTZ
);

-- Funding payments received (append-only; one row per perp funding settlement)
CREATE TABLE funding_payment (
  id                  BIGSERIAL PRIMARY KEY,
  hedge_position_id   BIGINT REFERENCES hedge_position(id),
  symbol              VARCHAR(20) NOT NULL,
  funding_time        TIMESTAMPTZ NOT NULL,
  rate                NUMERIC(10,8) NOT NULL,
  notional_usd        NUMERIC(14,4) NOT NULL,
  payment_usd         NUMERIC(14,4) NOT NULL,         -- negative if we paid (rate < 0)
  recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Withdrawals (audit log for off-platform transfers)
CREATE TABLE withdrawal (
  id                  BIGSERIAL PRIMARY KEY,
  destination         VARCHAR(80) NOT NULL,           -- 'BANK:IBAN:...' or 'WALLET:ADDRESS'
  asset               VARCHAR(10) NOT NULL,           -- 'USDT', 'EUR', 'BTC'
  amount              NUMERIC(20,8) NOT NULL,
  status              VARCHAR(16) NOT NULL,           -- PENDING|COMPLETED|FAILED
  external_id         VARCHAR(80),
  requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at        TIMESTAMPTZ
);

-- Risk events (halt triggers, anomalies)
CREATE TABLE risk_event (
  id              BIGSERIAL PRIMARY KEY,
  event_type      VARCHAR(32) NOT NULL,
  severity        VARCHAR(8) NOT NULL,                -- INFO|WARN|HALT
  details         JSONB,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  acknowledged_at TIMESTAMPTZ
);
```

### 6.2 Redis keys

```
rate:binance:{endpoint}       → counter, TTL 60s (Binance weight tracking)
funding:{symbol}              → latest funding snapshot JSON, TTL 120s
price:{symbol}                → latest market snapshot JSON, TTL 30s
hedge:open:{symbol}           → active hedge_position_id if any, no TTL
margin:ratio                  → current perp margin ratio (0.0–1.0), TTL 60s
halt:global                   → "1" if halted, no TTL (manual reset)
```

## 7. Core Algorithms

### 7.1 Funding score

For each symbol in the universe, score the attractiveness of entering a hedge:

```
ratio_annualised = funding_rate_per_8h * 3 * 365       # three 8h windows per day
persistence_score = fraction of last 72h where rate > entry_threshold
liquidity_score   = min(spot_bid_depth_usd, perp_bid_depth_usd) / required_notional

score = ratio_annualised
        * persistence_score
        * min(1.0, liquidity_score)
```

Candidate for entry requires all of:

- `funding_rate_per_8h >= ENTRY_THRESHOLD_BPS` (default 3 bps = 0.03%, ≈ 32.85% annualised gross)
- `persistence_score >= 0.60` (≥ 60% of last 9 funding windows were above threshold)
- `liquidity_score >= 1.0` (visible depth covers the planned notional on both legs)
- `basis_bps <= ENTRY_BASIS_CEILING` (default 20 bps) — avoid entering into already-expensive basis
- `time_to_next_funding >= 30 minutes` — guarantee we hold through the funding event we planned for

### 7.2 Exit rules

Exit an open hedge when **any** of these trigger:

| Condition | Threshold (default) | Rationale |
|---|---|---|
| Funding rate drops below exit threshold | < 1 bps per 8h | Edge has decayed below fee break-even |
| Funding rate turns negative for 2 consecutive windows | < 0 bps | Regime flip; we'd be paying to hold |
| Basis widens beyond tolerance | > 50 bps | Convergence risk; unwinding later costs more |
| Margin ratio on perp exceeds warning level | > 0.50 | Liquidation risk; spot unchanged, perp unrealised loss |
| Hedge age exceeds max holding period | > 14 days | Tail risk bounded; forced review |
| Global halt fired | — | Safety |

Exits are always **paired**: close perp short AND sell spot in the same cycle, sequenced to minimise momentary directional exposure.

### 7.3 Expected PnL per cycle

```
gross_funding_per_8h = funding_rate * notional_usd
cycles_per_day       = 3
annualised_gross     = funding_rate * 3 * 365 * notional_usd

entry_costs  = spot_taker_fee + perp_taker_fee + expected_slippage   (bps of notional)
exit_costs   = same
holding_cost = 0                                                     (funding is income, not cost)

net_expected_pnl_over_holding =
    sum(funding_payments_received) - entry_costs - exit_costs - basis_drift
```

Entry requires `net_expected_pnl_over_median_holding_period >= MIN_NET_EDGE_BPS` (default 10 bps of notional).

### 7.4 Fee model

Binance fees (as of the spec date, subject to monthly verification):

| Venue | Maker | Taker |
|---|---|---|
| Spot | 0.10% (default tier) / 0.075% (BNB-paid fees) | 0.10% / 0.075% |
| USDⓈ-M Perpetual | 0.020% | 0.050% |

Round-trip cost assumptions (taker on all four legs for V1):

- Spot buy + spot sell: 2 × 0.075% = **15 bps** (with BNB fee discount)
- Perp open + perp close: 2 × 0.050% = **10 bps**
- Total round-trip fee: **25 bps** of notional

To be profitable over a single cycle, realised funding must exceed 25 bps (plus slippage). The threshold of 3 bps per 8h × 9 cycles (3 days) = 27 bps — barely above break-even for a single-cycle hold. Holds of **3+ days** are the realistic target, accumulating 9+ funding payments against a single round-trip fee.

Implement the fee schedule as a pure function keyed by venue and maker/taker flag. Unit-test against worked examples. Refetch monthly; alert if the fee schedule has changed.

### 7.5 Position sizing

Per-hedge notional is capped by four constraints — take the minimum:

```
cap_per_symbol      = bankroll_usd * 0.25          # max 25% of bankroll per symbol
cap_total_exposure  = bankroll_usd * 1.00          # fully deployed only when ≥ 2 symbols active
cap_liquidity       = 0.20 * min(spot_bid_depth_usd, perp_bid_depth_usd)  # 20% of visible depth
cap_margin_room     = available_margin_usd / PERP_LEVERAGE_TARGET        # see §7.6
```

Always round down to Binance lot-step and notional-precision requirements.

### 7.6 Leverage and margin discipline

Perp position is entered with **Isolated Margin at 2x leverage** (V1). This gives:

- Liquidation distance: ≈ 50% adverse move in perp before the short is force-closed
- Clear isolation between hedges on different symbols
- Simpler risk accounting than cross-margin

For every $100 of notional on the perp leg, $50 of USDT sits as margin. The corresponding $100 spot leg is held fully funded (no spot margin). Capital efficiency is ~1.5x: $150 total capital supports $100 hedged notional.

**V2 candidate:** Binance Portfolio Margin / unified account to post spot collateral against perp, raising efficiency to ~1.9x. Requires additional liquidation modelling and is explicitly excluded from V1.

### 7.7 Risk manager rules (non-negotiable)

| Rule | Threshold | Action |
|---|---|---|
| Max exposure per symbol | 25% of bankroll | Reject entry |
| Max total notional | 100% of bankroll | Reject entry |
| Max concurrent hedges | 4 | Reject entry |
| Perp margin ratio | > 0.50 → warn; > 0.70 → force close perp + unwind spot | Auto-halt, unwind |
| Basis divergence | > 100 bps intraday | HALT + emergency unwind of all hedges |
| Funding-rate data stale | > 5 min since last snapshot | Reject new entries |
| Price data stale | > 60s since last snapshot | Reject new entries |
| Reconciler divergence | internal state vs. Binance truth differs by > 0.1% notional | HALT, manual reset required |
| Daily realised P&L floor | < -3% of bankroll | HALT for 24h |
| Weekly realised P&L floor | < -8% of bankroll | HALT, manual restart |
| Platform capital over-cap | balance > 1 week's operating capital for > 3 days | Alert, force withdrawal |
| Manual kill switch | `/var/run/fundingedge/STOP` exists | HALT immediately |

### 7.8 Weekly withdrawal rule

**This rule is non-negotiable and automated.** Platform risk is treated as a first-class risk, not a comment.

Every Friday at 18:00 Europe/Lisbon:

1. Compute `operating_cap_usd` = max bankroll used in the prior 7 days × 1.2
2. Compute `withdrawable_usd` = current_binance_balance - operating_cap_usd
3. If `withdrawable_usd > 50 USDT`, initiate a withdrawal to the configured destination (bank IBAN via SEPA-compatible provider, or a cold wallet address)
4. Log to `withdrawal` table, email operator
5. If withdrawal fails, retry Saturday 18:00; if still failing, HALT and alert

The bot never lets Binance become a savings account. Monthly reconciliation: profits should show up in the bank / cold wallet, not on exchange.

## 8. LLM Sanity-Check Contract

Before any hedge entry or exit, the strategy engine calls the configured LLM via an abstraction layer. Provider is interchangeable — DeepSeek (default), Anthropic Claude, OpenAI, or any model that supports structured JSON output.

### 8.1 Provider abstraction

Identical in shape to the MeteoEdge contract (reused verbatim — see archive). The `LLMProvider` protocol:

```python
class LLMProvider(Protocol):
    def sanity_check(self, request: SanityCheckRequest) -> SanityCheckResponse: ...
    def parse_text(self, prompt: str, response_schema: type[T]) -> T: ...
    @property
    def name(self) -> str: ...
    @property
    def cost_per_call_estimate(self) -> float: ...
```

Provider selection is via `LLM_PROVIDER` env var. The sanity-check module never imports a specific provider directly.

### 8.2 Input payload (provider-agnostic)

```json
{
  "proposed_action": {
    "symbol": "ETHUSDT",
    "action": "ENTER",
    "notional_usd": 500,
    "spot_leg": {"side": "BUY",  "price": 3241.5, "quantity": 0.154},
    "perp_leg": {"side": "SELL", "price": 3243.2, "quantity": 0.154}
  },
  "context": {
    "now_utc": "2026-04-24T14:15:00Z",
    "funding_rate_per_8h": 0.00045,
    "predicted_next_rate": 0.00052,
    "persistence_score_72h": 0.72,
    "basis_bps": 5.2,
    "spot_24h_volume_usd": 1850000000,
    "perp_24h_volume_usd": 12400000000,
    "current_margin_ratio": 0.18,
    "bankroll_utilisation_pct": 45,
    "recent_news_hint": "CPI print in 2h 15m"
  },
  "question": "Is there any reason this hedge entry is wrong? Consider imminent macro events, unusual funding-rate patterns, exchange maintenance windows, basis blow-out signals, regulatory announcements, or anything else that would invalidate the expected funding carry."
}
```

### 8.3 Required response schema (all providers produce this)

```json
{
  "approve": true,
  "confidence": 0.88,
  "reason": "Funding positive and persistent for 72h; basis well within tolerance; CPI is 2h away but the hedge is delta-neutral so macro shock risk is limited to basis widening, not directional.",
  "warnings": ["cpi_imminent"]
}
```

### 8.4 Handling logic

- `approve: false` → abort action, log rationale, increment rejection counter
- `approve: true, confidence < 0.7` → abort action (LLM uncertainty is a halt signal)
- `warnings` non-empty and flagging any of `["maintenance_window", "exchange_outage", "regulatory_action", "basis_anomaly"]` → abort
- LLM API error or timeout (> 10s) → abort action, log, continue
- Under no circumstance does LLM approval override a rules engine rejection (AND gate, not OR)

### 8.5 Cost control

Cap at 100 LLM calls per day (configurable via `LLM_DAILY_CALL_CAP`). If exceeded, halt new entries and alert. DeepSeek at ~€0.001/call → ~€3/month; Claude Sonnet at ~€0.015/call → ~€45/month.

## 9. External Integrations

### 9.1 Binance Spot API

- **Base URL:** `https://api.binance.com`
- **Auth:** HMAC-SHA256 request signing with API key + secret; read+trade+withdraw permissions (withdraw restricted to specific whitelisted addresses)
- **Rate limit:** weight-based (≈ 6000/min). Implement Redis-backed token bucket. Use weight-efficient endpoints (`/api/v3/ticker/bookTicker` not `/api/v3/ticker/24hr`).
- **Failure mode:** exponential backoff, max 5 retries, then halt
- **Endpoints used:**
  - `GET /api/v3/exchangeInfo` — lot sizes, precision, filters
  - `GET /api/v3/ticker/bookTicker` — best bid/ask
  - `GET /api/v3/depth` — depth for liquidity checks
  - `GET /api/v3/account` — balances
  - `POST /api/v3/order` — place order
  - `DELETE /api/v3/order` — cancel
  - `GET /api/v3/myTrades` — reconciliation
  - `POST /sapi/v1/capital/withdraw/apply` — withdrawals (whitelisted destination only)

### 9.2 Binance USDⓈ-M Futures API

- **Base URL:** `https://fapi.binance.com`
- **Auth:** same API key + secret; futures trading permission required
- **Rate limit:** weight-based, separate bucket from spot
- **Endpoints used:**
  - `GET /fapi/v1/exchangeInfo` — contract specs
  - `GET /fapi/v1/premiumIndex` — realised & predicted funding, mark price, index price
  - `GET /fapi/v1/fundingRate` — historical funding (used during spike for analysis)
  - `GET /fapi/v1/depth` — perp depth
  - `GET /fapi/v2/account` — margin, positions, unrealised P&L
  - `POST /fapi/v1/order` — place order
  - `DELETE /fapi/v1/order` — cancel
  - `POST /fapi/v1/leverage` — set leverage per symbol (2x)
  - `POST /fapi/v1/marginType` — set isolated margin per symbol
  - `GET /fapi/v1/income?incomeType=FUNDING_FEE` — funding payment history (for `funding_payment` table)
  - `GET /fapi/v1/userTrades` — reconciliation

### 9.3 LLM Provider API

Unchanged from MeteoEdge design. See archive spec §9.4.

| Provider | Env vars | Primary model | Approx. cost/call |
|---|---|---|---|
| `deepseek` (default) | `DEEPSEEK_API_KEY` | `deepseek-chat` | ~€0.001 |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` | ~€0.015 |
| `openai` | `OPENAI_API_KEY` | `gpt-4o` | ~€0.010 |

## 10. Deployment

### 10.1 Directory layout

```
/opt/fundingedge/
├── .venv/
├── src/
│   ├── fundingedge/
│   │   ├── pollers/
│   │   │   ├── funding.py
│   │   │   ├── price.py
│   │   │   └── account.py
│   │   ├── engine/
│   │   │   ├── scorer.py
│   │   │   ├── basis_monitor.py
│   │   │   ├── risk_manager.py
│   │   │   └── sizing.py
│   │   ├── llm/
│   │   │   ├── provider.py
│   │   │   ├── deepseek_provider.py
│   │   │   ├── anthropic_provider.py
│   │   │   ├── openai_provider.py
│   │   │   ├── factory.py
│   │   │   └── sanity_check.py
│   │   ├── execution/
│   │   │   ├── hedge_executor.py
│   │   │   ├── reconciler.py
│   │   │   └── withdrawal.py
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
│   ├── historical_funding/
│   ├── historical_prices/
│   └── fee_schedule_v*.json
├── scripts/
│   ├── backtest.py
│   ├── migrate.py
│   ├── emergency_unwind.py
│   └── weekly_withdrawal.py
└── README.md

/etc/fundingedge/
└── env                          (chmod 600, API keys, withdrawal whitelist)

/var/log/fundingedge/
├── poller.jsonl
├── engine.jsonl
├── execution.jsonl
├── withdrawal.jsonl
└── risk.jsonl

/var/run/fundingedge/
└── STOP                         (touch this file to halt immediately)
```

### 10.2 Systemd units

- `fundingedge-funding-poller.service` — long-running, 60s internal loop
- `fundingedge-price-poller.service` — long-running, 10s internal loop
- `fundingedge-account-poller.service` — long-running, 30s internal loop
- `fundingedge-engine.service` — long-running, 30s internal loop, depends on pollers
- `fundingedge-reconciler.service` — long-running, 60s internal loop
- `fundingedge-dashboard.service` — long-running, FastAPI on localhost:8080
- `fundingedge-daily-report.service` — once daily at 23:00 UTC, emails summary
- `fundingedge-weekly-withdrawal.service` — Fridays 18:00 Europe/Lisbon, withdraw profits

All services run as user `fundingedge`, `Restart=on-failure`, `RestartSec=30s`.

### 10.3 Environment variables

```
# Binance
BINANCE_ENV=prod|testnet
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_WITHDRAWAL_WHITELIST=BTC:bc1q...,USDT-ERC20:0x...,EUR:IBAN:PT50...

# LLM provider
LLM_PROVIDER=deepseek          # deepseek | anthropic | openai
LLM_PRIMARY_MODEL=...
LLM_DAILY_CALL_CAP=100

# Provider-specific keys
DEEPSEEK_API_KEY=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...

# Infra
POSTGRES_DSN=postgresql://fundingedge@localhost:5432/fundingedge
REDIS_URL=redis://localhost:6379/0

# Alerts
ALERT_EMAIL_FROM=...
ALERT_EMAIL_TO=...
SMTP_HOST=...
SMTP_USER=...
SMTP_PASSWORD=...

# Bankroll and operating caps
BANKROLL_CAP_USD=500
OPERATING_CAP_MULTIPLIER=1.2     # weekly withdrawal keeps this * max-used-last-7d on platform
TRADING_ENABLED=false            # master switch, set to true after Stage 3
UNIVERSE=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT

# Strategy thresholds
ENTRY_THRESHOLD_BPS=3
EXIT_THRESHOLD_BPS=1
ENTRY_BASIS_CEILING_BPS=20
EMERGENCY_BASIS_BPS=100
PERP_LEVERAGE_TARGET=2
```

## 11. Testing Strategy

Four stages. Advance only if the current stage passes.

### Stage 0 — MVP Observe-Only Spike (1 weekend to build, 2 weeks to run)

**Purpose:** Validate the premise on live Binance data before investing in infrastructure. Reuses the operating pattern that worked for MeteoEdge: a tiny script, no database, no services, observe-only, clear pass/fail.

**Exit criteria:**
- At least 30 entry signals accumulated over 2 weeks across the V1 universe
- For each signal, the counterfactual 3-day-hold P&L (computed from observed funding and basis) is positive after round-trip fees in ≥ 60% of cases
- Median counterfactual net yield per signal ≥ 20 bps over the hold period

See `funding-edge-mvp-spike.md` for the detailed design.

### Stage 1 — Historical Backtest (2 weeks to build, 1 week to run + iterate)

**Setup.**
- 12 months of 1-minute funding, spot and perp prices for the V1 universe (Binance exposes full history via `/fapi/v1/fundingRate` and klines — free, complete, no scraping needed)
- Identical production modules imported into the harness (no "backtest fork")

**Exit criteria (on held-out 20% of data):**

| Metric | Threshold |
|---|---|
| Total hedges | ≥ 50 |
| Net annualised return on deployed capital | > 15% |
| Sharpe ratio (annualised) | > 1.5 |
| Max drawdown | < 10% of deployed capital |
| Robustness under 10 bps extra slippage | Sharpe > 1.0 |
| Robustness under +3 bps per leg fees | Sharpe > 1.0 |

See `funding-edge-backtest-harness.md` for the detailed design.

### Stage 2 — Testnet Environment (2 weeks minimum)

**Setup.**
- Identical code, pointed at `testnet.binance.vision` (spot) and `testnet.binancefuture.com` (futures)
- Seed testnet balance to match planned live bankroll

**Execution.**
- Run engine unmodified against testnet
- Verify every code path: enter, hold through funding settlement, exit, partial fills, reconcile, emergency unwind
- Deliberately trigger each risk rule at least once

**Exit criteria.**
- Zero unhandled exceptions in 14 consecutive days
- All risk halts trigger correctly and recover cleanly
- Funding payments recorded in `funding_payment` table reconcile to Binance's reported funding-fee income log within $0.01
- Operator (you) can confidently answer "what happened yesterday?" from the dashboard alone

### Stage 3 — Micro Live (4 weeks)

**Setup.**
- Production API, spot + futures enabled
- Per-hedge notional capped at $50; bankroll on platform capped at $200
- Single symbol active at a time initially, expand to two after week 2 if clean

**Execution.**
- Run production and testnet in parallel, same signals, compare weekly
- Funding must actually be received on the real account, verifying the full loop

**Exit criteria.**
- Live P&L tracks testnet-simulated P&L within ±20% over the month
- No unexpected order rejections, margin-call surprises, or API edge cases
- Weekly withdrawal executed cleanly for 4 consecutive Fridays
- Operator has manually reviewed at least 15 live hedge cycles end-to-end

### Stage 4 — Graduated Scale-up (ongoing)

**Rules.**
- Double max per-hedge notional no more often than every 2 weeks
- Never double total bankroll on platform in a single step
- Weekly withdrawal enforced (automated; no override without operator password)
- Weekly P&L attribution: kill bottom-quintile sub-configurations (by symbol, time-of-day entry)
- Monthly fee schedule reverification
- Quarterly 12-month re-backtest with the most recent data

## 12. Security

- Binance API keys stored in `/etc/fundingedge/env`, mode `0600`, owner `fundingedge`, loaded via systemd `EnvironmentFile`
- **IP-restricted API keys** (Binance's built-in feature) — lock to the Mac mini's static IP; any key leak without the IP is useless
- Withdrawal permission is scoped to a **whitelist of addresses** configured on Binance's side; bot cannot send to arbitrary destinations
- No secrets in code, Git, logs, or dashboard
- Dashboard bound to `127.0.0.1` only; access via SSH tunnel
- PostgreSQL accepts localhost only; `fundingedge` role has no superuser rights
- Outbound network allowlist: `api.binance.com`, `fapi.binance.com`, `testnet.binance.vision`, `testnet.binancefuture.com`, active LLM provider endpoint, SMTP host
- Daily logrotate, keep 30 days, weekly gzipped archive to external storage
- All order placements logged with decision ID, timestamp, payload hash, and response
- Weekly audit: operator reviews 10 random hedges end-to-end from signal to settlement
- 2FA enforced on Binance account; withdrawal whitelist changes require email + SMS confirmation (Binance UI, manual)

## 13. Operational Runbook

**Normal startup.**
1. `systemctl start fundingedge-*`
2. Check dashboard at `http://localhost:8080`
3. Verify all pollers green, reconciler green, no active halts, platform balance ≤ operating cap

**Manual halt.**
1. `touch /var/run/fundingedge/STOP`
2. Engine stops entering new hedges within 30s
3. **Open hedges continue until natural exit or manual unwind** — halting is not the same as unwinding

**Resume after halt.**
1. Review risk event log and halt trigger
2. Document resolution in operator log
3. `rm /var/run/fundingedge/STOP`
4. Engine resumes on next cycle

**Emergency unwind (panic button).**
1. `scripts/emergency_unwind.py --confirm` closes all open hedges (market orders on both legs)
2. Operator-confirmed, logged, alerted
3. Expected to cost 20–50 bps extra vs. a patient unwind; use only when a hedge cannot be held safely

**Incident response.**
- Unexpected loss > 3% in a day → auto-halt, email alert, operator investigates within 24h
- Reconciler divergence → auto-halt, operator investigates within 1h (state drift is a first-class bug)
- Binance API error rate > 10% over 10 min → auto-halt, email alert
- Margin ratio > 0.70 on any perp → auto-unwind that hedge, email alert
- Testnet-prod divergence > 20% in a week → halt, full audit

## 14. Cost Model

| Item | Monthly cost |
|---|---|
| Hardware (sunk) | €0 |
| Electricity (Mac mini, 24/7) | €3–5 |
| PostgreSQL / Redis (self-hosted) | €0 |
| LLM API (~100 calls/day) | €3–45 (DeepSeek ~€3, OpenAI ~€30, Claude ~€45) |
| Binance trading fees | ~25 bps per full round-trip hedge cycle |
| Withdrawal fees (SEPA or on-chain) | ~€1–2 per weekly withdrawal |
| **Fixed operating cost** | **€8–60/month** (depends on LLM provider) |

**Break-even math (DeepSeek, €500 bankroll, 3-day avg hold):**
- Monthly fixed: ~€10
- Funding income at 10 bps per 8h × 0.75 deployed × €500 bankroll × 9 cycles = €3.4/cycle × ~10 cycles/month = €34
- Net after fees (25 bps × 10 round-trips on ~€375 avg notional = ~€9.4/month): ~€25/month gross, €15/month net
- **Break-even:** requires ~3% gross monthly return, achievable in normal funding regimes

## 15. Success Criteria (V1 exit)

V1 is considered successful if, over 3 months of Stage 4 operation:

- Net return after all costs and withdrawals > 15% quarterly on deployed capital
- Sharpe ratio > 1.5 on realised hedge cycles
- Max drawdown < 10%
- Zero unplanned manual interventions
- Every Friday withdrawal executed; never > 1 week's operating capital sitting on Binance
- Operator confidence high enough to grow bankroll by 2x

If any miss, V1 is paused and the strategy re-evaluated.

## 16. Suggested Epic Decomposition

See `Implementation-plan.md` for the full epic + story breakdown. Summary:

- **Epic 0** — MVP Observe-Only Spike (premise validation gate)
- **Epic 1** — Infrastructure & Data Platform
- **Epic 2** — Data Ingestion (funding, prices, account)
- **Epic 3** — Strategy Engine (scorer, basis monitor, sizing)
- **Epic 4** — Risk Management (rules, kill switch, margin monitor, weekly withdrawal)
- **Epic 5** — LLM Sanity Check (reused from MeteoEdge design)
- **Epic 6** — Hedge Execution (paired legs, reconciler, emergency unwind)
- **Epic 7** — Backtest Harness (12-month replay, parameter sweeps)
- **Epic 8** — Operational Tooling (dashboard, alerts, daily report)
- **Epic 9** — Compliance & Security (API key scoping, withdrawal whitelist, audit log)
- **Epic 10** — Production Rollout (Testnet → Micro-Live → Scale)

## 17. Agent Development Workflow

Unchanged from MeteoEdge. Multi-agent team per `/agents/`:

- **Tech Lead PM (Sonnet)** — Epic-level planning, arbitration, PR review, risk-critical code ownership
- **Designer (Sonnet)** — Operator dashboard specs, UX review
- **Mid Developer (Sonnet)** — Strategy engine, risk manager, hedge executor, reconciler
- **Junior Developer (Haiku)** — Pollers, SDK wrappers, scripts, dashboard components, docs

All risk-critical modules (scorer, basis monitor, risk manager, hedge executor, reconciler, fee model) require 100% unit test coverage. LLM-generated code is never trusted for risk-critical paths without Tech Lead review.

## 18. Open Questions

1. **Fee tier.** Default Binance spot fee is 0.10%. Paying fees in BNB grants a 25% discount. Confirm the bot holds a small BNB balance (~€10) for fee payment and tracks its depletion. Question: what happens if BNB runs out mid-trade? Recommend: top up weekly during the withdrawal job; fall back to USDT fees (higher) if BNB low.

2. **Withdrawal rail.** Two options for weekly off-platform transfers:
   - **SEPA-compatible fiat provider** (e.g. a Portuguese card-linked account) — withdrawal as EUR
   - **On-chain cold wallet** (BTC or USDT) — lower fees but requires manual FX to EUR when consuming profits

   Recommend: start with on-chain USDT to a Ledger, manual conversion to EUR only when needed. Fiat rails are slower and have their own compliance friction.

3. **Isolated vs. cross margin.** V1 uses Isolated at 2x. Capital efficiency gain from cross margin is ~1.3x. Recommend: defer to V2, revisit after Stage 4 scale-up shows the cap binding.

4. **Altcoin inclusion.** The V1 universe is 4 deep-liquidity pairs. Expanding to the top 20 roughly triples the number of actionable signals but increases operational complexity and basis risk. Recommend: hold at 4 pairs through Stage 4; expand deliberately as a V2 epic.

5. **LLM provider default.** DeepSeek is the cost-optimal default. Claude Sonnet is significantly stronger at reasoning about unusual contexts. Recommend: DeepSeek for Stage 1–3; evaluate Claude in Stage 4 against at least 4 weeks of parallel A/B logging before switching.

6. **Portugal tax treatment.** Crypto gains in Portugal are subject to 28% capital gains tax if held < 365 days (post-2023 rules). Funding income is likely classified as investment income. Recommend: operator consults a Portuguese accountant before Stage 3; track all hedges with tax-lot accuracy from day 1.

---

## Appendix A — Comparison Table: MeteoEdge → FundingEdge

| Concept | MeteoEdge (Kalshi weather) | FundingEdge (Binance funding) |
|---|---|---|
| Edge source | Price lag vs. physical observations | Funding premium on perpetuals |
| Predictive? | No (envelope-bounded) | No (rate already paid) |
| Data inputs | METAR, NWS forecasts, Kalshi order book | Funding rate, spot price, perp price, account state |
| Risk class | Bracket settlement (binary) | Basis + liquidation + platform |
| Hold period | Hours (intraday) | Days (multi-funding-cycle) |
| Capital efficiency | 100% (contracts prepaid) | ~67% (isolated margin at 2x) |
| Fees per cycle | 2–5% of trade value | 25 bps round-trip |
| Unique new rule | Stale data thresholds | Weekly withdrawal + margin ratio |
| Reusable | Risk manager, LLM pattern, staged testing, DB schema shape, dashboard | All of above — only the edge source and pollers change |

---

*End of specification. Ready for epic estimation and story decomposition.*
