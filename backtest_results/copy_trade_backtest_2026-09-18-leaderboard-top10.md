# Copy-Trading Hypothesis Spike (data collection + backtest)

**Run date:** 2026-09-18-leaderboard-top10  
**Status: exploratory spike, not a pre-registered finding.** No live-trading changes of any kind. This only answers whether the hypothesis is worth building further -- see the module docstring (`src/scripts/copy_trade_backtest.py`) for exactly what is and isn't modeled: BUY-only (no position-lifecycle reconstruction), a flat slippage stand-in for reaction latency (not a tape replay), size-mirrored aggregate $ PnL.  

**Assumed copier slippage:** 150 bps flat, worse fill only.  


---

## Per-wallet summary

| wallet | BUY trades | resolved | trader win% | trader mean ROI | copier win% | copier mean ROI | copier $ PnL (mirrored size) |
|---|---|---|---|---|---|---|---|
| `0x63cf8544…` | 27 | 27 | 44.4% | -31.5% | 44.4% | -32.5% | $1,085,111.73 |
| `0x369f4643…` | 30 | 16 | 81.2% | 10.9% | 81.2% | 9.3% | $318,255.20 |
| `0x1465b79b…` | 1281 | 538 | 80.9% | -8.8% | 80.9% | -10.1% | $-98,976.17 |
| `0xb87532a1…` | 33 | 23 | 78.3% | 62.8% | 78.3% | 60.4% | $31,695.87 |
| `0x49f206f4…` | 276 | 200 | 61.5% | 122.2% | 61.5% | 119.0% | $29,844.42 |
| `0x6ac5bb06…` | 3592 | 3483 | 49.6% | -2.0% | 49.6% | -3.5% | $-923,543.67 |
| `0xd106952e…` | 10500 | 10292 | 51.7% | 2.3% | 51.7% | 0.8% | $-84,545.90 |
| `0xabb89972…` | 8856 | 8014 | 51.3% | -8.2% | 51.3% | -9.6% | $-1,142,878.38 |
| `0x5e945820…` | 959 | 956 | 52.2% | 2.9% | 52.2% | 1.4% | $380,888.14 |
| `0xc9ae00ed…` | 4 | 2 | 100.0% | 119.8% | 100.0% | 116.6% | $11,018.26 |

## Aggregate

- Wallets evaluated: 10

- Total resolved BUY trades: 23551

- Wallets where the COPIER would have been net positive: 6/10

- Aggregate original-trader $ PnL (mirrored sizing): 602,822.80

- Aggregate copier $ PnL (mirrored sizing, 150 bps slippage): -393,130.50
