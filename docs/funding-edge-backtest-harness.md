# FundingEdge — Backtest Harness Design (Epic 7)

**Purpose:** A tick-by-tick replay engine that validates the FundingEdge strategy against 12 months of historical Binance funding and price data before any live capital is at risk.
**Scope:** The Stage 1 gate of the four-stage testing strategy in `funding-edge-spec.md §11`. If the backtest fails on held-out data, nothing downstream gets built.
**Audience:** Mid Developer (Sonnet) for design and risk-critical modules, Junior Developer (Haiku) for data ingestion and reporting.

---

## 1. Why a Backtest, and Why Now

Stage 0 (the observe-only spike) answers "does funding-rate carry show a signal in live data?" Stage 1 answers the harder question: "does the strategy *as implemented* — with real fees, real slippage, real margin constraints, real execution latency — actually print money?" A signal that works at zero cost can still be a negative-EV strategy once the full execution stack is modelled.

The harness must therefore be:

- **Realistic enough that passing it predicts live behavior** — conservative fills, real fees, real latency, real margin accounting, no future leakage
- **Fast enough to iterate** — replaying 12 months across 4 symbols in minutes, not hours
- **Debuggable** — when a hedge looks wrong, you can trace it back to the exact snapshot that produced it
- **Reusable** — the same harness runs continuous quarterly revalidation forever

A backtest that silently cheats (uses the next tick's data, assumes perfect fills, ignores funding-rate regime shifts) is worse than no backtest, because it produces false confidence. The harness's design is dominated by the single principle from MeteoEdge's backtest: **no future information may leak into any decision**.

## 2. Scope

### 2.1 In scope

- Replay of Binance funding rates, perp prices, and spot prices tick-by-tick from captured history
- Scorer, basis monitor, and risk manager evaluation using only data available at each simulated timestamp
- Fee application using the full Binance fee schedule (per-venue, maker/taker, BNB-discount aware)
- Conservative fill simulation: taker at book-top with latency-based re-fetch, depth-limited partial fills
- Funding settlement: accrue realised funding to the perp leg at each 8h boundary
- Margin accounting: track perp margin ratio; force-close hedges that hit the liquidation threshold
- Per-hedge, per-day, and per-config P&L attribution
- Reporting: win rate, net yield, annualised return, Sharpe, Sortino, max drawdown, per-symbol attribution
- Parameter sweeps: ENTRY_THRESHOLD_BPS, persistence fraction, basis ceiling, target hold period

### 2.2 Out of scope

- LLM sanity-check replay (too expensive in API calls for large sweeps; tested separately in Stage 2)
- WebSocket order stream replay (V2)
- Cross-exchange arbitrage (not in V1)
- Portfolio margin / auto-rehypothecation (V2)

## 3. Architectural Principles

**P1 — Immutable event log.** Historical data is stored as append-only event streams keyed by timestamp. No mutation, no updates, no "latest" columns. Each event carries the wall-clock time it was observed.

**P2 — Strict time discipline.** The harness advances a single "simulated now" clock. At time `t`, only events with `captured_at <= t` are visible to the strategy. Enforced at the query layer, not by convention.

**P3 — Deterministic replay.** Given the same event log and the same strategy config, the harness produces identical hedges and identical P&L on every run. No wall-clock calls, no randomness without seeded RNG, no ordering ambiguity.

**P4 — Separate strategy code from harness code.** The Scorer, Basis Monitor, Risk Manager, Sizing, and Fee Model from the production codebase are imported *unchanged* into the harness. If the backtest passes, the same code runs live. No "backtest version" forks.

**P5 — Explicit conservatism.** Every simulation choice resolves ambiguity against the strategy. Fills are at the worst plausible price. Latency is modelled. Slippage defaults are conservative. Funding is credited at the *realised* rate, never at the predicted rate. Margin is accounted at the perp mark price, not the index price.

## 4. Data Requirements

Binance makes this cheap. Unlike Kalshi, where we had to scrape history, Binance exposes all of the following directly from public endpoints with no auth, no rate limit drama, and full historical depth. This is a major advantage over MeteoEdge's backtest data cost.

### 4.1 Historical funding rates

**What:** Every funding settlement for every symbol in the V1 universe over the backtest window.

**Source:** `GET /fapi/v1/fundingRate` (public, free, paginated, ~12 months of history available).

**Fields per settlement:**
- `symbol`
- `funding_time` (ms epoch, UTC, exact 8h boundary)
- `funding_rate` (realised rate, as decimal)
- `mark_price` at the settlement moment

Coverage target: 12 months minimum. Binance routinely serves 2+ years via pagination; grab all available.

### 4.2 Historical klines (1-minute OHLCV) for spot and perp

**What:** 1-minute open/high/low/close/volume bars for both the spot and the perp, for each symbol.

**Sources:**
- Spot: `GET /api/v3/klines`
- Perp: `GET /fapi/v1/klines`

Both endpoints support up to 1000 bars per request and full history (Binance's klines go back to each symbol's listing date). Bulk-download via a Junior-Dev ingestion script.

**Fields per bar:**
- `open_time` (ms epoch)
- `open`, `high`, `low`, `close`
- `volume`, `quote_asset_volume`
- `trades_count`

### 4.3 Historical order-book depth (optional, V1.5)

**Problem.** Binance does not expose L2 order book history via public REST. Full-depth history requires:
- Self-capture during the spike and production operation (start now, accumulate over time)
- Or third-party providers (Kaiko, Tardis.dev, Crypto Lake — paid)

**Pragmatic solution for V1 backtest:** Use 1-minute klines to model fill prices, assume taker execution at the next minute's open price (with a configurable slippage add-on). This is conservative — real market orders typically fill better than next-minute-open — and it's the same trick MeteoEdge's backtest used (worst-case price at decision time, not mid).

**If the V1 backtest fails marginally,** consider buying 1 month of Tardis data for basis calibration before giving up on the strategy.

### 4.4 Fee schedule versions

**What:** A versioned record of Binance's fee schedule over the backtest window, including maker/taker and BNB-discount rates for spot and USDⓈ-M futures.

**Source:** Snapshot `binance.com/en/fee/schedule` monthly. Parse into JSON.

**Schema:**
```json
{
  "effective_from": "2025-01-01",
  "effective_to": "2025-06-30",
  "venues": {
    "spot": {
      "maker_bps": 10,
      "taker_bps": 10,
      "bnb_discount_maker_bps": 7.5,
      "bnb_discount_taker_bps": 7.5
    },
    "usdm_futures": {
      "maker_bps": 2,
      "taker_bps": 5,
      "bnb_discount_maker_bps": 1.8,
      "bnb_discount_taker_bps": 4.5
    }
  }
}
```

Unit-test the fee function against 10+ worked examples per version.

## 5. Harness Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      Backtest Runner                           │
│                                                                │
│  ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│  │ Event Loader │──►│ Time-indexed │◄──┤ Simulation Clock  │   │
│  │ (Parquet)    │   │  Event Store │   │  (ticks 60s)      │   │
│  └──────────────┘   └──────┬───────┘   └─────────┬─────────┘   │
│                            │                     │             │
│                            ▼                     ▼             │
│               ┌────────────────────────────────────┐           │
│               │ Time-Gated Query Interface         │           │
│               │  - get_funding_up_to(t, symbol)    │           │
│               │  - get_kline_at(t, symbol, venue)  │           │
│               │  - get_funding_history(t, symbol)  │           │
│               └──────────────┬─────────────────────┘           │
│                              │                                 │
│                              ▼                                 │
│    ┌─────────────────────────────────────────────┐             │
│    │     Production Strategy Modules (unchanged)  │             │
│    │   scorer.py, basis_monitor.py,               │             │
│    │   risk_manager.py, sizing.py, fee_model.py   │             │
│    └──────────────┬──────────────────────────────┘             │
│                   │                                            │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Fill Simulator                       │             │
│    │    - taker at next-minute open + slippage    │             │
│    │    - latency model                           │             │
│    │    - depth-limited partial fills             │             │
│    └──────────────┬──────────────────────────────┘             │
│                   │                                            │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Hedge Portfolio                      │             │
│    │    - open hedges, spot + perp legs           │             │
│    │    - unrealised basis P&L                    │             │
│    │    - margin ratio tracking                   │             │
│    │    - funding accrual on 8h boundaries        │             │
│    └──────────────┬──────────────────────────────┘             │
│                   │                                            │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Liquidation Engine                   │             │
│    │    - if perp mark moves against us, track    │             │
│    │      margin ratio; force-close at threshold  │             │
│    └──────────────┬──────────────────────────────┘             │
│                   ▼                                            │
│    ┌─────────────────────────────────────────────┐             │
│    │         Results & Reporting                  │             │
│    │    - per-hedge log, metrics, plots           │             │
│    │    - parameter sweep comparison              │             │
│    └─────────────────────────────────────────────┘             │
└────────────────────────────────────────────────────────────────┘
```

## 6. Module Specifications

### 6.1 Event store

Persisted as Parquet files partitioned by `date` and `symbol`:

```
data/backtest/
├── funding/
│   ├── symbol=BTCUSDT/date=2025-08-01/part-0.parquet
│   └── ...
├── klines_spot/
│   ├── symbol=BTCUSDT/date=2025-08-01/part-0.parquet
│   └── ...
├── klines_perp/
│   └── symbol=BTCUSDT/date=2025-08-01/part-0.parquet
├── fee_schedule_versions.json
└── manifest.json                      # coverage report, gaps flagged
```

Columnar, compressed, indexable by timestamp, queryable by DuckDB without a server. On the Mac mini, memory usage stays bounded and queries run in single-digit seconds over a full year.

Loaded into DuckDB at runtime:

```python
import duckdb
con = duckdb.connect(":memory:")
con.execute("""
  CREATE VIEW funding AS
  SELECT * FROM read_parquet('data/backtest/funding/**/*.parquet');
  CREATE VIEW klines_spot AS
  SELECT * FROM read_parquet('data/backtest/klines_spot/**/*.parquet');
  CREATE VIEW klines_perp AS
  SELECT * FROM read_parquet('data/backtest/klines_perp/**/*.parquet');
""")
```

### 6.2 Simulation clock

```python
@dataclass
class SimulationClock:
    start: datetime
    end: datetime
    step: timedelta = timedelta(seconds=60)     # match live polling cadence
    _now: datetime = field(init=False)

    def __post_init__(self):
        self._now = self.start

    def now(self) -> datetime:
        return self._now

    def tick(self) -> bool:
        self._now += self.step
        return self._now <= self.end
```

The clock is injected into every module that would otherwise call `datetime.now()`. Production code takes a `clock` parameter with a default of `SystemClock`; backtests pass `SimulationClock`. **Strategy code must never call `datetime.now()` directly.**

### 6.3 Time-gated query interface

The firewall against future leakage. Every query filters by `<= clock.now()`:

```python
class TimeGatedDataSource:
    def __init__(self, duckdb_conn, clock: SimulationClock):
        self.con = duckdb_conn
        self.clock = clock

    def latest_funding(self, symbol: str) -> FundingRow | None:
        """Most recent realised funding settlement at or before now."""
        t = self.clock.now()
        return self.con.execute("""
            SELECT * FROM funding
            WHERE symbol = ? AND funding_time <= ?
            ORDER BY funding_time DESC LIMIT 1
        """, [symbol, t]).fetchone()

    def funding_history(self, symbol: str, lookback: timedelta) -> list[FundingRow]:
        t = self.clock.now()
        t_lo = t - lookback
        return self.con.execute("""
            SELECT * FROM funding
            WHERE symbol = ? AND funding_time <= ? AND funding_time >= ?
            ORDER BY funding_time DESC
        """, [symbol, t, t_lo]).fetchall()

    def latest_kline(self, symbol: str, venue: str) -> KlineRow | None:
        """Most recent closed 1-min bar at or before now."""
        t = self.clock.now()
        view = f"klines_{venue}"
        return self.con.execute(f"""
            SELECT * FROM {view}
            WHERE symbol = ? AND open_time <= ?
            ORDER BY open_time DESC LIMIT 1
        """, [symbol, t]).fetchone()

    def next_kline(self, symbol: str, venue: str, after: datetime) -> KlineRow | None:
        """Used by the fill simulator for latency-aware fills."""
        view = f"klines_{venue}"
        return self.con.execute(f"""
            SELECT * FROM {view}
            WHERE symbol = ? AND open_time > ?
            ORDER BY open_time ASC LIMIT 1
        """, [symbol, after]).fetchone()
```

**Test for leakage:** a unit test that sets `clock.now() = T`, queries for "latest" data, and verifies no returned row has `captured_at > T`. Run on every CI build. This is one of the highest-value tests in the system — same discipline as MeteoEdge's harness.

### 6.4 Strategy modules (imported from production)

The backtest does not implement its own scorer, basis monitor, or risk manager. It imports the production modules directly. They must accept a data source and a clock, and must not call wall-clock functions.

```python
from fundingedge.engine.scorer import should_enter, should_exit, persistence_fraction
from fundingedge.engine.basis_monitor import compute_basis_bps, basis_tolerance_check
from fundingedge.engine.risk_manager import RiskManager
from fundingedge.engine.sizing import compute_notional
from fundingedge.engine.fee_model import fee_bps
```

If a production module has wall-clock dependencies, refactor it. Same rule as MeteoEdge.

### 6.5 Fill simulator

Given a proposed hedge entry or exit (symbol, spot quantity, perp quantity), determine how much fills, at what price, and over what latency.

**Entry:**
- Decision made at `clock.now() = t_d`
- Apply `execution_latency` (default 500ms) — advance a local fill clock to `t_d + 500ms`
- Fetch the *next* 1-min kline strictly after the fill clock, for spot and perp
- Fill the spot buy at `spot_next_kline.open * (1 + slippage_bps/10000)`
- Fill the perp sell at `perp_next_kline.open * (1 - slippage_bps/10000)`
- Defaults: `slippage_bps = 2` (conservative taker slippage on deep-liquidity pairs)

**Exit:** same mechanic, reversed direction.

**Why this is conservative:** using the *next* minute's open price means we don't get the price we saw at decision time — we get what was available after our decision propagated. This is the single most important conservative assumption in the harness, and it matches MeteoEdge's latency model.

**Partial fills:**
- V1 backtest assumes full fills at these prices; depth modelling is V2 (would require third-party L2 data).
- Sensitivity analysis: run the parameter sweep with `slippage_bps = [2, 5, 10, 20]` to see where the strategy breaks.

```python
@dataclass
class FillResult:
    filled_spot_quantity: float
    filled_perp_quantity: float
    spot_fill_price: float
    perp_fill_price: float
    spot_fees_usd: float
    perp_fees_usd: float
    slippage_applied_bps: float


class FillSimulator:
    def __init__(self, data: TimeGatedDataSource, clock: SimulationClock,
                 execution_latency: timedelta = timedelta(milliseconds=500),
                 slippage_bps: float = 2.0):
        ...

    def simulate_hedge_entry(self, symbol: str, notional_usd: float) -> FillResult: ...
    def simulate_hedge_exit(self, hedge: HedgePosition) -> FillResult: ...
```

### 6.6 Hedge portfolio

In-memory, full history:

```python
class BacktestHedgePortfolio:
    def __init__(self, starting_cash_usd: float):
        self.cash = starting_cash_usd
        self.hedges: dict[str, HedgePosition] = {}     # symbol -> position (max 1 per symbol)
        self.closed_hedges: list[HedgePosition] = []
        self.daily_snapshots: list[DailySnapshot] = []

    def open_hedge(self, symbol: str, fill: FillResult, clock: SimulationClock): ...
    def close_hedge(self, symbol: str, fill: FillResult, clock: SimulationClock, reason: str): ...
    def accrue_funding(self, symbol: str, settlement: FundingRow): ...
    def mark_to_market(self, data: TimeGatedDataSource): ...
    def margin_ratio(self, symbol: str, data: TimeGatedDataSource) -> float: ...
    def snapshot_eod(self, date: date): ...
```

**Funding accrual.** On each simulated tick, for each open hedge, check if a funding-settlement boundary has passed since the last accrual. If so:

```python
funding_payment = settlement.funding_rate * hedge.perp_notional_usd
hedge.realised_funding += funding_payment
hedge.cash += funding_payment     # credited immediately to cash balance
```

The rate used is the **realised** rate at that exact boundary, pulled via `data.latest_funding(symbol)`.

**Mark-to-market.** On each tick:

```python
unrealised_spot_pnl = (spot_mid_now - hedge.spot_entry_price) * hedge.spot_quantity
unrealised_perp_pnl = (hedge.perp_entry_price - perp_mid_now) * hedge.perp_quantity  # short
unrealised_basis_pnl = unrealised_spot_pnl + unrealised_perp_pnl  # ≈ 0 for a clean hedge
```

**Margin ratio.** Perp leg uses Isolated Margin at 2x:

```python
initial_margin = hedge.perp_notional_entry / 2
adverse_move_usd = max(0, (perp_mark_now - hedge.perp_entry_price) * hedge.perp_quantity)  # short loses on rally
current_margin = initial_margin - adverse_move_usd
margin_ratio = adverse_move_usd / initial_margin     # 0.0 = safe, 1.0 = liquidated
```

If `margin_ratio > 1.0`, the position is liquidated — the liquidation engine closes both legs at that tick's prices, applies a 10 bps liquidation penalty, and records the hedge as `LIQUIDATED`.

### 6.7 Liquidation engine

```python
def check_liquidations(portfolio: BacktestHedgePortfolio, data: TimeGatedDataSource,
                      clock: SimulationClock):
    for symbol, hedge in list(portfolio.hedges.items()):
        if portfolio.margin_ratio(symbol, data) >= 1.0:
            fill = simulate_liquidation_close(hedge, data, clock)
            portfolio.close_hedge(symbol, fill, clock, reason="LIQUIDATED")
            # apply penalty
            portfolio.cash -= hedge.perp_notional_entry * 0.0010    # 10 bps
```

In a pure cash-and-carry hedge, liquidation should essentially never happen — the spot leg appreciates when the perp leg loses. The only scenario is a catastrophic basis blow-out where spot and perp diverge dramatically. The backtest must still model this because it's the dominant tail risk.

### 6.8 Results & reporting

At end of backtest, compute and emit:

- **Per-hedge CSV:** every completed hedge with entry/exit context, funding accrued, basis P&L, fees, net result
- **Per-day CSV:** daily realised P&L, fees, cash balance, open-hedge count, margin usage
- **Summary JSON:**
  - total hedges, wins, losses
  - win rate, median net yield bps, mean net yield bps
  - gross funding collected, fees paid, basis P&L, liquidation penalties
  - net annualised return on deployed capital
  - Sharpe (annualised), Sortino (annualised)
  - max drawdown (absolute and % of starting capital)
  - max consecutive losing hedges
  - per-symbol attribution
  - funding-regime attribution (bucketed by prevailing 7-day average funding rate)
- **Plots** (matplotlib, save to PNG):
  - Cumulative P&L curve with funding vs. basis decomposition
  - Per-symbol cumulative P&L
  - Distribution of hedge hold times
  - Funding rate heatmap over time per symbol

### 6.9 Parameter sweep

Pure function from (config, event store) to summary metrics. Run in parallel across a grid:

```python
def run_single_backtest(config: StrategyConfig) -> SummaryMetrics:
    ...

grid = [
    StrategyConfig(
        entry_threshold_bps=e,
        exit_threshold_bps=x,
        persistence_min=p,
        basis_ceiling_bps=b,
        slippage_bps=s,
    )
    for e in [2.0, 3.0, 5.0, 7.0]
    for x in [0.5, 1.0, 2.0]
    for p in [0.50, 0.60, 0.70]
    for b in [15, 20, 30]
    for s in [2.0, 5.0, 10.0]
]
results = parallel_map(run_single_backtest, grid, workers=3)
```

The Mac mini's i7 has 4 cores; cap workers at 3 to leave headroom.

Report: which config produces the best Sharpe, which is most robust to slippage, which has the smallest drawdown. Select the operating config **conservatively** — prefer moderate parameters that perform well across the whole grid over aggressive parameters optimal only at their exact setting (overfitting).

## 7. Exit Criteria (Stage 1 Gate)

The backtest passes and unlocks Stage 2 if **all** of the following hold on out-of-sample data (see §8):

| Metric | Threshold |
|---|---|
| Total hedges | ≥ 50 |
| Win rate | > 60% |
| Net annualised return on deployed capital | > 15% |
| Sharpe ratio (annualised) | > 1.5 |
| Max drawdown | < 10% of deployed capital |
| Max consecutive losing hedges | ≤ 5 |
| Robustness under 10 bps extra slippage | Sharpe > 1.0 |
| Robustness under +3 bps per leg fees | Sharpe > 1.0 |
| Liquidation count | 0 on baseline config |

If any fail on out-of-sample data, do not proceed. Revisit the spec.

## 8. Out-of-Sample Discipline

This is the single most important methodological point. Skip it and the backtest is worthless — the same rule that protected MeteoEdge.

**Split the 12-month window:**
- Earliest 60% (~7 months) → **training set**. Used for parameter tuning, sweeps, iteration.
- Middle 20% (~2.5 months) → **validation set**. Used to pick the final operating config from the sweep.
- Most recent 20% (~2.5 months) → **held-out test set**. Touched exactly once, at the end.

**The rule:** you may look at training and validation data as many times as you want. You may touch the held-out set exactly once, at the end, to produce the numbers you report against Stage 1 exit criteria. If the held-out set fails, you do *not* get to retune and retest. You go back to the spec.

This is uncomfortable because it means you might do weeks of work and then fail at the gate. That is the point. It is the only way to produce a number you can trust.

## 9. Implementation Plan (Stories)

Order of implementation that delivers an end-to-end smoke test fast, then hardens each component:

1. **Event store skeleton** — Parquet layout, DuckDB views, basic loader
2. **Simulation clock + time-gated data source** — including the leakage unit test
3. **Strategy module refactor** — remove wall-clock calls from production scorer/basis monitor/risk manager, inject clock and data source
4. **Minimal fill simulator** — taker-only, zero latency, zero slippage. End-to-end smoke test passes.
5. **Hedge portfolio + funding accrual** — P&L reconciles for a hand-crafted test week
6. **Summary metrics + CSV output** — enough to eyeball a single backtest
7. **Historical data ingestion** — pull 12 months of funding + klines (spot & perp) into Parquet
8. **First end-to-end backtest** — on training set only. Iterate on bugs until metrics are plausible.
9. **Fee model** — replace flat assumption with versioned Binance fee schedule
10. **Latency model + conservative fill** — replay order against *next* minute's open, not decision-time
11. **Margin accounting + liquidation engine** — force-close on margin-ratio breach
12. **Parameter sweep harness** — parallel, with structured results
13. **Reporting: plots, per-segment attribution**
14. **Leakage audit** — deliberate injection of future-leaking code, verify it's caught
15. **Out-of-sample gate run** — exactly once, against the held-out 20%

## 10. Agent Assignments

| Story | Owner | Why |
|---|---|---|
| Event store, DuckDB setup | Junior (Haiku) | Boilerplate, well-specified |
| Simulation clock + time-gated query | Mid (Sonnet) | Correctness-critical; the leakage firewall |
| Strategy module refactor | Mid (Sonnet) | Touches production code, changes interface |
| Fill simulator | Mid (Sonnet) | Most consequential assumption in the harness |
| Hedge portfolio + funding accrual | Mid (Sonnet) | P&L correctness; must match production reconciler |
| Margin / liquidation engine | Mid (Sonnet) | Tail-risk modelling |
| Summary metrics | Junior (Haiku) | Arithmetic on known shapes |
| Historical data ingestion | Junior (Haiku), one script per source | Parallelizable, bounded scope |
| Fee model | Mid (Sonnet) | Small surface, high consequence |
| Parameter sweep harness | Junior (Haiku) | Map-reduce over a grid |
| Reporting / plots | Junior (Haiku) | Templates and matplotlib |
| Leakage audit | Mid (Sonnet), adversarial | Deliberately tries to break its own firewall |
| Out-of-sample gate run | Operator (André) | One-shot, ceremonial, not automated |

Tech Lead PM reviews every PR. 100% unit test coverage on: time-gated query interface, fee model, risk manager, fill simulator, margin/liquidation engine.

## 11. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Future leakage slips past the firewall | Backtest looks profitable but live bleeds | Dedicated leakage audit story (14); adversarial tests; CI enforcement |
| Fee schedule drift during backtest window | Over/understates net edge | Versioned fee schedule; alert if live fills diverge from modeled fees by > 10% |
| Over-tuning on training set | Validation passes, held-out fails | Rigid three-way split; operator discipline on touching held-out once |
| Fill simulator too optimistic | Live P&L undershoots backtest | Slippage sweep; require Sharpe > 1.0 under stress, not just baseline |
| 1-min kline resolution misses intra-minute basis spikes | Liquidation underreported | Run a sanity sweep with synthetic noise on the perp mark; if liquidations jump, demand finer data |
| Funding rate regime dominated by bull-market carry | Backtest not representative of sideways / bear regimes | 12-month window should span at least one regime shift; plot funding-regime attribution and reject if performance is monotonic in funding level |
| Strategy modules drift between prod and backtest | Backtest no longer predicts prod | Same modules imported in both; compile-time failure if signatures diverge |
| Mac mini runs out of memory on large sweep | Harness crashes mid-run | Parquet + DuckDB streams from disk; cap parallel workers at 3 |

## 12. What Done Looks Like

Epic 7 is complete when, on a fresh clone of the repo, the operator can:

1. Run `make ingest` to pull 12 months of historical data into Parquet
2. Run `make backtest` to produce a summary on the training set
3. Run `make sweep` to produce a grid over configs
4. Run `make validate CONFIG=best.json` to evaluate the winning config on the validation set
5. Run `make gate CONFIG=final.json` exactly once to produce held-out numbers

If the gate numbers hit all Stage 1 exit criteria, the Tech Lead PM opens the PR transitioning `TRADING_ENABLED=false` to the Stage 2 testnet run. If they do not, the operator writes a retro, closes Epic 7, and reopens the strategy spec.

---

*End of backtest harness design. Ready for story estimation.*
