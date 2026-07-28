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
| **Pass-1 skill test vs. the market** (2026-07-26) | **BSS = −0.2813** over 404 brackets / 300 station-days | The pre-fix model's probabilities are **28% worse than the market's own prices**. See M1 below |

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
- **#799 (merged; premise re-verified 2026-07-28)** — `σ_raw` is the constant
  `FORECAST_STDDEV_F = 2.0`, so the EMOS `c`/`d` coefficients are unidentifiable. No quantity
  of extra data fixes a coefficient on a constant predictor.
  *Verification note:* with σ constant, `c` and `d` enter the objective only as `c + d·σ` — a
  flat ridge, so **any** `(c, d)` on that line fits identically. A fitted `d` on the `fixed`
  track is therefore optimizer position, not signal: the originally-recorded `d ≈ 0.001` and
  the later-observed median `d = 0.88` are the same fit at different points on the same ridge.
  The premise is correct; #872 closed on this. The `ensemble` track is genuinely identifiable —
  σ there varies 0.0–9.9 with 100% coverage — but see M2's status note: it is not served.

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

**Status 2026-07-26 — COMPLETE. Verdict: the pre-fix model has no edge over the market.**

| Run | n | Station-days | BSS | Reading |
|---|---|---|---|---|
| 2026-07-25 (settlements join) | 20 | — | +0.0503 | Not a result — 6.7% of required power |
| **2026-07-26 (#865, resolver)** | **404** | **300** | **−0.2813** | **No edge (BSS ≤ 0)** |

The first run died on the **outcome join**, not on missing data: Pass 1 keyed outcomes off
`settlements`, which only covers brackets we actually *traded* (~156 rows all-time), so 360
of 380 de-duplicated brackets were dropped. #865 re-pointed Pass 1 at
`resolve_bracket_outcomes` (the Gamma-first capability built for Pass 2 in
#850/#858/#860/#863), lifting n to 404 across **300 station-days** — the decision rule's
power bar, on the *same archived data*, with no waiting.

Ground truth is strong: **95.3% Gamma** (Polymarket's official on-chain resolution), 4.7%
METAR fallback — so the result is not an artefact of the proxy #644 measured as wrong ~22%
of the time.

**No pocket of skill anywhere.** All ten UTC buckets negative; `same_day` −0.2744,
`next_day` −0.2969. Least-bad bucket UTC+8 at −0.0787.

**The mechanism, from the reliability table.** 62.6% of the model's output sits at a rail —
33.4% at `p_yes` 0.00–0.02 (observed YES rate **24.4%**) and 29.2% at 0.95–1.00 (observed
**81.4%**). Meanwhile the market puts 63.9% of its mass in 0.20–0.35 and lands at 22.9%
observed against 23.5% predicted: **a 0.6pp calibration gap.** The market is nearly perfectly
calibrated on the exact population where the model emits false certainties. This is the
one-line diagnosis at the top of this document, now measured at scale.

**Known contamination, bounded.** The 2026-07-26 run flagged 5 station-days resolving YES on
more than one bracket — 1 boundary collision (#861) and 4 *disjoint* Gamma collisions (#867,
opened from this run). 10 rows of 404 = 2.5%; flipping all five spurious YES to NO in the
maximally model-favourable direction moves `BS_model` by at most 5/404 = 0.0124, taking BSS
to ≈ −0.21. **The verdict does not turn on it.** #867 is nonetheless blocking for M3, where
the sample is Gamma-resolved at ~95% and the verdict actually binds.

**What this does NOT settle: M3.** This archive predates every M0/M2 fix — #810, #820, #799,
#798, #823, #824. Per this plan's own terms, a negative Pass 1 condemns the *old* model, not
the fixed one. It does raise the prior on M3 sharply. The one genuine mechanism by which M3
could differ: this model's failure is pathological overconfidence, and the σ work
(#799/#798/#824) is precisely the lever that governs sharpness.

#### Runbook — running Pass 1 on the bot host

```bash
python -m src.scripts.bss_market_vs_model_report \
    --candidates-csv logs/candidates.csv --db data/meteoedge.db --out backtest_results
```

Writes `backtest_results/bss_market_vs_model_pass1_<date>.md`. Read-only against the
database; self-gates and writes nothing if `logs/` or the DB is absent, so it is safe to run
anywhere. Gamma resolutions are fetched once per ticker and cached permanently in
`logs/gamma_resolution_cache.v2.json` (bumped from `.json` by issue #867's wrong-market-read
fix — the old file is orphaned and no longer read) — the first run fetches ~380 tickers, later
runs are near-instant.

- `--no-network` — cache only, zero HTTP requests; uncached tickers fall back to the
  observed daily high.
- `--outcome-source settlements` — reproduces the original n=20 report.

**Check before citing the number:** the report's *Outcome ground truth* section states the
`gamma` / `metar` split and counts the impossible-outcome exposure. A result dominated by
`metar` rows deserves more scepticism — #644 measured that proxy disagreeing with the official
outcome ~22% of the time. Read the **station-day** count, not the bracket-row `n`, against
the power requirement.

> **Keep the Gamma cache warm.** This is now operational, not an optimisation. Gamma-resolved
> brackets never touch the interval logic, so they are immune to #861; METAR-resolved ones
> are not. The 2026-07-26 run was 95% Gamma and showed 5 collisions in 300 station-days. The
> 2026-07-28 run used `--no-network` against a cache holding only the older tickers, fell
> back to 88% METAR, and showed **70 collisions in 105 station-days**. Run the resolver
> **without** `--no-network` regularly so newly-logged tickers get cached while their markets
> are settling.

### M2 · Make the model honest — target 2026-08-07

Switch on ensemble spread and retrain, **coupling train and serve** (decoupling them
recreates the #658 skew — the main review risk). The predictor already exists and is unused:
`sigma_f` is populated on 3,546 open-meteo and 2,717 GEFS rows while `USE_ENSEMBLE_SIGMA`
sits at `False`.

**Outcome:** `c`/`d` become identifiable, so EMOS can finally express **sharpness** — the only
lever that could legitimately reopen the 74–79¢ band.
**Issues:** #799, #798, #823, #824.

**Status 2026-07-28 — TRAINING ONLY. The serving half was never wired.** M2 was previously
recorded here as complete. It is not, and the distinction is material to M3.

`USE_ENSEMBLE_SIGMA` is `true` in `bot_config` (set 2026-07-25) and the ensemble EMOS track
has fitted coefficients (27 rows, retrained 2026-07-28). **Neither reaches a served
probability.** Two independent blocks, either sufficient on its own:

| Layer | State | Consequence |
|---|---|---|
| Ensemble spread → `WeatherState` | `ensemble_sigma_f` has **zero assignment sites** in `src/` (AST-verified) | `resolve_sigma_raw()` always returns `FORECAST_STDDEV_F = 2.0` |
| EMOS calibrated σ → probability | **Zero `emos_primary` rows** — all 57 coefficient rows are `emos_shadow` | `emos_stddev_override` never set; `scanner.py`'s shadow branch is *"logging only; legacy probabilities are served unchanged"* |

`compute_ensemble_sigma()` — which applies `SIGMA_FLOOR_F` and the calibration regression —
has no production callers. Its own docstring says integration is tracked in **#448 (Week 3)**,
still open. Live serving is the legacy envelope at fixed σ = 2.0, exactly as pre-M2.

**Three consequences that follow directly:**

1. **#874's null result is explained.** A CRPS delta of −0.0041 "within noise" is what you
   get comparing two coefficient tracks when *neither* is served.
2. **The 2026-07-28 sharpness gain is NOT attributable to M2.** High rail 0.4% against a 9.1%
   structural ceiling, middle mass 37.0% against 0.0% pre-fix — real, but served σ is
   unchanged, so the cause is **M0 (#820)**, which was manufacturing exact-`0.0` certainties
   at scale. #798's shrinkage blend is a secondary candidate.
3. **M3 currently tests M0, not M2.** This plan's premise is that sharpness is *"the only lever
   that could carry a model from −0.28 to positive"*. **That lever has not been pulled.** A
   negative M3 verdict would not have tested the sharper model, because none is running.

A second, independent problem blocks simply switching it on: **63% of GEFS `sigma_f` values sit
below the 1.0 °F floor** (2,095 of 3,330), the classic under-dispersive signature. Feeding raw
spread into live probabilities would reproduce the overconfidence M0 just removed.

**#887 decision (2026-07-28): train-raw / serve-calibrated.** EMOS training keeps consuming
`sigma_f` exactly as persisted — raw, unfloored (`fetch_training_data`'s `sigma_source="ensemble"`
default is unchanged) — so its `(c, d)` regression learns the real spread-vs-error relationship
instead of one starved by a pre-applied floor (this is what #555 already established for capture
vs. consumption; #887 does not touch it). The open question was serving: `state.ensemble_sigma_f`
has **two** downstream consumers with different needs once #885 wires it —
`resolve_sigma_raw → apply_emos` (EMOS-served cities), which must keep receiving the SAME raw
value training saw (`apply_emos`'s own `c/d` transform IS its calibration step — flooring here
would feed the regression an input distribution it never trained on), and
`true_probability_yes`'s direct substitution (still every station's *only* serving path per #886 —
zero cities are `emos_primary`), which has no transform downstream at all. Fixed in this issue:
`true_probability_yes` now floors that direct substitution at `SIGMA_FLOOR_F` itself
(`src/model/envelope.py`), so #885 can populate `ensemble_sigma_f` with the raw quantity
everywhere and each consumer stays responsible for its own floor/calibration. Added
`scripts/check_emos_data_quality.py`'s sigma report as the sub-floor-share monitor: it now prints
`sigma_f < SIGMA_FLOOR_F` per model alongside the existing exactly-at-floor and NULL rates, so a
future capture-side regression that started flooring `sigma_f` before persisting it (undoing #555)
is visible as this share dropping toward 0%.

**Issues:** #885 (wire `ensemble_sigma_f` — the real remaining M2 work, now unblocked by #887's
decision), #886 (no city ever promoted to `emos_primary`), #887 (raw-vs-calibrated σ decision) —
✅ resolved, #888 (zero-σ guard).

### M3 · Decision gate — target 2026-08-22

Re-run the skill test on clean, post-fix, all-bracket data. **This is the moment the thesis is
accepted or abandoned.** See the decision rule below.

**Outcome:** a written verdict.
**Issues:** #822 (pass 2), #450.

#### Checks that do NOT require waiting

M3 is data-bound, but two classes of check are available *now* and can shorten it.

**Leading indicators (#869) — no outcomes required at all.** These score the prediction
side, so they run on data already accruing:

```bash
python -m src.scripts.post_fix_model_health
python -m src.scripts.post_fix_model_health --since 2026-07-25   # rows after a fix landed
```

Writes `backtest_results/post_fix_model_health_<date>.md`. Three measurements against
pre-fix baselines: rail concentration (**62.6%** of pre-fix output sat at a rail), the
exact `p_yes_raw == 0.0` artifact rate (**17.8%** pre-fix), and EMOS `d` identifiability
(`d ≈ 0.001` pre-fix). **If the rail share is still near 62.6% after the σ work, M3 is a
foregone conclusion and the remaining spend should stop** — sharpness is the only lever
that could carry a model from −0.28 to positive.

**Ground-truth quality (#870)** — folded into the resolver dry run, so a warm Gamma cache
makes it free:

```bash
python -m src.scripts.resolve_bracket_outcomes --no-network
```

Reports the Gamma-vs-METAR disagreement rate on the full evaluated population, zero-YES
station-days split by whether the observed high fell in a real bracket gap, and the
brackets-per-station-day distribution. M3 will be ~95% Gamma-scored; this is how that
truth's error rate stops being unknown.

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

*Status as of 2026-07-25. M0 and M2 are complete, both ahead of target — the plan is now
purely data-bound: the only thing between here and M3 is station-days accruing.*

| Issue | What | Stage | Status |
|---|---|---|---|
| #820 | Evening entries price tomorrow with today's observations | M0 | ✅ Merged |
| #826 | Persist all evaluated-bracket snapshots | M0 (parallel) | ✅ Merged — clean-data clock started 2026-07-24 |
| #865 | Re-point Pass 1 at `resolve_bracket_outcomes` (n=20 → 404) | M1 | ✅ Merged — Pass 1 complete |
| #867 | Gamma resolves DISJOINT brackets as YES on one station-day | before M3 | ✅ Merged (#875) — root cause was direction-blindness, not wrong-market reads; 4 → 0 collisions |
| #869 | Post-fix model health — rail concentration, artifact rate, σ identifiability | **now, no waiting** | Shipped — run it |
| #870 | Ground-truth quality — Gamma-vs-METAR rate, zero-YES days, ladder completeness | **now, no waiting** | Shipped — run it |
| #822 | Market-vs-model skill test | M1 · M3 | Pass 1 done (**BSS −0.28**); **Pass 2 = the decision** |
| #799 | σ unidentifiable — switch on ensemble spread, retrain | M2 | ✅ Merged |
| #798 | Partial pooling instead of hard 60-sample cutover | M2 | ✅ Merged |
| #823 | Recompute promotion bars excluding artifact rows | M2 | ✅ Merged |
| #824 | Capture ECMWF ensemble spread (2,750 rows have none) | M2 | ✅ Merged |
| #861 | Bracket-boundary convention (**adjacent** brackets both YES) | before M3 | ✅ Merged (#881) — `[lo, hi)` matches Gamma on 96.9% of 2,619 settlements (vs 48.8% inclusive); 70 → 0 collisions |
| #871 | `bracket_evals.emos_mode` overwritten with `next_day` on half the rows | **now** | ✅ Merged (#880) — verified live. **10,503 pre-fix rows keep the corrupted value permanently** |
| #872 | M2 premise check — constant-σ rows show median `d` = 0.88, not 0.001 | before citing #799 | ✅ Resolved — premise CORRECT. On a constant σ, `c`/`d` lie on a flat ridge (`c + d·σ`), so a fitted `d` is optimizer position, not evidence. #869's check 3 measured nothing |
| #450 | Calibration backtest: reliability + CRPS over 30 days | M3 | ✅ Merged (#874) — HOLD ensemble sigma, CRPS delta −0.0041 (within noise; see #885 for why) |
| **#885** | **`ensemble_sigma_f` never populated — `USE_ENSEMBLE_SIGMA` is a no-op at serving** | **M2 (real remaining work)** | **Open — M2 BLOCKER** |
| #886 | No city ever promoted to `emos_primary` — EMOS has never served a probability | M2 | Open — investigation |
| #887 | 63% of GEFS σ below the 1 °F floor; raw-vs-calibrated decision | before #885 | ✅ Decided — train-raw/serve-calibrated; `true_probability_yes` now floors its direct-substitution consumption at `SIGMA_FLOOR_F`, sub-floor-share monitor added |
| #888 | `p_normal_between()` ZeroDivisionError on σ=0 | before #885 | Open — Simple, cheap now |
| #844 | Test suite flakes in the 15 min before UTC midnight | anytime | ✅ Merged (#882) |
| #876 | Carry `direction` in candidates CSV / bracket_evals | supports #867 | ✅ Merged (#884) |
| #877 | Windows `read_text()` encoding crash | anytime | ✅ Merged (#883) |
| #782 | Anchor day partitions to settlement window | after M0 | Unblocked — #820 is merged |
| #591 | Climb tables / entry windows — same mechanism as #820 | after M0 | Unblocked — #820 is merged |
| #825 | Optional historical-forecast backfill (separate regime) | deferred | Not the unlock |
| #819 | Gate-column chip prefix (completes #811) | anytime | ✅ Merged |

### Standing guardrails

- **Do not change `MIN_PRICE_CENTS` (74) or `MAX_EDGE_CENTS` (20)** while #822 is open
  (still binding — #820 merged 2026-07-25, #822 did not). Every live fill sits in the 74–78¢
  band those two knobs create; moving either destroys the population we are trying to measure.
- **Do not implement #782 or #591 before #820.** *Satisfied as of 2026-07-25 — #820 is
  merged, so both are now unblocked.* Kept for the record: starting either early would have
  reclassified the evening entries as `is_next_day=1`, force-shadowed them under #687, and
  zeroed the live book silently with no failing test.
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

**Updated 2026-07-26.** Pass 1 came in at **BSS = −0.2813** on 300 station-days — full power,
95% official ground truth, negative in every segment. That is the pre-fix model, so it does
not pre-empt M3; but the base case above should now be read as the *strong* case, not the
cautious one. M3 has to travel from −0.28 to positive, and the only lever that could carry it
is the σ work's effect on sharpness. Plan accordingly: the cost of reaching M3 is already
sunk and small, but M4/M5 should be treated as unlikely to be reached.

**Updated 2026-07-28 — the sharpness lever has not actually been pulled.** See M2's status
note: `ensemble_sigma_f` is never populated and no city has ever been promoted to
`emos_primary`, so served σ is still the fixed 2.0 and M2 delivered training infrastructure
only. The measured sharpness gain (high rail at 4% of its structural ceiling; middle mass
37% against a pre-fix 0.0%) is real but attributable to **M0**, not M2.

This does not make M3 pointless — the model genuinely is better behaved, and a gate on the
post-#820 model is worth running. It does mean **M3 as currently configured measures M0's
effect, and a negative verdict would leave the plan's own stated lever untested.** The
sequencing decision is therefore explicit and belongs to the Tech Lead PM:

- **Run M3 on the M0-fixed model as-is** (~2026-08-05 at current accrual). Cheapest, and a
  clean read on what is actually deployed. If it fails, the σ lever is still untried, so the
  verdict condemns the current serving stack rather than the thesis.
- **Or wire #885/#886/#887/#888 first, then start a fresh collection window.** Tests the model
  the plan intended, but resets the clean-data clock — switching serving σ mid-collection
  would split the sample and invalidate the accrued station-days.

Doing both in sequence is the only way to attribute a result to the σ work at all.

---

*Compiled 2026-07-24 from a live production extract (`meteoedge.db`, `bot.log`) and the
Polymarket Gamma API. Investigation thread: #810, #811, #819, #820, #822–#825.*
