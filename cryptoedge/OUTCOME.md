# Outcome — the pre-registered test ran once. Verdict: **NULL**

**Run date:** 2026-09-12
**Pre-registration:** `cryptoedge/PREREGISTRATION.md`, signed off 2026-09-05, unchanged
**This was the one look.** The test will not be re-run on this data.

---

## Verdict

```
PRIMARY TEST — logit P(Up) = a + b·logit(p_market) + c·logit(p_model)

  a = −0.0563
  b = −0.6662   (market)
  c = −0.0837   (model)   ← THE VERDICT
  c 95% window-bootstrap CI: [−0.5709, +0.4056]   (B=2000)
  P(c > 0) = 0.3745

  Brier market-only : 0.249680
  Brier combined    : 0.249665   (+0.000016)

  CI excludes 0         : False
  combined beats market : True

VERDICT: NULL
```

The CI straddles zero comfortably and the Brier "improvement" is 1.6e-05 — the
fifth decimal place. Per the pre-registration the EV gate is **not** evaluated,
and the thesis is closed.

## Population — exactly as pre-registered

| | |
|---|---|
| Series | BTC 5m only |
| Decision point | quote nearest `t−300s`, ±30s, one per window |
| Filters | book present and sane, `ok=1` polls, outage window excluded |
| Fit half | 929 windows (`window_end` < 2026-09-06) |
| **Evaluation half** | **1,823 windows** (bar: ≥1,000) — **POWERED** |
| Median liquidity at entry | **$13,519** (bar: ≥$5,000) — passes |
| Mean spread at entry | 0.0104 |
| Base rate P(Up) | 0.4838 |

The database was verified `integrity_check: ok` before use. Two earlier copies
were corrupt — taken with `cp` against a live SQLite file — and were discarded
rather than salvaged; `.backup` produced a clean snapshot.

---

## Why it is NULL — three findings, and none of them is "the model was unlucky"

### 1. The market has no predictive power at window open either

| | |
|---|---|
| Brier(market) | **0.250400** |
| Brier(constant 0.5) | 0.250000 |
| market directional accuracy | **49.75%** |

The price at entry is **worse than a constant 0.5**. This is the opposite of the
weather markets, where the price was near-perfectly calibrated (`b ≈ 1.04`,
`a ≈ 0.03`). Here there is nothing to encompass.

### 2. There is almost no price variation to regress against

| `p_market` at entry | n | share |
|---|---|---|
| **0.505** | 1,294 | **71.0%** |
| 0.495 | 267 | 14.6% |
| 0.485 | 66 | 3.6% |
| 0.515 | 60 | 3.3% |

`std(p_market) = 0.0112`. Seven in ten windows open at exactly bid 0.50 / ask
0.51. **This makes `b` weakly identified, and the `b = −0.6662` above should not
be read as "the market is inversely informative" — it is noise estimated off
almost no variance.** `c` is unaffected and remains the verdict.

### 3. The model has no skill on the evaluation half

| | |
|---|---|
| model directional accuracy | **49.81%** (−0.16σ, n=1,823) |
| Brier(model) | 0.252512 — **worse than a constant** |

Against **55.4%** on the year-long backtest's top decile. The model did not
degrade; it did not work at all.

---

## The explanation: 17.3% label noise

The backtest trained and scored against a **Binance-derived TWAP proxy**. These
markets settle on the **Chainlink BTC/USD 60s-TWAP stream**. Measured on the
2,752 collected windows:

| | |
|---|---|
| proxy vs actual resolution agreement | **82.67%** |
| disagreement | **477 of 2,752 windows** |
| proxy says Up | 48.66% |
| actually Up | 49.06% |

**The label the model was trained on is wrong about the real outcome 17.3% of
the time, before the model makes any error of its own.** The marginal rates
match almost exactly (48.66% vs 49.06%), so this is not bias — it is noise, and
it concentrates precisely on the near-the-line windows where any edge would
live.

A ~5pp edge measured against a label with 17pp of noise does not survive
contact with the real outcome. That is what happened.

The README flagged this risk when the collector was built ("a Binance-derived
TWAP is a *proxy* and will disagree on a minority of windows"). It was **not
quantified** until now. 17.3% is far larger than "a minority" implied.

---

## Two design weaknesses in the pre-registration, owned

Recorded because the verdict stands regardless, and because hiding them would
make the next pre-registration worse:

1. **The fit half was too small.** "Fit on the fit half" resolved to **929
   windows for 11 features**. The +6.9σ signal came from fitting on ~73,000
   windows of a year of Binance history. There was no reason to restrict
   fitting to collected windows — the features come from Binance, which has
   years of history available. This should have been specified as "fit on all
   Binance history before the split date".
2. **The encompassing framing assumed an informative price.** It is the right
   test when the market carries information (weather: `b ≈ 1.04`). Here the
   market is a near-constant 0.505 with no skill, so the regression is weakly
   identified and the interesting question collapses to the simpler one: *does
   anything beat 52.75% at a 51¢ ask?* The answer on this data is no — the
   model managed 49.81%.

Neither weakness rescues the result. The model's outright failure (49.81%,
−0.16σ) is not a power problem.

---

## What this closes, and the trap to avoid

**Closed:** this model, this label, this market. The thesis as pre-registered
is dead.

**The honest strategic read.** It is tempting to file "one more variant" —
re-train against the real Chainlink stream, or fit on the full year. The
17.3% label-noise finding genuinely motivates that. But this is exactly the
pattern the weather thesis already burned months on: M3 → M3b → M3c → the σ
lever → the forecast-stack pivot, each a narrower re-test of a hypothesis that
had already returned negative, each individually reasonable.

Before any successor is opened, the two structural facts here should be
confronted rather than routed around:

- **The event is close to unpredictable at this horizon.** The market itself
  achieves 49.75%, the model 49.81%, and the base rate is 48.38%. Nothing in
  this dataset — price or model — beats a coin.
- **The cost bar is high and fixed.** A 1¢ spread and a 1.75¢ crypto taker fee
  at 50¢ put breakeven at ~52.75%. Any successor must clear that, not 50%.

A successor is only worth opening if it can plausibly clear **52.75%**, and it
needs its own pre-registration written before any analysis — never a re-run of
this one.

## What it does not license

No live trading. #1053/#1054's halt is unconditional. `cryptoedge` places no
orders and is structurally incapable of doing so
(`tests/test_isolation.py`).

---

## Collection performance, for the record

The instrument worked, and that part is reusable:

| | |
|---|---|
| quotes collected | 1,213,800 |
| windows resolved | 8,146 |
| poll cadence | 240/hour, 15.0s, sustained |
| data integrity | 0 quotes from failed polls, 0 fabricated prices, 0 crossed books |
| one incident | 28h host network outage (2026-09-01), ~340 windows lost, fixed by #1092 |
