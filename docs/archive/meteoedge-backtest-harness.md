# MeteoEdge — Backtest Harness Design (Epic 7)

**Purpose:** A tick-by-tick replay engine that validates the MeteoEdge strategy against historical Kalshi weather markets before any live capital is at risk.
**Scope:** The Stage 1 gate of the four-stage testing strategy in the main spec. If the backtest fails, nothing downstream gets built.
**Audience:** Senior agent (Sonnet) for design and risk-critical modules, junior agents (Haiku) for data ingestion and reporting.

---

## 1. Why a Backtest, and Why Now

Stage 1 is the cheapest possible answer to "does this strategy actually work?" Every later stage (demo, micro-live, scale-up) costs time and capital that can only be recovered if the backtest passes first. The harness must therefore be:

- **Realistic enough that passing it predicts live behavior** — conservative fills, real fees, real latency, no future leakage
- **Fast enough to iterate** — replaying 90 days in minutes, not hours
- **Debuggable** — when a trade looks wrong, you can trace it back to the exact snapshot that produced it
- **Reusable** — the same harness runs continuous quarterly revalidation forever

A backtest that silently cheats (uses the next tick's data, assumes perfect fills, ignores fees) is worse than no backtest, because it produces false confidence. The harness's design is dominated by the single principle: **no future information may leak into any decision**.

## 2. Scope

### 2.1 In scope

- Replay of Kalshi weather markets tick-by-tick from captured snapshots
- Envelope and edge computation using only data available at each simulated timestamp
- Fee application using the full Kalshi fee schedule (not the spike approximation)
- Conservative fill simulation against the captured order book
- Settlement against captured NWS Daily Climate Report outcomes
- Per-trade, per-day, and per-strategy-variant P&L attribution
- Reporting: win rate, EV, Sharpe, Sortino, max drawdown, hit rate by city/bracket/time-of-day
- Parameter sweeps: MIN_EDGE_CENTS, MIN_CONFIDENCE, polling cadence, sizing model

### 2.2 Out of scope

- LLM sanity-check replay (too expensive in API calls for large sweeps; tested separately in Stage 2)
- Websocket order book replay (V2)
- Partial fill simulation beyond the simple ladder model (V2 if needed)
- Multi-venue arbitrage (not in V1)

## 3. Architectural Principles

**P1 — Immutable event log.** Historical data is stored as append-only event streams keyed by timestamp. No mutation, no updates, no "latest" columns. Each event carries the wall-clock time it was captured.

**P2 — Strict time discipline.** The harness advances a single "simulated now" clock. At time `t`, only events with `captured_at <= t` are visible to the strategy. This is enforced at the query layer, not by convention.

**P3 — Deterministic replay.** Given the same event log and the same strategy config, the harness produces identical trades and identical P&L on every run. No wall-clock calls, no randomness without seeded RNG, no ordering ambiguity.

**P4 — Separate strategy code from harness code.** The Envelope Engine, Edge Scanner, Risk Manager, and Fee Model from the production codebase are imported *unchanged* into the harness. If the backtest passes, the same code runs live. No "backtest version" forks.

**P5 — Explicit conservatism.** Every simulation choice resolves ambiguity against the strategy. Fills are at the worst plausible price. Latency is modeled. Slippage defaults are conservative. If reality turns out better, great; if worse, we already caught it.

## 4. Data Requirements

### 4.1 Historical Kalshi market snapshots

**What:** A captured snapshot of every relevant market (daily-high brackets for the 5 cities) at every polling interval, including full order book depth at each snapshot.

**Coverage target:** 90 days minimum. Longer is better but 90 days is the minimum credible sample to detect the edge.

**Fields per snapshot:**
- `captured_at` (UTC, millisecond precision)
- `ticker`
- `event_ticker`
- `station_code` (derived)
- `trade_date` (the local date the market settles on)
- `bracket_low_f`, `bracket_high_f` (parsed)
- `close_time` (market expiration)
- `status` (open/closed)
- `yes_bid`, `yes_ask`, `no_bid`, `no_ask` (cents)
- `yes_bid_size`, `yes_ask_size`, `no_bid_size`, `no_ask_size`
- `orderbook_yes` — ladder of (price, size) tuples, top 10 levels
- `orderbook_no` — same
- `volume`, `open_interest`

**Sources, in preference order:**
1. **Self-captured** during the spike and subsequent observation period. Zero cost, exact schema match.
2. **DeltaBase** (third-party Kalshi historical data). Paid but comprehensive.
3. **Kalshi's own 3-month historical window** via the candle/trades endpoints. Limited fields (no full order book) but free.

**Recommendation:** Use source 3 to seed the first backtest. Transition to self-captured data accumulated from the observe-only run plus any early-stage operation. Only pay for source 2 if the edge is confirmed and more history is needed to tune parameters.

### 4.2 Historical weather observations

**What:** Hourly METAR observations for KNYC, KORD, KMIA, KAUS, KLAX covering the same 90+ day window.

**Source:** Iowa Environmental Mesonet (IEM) ASOS archive. Free, bulk CSV download, authoritative.

**Fields per observation:**
- `station`, `observed_at` (UTC), `temp_c`, `temp_f`, `wind`, `conditions`, `raw_metar`

### 4.3 Historical NWS forecasts

**What:** The forecast high for each station as it evolved over the day, for each day in the window.

**Problem:** NWS forecasts are not natively archived in a clean queryable form. The IEM has an NDFD archive which is the closest approximation.

**Pragmatic solution for V1 backtest:** Use forecast highs as-of 06:00 local on the target day as a single snapshot. This understates how useful evolving forecasts are in the production system (the real system pulls every hour), so if the backtest passes with this weaker signal, the live strategy has *more* edge, not less. If the backtest fails, consider whether the forecast signal quality was the bottleneck before giving up.

**Fields:**
- `station`, `forecast_issued_at`, `valid_for_date`, `forecast_high_f`

### 4.4 Historical settlement outcomes

**What:** The official NWS Daily Climate Report high temperature for each (station, date) in the window.

**Source:** NWS F6 climate products, archived by IEM and NCEI.

**Fields:**
- `station`, `trade_date` (local), `official_high_f`, `report_issued_at`

### 4.5 Fee schedule versions

**What:** A versioned record of Kalshi's fee schedule over the backtest window.

**Source:** Monthly snapshots of `kalshi.com/fee-schedule` as PDF/HTML. Parse into a JSON structure.

**Schema:**
```json
{
  "effective_from": "2025-01-01",
  "effective_to": "2025-06-30",
  "formula": "ceil(0.07 * quantity * price * (1 - price))",
  "floor_cents": 1,
  "category_overrides": { "Climate": { "formula_override": "..." } }
}
```

## 5. Harness Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      Backtest Runner                           │
│                                                                │
│  ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│  │ Event Loader │──►│ Time-indexed │◄──┤ Simulation Clock  │   │
│  │ (Parquet/DB) │   │  Event Store │   │  (advances one    │   │
│  └──────────────┘   └──────┬───────┘   │   tick at a time) │   │
│                            │           └─────────┬─────────┘   │
│                            ▼                     ▼             │
│               ┌────────────────────────────────────┐           │
│               │ Time-Gated Query Interface         │           │
│               │  - get_metars_up_to(t, station)    │           │
│               │  - get_forecast_at(t, station)     │           │
│               │  - get_orderbook_at(t, ticker)     │           │
│               └──────────────┬─────────────────────┘           │
│                              │                                 │
│                              ▼                                 │
│    ┌─────────────────────────────────────────────┐             │
│    │     Production Strategy Modules (unchanged)  │             │
│    │   envelope.py, edge_scanner.py,              │             │
│    │   risk_manager.py, sizing.py, fee_model.py   │             │
│    └──────────────┬──────────────────────────────┘             │
│                   │                                            │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Fill Simulator                       │             │
│    │    - conservative match against orderbook    │             │
│    │    - latency model                           │             │
│    │    - partial fill ladder walk                │             │
│    └──────────────┬──────────────────────────────┘             │
│                   │                                            │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Portfolio Tracker                    │             │
│    │    - positions, cash, exposure               │             │
│    │    - realized / unrealized P&L               │             │
│    │    - fees paid                               │             │
│    └──────────────┬──────────────────────────────┘             │
│                   │                                            │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Settlement Engine                    │             │
│    │    - at trade_date+1 morning, resolve all    │             │
│    │      open positions against official high    │             │
│    └──────────────┬──────────────────────────────┘             │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Results & Reporting                  │             │
│    │    - per-trade log, metrics, plots           │             │
│    │    - parameter sweep comparison              │             │
│    └─────────────────────────────────────────────┘             │
└────────────────────────────────────────────────────────────────┘
```

## 6. Module Specifications

### 6.1 Event store

Persisted as Parquet files partitioned by `trade_date`, one directory per event type:

```
data/backtest/
├── market_snapshots/
│   ├── date=2025-08-01/part-0.parquet
│   ├── date=2025-08-02/part-0.parquet
│   └── ...
├── metar/
│   └── date=...
├── forecasts/
│   └── date=...
├── settlements/
│   └── date=...
└── fee_schedule_versions.json
```

Why Parquet: columnar, compressed, indexable by timestamp, queryable by DuckDB without a server. For the Mac mini this keeps memory usage bounded and queries fast.

Loaded into DuckDB at runtime:

```python
import duckdb
con = duckdb.connect(":memory:")
con.execute("""
  CREATE VIEW market_snapshots AS
  SELECT * FROM read_parquet('data/backtest/market_snapshots/**/*.parquet')
""")
```

### 6.2 Simulation clock

```python
@dataclass
class SimulationClock:
    start: datetime
    end: datetime
    step: timedelta = timedelta(seconds=30)  # match live polling cadence
    _now: datetime = field(init=False)

    def __post_init__(self):
        self._now = self.start

    def now(self) -> datetime:
        return self._now

    def tick(self) -> bool:
        self._now += self.step
        return self._now <= self.end
```

The clock is injected into every module that would otherwise call `datetime.now()`. Production code takes a `clock` parameter with a default of `SystemClock`; backtests pass `SimulationClock`. Never call `datetime.now()` directly in strategy code.

### 6.3 Time-gated query interface

This is the firewall against future leakage. Every query filters by `captured_at <= clock.now()`:

```python
class TimeGatedDataSource:
    def __init__(self, duckdb_conn, clock: SimulationClock):
        self.con = duckdb_conn
        self.clock = clock

    def latest_metars(self, station: str, lookback: timedelta = timedelta(hours=24)) -> list[MetarRow]:
        t = self.clock.now()
        t_lo = t - lookback
        return self.con.execute("""
            SELECT * FROM metar
            WHERE station = ? AND observed_at <= ? AND observed_at >= ?
            ORDER BY observed_at DESC
        """, [station, t, t_lo]).fetchall()

    def latest_forecast(self, station: str, target_date: date) -> ForecastRow | None:
        t = self.clock.now()
        return self.con.execute("""
            SELECT * FROM forecasts
            WHERE station = ? AND valid_for_date = ? AND forecast_issued_at <= ?
            ORDER BY forecast_issued_at DESC
            LIMIT 1
        """, [station, target_date, t]).fetchone()

    def latest_orderbook(self, ticker: str, max_staleness: timedelta = timedelta(seconds=60)) -> OrderbookRow | None:
        t = self.clock.now()
        t_lo = t - max_staleness
        return self.con.execute("""
            SELECT * FROM market_snapshots
            WHERE ticker = ? AND captured_at <= ? AND captured_at >= ?
            ORDER BY captured_at DESC
            LIMIT 1
        """, [ticker, t, t_lo]).fetchone()
```

**Test for leakage:** a unit test that sets `clock.now() = T`, queries for "latest" data, and verifies no returned row has `captured_at > T`. Run on every CI build. This is one of the highest-value tests in the system.

### 6.4 Strategy modules (imported from production)

The backtest does not implement its own envelope, edge scanner, or risk manager. It imports the production modules directly. They must accept a data source and a clock, and must not call wall-clock functions.

```python
from meteoedge.engine.envelope import compute_envelope
from meteoedge.engine.edge_scanner import scan_edges
from meteoedge.engine.risk_manager import RiskManager
from meteoedge.engine.fee_model import fee_cents
from meteoedge.engine.sizing import fractional_kelly_size
```

If a production module has wall-clock dependencies, refactor it. This refactoring is one of the first stories in Epic 7 and affects Epics 3 and 4.

### 6.5 Fill simulator

Given a proposed order (ticker, side, action, price, quantity) and the current order book snapshot, determine how much fills, at what price.

**Limit orders (maker):**
- If the limit price is at or better than the current best bid/ask (crossing the spread), simulate as a taker at the opposite-side ask/bid.
- If the limit price rests in the book, assume it does not fill until the opposite side reaches that price. Walk forward in the event stream until either (a) a snapshot shows the opposite side reaching our price, counting us as filled, or (b) the market closes, counting us as unfilled.
- Conservative assumption: when a crossing trade happens in the data, the historical liquidity is consumed before we queue into it. Our order fills only if the opposing side's size at that price *exceeded* our position size.

**Market orders (taker):**
- Walk the opposing ladder from best price, consuming (price, size) levels until the quantity is filled.
- Apply a configurable slippage model: default `slippage_bps = 0`, but allow a user to sweep `[0, 25, 50, 100]` to see how sensitive P&L is to execution quality.

**Latency model:**
- Between decision and order placement, advance the clock by a configurable `execution_latency` (default 250ms).
- Re-fetch the order book at the advanced clock; fill against *that* book, not the one the decision was made against. This is the single most important conservative assumption in the harness.

**Partial fills:**
- If the ladder does not have enough size to fill our quantity entirely, fill what is available and record the remainder as cancelled (matching the production risk rule that orphan legs are not held).

```python
@dataclass
class FillResult:
    filled_quantity: int
    avg_fill_price_cents: float
    fees_cents: float
    unfilled_quantity: int
    fill_events: list[tuple[datetime, int, int]]  # (timestamp, quantity, price)


class FillSimulator:
    def __init__(self, data: TimeGatedDataSource, clock: SimulationClock,
                 execution_latency: timedelta = timedelta(milliseconds=250),
                 slippage_bps: int = 0):
        ...

    def simulate_market_order(self, ticker: str, side: str, action: str, quantity: int) -> FillResult:
        ...

    def simulate_limit_order(self, ticker: str, side: str, action: str,
                             price_cents: int, quantity: int,
                             ttl: timedelta) -> FillResult:
        ...
```

### 6.6 Portfolio tracker

Mirrors the production portfolio but in memory and with full history:

```python
class BacktestPortfolio:
    def __init__(self, starting_cash_cents: int):
        self.cash = starting_cash_cents
        self.positions: dict[tuple[str, str], Position] = {}  # (ticker, side) -> Position
        self.trade_log: list[Trade] = []
        self.daily_snapshots: list[DailySnapshot] = []

    def record_fill(self, fill: FillResult, ticker: str, side: str, action: str):
        ...

    def settle_position(self, ticker: str, side: str, official_result_yes: bool):
        ...

    def snapshot_eod(self, date: date):
        ...
```

### 6.7 Settlement engine

At the start of each simulated day, before the trading loop advances, settle all positions whose `trade_date` is yesterday:

```python
def settle_yesterday(portfolio: BacktestPortfolio, data: TimeGatedDataSource,
                     settlement_date: date, clock: SimulationClock):
    for (ticker, side), pos in list(portfolio.positions.items()):
        if pos.trade_date != settlement_date:
            continue
        official_high = data.official_high(pos.station, settlement_date)
        yes_won = pos.bracket_low_f <= official_high <= pos.bracket_high_f
        portfolio.settle_position(ticker, side, yes_won)
```

The settlement time is modeled as 09:00 local on trade_date+1, matching the NWS Daily Climate Report schedule.

### 6.8 Results & reporting

At end of backtest, compute and emit:

- **Per-trade CSV:** every executed trade with decision context and outcome
- **Per-day CSV:** daily realized P&L, fees, drawdown, exposure
- **Summary JSON:**
  - total trades, wins, losses
  - win rate, average P&L, median P&L
  - gross P&L, fees paid, net P&L
  - Sharpe (annualized), Sortino (annualized)
  - max drawdown (absolute and as % of starting capital)
  - max consecutive losing days
  - per-station attribution
  - per-bracket-distance attribution (how far the bracket is from current high at decision time)
  - per-time-of-day attribution
- **Plots** (matplotlib, save to PNG):
  - Cumulative P&L curve
  - Daily P&L histogram
  - Rolling 7-day Sharpe
  - Per-station cumulative P&L

### 6.9 Parameter sweep

Pure function from (config, event store) to summary metrics. Run in parallel across a grid:

```python
def run_single_backtest(config: StrategyConfig) -> SummaryMetrics:
    ...

grid = [
    StrategyConfig(min_edge_cents=e, min_confidence=c, latency_ms=l)
    for e in [2, 3, 4, 5, 7]
    for c in [0.70, 0.75, 0.80, 0.85, 0.90]
    for l in [100, 250, 500, 1000]
]
results = parallel_map(run_single_backtest, grid, workers=4)
```

The Mac mini's i7 has 4 cores; cap workers at 3 to leave headroom.

Report: which config produces the best Sharpe, which is most robust to latency, which has the least drawdown. Select the operating config from this sweep *conservatively* — prefer moderate parameters that perform well across the whole grid over aggressive parameters that are optimal only at their exact setting (overfitting).

## 7. Exit Criteria (Stage 1 Gate)

The backtest passes and unlocks Stage 2 if **all** of the following hold on out-of-sample data (see §8):

| Metric | Threshold |
|---|---|
| Total trades | ≥ 100 |
| Win rate | > 55% |
| Average EV per trade, post-fee | > 8 cents |
| Sharpe ratio (annualized) | > 1.5 |
| Max drawdown | < 20% of starting capital |
| Max consecutive losing days | ≤ 5 |
| Robustness under latency stress (500ms) | Sharpe > 1.0 |
| Robustness under 25 bps slippage | Sharpe > 1.0 |

If any of these fail on out-of-sample data, do not proceed. Revisit the spec.

## 8. Out-of-Sample Discipline

This is the single most important methodological point. Skip it and the backtest is worthless.

**Split the historical window:**
- Earliest 60% → **training set**. Used for parameter tuning, sweeps, and iteration.
- Middle 20% → **validation set**. Used to pick the final operating config from the sweep.
- Most recent 20% → **held-out test set**. Touched exactly once, at the end.

**The rule:** you may look at training and validation data as many times as you want. You may touch the held-out set exactly once, at the end, to produce the numbers you report against the Stage 1 exit criteria. If the held-out set fails, you do *not* get to retune and retest. You go back to the spec.

This is uncomfortable because it means you might do weeks of work and then fail at the gate. That is the point. It is the only way to produce a number you can trust.

## 9. Implementation Plan (Stories)

An order of implementation that delivers an end-to-end smoke test as fast as possible, then hardens each component:

1. **Event store skeleton** — Parquet layout, DuckDB views, basic loader
2. **Simulation clock + time-gated data source** — including the leakage unit test
3. **Strategy module refactor** — remove wall-clock calls from production envelope/edge/risk, inject clock and data source
4. **Minimal fill simulator** — taker-only, zero latency, zero slippage, crossing at best ask. End-to-end smoke test passes.
5. **Portfolio tracker + settlement engine** — P&L reconciles for a hand-crafted test day
6. **Summary metrics + CSV output** — enough to eyeball a single backtest
7. **Historical data ingestion** — ingest 90 days of Kalshi, METAR, forecasts, settlements into Parquet
8. **First end-to-end backtest** — on training set only. Iterate on bugs until metrics are plausible.
9. **Fee model** — replace rough approximation with the versioned Kalshi fee schedule
10. **Latency model + conservative fill ladder** — replay an order against a *future* book, not the decision-time book
11. **Slippage and partial fill handling**
12. **Parameter sweep harness** — parallel, with structured results
13. **Reporting: plots, per-segment attribution**
14. **Leakage audit** — deliberate injection of future-leaking code, verify it's caught
15. **Out-of-sample gate run** — exactly once, against the held-out 20%

## 10. Agent Assignments

| Story | Owner | Why |
|---|---|---|
| Event store, DuckDB setup | Junior (Haiku) | Boilerplate, well-specified |
| Simulation clock + time-gated query interface | Senior (Sonnet) | Correctness-critical; the leakage firewall |
| Strategy module refactor | Senior (Sonnet) | Touches production code, changes interface |
| Fill simulator | Senior (Sonnet) | Most consequential assumption in the harness |
| Portfolio tracker + settlement | Senior (Sonnet) | P&L correctness; must match production reconciler |
| Summary metrics | Junior (Haiku) | Arithmetic on known shapes |
| Historical data ingestion | Junior (Haiku), one script per source | Parallelizable, bounded scope |
| Fee model | Senior (Sonnet) | Small surface, high consequence |
| Latency model | Senior (Sonnet) | Subtle; future-book lookup |
| Parameter sweep harness | Junior (Haiku) | Map-reduce over a grid |
| Reporting / plots | Junior (Haiku) | Templates and matplotlib |
| Leakage audit | Senior (Sonnet), adversarial | Deliberately tries to break its own firewall |
| Out-of-sample gate run | Operator (André) | One-shot, ceremonial, not automated |

Reviewer (separate Haiku agent, different GitHub machine user) reviews every PR. 100% unit test coverage on: time-gated query interface, fee model, risk manager, fill simulator.

## 11. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Future leakage slips past the firewall | Backtest looks profitable but live bleeds | Dedicated leakage audit story (14); adversarial tests; CI enforcement |
| Fee schedule drift during backtest window | Over/understates net edge | Versioned fee schedule; alert if live fills diverge from modeled fees by > 10% |
| Over-tuning on training set | Validation passes, held-out fails | Rigid three-way split; operator discipline on touching held-out once |
| Fill simulator is too optimistic | Live P&L undershoots backtest | Latency + slippage sweep; require Sharpe > 1.0 under stress, not just baseline |
| Kalshi order book history is incomplete | Fill simulator runs against partial data | Prefer self-captured data; flag and exclude days with < 90% snapshot coverage |
| NWS archive has gaps | Settlement unknown for some days | Cross-check with METAR 24h max; exclude days that disagree by > 1°F |
| Strategy modules drift between prod and backtest | Backtest no longer predicts prod | Same modules imported in both; compile-time failure if signatures diverge |
| Mac mini runs out of memory on large sweep | Harness crashes mid-run | Parquet + DuckDB streams from disk; cap parallel workers at 3 |

## 12. What Done Looks Like

Epic 7 is complete when, on a fresh clone of the repo, the operator can:

1. Run `make ingest` to pull historical data into Parquet
2. Run `make backtest` to produce a summary on the training set
3. Run `make sweep` to produce a grid over configs
4. Run `make validate CONFIG=best.json` to evaluate the winning config on the validation set
5. Run `make gate CONFIG=final.json` exactly once to produce the held-out numbers

If the gate numbers hit all eight Stage 1 exit criteria, the senior agent opens the PR transitioning `TRADING_ENABLED=false` to the Stage 2 demo run. If they do not, the operator writes a retro, closes Epic 7, and reopens the strategy spec.

---

*End of backtest harness design. Ready for story estimation.*
