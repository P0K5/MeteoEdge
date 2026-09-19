# Copy-Trading Hypothesis Spike (data collection + backtest)

**Run date:** 2026-09-19-candidate-replay  
**Status: exploratory spike, not a pre-registered finding.** No live-trading changes of any kind. This only answers whether the hypothesis is worth building further -- see the module docstring (`src/scripts/copy_trade_backtest.py`) for exactly what is and isn't modeled: BUY-only (no position-lifecycle reconstruction), a flat slippage stand-in for reaction latency (not a tape replay), size-mirrored aggregate $ PnL.  

**Assumed copier slippage:** 150 bps flat, worse fill only.  


---

## Per-wallet summary

Sorted by copier **median** ROI, not mean or $ PnL -- mean and size-mirrored $ PnL are both easily dominated by one or two huge tail bets (a wallet can show a triple-digit mean ROI or a six-figure $ PnL while its *typical* trade is flat or a loser). Median ROI is the size-independent read on whether the typical trade actually wins, which is what "is this a repeatable edge" requires. A wide mean/median gap is itself a flag, not noise.

| wallet | BUY trades | resolved | trader win% | trader mean ROI | trader median ROI | copier win% | copier mean ROI | copier median ROI | copier $ PnL (mirrored size) | copier $ PnL (flat stake) |
|---|---|---|---|---|---|---|---|---|---|---|
| `0x0d18e30e…` | 10499 | 10499 | 52.2% | 2.7% | 81.8% | 52.2% | 1.2% | 79.1% | $-125,408.65 | $606.93 |
| `0x5e945820…` | 969 | 969 | 51.8% | 2.2% | 63.9% | 51.8% | 0.7% | 61.5% | $283,116.68 | $35.18 |
| `0xd106952e…` | 10500 | 10292 | 51.7% | 2.3% | 49.3% | 51.7% | 0.8% | 47.0% | $-84,545.90 | $396.17 |
| `0xce29c004…` | 341 | 331 | 57.1% | 12.7% | 13.6% | 57.1% | 11.1% | 12.0% | $67,763.20 | $183.13 |
| `0x2005d16a…` | 10500 | 10199 | 52.5% | 5.1% | 7.5% | 52.5% | 3.6% | 5.9% | $405,972.98 | $1,832.07 |
| `0x4ebc2722…` | 10254 | 10116 | 57.1% | 5.4% | 6.4% | 57.1% | 3.9% | 4.8% | $150,918.71 | $1,961.56 |
| `0xd3b034d7…` | 9147 | 2271 | 49.3% | 75.3% | -100.0% | 49.3% | 72.7% | -100.0% | $726.16 | $8,258.02 |

## Aggregate

- Wallets evaluated: 7

- Total resolved BUY trades: 44677

- Wallets where the COPIER would have been net positive (mirrored sizing): 5/7

- Aggregate original-trader $ PnL (mirrored sizing): 1,161,112.07

- Aggregate copier $ PnL (mirrored sizing, 150 bps slippage): 698,543.18

- Wallets where the COPIER would have been net positive (flat stake ($5.00/trade)): 7/7

- Aggregate copier $ PnL (flat stake ($5.00/trade), 150 bps slippage): 13,273.06
