# Copy-Trading Hypothesis Spike (data collection + backtest)

**Run date:** 2026-09-18-sustainable-new24  
**Status: exploratory spike, not a pre-registered finding.** No live-trading changes of any kind. This only answers whether the hypothesis is worth building further -- see the module docstring (`src/scripts/copy_trade_backtest.py`) for exactly what is and isn't modeled: BUY-only (no position-lifecycle reconstruction), a flat slippage stand-in for reaction latency (not a tape replay), size-mirrored aggregate $ PnL.  

**Assumed copier slippage:** 150 bps flat, worse fill only.  


---

## Per-wallet summary

Sorted by copier **median** ROI, not mean or $ PnL -- mean and size-mirrored $ PnL are both easily dominated by one or two huge tail bets (a wallet can show a triple-digit mean ROI or a six-figure $ PnL while its *typical* trade is flat or a loser). Median ROI is the size-independent read on whether the typical trade actually wins, which is what "is this a repeatable edge" requires. A wide mean/median gap is itself a flag, not noise.

| wallet | BUY trades | resolved | trader win% | trader mean ROI | trader median ROI | copier win% | copier mean ROI | copier median ROI | copier $ PnL (mirrored size) |
|---|---|---|---|---|---|---|---|---|---|
| `0x0d18e30e…` | 10499 | 10499 | 52.2% | 2.7% | 81.8% | 52.2% | 1.2% | 79.1% | $-125,408.65 |
| `0xd3b034d7…` | 7523 | 7498 | 60.7% | 10.0% | 35.5% | 60.7% | 8.4% | 33.4% | $406,990.77 |
| `0xf0b6a8eb…` | 161 | 160 | 68.8% | -1.4% | 22.5% | 68.8% | -2.8% | 20.7% | $133,557.49 |
| `0x1d1ade62…` | 965 | 962 | 52.0% | -0.3% | 22.0% | 52.0% | -1.7% | 20.1% | $-421,507.36 |
| `0x26b46988…` | 2214 | 2212 | 50.9% | -3.4% | 16.7% | 50.9% | -4.8% | 15.0% | $-470,633.63 |
| `0xa2c908ce…` | 1506 | 1494 | 79.3% | -0.1% | 14.9% | 79.3% | -1.6% | 13.2% | $-14,480.77 |
| `0xce29c004…` | 325 | 325 | 57.2% | 11.9% | 13.6% | 57.2% | 10.3% | 12.0% | $64,245.72 |
| `0xa1e6ba93…` | 75 | 75 | 54.7% | -13.9% | 8.7% | 54.7% | -15.2% | 7.1% | $-234,958.94 |
| `0x4ebc2722…` | 10249 | 10207 | 57.3% | 4.9% | 6.4% | 57.3% | 3.4% | 4.8% | $148,742.26 |
| `0x2005d16a…` | 10500 | 10437 | 51.8% | 3.3% | 5.4% | 51.8% | 1.7% | 3.8% | $356,288.95 |
| `0x96489abc…` | 832 | 766 | 62.0% | -11.2% | 1.8% | 62.0% | -12.5% | 0.3% | $-84,285.78 |
| `0xd1049158…` | 229 | 214 | 61.7% | -18.6% | 1.5% | 61.7% | -19.8% | 0.0% | $-89,599.64 |
| `0xf68a2819…` | 9133 | 8970 | 50.2% | 7.1% | 0.4% | 50.2% | 5.5% | 0.0% | $41,543.67 |
| `0x4f1d5ae2…` | 10500 | 3468 | 75.2% | -8.8% | 0.4% | 75.2% | -9.8% | 0.0% | $3,879.18 |
| `0x1387d145…` | 10282 | 2935 | 82.0% | 0.9% | 1.0% | 82.0% | -0.2% | 0.0% | $189.99 |
| `0x8a8685a7…` | 841 | 838 | 39.0% | 1.8% | -100.0% | 39.0% | 0.3% | -100.0% | $731,250.09 |
| `0xe2d0f058…` | 467 | 467 | 38.8% | -15.8% | -100.0% | 38.8% | -17.0% | -100.0% | $46,773.70 |
| `0x3f985e3a…` | 560 | 552 | 43.7% | -3.9% | -100.0% | 43.7% | -5.4% | -100.0% | $30,151.59 |
| `0x78becf0a…` | 1125 | 1124 | 49.4% | 6.2% | -100.0% | 49.4% | 4.6% | -100.0% | $218,897.33 |
| `0xe392d6cc…` | 2059 | 1960 | 33.1% | -16.1% | -100.0% | 33.1% | -17.4% | -100.0% | $-1,528.46 |
| `0x19ae9efe…` | 3098 | 3040 | 38.8% | -8.7% | -100.0% | 38.8% | -10.0% | -100.0% | $654.17 |

## Aggregate

- Wallets evaluated: 21

- Total resolved BUY trades: 68203

- Wallets where the COPIER would have been net positive: 13/21

- Aggregate original-trader $ PnL (mirrored sizing): 1,963,182.71

- Aggregate copier $ PnL (mirrored sizing, 150 bps slippage): 740,761.68
