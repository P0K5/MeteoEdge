# Copy-Trading Hypothesis Spike (data collection + backtest)

**Run date:** 2026-09-18-sustainable-top30  
**Status: exploratory spike, not a pre-registered finding.** No live-trading changes of any kind. This only answers whether the hypothesis is worth building further -- see the module docstring (`src/scripts/copy_trade_backtest.py`) for exactly what is and isn't modeled: BUY-only (no position-lifecycle reconstruction), a flat slippage stand-in for reaction latency (not a tape replay), size-mirrored aggregate $ PnL.  

**Assumed copier slippage:** 150 bps flat, worse fill only.  


---

## Per-wallet summary

| wallet | BUY trades | resolved | trader win% | trader mean ROI | copier win% | copier mean ROI | copier $ PnL (mirrored size) |
|---|---|---|---|---|---|---|---|
| `0x49f206f4…` | 276 | 200 | 61.5% | 122.2% | 61.5% | 119.0% | $29,844.42 |
| `0xe015b5a2…` | 1524 | 1434 | 52.9% | 4.1% | 52.9% | 2.7% | $71,808.54 |
| `0x5e945820…` | 959 | 956 | 52.2% | 2.9% | 52.2% | 1.4% | $380,888.14 |
| `0xd106952e…` | 10500 | 10292 | 51.7% | 2.3% | 51.7% | 0.8% | $-84,545.90 |
| `0xe40ecff6…` | 236 | 225 | 59.1% | 1.1% | 59.1% | -0.4% | $123,874.13 |
| `0x82398835…` | 446 | 427 | 55.5% | 0.5% | 55.5% | -1.0% | $141,054.32 |
| `0x03805a13…` | 10499 | 4651 | 89.1% | -0.2% | 89.1% | -1.1% | $-236.15 |
| `0x1610db79…` | 4884 | 4860 | 39.9% | 0.4% | 39.9% | -1.1% | $982,304.57 |
| `0x6ac5bb06…` | 3592 | 3483 | 49.6% | -2.0% | 49.6% | -3.5% | $-923,543.67 |
| `0x43b68e2a…` | 207 | 207 | 43.5% | -2.7% | 43.5% | -4.2% | $-64,666.83 |
| `0xabb89972…` | 8856 | 8013 | 51.3% | -8.2% | 51.3% | -9.5% | $-1,131,364.67 |
| `0x1465b79b…` | 1281 | 538 | 80.9% | -8.8% | 80.9% | -10.1% | $-98,976.17 |
| `0x5a218c7a…` | 5049 | 2956 | 72.5% | -9.0% | 72.5% | -10.3% | $-1,882.90 |
| `0x0e604be1…` | 893 | 847 | 29.8% | -9.8% | 29.8% | -11.2% | $-172,734.26 |
| `0x68200501…` | 83 | 82 | 43.9% | -11.9% | 43.9% | -13.2% | $22,223.24 |
| `0xa080dadd…` | 237 | 228 | 44.3% | -17.2% | 44.3% | -18.4% | $-150,573.86 |
| `0xf0318c32…` | 203 | 198 | 29.8% | -20.2% | 29.8% | -21.4% | $-689,192.64 |
| `0x6db983ff…` | 4284 | 3779 | 25.8% | -24.4% | 25.8% | -25.5% | $-14,400.77 |

## Aggregate

- Wallets evaluated: 18

- Total resolved BUY trades: 43376

- Wallets where the COPIER would have been net positive: 7/18

- Aggregate original-trader $ PnL (mirrored sizing): -273,151.95

- Aggregate copier $ PnL (mirrored sizing, 150 bps slippage): -1,580,120.46
