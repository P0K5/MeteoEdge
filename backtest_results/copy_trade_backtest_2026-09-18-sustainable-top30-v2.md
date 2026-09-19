# Copy-Trading Hypothesis Spike (data collection + backtest)

**Run date:** 2026-09-18-sustainable-top30-v2  
**Status: exploratory spike, not a pre-registered finding.** No live-trading changes of any kind. This only answers whether the hypothesis is worth building further -- see the module docstring (`src/scripts/copy_trade_backtest.py`) for exactly what is and isn't modeled: BUY-only (no position-lifecycle reconstruction), a flat slippage stand-in for reaction latency (not a tape replay), size-mirrored aggregate $ PnL.  

**Assumed copier slippage:** 150 bps flat, worse fill only.  


---

## Per-wallet summary

Sorted by copier **median** ROI, not mean or $ PnL -- mean and size-mirrored $ PnL are both easily dominated by one or two huge tail bets (a wallet can show a triple-digit mean ROI or a six-figure $ PnL while its *typical* trade is flat or a loser). Median ROI is the size-independent read on whether the typical trade actually wins, which is what "is this a repeatable edge" requires. A wide mean/median gap is itself a flag, not noise.

| wallet | BUY trades | resolved | trader win% | trader mean ROI | trader median ROI | copier win% | copier mean ROI | copier median ROI | copier $ PnL (mirrored size) |
|---|---|---|---|---|---|---|---|---|---|
| `0x5e945820…` | 969 | 969 | 51.8% | 2.2% | 63.9% | 51.8% | 0.7% | 61.5% | $283,116.68 |
| `0xd106952e…` | 10500 | 10292 | 51.7% | 2.3% | 49.3% | 51.7% | 0.8% | 47.0% | $-84,545.90 |
| `0xe40ecff6…` | 238 | 233 | 59.7% | 2.8% | 22.0% | 59.7% | 1.2% | 20.1% | $133,156.87 |
| `0x82398835…` | 450 | 431 | 55.2% | 0.0% | 20.5% | 55.2% | -1.5% | 18.7% | $135,088.58 |
| `0x1465b79b…` | 1281 | 538 | 80.9% | -8.8% | 3.8% | 80.9% | -10.1% | 2.3% | $-98,976.17 |
| `0x5a218c7a…` | 5146 | 2772 | 73.2% | -6.1% | 2.0% | 73.2% | -7.4% | 0.5% | $-498.19 |
| `0x49f206f4…` | 276 | 200 | 61.5% | 122.2% | 1.0% | 61.5% | 119.0% | 0.0% | $29,844.42 |
| `0xabb89972…` | 8852 | 7999 | 51.4% | -8.0% | 1.1% | 51.4% | -9.4% | 0.0% | $-531,058.89 |
| `0x03805a13…` | 10499 | 4605 | 88.7% | 0.1% | 1.0% | 88.7% | -0.8% | 0.0% | $-935.97 |
| `0xe015b5a2…` | 1525 | 1436 | 52.8% | 4.0% | 0.1% | 52.8% | 2.5% | 0.0% | $69,882.71 |
| `0x6ac5bb06…` | 3593 | 3489 | 49.6% | -2.1% | -100.0% | 49.6% | -3.5% | -100.0% | $-950,527.05 |
| `0x1610db79…` | 4982 | 4975 | 39.6% | -0.3% | -100.0% | 39.6% | -1.8% | -100.0% | $886,541.82 |
| `0xf0318c32…` | 203 | 198 | 29.8% | -20.2% | -100.0% | 29.8% | -21.4% | -100.0% | $-689,192.64 |
| `0x68200501…` | 91 | 90 | 43.3% | -12.1% | -100.0% | 43.3% | -13.4% | -100.0% | $3,437.42 |
| `0x43b68e2a…` | 207 | 207 | 43.5% | -2.7% | -100.0% | 43.5% | -4.2% | -100.0% | $-64,666.83 |
| `0x0e604be1…` | 905 | 859 | 30.7% | -7.4% | -100.0% | 30.7% | -8.7% | -100.0% | $-142,373.30 |
| `0xa080dadd…` | 242 | 232 | 44.0% | -17.8% | -100.0% | 44.0% | -19.0% | -100.0% | $-154,757.47 |
| `0x6db983ff…` | 4287 | 3779 | 25.8% | -24.4% | -100.0% | 25.8% | -25.5% | -100.0% | $-14,400.77 |

## Aggregate

- Wallets evaluated: 18

- Total resolved BUY trades: 43304

- Wallets where the COPIER would have been net positive: 7/18

- Aggregate original-trader $ PnL (mirrored sizing): 121,663.99

- Aggregate copier $ PnL (mirrored sizing, 150 bps slippage): -1,190,864.68
