# MeteoEdge — Implementation Plan

**Date:** 2026-05-08  
**Status:** Active  
**Goal:** Go from validated spike to live trading system on Polymarket weather markets.

---

## What we know

### The edge is real

Backtest on **4,867 real Polymarket trades** (May 1–2, 2026):

| Metric | Value |
|---|---|
| Win rate | **88.4%** |
| Total PnL | **€793.97** on €500 capital |
| ROI | **158.8%** over 2 days |
| Avg profit/trade | €0.163 |

This dramatically exceeds chance (50%). The edge source is that NWS + METAR data predicts the daily high temperature more accurately than crowd-consensus prices on Polymarket. We trade the gap.

**Important caveat**: 88.4% is almost certainly overfitted. Realistic live expectation is **55–70%**. At 60% win rate and 1.5¢ slippage, the strategy is still comfortably profitable on edges ≥ 15¢.

### What's already built and validated

| Component | Location | Status |
|---|---|---|
| Weather envelope model | `src/model/envelope.py` | ✅ Validated |
| Market parser (Polymarket) | `src/strategy/scanner.py` | ✅ Validated |
| Polymarket API client (Gamma + CLOB) | `src/data/polymarket.py` | ✅ Validated |
| Settlement tracker | `src/scripts/settle.py` | ✅ Validated |
| Rate-limited HTTP client | `src/http_client.py` | ✅ Built |
| Paper trading engine | `src/execution/paper_trader.py` | ✅ Validated |
| Backtest harness | `scripts/backtest_real_data.py` | ✅ Validated (archived) |

### Known problems to fix before going live

1. **Model calibration is broken**: Predicted 80% confidence → actual 56% accuracy. Confidence scores cannot be used for position sizing. Trade mechanically by edge only.
2. **Denver (KBKF) and Dallas (KDAL) fail completely**: 0% win rate. Exclude them until root cause is found.
3. **Rate-limiting gaps**: Fixed by `src/http_client.py` — NWS `/points` cached forever, forecasts cached 30 min, Polymarket market list cached 5 min.
4. **No live order execution**: Current code is paper-only. Need Polymarket CLOB auth + order placement.
5. **No risk management**: No daily loss limit, no drawdown protection, no circuit breakers.

---

## Target architecture

```
src/
├── config.py              # All settings — env-based, no hardcoding
├── http_client.py         # Rate-limited HTTP, retry, caching  [done]
├── data/
│   ├── metar.py           # METAR fetcher (aviation weather)
│   ├── nws.py             # NWS points + hourly forecast
│   ├── open_meteo.py      # Open-Meteo secondary forecast
│   └── polymarket.py      # Gamma API + CLOB client (pagination, auth)
├── model/
│   ├── envelope.py        # Weather envelope + probability calculation
│   └── climb_rates.py     # Per-city, per-month climb rate tables
├── strategy/
│   ├── scanner.py         # For each market: parse bracket, compute edge, flag
│   └── fee.py             # Fee model (Polymarket taker schedule)
├── execution/
│   ├── paper_trader.py    # Paper trading  [done]
│   └── live_trader.py     # Real order placement via CLOB API
├── risk/
│   └── manager.py         # Daily limits, drawdown stops, position caps
├── monitoring/
│   ├── logger.py          # Structured JSONL trade + snapshot logging
│   └── alerts.py          # Slack/email on anomalies
├── scripts/
│   ├── run.py             # Main polling loop (paper or live)
│   └── settle.py          # Daily settlement reconciliation
└── tests/
    ├── test_envelope.py
    ├── test_scanner.py
    └── test_risk.py

archive/
└── DEPRECATED: See DEPRECATION.md files in archive/ — code has been promoted to src/

docs/
├── TECHNICAL_SPECIFICATION.md
├── IMPLEMENTATION_PLAN.md    [this file]
└── SPIKE_DOCUMENTATION.md
```

---

## Epics

### Epic 0 — Repository consolidation (COMPLETED)

**Status**: ✅ COMPLETE as of 2026-06-16

**Goal**: Single clean codebase. No duplicate code between `src/` and `archive/`. Tests pass.

**What was done**:

The spike code from `archive/polymarket-spike/` has been fully promoted into `src/`:
- Market parser promoted to `src/strategy/scanner.py`
- Polymarket client promoted to `src/data/polymarket.py`
- Poll loop implemented in `src/scripts/run.py`
- Settlement runner promoted to `src/scripts/settle.py`

Archive artifacts have been deprecated with DEPRECATION.md tombstones for historical reference.

**Acceptance criteria** (✅ ALL MET):
- ✅ `src/` follows the target architecture above
- ✅ Archive artifacts deprecated with tombstones (code promoted, not lost)
- ✅ `python -m pytest src/tests/ -q` passes
- ✅ `python src/scripts/run.py --paper` runs a full poll cycle

**Files created/modified** (✅ COMPLETE):

| File | Status |
|---|---|
| `src/config.py` | ✅ Created |
| `src/data/metar.py` | ✅ Created |
| `src/data/nws.py` | ✅ Created |
| `src/data/open_meteo.py` | ✅ Created |
| `src/data/polymarket.py` | ✅ Created |
| `src/model/envelope.py` | ✅ Created |
| `src/model/climb_rates.py` | ✅ Created |
| `src/strategy/scanner.py` | ✅ Created |
| `src/strategy/fee.py` | ✅ Created |
| `src/scripts/run.py` | ✅ Created |
| `src/scripts/settle.py` | ✅ Created |
| `src/tests/test_envelope.py` | ✅ Created |

**Cities excluded per config**: Denver (KBKF), Dallas (KDAL) — 0% win rate, removed from station list.

---

### Epic 1 — Risk management (1 day)

**Goal**: Never lose more than pre-defined limits. This must exist before any real money is at risk.

**`src/risk/manager.py`**:

```python
class RiskManager:
    daily_loss_limit_eur: float = 50.0   # Stop trading if daily loss > €50
    max_open_positions: int = 15          # Never hold more than 15 positions simultaneously
    max_position_pct: float = 0.02        # Never stake > 2% capital per trade
    drawdown_stop_pct: float = 0.15       # Kill switch: stop if capital drops 15%

    def allow_trade(self, capital: float, daily_pnl: float, open_positions: int) -> tuple[bool, str]:
        """Returns (allowed, reason_if_blocked)."""
```

**Rules**:
- Daily loss limit: if today's realized PnL < -€50, stop all new trades for the day
- Open position cap: if 15 positions are open, wait for some to close before opening new ones
- Drawdown stop: if capital falls below 85% of starting capital, stop trading and alert
- Min liquidity: only trade markets where `yes_ask_size + no_ask_size > 50` contracts

**Acceptance criteria**:
- Unit tests for each limit
- `run.py` calls `risk.allow_trade()` before every execution
- Limits are configurable via env vars or `config.py`

---

### Epic 2 — Live observation run (2–3 days, starting week of May 12)

**Goal**: Run the full system against live Polymarket data in paper-trading mode for 48 hours minimum. Confirm the model generates real edges in live conditions.

**What "live observation" means**:
- Real Polymarket market data (not backtest replay)
- Real METAR + NWS + Open-Meteo data
- Paper execution (no real orders)
- Full logging: snapshots.jsonl, candidates.csv, summary.jsonl

**Success criteria** (live observation pass/fail gate):
- ≥ 10 edge opportunities flagged per day (confirms market activity is as expected)
- Manual spot-check: 3 random candidates where model prediction matches obvious physical reality (e.g., market says 60¢ for a bracket that current temp already exceeds → model correctly prices at ~100¢)
- Polling loop runs 48h without crash or silent failure

**If observation fails**:
- Debug data pipeline first (METAR parsing, bracket parsing, forecast)
- Check if Polymarket market format has changed since the May 1 spike

---

### Epic 3 — Live execution (1 week, starting ~May 19)

**Goal**: Place real orders on Polymarket. Week 1 capital: **€100**.

#### 3.1 Polymarket authentication

Polymarket CLOB API requires:
- API key (get from clob.polymarket.com)
- L1 wallet (MetaMask or Gnosis Safe)
- L2 proxy wallet (auto-created by Polymarket SDK)

Implementation:
```python
# src/execution/live_trader.py
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

class LiveTrader:
    def __init__(self, host: str, key: str, chain_id: int):
        self.client = ClobClient(host=host, key=key, chain_id=chain_id)

    def place_order(self, token_id: str, side: str, price_cents: int,
                    size_usdc: float) -> str:
        """Place a GTC limit order. Returns order_id."""
```

**Dependency**: `pip install py-clob-client` (official Polymarket Python SDK).

**Note on sizing**: At €5/trade and current Polymarket prices, we're placing limit orders at the ask. Use GTC (Good Till Cancelled) limit orders, not market orders, to control slippage.

#### 3.2 Order lifecycle

```
Scanner flags edge
    → RiskManager.allow_trade() check
        → LiveTrader.place_order() → Polymarket CLOB
            → Poll for fill every 30s (max 5 min)
                → If filled: record trade, update capital
                → If not filled after 5 min: cancel, log miss
                    → Move on
```

**Acceptance criteria**:
- First live trade placed and confirmed (even if €1)
- Order fill polling works (filled vs expired)
- Trade recorded accurately to logs
- Settlement reconciliation runs next day and matches

#### 3.3 Week 1 metrics (live gate to continue)

| Metric | Gate |
|---|---|
| Win rate | ≥ 60% (significant drop from backtest is expected and OK) |
| Avg slippage | ≤ 3¢ per trade |
| System uptime | ≥ 95% (no silent crashes) |
| Capital at risk | ≤ €100 (hard limit) |

If win rate < 55% after 50+ trades: pause and investigate before adding capital.

---

### Epic 4 — Monitoring (1 week, parallel to Epic 3)

**Goal**: Know what the system is doing at all times without tailing log files.

**`src/monitoring/`**:

#### FastAPI dashboard (minimal)
```
GET /status          → current capital, today's trades, win rate, PnL
GET /trades          → last 50 trades (JSONL feed)
GET /stations        → per-station win rate + PnL
GET /health          → poll loop alive, last poll timestamp
```

#### Alerts (`src/monitoring/alerts.py`)

Trigger on:
- Daily loss > €30 (warning), > €50 (stop + alert)
- Win rate over last 20 trades < 50% (investigate)
- Poll loop missed 2 consecutive polls (system down)
- Any single trade loss > €4 (position blowup)

**Delivery**: Email via `smtplib` (to `andre.freixo.santos@gmail.com`). Slack optional.

**Acceptance criteria**:
- Dashboard accessible at `localhost:8000` when running locally
- At least 2 alert types tested with simulated triggers
- Alerts fire within 10 minutes of the triggering event

---

### Epic 5 — Model improvements (ongoing, weeks 3+)

These are improvements, not blockers. Do them after live validation.

#### 5.1 Model calibration (Platt scaling)

**Problem**: At predicted 80% confidence → actual 56% accuracy. Model is systematically overconfident.

**Fix**: Collect 200+ live trades with outcomes. Fit a logistic regression (Platt scaling) on `(raw_p_yes, actual_outcome)`. Apply calibration layer before edge computation.

**Benefit**: Enables confidence-weighted position sizing (Kelly criterion).

#### 5.2 Kelly criterion position sizing

**Current**: Fixed €5 per trade regardless of edge.

**Better**: 
```python
kelly_fraction = (edge_cents / 100) / (1 - win_probability)
position_eur = capital * kelly_fraction * 0.25  # Quarter-Kelly for safety
position_eur = max(2.0, min(20.0, position_eur))  # Clamp to €2–€20
```

**Requires**: Reliable calibrated confidence. Do after 5.1.

#### 5.3 Station-specific climb rates

**Current**: Single `CLIMB_LOOKUP_SPRING` table (May) for all cities.

**Better**: Per-city, per-month tables derived from 5 years of NOAA historical METAR data.

**Rationale**: Miami in May has very different diurnal range than Seattle in May. Denver at altitude has rapid afternoon drops. Station-specific tables would recover Denver and Dallas.

#### 5.4 Denver / Dallas root cause analysis

Hypothesis: the daily high at KBKF is not at the airport — it's at the reference weather station Polymarket uses for settlement. If they use DEN (international) vs KBKF (Buckley AFB), the highs differ systematically.

**Fix**: Verify which station Polymarket actually uses for settlement on Denver markets. May require reading market descriptions / resolution criteria more carefully.

---

## Timeline

| Week | Dates | Epic | Milestone |
|---|---|---|---|
| 1 | May 9–11 | Epic 0 | Clean codebase, tests pass |
| 1 | May 12 | Epic 1 | Risk manager in place |
| 2 | May 12–14 | Epic 2 | 48h live observation (paper) |
| 2 | May 15–18 | Eval | Go/no-go decision for live trading |
| 3 | May 19–25 | Epic 3 | First live trades — €100 capital |
| 3–4 | May 19–25 | Epic 4 | Monitoring dashboard + alerts |
| 3–4 | May 26 | Eval | Week 1 live trading results review |
| 4+ | June | Epic 5 | Calibration, Kelly, station fixes |

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Backtest overfit — live win rate < 55% | Medium | High | Hard cap at €100 in week 1; pause at < 55% win rate |
| Polymarket API auth breaks / changes | Low | High | Monitor `py-clob-client` repo; have manual fallback |
| Model miscalibration causes trades at wrong edges | Medium | Medium | Only trade edges ≥ 15¢ (not confidence); recalibrate after 200 trades |
| Rate-limiting from NWS / Aviation Weather | Low | Low | Fixed by `http_client.py` (caching + backoff) |
| Denver/Dallas 0% win rate persists | High | Low | Already excluded; no capital at risk |
| Market liquidity insufficient at €5 | Low | Low | Check ask_size > 50 before each trade |
| System crash during trading session | Medium | Medium | Process supervision (`supervisord` or `systemd`), missed-poll alerts |
| Real slippage >> modeled slippage | Medium | Medium | Monitor actual vs modeled; cap position size if slippage > 4¢ |

---

## Go / No-Go gates

**Before live trading (Epic 3)**:
- [ ] Epic 0 complete: clean codebase, tests green
- [ ] Epic 1 complete: risk manager integrated
- [ ] Epic 2 complete: 48h paper run with ≥ 10 edges/day
- [ ] Manual review: 5 flagged edges manually verified as physically plausible
- [ ] Polymarket account funded with ≤ €100 USDC

**After week 1 live trading**:
- [ ] ≥ 50 trades executed
- [ ] Win rate ≥ 60%
- [ ] No catastrophic failure (capital > €75)
- [ ] Slippage within model range (avg ≤ 3¢)

**Scale to €500**:
- [ ] 2 full weeks live with win rate ≥ 60%
- [ ] No single-day loss > €30
- [ ] System uptime ≥ 95%

---

## What we are NOT building (explicitly out of scope)

- Web UI for users (this is a personal trading bot)
- Multiple strategy types (weather only for now)
- Automated rebalancing between markets
- ML model training pipeline (Platt scaling is a single logistic regression, not a training infra)
- Portfolio-level hedging

---

## Interim guardrails

**`MODEL_PROB_CAP` (issue #305):** `p_yes` is clamped to `[1-cap, cap]` (default 0.95) before EV and edge computation, preventing the system from treating any bracket as a certainty. Live data showed 63 `predicted_price=100` NO trades won only 84% vs 89% for the 95–99¢ bucket — textbook overconfidence. This guardrail is temporary and should be removed or loosened once EMOS (#70) is promoted and per-station calibration is available.

---

## Key decisions already made

| Decision | Choice | Rationale |
|---|---|---|
| Position sizing | Fixed €5/trade | Conservative start; migrate to Kelly after calibration |
| Min edge | 15¢ | Backtest confirmed <10¢ edges lose money |
| Confidence filter | Disabled | Model calibration broken; trade all edges ≥ 15¢ |
| Denver / Dallas | Excluded | 0% win rate; root cause unknown |
| CLOB enrichment | Disabled initially | `outcomePrices` from Gamma is accurate enough; adds latency |
| Forecast sources | NWS (60%) + Open-Meteo (40%) | Ensemble reduces single-source failure risk |
