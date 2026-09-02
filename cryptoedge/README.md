# cryptoedge — Polymarket crypto up/down price collector

**A separate thesis from MeteoEdge's weather markets, deliberately isolated.**

## Why this exists

`backtest_results/edge_hunt_2026-08-27.md` closed the weather thesis: the market
*encompasses* the model (`c = +0.0125`, 95% CI `[−0.0875, +0.1281]`). The lesson
that mattered was methodological — for months the project measured model skill
against a **50/50 prior** instead of **against the market price**, and only the
encompassing test settled it.

Polymarket runs 5- and 15-minute BTC/ETH/SOL/XRP "Up or Down" markets. A logistic
model on Binance order-flow shows real predictive signal (+4.4σ out of sample)
*against a 50/50 prior*. That is exactly the number that misled the weather work.
This package exists to collect the one thing that can answer the real question:
**the market's own price at the moment you could trade.**

Then `encompass.py` runs the identical test:

```
logit P(Up) = a + b·logit(p_market) + c·logit(p_model)
```

`c ≈ 0` ⇒ the price already knows it, same as weather, stop.
`c` materially positive ⇒ you have something the price does not.

## Isolation invariant — READ BEFORE EDITING

This package **must never** import from, or write to, MeteoEdge's trading path:

- ❌ no imports from `src.execution.*`, `src.strategy.*`, `src.model.*`
- ❌ never opens `data/meteoedge.db`
- ❌ never touches `station_overrides`, `trades`, `open_positions`, `risk_state`

It writes only to its own `data/cryptoedge.db` and places **no orders of any
kind**. The live-trading halt (#1053/#1054) is a safety invariant of this repo;
this package is structurally incapable of affecting it, and it must stay that way.

`tests/test_isolation.py` enforces this by AST-scanning every module here.

## What it records

Per poll (default every 15s), for each live up/down market:

| field | why |
|---|---|
| `best_bid`, `best_ask`, `spread` | **a real two-sided book** — unlike the weather markets, where `yes_ask + no_ask == 100` on 99.34% of rows (a mid split into complements). This is an executable price. |
| `outcome_prices` | Gamma's mid, for comparison |
| `liquidity` | capacity — ~$16k/market observed, orders of magnitude more than a weather bracket |
| `seconds_to_settlement` | the edge decays through the window; entry timing is the whole question |
| `window_start_ms`, `window_end_ms` | parsed from the slug epoch; joins to the Binance tape |

The Binance tape is **not** logged live — Binance serves historical klines
retroactively, so it is fetched at analysis time. Only Polymarket prices are
ephemeral, so only they are collected.

## Resolution — get this right

These markets do **not** resolve on close-vs-open:

> resolve **"Up"** if the **Chainlink BTC/USD 60s-TWAP** over the window is
> **≥ the price at the start of the window**, else **"Down"**.
> Source: `https://data.chain.link/streams/btc-usd-twap-60s-streams`

Three consequences, each of which invalidates a naive backtest:

1. The target is **TWAP-of-window vs window-open**, not close vs open.
2. **Ties resolve Up** (`≥`), so flat windows are wins for Up, not losses.
3. The oracle is **Chainlink**, not Binance. A Binance-derived TWAP is a *proxy*
   and will disagree on a minority of windows — treat any Binance-based label as
   approximate, and prefer the recorded Gamma resolution (`resolve.py`) as truth.

`resolve.py` backfills the authoritative outcome from Gamma once a market closes.

## Usage

```bash
# collect (long-running; systemd unit provided)
python -m cryptoedge.collector --db data/cryptoedge.db --interval 15

# backfill outcomes for closed markets (cron/timer, every 15 min)
python -m cryptoedge.resolve --db data/cryptoedge.db

# once enough data has accrued -- see the pre-registration first
python -m cryptoedge.encompass --db data/cryptoedge.db --asset btc --window 5m
```

## Health check

```bash
bash cryptoedge/healthcheck.sh            # defaults to data/cryptoedge.db
```

Healthy reads: `polls_last_hour` ~240, `FAILED_last_hour` 0, `stuck_resolutions`
0, and every integrity counter 0.

**`FAILED_last_hour` is the number that matters.** On 2026-09-01 the host lost
network for 28 hours; the collector stayed `active`, retried, and collected
nothing, and it was found only because SSH dropped. ~340 windows were lost.
Since then the collector EXITS non-zero after `MAX_CONSECUTIVE_FAILURES`
(~5 min of collecting nothing) so systemd restarts it and `NRestarts` makes
the stall visible.

> **Do not hand-write the time filters.** `poll_runs.ts` is ISO8601 with a `T`
> separator; `datetime('now')` returns a space separator, and `'T'` (0x54) sorts
> above `' '` (0x20). So `WHERE ts > datetime('now','-1 hour')` matches every row
> from the same DATE. That bug reported 3,027 polls / 513 failures for an hour
> that actually had 240 polls and zero failures, and cost an unnecessary service
> restart. Always wrap the column: `datetime(ts)`. `healthcheck.sh` does, and a
> test enforces it.

## Do not run the test early

`encompass.py` refuses to report below a minimum sample and prints the count
only. As with M3, an underpowered look is not a verdict in either direction, and
looking twice inflates the false-positive rate. Fix the population, the split and
the bar **before** the first powered run.
