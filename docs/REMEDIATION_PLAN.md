# MeteoEdge — Remediation & Validation Plan

**Date:** 2026-07-24
**Status:** Active
**Goal:** Stop trading on a false premise, make the forecast honest, then run one decisive test that establishes whether the edge is real.

> **Revises `docs/IMPLEMENTATION_PLAN.md` (2026-05-08).** That document opens with
> *"The edge is real"*, based on a 4,867-trade backtest showing an 88.4% win rate — while
> noting it was "almost certainly overfitted". Live results since do not support the claim.
> This plan treats the edge as **unproven** and defines the test that settles it.

---

## Where we actually stand

| Measure | Value | Meaning |
|---|---|---|
| Live book, excluding one cluster | **+$0.37** over 8 fills | 96% of the headline +$10.32 came from a single day (7 repeats on one bracket, 06-29) |
| Next-day shadow, NO side | **75%** win rate, **−$1.64** over 84 settled | Breakeven at a 76¢ entry is **76%**. We are just under water |
| EMOS shadow vs legacy (Chicago CRPS) | **2.79 vs 2.74** | Shadow is currently *worse* than the baseline it should replace |
| KORD live entries on a false signal | **100%** | Every live entry fires at the 19:00-CDT UTC rollover on an exact `p_yes = 0.0` code artifact |

### The one-line diagnosis

We enter at 76¢ on brackets our model calls impossible and the market prices at ~25%.
A well-calibrated model rarely disagrees with a market that violently — that signature is a
bug, not an edge. The wins come from day-to-day temperature autocorrelation we never modelled.

### Root causes found (2026-07-23/24)

- **#810 (merged)** — `fetch_hourly_temp_now` parsed `timezone=auto` local timestamps as UTC,
  selecting a temperature hours off in the diurnal cycle. At dawn for KORD this produced
  `obs_bias_offset_f ≈ −18 °F`, added straight onto the daily-high mean, collapsing
  `forecast_mean` 79 → ~61 and yielding a spurious `P(≤67) = 0.727` against a market at ~1%.
- **#820 (open, blocking)** — the evening window prices *tomorrow's* market with *today's
  finished* observations. `expected_additional_rise` returns 0 after the peak, `max_env`
  collapses onto the finished day's high, and the envelope certainty shortcuts emit an exact
  `p_yes = 0.0`. This violates #687's own stated invariant: *"never trade a bracket using
  another day's weather observations."*
- **#799 (open)** — `σ_raw` is the constant `FORECAST_STDDEV_F = 2.0`, so the EMOS `c`/`d`
  coefficients are unidentifiable (`d ≈ 0.001` across all 30 cities). No quantity of extra
  data fixes a coefficient on a constant predictor.

### Verified *not* affected

EMOS training data is clean. EMOS trains on `model_forecast_log` plus local-day observed
highs and scores forecast distributions; it never reads `trades.p_yes_raw`. No promotion
clock needs resetting and no retraining is required on account of #820.

---

## Plan

Development is agentic and fast; the only genuine wall-clock constraint is **forward clean
data**. Dates below reflect that, and are targets — the M3 gate governs whether M4/M5 happen at all.

### M0 · Stop the bleeding — target 2026-07-28

Route the evening false-certainty entries to shadow (**decision taken: follow the data,
accept that KORD goes quiet**). Ship the interim guard requiring a bracket to clear the
**settlement day's forecast range**, not merely yesterday's realized range.

*In parallel, and starting immediately:* persist **all evaluated-bracket snapshots**, not just
flagged candidates. `scan_decisions` currently retains only the latest poll, and the
`candidates.*.csv.gz` archive is gate-selected. This starts the unbiased clean-data clock
now rather than after M2, and is the single largest compressor of the M3 date.

**Outcome:** live trading goes to ~zero by design; trustworthy data begins accumulating.
**Issues:** #820, #782 (blocked by it), snapshot persistence.

### M1 · Fast verdict — target 2026-07-31

Run the skill test retrospectively on the 37 days of archived data
(`logs/candidates.*.csv.gz`, which carries `yes_ask`/`no_ask` alongside `p_yes_raw`),
excluding `p_yes_raw = 0.0` artifact rows. No waiting required.

**Caveats:** this scores probabilities produced by the *pre-fix* model, so a negative result
condemns the old model rather than the fixed one. The archive is gate-selected, so it answers
*"on the brackets we chose, were we better than the market?"* — operationally relevant, but
not a general calibration measure.

**Outcome:** an early directional read, weeks before the formal gate.
**Issue:** #822 (pass 1).

### M2 · Make the model honest — target 2026-08-07

Switch on ensemble spread and retrain, **coupling train and serve** (decoupling them
recreates the #658 skew — the main review risk). The predictor already exists and is unused:
`sigma_f` is populated on 3,546 open-meteo and 2,717 GEFS rows while `USE_ENSEMBLE_SIGMA`
sits at `False`.

**Outcome:** `c`/`d` become identifiable, so EMOS can finally express **sharpness** — the only
lever that could legitimately reopen the 74–79¢ band.
**Issues:** #799, #798, #823, #824.

### M3 · Decision gate — target 2026-08-22

Re-run the skill test on clean, post-fix, all-bracket data. **This is the moment the thesis is
accepted or abandoned.** See the decision rule below.

**Outcome:** a written verdict.
**Issues:** #822 (pass 2), #450.

### M4 · Rebuild the entry rule — conditional, ~2026-09-05

Only if M3 passes. Replace the near-certainty gate with an EV-based rule on calibrated
probabilities, with position sizing. The current rule demands a 16–21 point disagreement with
the market, which an honest model will rarely produce.

### M5 · Staged live re-enable — conditional, from ~2026-09-12

Only if M4 holds. Per station and per side, through the existing promotion-bar machinery
(#559), on clean data, smallest size first.

---

## The decision gate (#822)

**Question:** do our bracket probabilities predict outcomes better than the market's own prices?

```
BS_model  = mean( (p_model  − outcome)² )
BS_market = mean( (p_market − outcome)² )
BSS       = 1 − (BS_model / BS_market)        ← Brier Skill Score vs the market
```

| Result | Verdict |
|---|---|
| **BSS > 0.05** | The edge is real. Proceed to M4. |
| **0 < BSS ≤ 0.05** | Marginal. Stay shadow-only; re-test after the σ work bites. Do not re-enable live. |
| **BSS ≤ 0** | **It was a dream.** The public price forecasts weather at least as well as we do. Stop the thesis — pivot the model materially or shut the live path down. |

Requires `n ≥ 300` de-duplicated settled brackets. **Note on power:** all ~11 brackets on a
station-day are determined by one daily high, so effective sample size is **station-days**
(~30/day), not bracket-rows. Rows with `p_yes_raw = 0.0` are excluded, or the model is
flattered on rows where it never forecast.

**Why this beats P&L as evidence.** P&L is a noisy proxy over a handful of fills — it is
precisely how a breakeven book came to look profitable. A proper scoring rule uses every
evaluated bracket and benchmarks against the sharpest baseline available: the market itself.
It requires no trading to compute.

The rule is fixed **before** the number is seen, so it cannot be rationalised afterwards.

---

## Open work, in dependency order

| Issue | What | Stage | Status |
|---|---|---|---|
| #820 | Evening entries price tomorrow with today's observations | M0 | **Blocking** |
| — | Persist all evaluated-bracket snapshots | M0 (parallel) | Start now |
| #822 | Market-vs-model skill test | M1 · M3 | **Decision** |
| #799 | σ unidentifiable — switch on ensemble spread, retrain | M2 | Highest leverage |
| #798 | Partial pooling instead of hard 60-sample cutover | M2 | Ready |
| #823 | Recompute promotion bars excluding artifact rows | M2 | Ready |
| #824 | Capture ECMWF ensemble spread (2,750 rows have none) | M2 | Ready |
| #782 | Anchor day partitions to settlement window | after M0 | **Hold** |
| #591 | Climb tables / entry windows — same mechanism as #820 | after M0 | **Hold** |
| #825 | Optional historical-forecast backfill (separate regime) | deferred | Not the unlock |
| #819 | Gate-column chip prefix (completes #811) | anytime | Quick win |

### Standing guardrails

- **Do not change `MIN_PRICE_CENTS` (74) or `MAX_EDGE_CENTS` (20)** while #820/#822 are open.
  Every live fill sits in the 74–78¢ band those two knobs create; moving either destroys the
  population we are trying to measure.
- **Do not implement #782 or #591 before #820.** Both move the same mechanism; starting
  either early reclassifies the evening entries as `is_next_day=1`, force-shadows them under
  #687, and zeroes the live book silently with no failing test.
- Whoever takes #619 must not regress the merged #810 `utc_offset_seconds` fix.
- **Never** use ERA5 / reanalysis as a forecast feature (see #825) — it is a lookahead leak.
  Only forecasts-as-issued are admissible predictors.

---

## What cannot be compressed

Development is fast. These are not:

1. **Forward clean data.** ~30 station-days accrue per day; days cannot be manufactured.
2. **Statistical power in station-days, not rows** — brackets within a day are near-perfectly
   correlated.
3. **Regime coverage.** Every retained day is mid-summer. A verdict from summer data is a
   verdict about summer. This limits confidence, not timing, and speed does not fix it.
4. Review / CI / deploy cycles required by repo governance.

---

## Honest base case

A 75% win rate against a 76% breakeven, and EMOS shadow trailing legacy on CRPS, are not the
fingerprints of a system beating its market. **The most likely outcome of M3 is `BSS ≤ 0`.**
This plan is built so that discovering it is cheap and fast rather than expensive and slow.

---

*Compiled 2026-07-24 from a live production extract (`meteoedge.db`, `bot.log`) and the
Polymarket Gamma API. Investigation thread: #810, #811, #819, #820, #822–#825.*
