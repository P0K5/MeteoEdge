# Copy-Trading Hypothesis Spike (data collection + backtest)

**Run date:** 2026-09-18  
**Status: exploratory spike, not a pre-registered finding.** No live-trading changes of any kind. This only answers whether the hypothesis is worth building further -- see the module docstring (`src/scripts/copy_trade_backtest.py`) for exactly what is and isn't modeled: BUY-only (no position-lifecycle reconstruction), a flat slippage stand-in for reaction latency (not a tape replay), size-mirrored aggregate $ PnL.  

**Assumed copier slippage:** 150 bps flat, worse fill only.  


---

## Per-wallet summary

| wallet | BUY trades | resolved | trader win% | trader mean ROI | copier win% | copier mean ROI | copier $ PnL (mirrored size) |
|---|---|---|---|---|---|---|---|
| `0x5d4aba8a…` | 10500 | 10460 | 58.5% | 11.3% | 58.5% | 9.7% | $1,197.72 |
| `0x9d57c42e…` | 9707 | 9639 | 55.1% | 1.0% | 55.1% | -0.5% | $-2,129.51 |

## Aggregate

- Wallets evaluated: 2

- Total resolved BUY trades: 20099

- Wallets where the COPIER would have been net positive: 1/2

- Aggregate original-trader $ PnL (mirrored sizing): 1,493.80

- Aggregate copier $ PnL (mirrored sizing, 150 bps slippage): -931.79
