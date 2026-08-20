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
| **Pass-1 skill test vs. the market** (2026-07-29 re-run, clean) | **BSS = −0.2752** over 442 brackets / 332 station-days — ✅ **direction-clean** (43 low markets all resolved against observed daily LOW per #875/#902) | The pre-fix model's probabilities are materially worse than the market's own prices. 96.8% Gamma-resolved, 0 impossible outcomes. See M1 below |

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
  The premise is correct; #872 closed on this. The `ensemble` track is identifiable *in the raw
  `gefs` column* (σ varies 0.0–9.9 at 100% coverage) — but per #893 that column is filtered out
  of training by the baseline regime, and 5 US stations train on a constant instead. See M2's
  status note.

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

**Second contamination — direction — RESOLVED (2026-07-29).** Pass 1 was re-run with the
direction-aware resolver (#875), which correctly dispatches LOW-direction markets against
the observed daily LOW (the fix was already in place from #867; #902 verified it via code
audit and added the missing test coverage, merged as #903). The re-run found **43 low
markets** in the archive, all correctly classified and resolved — 0 unknown directions,
0 impossible outcomes. BSS moved from −0.2813 to **−0.2752**; the small delta confirms the
contamination was not the dominant issue, and the verdict does not turn on it.

**The mechanism above is substantially a BUG, not a model property (found 2026-07-31).** The
reliability reading — 62.6% of output at a rail, false certainty against a well-calibrated
market — is the fingerprint of two probability-mass defects, not of a badly-tuned forecast.
Per the #917 diagnosis, the half-width parser is *"very likely the mechanism behind the recorded
calibration finding that `raw_p_yes` under-predicts YES by 2–3× in the 0.02–0.20 band — i.e.
precisely the NO entry zone — and behind the 62.4% pile-up at the 0.05 clamp floor."* #920
(truncation without renormalisation) pushes the same direction, hardest on °C ladders.

Two consequences, and they differ:

- **The verdict stands.** The old model had no edge over the market. Nothing here reverses that.
- **The explanation does not.** "The model is pathologically overconfident" should read "the
  ladder lost much of its probability mass before it reached the comparison." The distinction
  matters because the plan's stated M3 lever is σ/sharpness — and sharpness was never the binding
  problem on these rows.

**Pass 1 cannot be repaired by re-running it.** The report re-resolves *outcomes*; it does not
recompute `p_yes_raw`, which is frozen in the archive as the buggy scanner wrote it. M1 is
therefore retired as a statement about the forecast model, not re-run.

**What this does NOT settle: M3.** This archive predates every M0/M2 fix — #810, #820, #799,
#798, #823, #824. Per this plan's own terms, a negative Pass 1 condemns the *old* model, not
the fixed one. It does raise the prior on M3 sharply. The mechanism by which M3 could differ was
originally stated as sharpness (the σ work, #799/#798/#824). After the paragraph above, the
larger part of that mechanism is simply that the probabilities M3 scores are computed correctly
and M1's were not.

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

**And the training half is compromised too (#893, triaged 2026-07-28).** `fetch_training_data`
filters rows to the active regime *before* selecting σ, and `FORECAST_STACK = baseline` resolves
to `frozenset({"nws", "open_meteo"})` — **`gefs` is not in it**. σ is then whichever model sorts
first with a non-NULL value, an artifact of `ORDER BY date ASC` plus alphabetical model names.
`nws` sorts before `open_meteo` and has 0% NULL σ, so for all **5 US stations** (KATL, KHOU,
KLAX, KMIA, KORD) the training σ is `nws`'s per-lead climatological **constant**, on 100% of
dates. `c` and `d` collapse onto the flat `c + d·σ` ridge and **`d` is unidentifiable** — the
#799 condition the ensemble track existed to escape. The 25 non-US stations get `open_meteo`'s
varying cross-model stdev and are fine.

So the "ensemble" σ track never sees GEFS ensemble spread at all under the baseline stack —
a third independent reason #874's CRPS delta came out within noise.

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

#### #886 investigation (2026-07-28) — verdict: **bar unmet, not a defect, not a general shadow regression**

Of the issue's three candidate explanations, the evidence supports the first and rules out the
other two:

1. **The promotion bar has genuinely never been met — CONFIRMED, this is the cause.**
   `emos_mode.py::_primary_allowed` requires `db.get_emos_crps_count(city) >=
   EMOS_MIN_SAMPLES_PROMOTION` (default **60**, `src/config.py`). `model_forecast_log` was reset
   2026-06-25; issue #556's own documented timeline puts the *shadow* threshold (35 samples) at
   "late July (July 28–31)" and promotion (60 samples) at ~August 23 **for the original clock**.
   The 2026-07-28 `ensemble_sigma_calibration` backtest confirms this empirically: max **28**
   triples for any city, and its own text states *"No station has >= 60 triples yet."*
   `emos_crps_log`'s per-track counts (the exact quantity `_primary_allowed` reads) confirm it a
   second way: the `fixed` track has ~15 samples/city (453 rows / 30 cities, 2026-07-09 to
   2026-07-25); the `sigma_source` that is **actually active** (`ensemble`, since
   `USE_ENSEMBLE_SIGMA` flipped true 2026-07-25) has only **~3 samples/city** (81 rows / 27
   cities, 2026-07-25 to 2026-07-27) — `get_emos_crps_count`/`emos_crps_logged_for_date` key on
   `(city, model_mode, forecast_source, sigma_source)` by design (#759, #851), so that flip
   correctly started a fresh clock for the served track rather than pooling incompatible
   evidence, but the side effect is that the currently-active track is further from 60 than the
   dormant `fixed` track was. `docs/OPERATIONS.md`'s own forecast-stack-expansion note already
   projects **baseline promotion at ~mid-September**, consistent with this reading.

2. **"Blocked by a defect" — NOT SUPPORTED.** `get_emos_crps_count`, `_primary_allowed`, and
   `get_city_mode` were read end-to-end; the per-track isolation, the sample-count comparison,
   and the operator-override precedence all match their docstrings and are covered by
   `test_emos_shadow_scaffolding.py` (which explicitly asserts, by design,
   `ready_for_promotion` can never be set to 1 by any automated path). Separately —
   independent of the sample bar — promotion also requires an explicit two-step **manual**
   operator action (`POST /api/emos/{city}/mark-ready` then `POST /api/emos/{city}/promote}`,
   `src/dashboard/api.py`); `save_coefficients()` never sets `ready_for_promotion=1` itself,
   by design (issue #556). No evidence either step has ever been invoked for any city. This is
   a deliberate safety gate, not a bug — but it means the sample bar alone is not the only thing
   standing between here and a first promotion; a human action is also required once it clears.

3. **"Shadow genuinely worse than legacy" — NOT the general explanation.** The production
   `emos_crps_log` cross-check shows `emos_shadow` beating `legacy` on mean CRPS by a wide
   margin in aggregate, on both currently-populated tracks: `fixed` 1.4896 vs 2.0768 (30
   cities), `ensemble` 1.2982 vs 2.0267 (27 cities). Issue #762's per-city triage (2026-07-21)
   found EMOS beat legacy in **23 of 30 cities**; the four regressions (Jinan, Shenzhen, Tel
   Aviv, Manila) each have a documented, city-specific cause and are already held out via
   `emos_mode_override` (follow-ups #765/#766/#767). This plan's earlier "Chicago 2.79 vs 2.74"
   line (see "Where we actually stand" above) is a real but isolated data point, not
   representative of the cohort — it predates the #798 partial-pooling shrinkage blend and
   should be re-measured before being cited as evidence of a general regression.

**Conclusion:** #886 is not a blocker to fix — it is the expected, by-design state at day 33
of a reset-then-re-reset 60-day clock, compounded by a promotion step that additionally requires
manual operator action. No code change is proposed. Re-check once the active (`ensemble`)
track's per-city CRPS counts approach 60 (~mid-September at current accrual, per #556/OPERATIONS)
and, at that point, an operator must still explicitly mark-ready + promote per city — this will
not happen automatically. **No city was promoted to `emos_primary` as part of this
investigation**, consistent with not disturbing the M3 clean-data collection window.

### M3 · Decision gate — clock restarted **2026-08-06**, gate ~2026-08-24

Re-run the skill test on clean, post-fix, all-bracket data. **This is the moment the thesis is
accepted or abandoned.** See the decision rule below.

**Outcome:** a written verdict.
**Issues:** #822 (pass 2), #450.

> ## The first M3 window was void, and the second one is now running
>
> **The 2026-07-24 window collected contaminated data for thirteen days.** Two defects in how
> bracket probabilities are computed — both feeding `p_yes_raw`, the exact quantity the gate
> scores — were live for part or all of it. Neither was caught by a test, because **nothing in
> the stack asserted the one invariant they both violated**: a gap-free bracket ladder must sum
> to ~1.0. #917 added mass conservation as a *synthetic-ladder* test; nothing checked it against
> production. That is how two independent defects survived for weeks in the gate's own input.
>
> Both are now fixed, deployed, and **verified in the production data rather than by inference**:
>
> | Defect | What | Merged | Verified live |
> |---|---|---|---|
> | **#917** | °F dash-range brackets integrated at half width | 2026-07-31 | 2026-08-01 — °F ladder mass **0.53 → 0.99**, gap rate **100% → 0%** |
> | **#920** | Brackets outside the surviving interval zeroed without renormalising the remainder | 2026-08-05 (#934) | 2026-08-06 — every station, both ladder kinds, every cut level reads **1.00** |
> | #921 | Mild two-sided excess (one poll summed 1.156) | — | Measured at **1.04** on untruncated °C ladders. Real but immaterial; **stood down, not a gate precondition** |
>
> ### What #920 actually was — the filed scope was the cheap half
>
> Measured 2026-08-05 over 5,829 ladders. Splitting by *which end* the ladder was truncated at:
>
> | kind | cut | ladders | mean SUM |
> |---|---|---|---|
> | °C | none | 224 | **1.04** |
> | °C | bottom only | 1292 | 0.97 |
> | °C | **top only** | 1273 | **0.85** |
> | °C | both | 1934 | **0.80** |
> | °F post-#917 | any | 497 | 0.97–1.01 |
>
> **Untruncated ladders conserve mass** — so the forecast was never the problem; the whole deficit
> was truncation without renormalisation. The asymmetry is the finding, and it is physical: the
> bottom cut costs ~3%, the top cut ~15%. Brackets below an already-observed high carry ~0
> probability anyway, because a running maximum cannot decrease. Brackets *above* `max_env` carry
> real mass, because the day can still warm.
>
> #920 was filed against the bottom cut. The expensive half was the top one, and measuring only
> leading zeros is what hid it — a mistake this document records because the same measurement
> error would hide the next one. The fix replaced all three shortcuts with a single conditional,
> `P(bracket │ high ∈ [current_high, max_env])`, which renormalises by construction.
>
> Likely why °F looked immune: **poll timing against the diurnal peak.** US stations poll at
> 13:00 UTC ≈ 08:00 local — pre-peak, `max_env` well above the ladder. Asian °C stations poll at
> 11:42 UTC = 20:42 KST — post-peak, `expected_additional_rise → 0`, `max_env` collapses onto
> `current_high`, and both cuts fire at once. That predicts the observed pattern exactly and would
> make severity a function of timezone offset.
>
> ### Consequences for the gate
>
> - **The clock start is `2026-08-06`**, a constant in `daily_health_report.py`
>   (`M3_CLEAN_DATA_CLOCK_START`), not a literal buried in a query. It has moved twice; assume it
>   can move again.
> - **Every station-day before that date is void for M3.** Leaving the old date in place is how
>   the health report came to print `362/300 (121%)` on 2026-08-04 against a window that was
>   entirely contaminated (#932).
> - **Both the gate and the health report must be run with `--since 2026-08-06`.** The BSS report
>   filters on **poll time**, not settlement date (#941): contamination is a property of when the
>   probability was computed, not of when the market resolved.
> - **The timeline. Corrected 2026-08-10 — the earlier ~55/day was wrong.** See
>   "How the 300 bar is counted" below. Measured on live data: **81 resolved** scoreable
>   station-days in five days (~16/day) → the bar lands **~2026-08-24**.

**Status 2026-08-06 — tooling READY, clean window collecting.**

| Prerequisite | State |
|---|---|
| Pass 2 tool | ✅ Built and run end-to-end — `bss_market_vs_model_report --population all-bracket` |
| Ground truth | ✅ Clean — 0 boundary + 0 disjoint collisions, Gamma-vs-METAR disagreement **1.7%** (was 8.8% pre-#881), ladder completeness 11/11 |
| Direction contamination | ✅ Not applicable — `bracket_evals` starts 2026-07-24, a week after the 2026-07-17 LOW-market rollback (#733/#734), so no low market can be in this population. The report now date-checks this itself |
| Probability-mass integrity | ✅ Ladders sum to **1.00** across all stations and both ladder kinds from 2026-08-06. Re-checked daily — see the diagnostic runbook below |
| Station-days | ⏳ **81 resolved / 300** on 2026-08-10 at ~16/day → gate **~2026-08-24**. See the counting note below — three earlier estimates (55/day, 29/day, 58.8/day) were all measurement errors, not accrual changes |

#### How the 300 bar is counted — and four ways it was counted wrong

**The bar is distinct `(station, settlement_date)` pairs that survive the gate's funnel, after
outcome resolution.** That is `bss_market_vs_model_report.py`'s own definition:

```python
counts["n_station_days"] = len({(r["station"], r["settlement_date"]) for r in out})
```

Everything about it is load-bearing, and each clause was got wrong at least once:

| Clause | The error | What it printed | Fixed |
|---|---|---|---|
| From `bracket_evals` | Read `scan_decisions`, an upsert table | — | #956 |
| Distinct pairs over the **window** | Counted per **poll day** and summed. A settlement date is polled as next-day *and* as same-day, so summing double-counts it | 113 vs a true 85 | #965 / #968 |
| **De-duplicate, then exclude** | Excluded first, keeping any bracket-day ever contested at *any* poll. The gate judges a bracket-day on the model's **final** word | 173 vs a true 111 | #965 |
| After **outcome resolution** | Counted candidates, not resolved | 111 vs a true 81 | labelled as an upper bound (#965) |
| Marginal, not average | `total / elapsed` — inflated forever by the opening day, which opens two settlement dates at once | 42/day, then 38/day, vs a true ~28 | #958 |

**Measured 2026-08-10, five days into the window:**

| Basis | Count | Rate | Bar |
|---|---|---|---|
| Exclude, no de-dup (wrong) | 173 | ~35/day | ~Aug 14 |
| De-dup → exclude (candidates) | 111 | ~22/day | ~Aug 17 |
| **…→ resolved (what the gate scores)** | **81** | **~16/day** | **~Aug 24** |

**Plan against ~Aug 24.** 111 is a pre-resolution upper bound; the gap closes as Gamma resolves
recent settlement dates, so the true date sits between, but the optimistic figure is the one that
gets a gate run underpowered. Three tools now share one implementation
(`daily_health_report._scoreable_pairs`) precisely because four separate implementations is how
this went wrong four times.

##### Confirmed 2026-08-18 — ~Aug 24 holds, and the resolution gap is a lag, not a haircut

The 2026-08-10 figures above were taken **five days** into the window, when most of it was too
recent for Gamma to have resolved. That made the pre-resolution-vs-resolved gap look like a
proportional haircut, and the `~25-30% fewer` caveat the health report still prints was
calibrated on it (81/111 = a 27% shortfall). **That reading was wrong, and it was wrong in the
pessimistic direction** — it briefly supported a projected gate slip to ~Aug 30.

Measured twelve days in, from a real Pass-2 invocation:

```
[bss] resolved 658/815 de-duplicated brackets (613 gamma, 45 metar) across 205 station-days
[bss] PASS 2 / M3 GATE -- n=658 bracket-rows, 205 station-days (need 300) | UNDERPOWERED -- not a verdict
```

| Basis | 2026-08-10 | 2026-08-18 | Rate over the 8 days |
|---|---|---|---|
| De-dup → exclude (pre-resolution) | 111 | 233 | ~15/day |
| **…→ resolved (what the gate scores)** | **81** | **205** | **~15.5/day** |
| Gap | 30 (27%) | 28 (12%) | **flat in absolute terms** |

**The gap is a constant ~28-station-day lag buffer, not a percentage.** It is roughly two days of
accrual at ~16/day — recent settlement dates awaiting Gamma — and it does not grow with the
window. So resolved station-days accrue at the same steady-state rate as pre-resolution ones,
offset by ~2 days, and the shortfall *percentage* shrinks as the window lengthens (27% → 12%).

`(300 − 205) / 15.5 ≈ 6.1 days` → **the gate lands ~2026-08-24**, which is what this section has
planned against since 2026-08-10. The resolved rate is now measured across two points eight days
apart, not inferred from one.

**Consequence for the health report.** Its `~25-30% fewer` footnote is a stale constant — the
measured figure today is 12% — and its `power bar ~<today> + Nd` line computes `N` against the
*pre-resolution* count while `300` is the *resolved* bar, so it compares two populations. The
error is real but currently costs ~1-2 days, not the week an earlier draft of this note claimed.
Tracked in **#1018**, which also covers the missing `--since` on `resolve_bracket_outcomes` —
the reason the resolved count could not be read without also computing a BSS.

**Also clean at this checkpoint:** 613/658 Gamma (93%, close to the ~95% anticipated), 45 METAR,
and **0** impossible-outcome station-days (#861/#867) across all 205.

**First real Pass-2 run, 2026-07-29 — UNDERPOWERED, NOT A VERDICT.** 808 de-duplicated brackets
across **139 station-days** (46% of the bar); BS_model 0.1358 vs BS_market 0.0763. The report
correctly refused to print a verdict section. Recorded here as a **dry run of the gate, not a
reading of it** — the number is not a result and must not be argued from in either direction.

Two things it did establish, which are not power-dependent:

- **The exclusion funnel is the thing to watch.** Of 27,344 input rows, 10,417 (38.1%) are
  dropped as `p_yes_raw == 0.0` #820 artifacts and 7,180 (26.3%) as 1¢/99¢ rail — **64.4%
  excluded before de-duplication**. Whether the artifact exclusion is still right *after* the
  #820 fix is an open question that must be settled **before** the powered run, not after seeing
  its number. See open work below.
- **Ground truth held up at scale**: 80.6% Gamma / 19.4% METAR, **0** impossible-outcome
  collisions across 139 station-days.

#### Runbook — check the window still conserves mass

```bash
bash scripts/run_m3_diagnostics.sh          # defaults to the live clock window
bash scripts/run_m3_diagnostics.sh --since 2026-07-24   # re-read the void window
```

Read-only against `logs/bracket_evals.*.jsonl` — the gate's own population, **not**
`scan_decisions`, which upserts on `(station, ticker, date)` and so assembles "ladders" from
brackets observed at different times. The wrapper fast-forwards master, runs the diagnostic,
publishes the output to a report branch so it can be read from a phone, and leaves the host back
on master. It reports:

1. **Which parser wrote each day.** Bracket width is the fingerprint: 1.0 °F = pre-#917
   dash-range, 2.0 °F = fixed, 1.8 °F = °C via `_LABEL_EXACT`, never affected. Confirms a deploy
   in the data rather than by trusting a systemd timestamp.
2. **Mass conservation, split by censoring and by which end was cut.** Splitting is the point: an
   uncensored deficit isolates a parser bug, a censored-only deficit isolates a renormalisation
   bug, and a `top`/`both` deficit against a healthy `none` column is what #920 looked like.
3. **The real accrual rate**, in *scoreable* station-days — after the gate's own exclusions, which
   is what the 300 bar counts and what the health report's raw count overstates (#932).

**This is now a standing regression check, not a one-off triage.** Mass conservation is the
invariant whose absence let #917 and #920 both live for weeks. Run it before the gate; treat any
day that fails to conserve as a regression and stop the clock.

#### Before the gate — settle the certainty exclusions

The gate's exclusion funnel drops **64.4%** of input rows across two exclusions, and whether
the first is still justified moves the verdict a long way in either direction. It must be
decided **blind**, before the powered run:

```bash
python -m src.scripts.certainty_exclusion_check --population all-bracket --since 2026-08-06
```

> **The 2026-07-30 result is STALE — re-run it on the post-2026-08-06 window.** The exact-zero
> rows it classified were largely produced by #920's un-renormalised truncation, and that code
> path no longer exists. The population it measured is not the population the gate will see.
> Re-run and **record the verdict before the gate**, not after seeing its number — that ordering
> is the whole point of the pre-registration.

Writes `backtest_results/certainty_exclusion_check_<date>.md`. **It computes no BSS** — the
Brier machinery is not even imported, and a test asserts that — so running it before the gate
cannot spoil the pre-registration.

It reports both certainty classes, never one alone. `p_yes_raw == 0.0` removes rows the
**model** is certain about; the 1¢/99¢ rail removes rows the **market** is certain about.
Dropping one while keeping the other would restore one side's easy wins and not the other's,
which reads as skill and is not. **Any change applies to both classes or to neither.**

The rule it applies, pre-registered in the module:

| Condition | Verdict |
|---|---|
| 95% interval straddles the 2% bar | **Underpowered — not a decision.** Keep the exclusion, re-run when the class is larger |
| Model-certain rows resolve YES at **> 2%** | **The certainty is false.** Keep the exclusion and file it — something still emits false certainty post-#820 |
| **≤ 2%**, but the zeros are still bit-exact | **Honest, still artifact-shaped.** Keep it; provenance is a code question |
| **≤ 2%** and no bit-exact zeros remain | **A genuine opinion.** Drop it — together with the market-certain class |

Two halves have to agree before an exclusion is dropped: *calibration* (are the certain rows
right?) and *provenance* (is an exact 0.0 computed, or asserted by the shortcut?). The tool
answers the first and reports a signal on the second — bit-exact zeros vs. tiny-but-computed
probabilities — while stating plainly that no outcome rate can close it. **An artifact that
happens to be right is still an artifact.**

#### DECIDED 2026-08-10 — KEEP both exclusions

Run blind, before the gate, on the clean window (`--since 2026-08-06`, added in #963 — without
it the check reads the void window, where the exact zeros were manufactured by #917/#920 and the
answer is predetermined).

| Class | rows | resolved | station-days | observed YES | 95% CI (Wilson) | mean market P(YES) |
|---|---|---|---|---|---|---|
| Model-certain (`p_yes_raw == 0.0`) | 1105 | 1049 | 139 | **1.0%** | **0.5% – 1.7%** | 2.0% |
| Market-certain (1¢/99¢ rail) | 1447 | 1265 | 144 | 5.1% | 4.1% – 6.5% | 5.7% |
| Contested (what the gate scores) | 410 | 263 | **81** | 27.4% | 22.3% – 33.1% | 28.4% |

**Verdict: rule row 3 — "honest but still artifact-shaped."** The interval's upper bound (1.7%)
sits clear of the 2% bar, so this is a decision and not the underpowered branch. Calibration
passes: model-certain rows resolve YES at 1.0%. Provenance fails: **all 1105 are bit-exact
`0.0`**, which is the shortcut's signature rather than a computed tail. Per the symmetry
constraint both exclusions stay, so the gate's funnel is unchanged.

> **Observed, and deliberately not acted on.** Post-#920 those zeros have a different source:
> `conditional_bracket_probability` returns exactly 0.0 when a bracket falls entirely outside
> `[current_high, max_env]` — a structurally correct clip, not an un-renormalised shortcut. There
> is an argument that makes them a genuine opinion. **Re-reading the provenance branch after
> seeing the result is what pre-registration exists to prevent**, so the rule stands as written
> and this is a question for a *future* gate.

Full report: `backtest_results/certainty_exclusion_check_2026-08-10.md`.

#### Runbook — running Pass 2 (the gate) on the bot host

```bash
.venv/bin/python -m src.scripts.bss_market_vs_model_report --population all-bracket --since 2026-08-06
```

Writes `backtest_results/bss_market_vs_model_pass2_<date>.md` — a distinct filename from
Pass 1's, so the two never overwrite each other. Read-only against the database; self-gates and
writes nothing without real data.

**Checking progress without reading the score.** The run logs the power line to stderr *before*
the BSS number appears anywhere on screen:

```
[bss] PASS 2 / M3 GATE -- wrote <path> | n=NNN bracket-rows, NNN station-days (need 300) | UNDERPOWERED -- not a verdict
```

That line carries the station-day count and the POWERED/UNDERPOWERED flag and **no BSS value**,
so a pre-gate progress check can read it off the terminal without opening the report. Same for
grepping the file — `grep -i "station-day"` returns the effective-sample-size row and the power
note, neither of which contains the score.

> **The score still lands on disk either way.** Reading only stderr keeps it out of your head,
> not out of the file. That matters because the decision rule is pre-registered: looking at an
> underpowered BSS and then looking again at 300 is two looks, and it inflates the false-positive
> rate whatever the first look concluded. Until `resolve_bracket_outcomes` grows a `--since`
> (#1018), progress checks and the gate share one instrument, and the discipline has to come
> from the operator rather than the tool.

**On the day, run it once and record both lines.** The population, the power treatment and the
dawn-cohort sensitivity read are pre-registered — see "Pre-registration — signed off
2026-08-20" under the decision gate. The headline verdict is the **inclusive** run:

```bash
# verdict (pre-registered population, includes the 12 dawn station-days)
.venv/bin/python -m src.scripts.bss_market_vs_model_report --population all-bracket --since 2026-08-06
```

The exclusive figure is recorded alongside it as a sensitivity check and never substituted for
the verdict. Confirm `station-days >= 300` on the stderr power line — the **resolved** count,
not the health report's pre-resolution one.

> **`--since 2026-08-06` is not optional for the gate.** `bracket_evals` spans three
> incompatible probability eras — pre-#917 °F ladders integrated at half width, pre-#920 ladders
> leaking up to 20% of their mass to truncation, and clean rows from 2026-08-06. Without the
> cutoff the gate scores all three together: on 2026-08-11 that is **~72% pre-#920 rows**, which
> would undo the entire fix at the last step while looking like a normal run.
>
> The filter is on **poll time, not settlement date** — contamination is a property of when the
> probability was computed, not when the market resolved. A run without `--since` prints a
> warning banner saying it is not a legitimate gate run; that banner is the only thing standing
> between a mis-invocation and a wrong verdict, so do not remove it.

Both passes share one module. Everything about **how** a bracket is scored is identical — the
BSS math, the row exclusions, the de-duplication rule, outcome resolution, the market-price
convention. Only **which** brackets are in scope differs. That is deliberate: the gate must not
be able to drift from the read that preceded it.

**The report refuses to call an underpowered run a verdict.** Below 300 station-days it prints
`UNDERPOWERED — THIS IS NOT A VERDICT` and omits the verdict section entirely, in either
direction. That guard exists because the n=20 Pass-1 run was briefly read as "edge appears real"
at 6.7% of the required sample.

**Pass 1 and Pass 2 are not comparable.** Pass 1 was gate-selected and answered *"on the brackets
we chose to trade, were we better than the market?"*; Pass 2 scores every evaluated bracket and
answers the general calibration question. A different number is expected from population alone —
recorded here **before** the number exists.

#### What M3 will and will not have tested

Per M2's status note, the σ lever was never pulled: `ensemble_sigma_f` is unpopulated (#885), no
city is promoted (#886), and the training σ is a constant for 5 US stations (#893). **M3 as
configured measures M0's effect, not M2's.**

That does not make it a formality. M0 produced a materially different model — Pass 1's had
*literally zero* probability mass between 0.05 and 0.95; the current one has **37.0%**, with the
high rail at **4% of its structural ceiling**. Testing it is a real experiment.

But if M3 comes back negative, the plan's own stated lever remains untested, and that is a
**second** experiment rather than a refutation of the thesis. Sequential beats confounded: fixing
#885/#893 first would reset the clean-data clock and push the gate into September for no gain.

> **This was argued both ways on 2026-08-04/05. The conclusion above is the one that stands.**
> Recorded because the reasoning is worth keeping, not just the answer.
>
> When #920 was open, the sequential argument looked dead: its whole force was "don't reset the
> clock," and #920 reset it regardless, so bundling #885/#893 into the same window appeared free.
> That was **reversed once accrual was actually measured.** At the accrual rate believed at the
> time (~55/day) the second window costs about **five days**, not the ~12 assumed when the trade was proposed — and
> five days is a cheap price for being able to attribute a negative M3 to M0 or to σ rather than
> to "one of these two things."
>
> **Hold #885/#893 until after M3.** The trade only made sense while the wait was long.
>
> **Re-opened by the 2026-08-10 correction, and worth stating plainly.** That argument's whole
> arithmetic was "a second window costs ~5 days, cheap for attribution." The measured rate is
> ~16 resolved station-days/day, so a second window costs **~19 days**, not five — the premise
> moved by roughly 4×. The conclusion is *not* automatically unchanged:
>
> - **Still hold** if attribution is worth ~19 days. A negative M3 that cannot separate M0 from σ
>   is a result nobody can act on, and this plan has already paid for confounded evidence once.
> - **Bundle** if it is not. The σ lever is the plan's own stated mechanism and remains untested
>   either way.
>
> Held for now on the first reading, but this is now a real trade rather than an obvious one, and
> it should be re-decided explicitly if the gate slips further.

#### What may land before the gate, and what may not

**The test: does the change alter what goes into `bracket_evals`?** If yes, landing it mid-window
splits the sample and the window is no longer homogeneous — which is exactly what #917 and #920
did, at a cost of thirteen discarded days. If no, it cannot contaminate anything and there is no
reason to hold it.

| Change | Touches the collected probabilities? | Decision |
|---|---|---|
| **#967** — forecast capture killed by a 45-min timeout; leads 12 and 24 halved since 2026-07-28 | **Yes** — forecast inputs feed the model that computes `p_yes_raw` | **HOLD until after the gate** (decided 2026-08-10) |
| **#969** — rail / artifact WARN thresholds calibrated on the wrong population | No — reporting only | **Land now** |
| #885 / #893 — the σ lever | Yes | HOLD until after M3, per the sequencing argument above — and now also to avoid pooling mislabelled CRPS evidence, see "DECIDED 2026-08-18" below |
| **EMOS retraining** (`run_emos_shadow`, any cadence) | **No** — shadow coefficients are computed and logged, never served (`scanner.py:773`) | **Land now**, unconditionally, while `primary_rows=0` |
| **`FORECAST_STACK` switch** (opening the stack) | **Yes** — DEB weights are filtered to the active stack (`deb_weighting.py:565-580`) and legacy serving consumes that blend | **HOLD until after the gate** (decided 2026-08-18) |

**#967, stated explicitly because it is a real trade and should not be made by default.** The
degradation predates the clean window, so the whole window is affected uniformly:

- **Fix before the gate** → forecast inputs change mid-window, the sample splits, and we are back
  to arguing which days are comparable.
- **Fix after** → the window stays homogeneous, but M3 scores a model running on halved lead-12
  and lead-24 input, and a negative verdict cannot separate "no edge" from "no edge on degraded
  input."

**Decision: fix after.** M3's question is whether the model *as deployed* beats the market, and a
split window is the more expensive failure — it has already cost this plan thirteen days once. The
cost is accepted knowingly: **if M3 comes back negative, #967 is a live alternative explanation
and must be named as one**, not discovered afterwards.

Note also that #967 is not merely a tight budget. The 45-minute limit was set by #717 as a
backstop against a hung run, on the stated basis that "a full run takes ~15-25min". Runs now take
41–43 minutes, so the runtime roughly doubled and nothing noticed — raising the number without
finding out why would remove the only guard against #717's silent six-day stall.

#### DECIDED 2026-08-18 — the EMOS/stack freeze, and why the window was *not* split

An EMOS retrain wrote 27 `emos_shadow` coefficient rows at `2026-08-16T23:05`, nine days into the
window. This was initially read as a mid-window model change that split the sample and would have
cost either a revert or a clock restart. **That reading was wrong, and the correction is recorded
here because the wrong version circulated for a day.**

**Shadow coefficients cannot reach serving.** `src/strategy/scanner.py:773` computes the EMOS
construction in shadow mode and passes it to `log.debug` only — `state` is never patched,
`emos_stddev_override` stays `None`, and legacy probabilities are served unchanged. Only the
`emos_primary` branch does `_dc_replace(state, corrected_mu_f=_mu_final)`. The mode iteration
inside `emos_mode._select_emos_row` (`("emos_primary", "emos_shadow")`) is real but reachable
only from that primary branch.

Verified on the live DB, 2026-08-18:

```
overrides=0 primary_rows=0
```

With no `emos_mode_override` row and no `emos_primary` calibration row, every city resolves to
`emos_shadow` through `get_city_mode`. **The window is homogeneous. No revert, no restart — the
2026-08-06 clock stands and the gate holds at ~2026-08-24/26.**

**Consequence: retraining is unconditionally safe while `primary_rows=0`.** Shadow fits are
structurally unable to alter `bracket_evals`, so the "does it touch the collected probabilities"
test above returns *no* for any amount of EMOS training. This is a property of the serving code,
not a scheduling convention.

**The freeze is one key: `FORECAST_STACK`.** It is not EMOS-only.
`src/model/deb_weighting.py:565-580` filters DEB weights to the active stack, and legacy serving
consumes that blend via `corrected_mu_f`/`deb_mu_f` — so switching the stack changes served
probabilities for every station immediately, with no EMOS involvement. It would *also* reset the
EMOS promotion counter by design (#759 puts `forecast_source` on `emos_crps_log`). Two resets for
one switch.

`USE_ENSEMBLE_SIGMA` is currently **inert for serving**: both consumers guard on the value
existing — `emos_mode.resolve_sigma_raw` (`emos_mode.py:202`) and
`envelope.true_probability_yes` (`envelope.py:327`) — and nothing under `src/weather/` ever
assigns `WeatherState.ensemble_sigma_f`, so both always take the fallback to `FORECAST_STDDEV_F`.
It still must not be touched, but for the counter reason below, not a contamination one.

**"All models" is not an available option.** `FORECAST_STACK_MODELS["full"]` includes `gefs`,
which has no `MODEL_STATE_ATTRS` entry and fails the serving-member parity guard by design
(`src/config.py:751-755`); only `hrrr_nbm` and `intl_ecmwf_icon` are promotable today. Both can
be *evaluated* now without switching anything — `model_forecast_log` captures every channel
regardless of the active stack — via `src/scripts/ecmwf_icon_backtest.py`.

##### EMOS promotion is ~5 weeks out; M3 lands ~4 weeks before it

`emos_crps_log`, measured 2026-08-18:

| forecast_source | sigma_source | rows | cities | days | first | last |
|---|---|---|---|---|---|---|
| baseline | ensemble | 621 | 27 | 23 | 2026-07-25 | 2026-08-17 |
| baseline | fixed | 453 | 30 | 17 | 2026-07-09 | 2026-07-25 |

Active track is `baseline`/`ensemble` (`FORECAST_STACK=baseline`, `USE_ENSEMBLE_SIGMA=true`).
621 ÷ 27 = exactly 23 — one entry per city per day, no gaps. `_primary_allowed` requires
`get_emos_crps_count(city) >= EMOS_MIN_SAMPLES_PROMOTION` (60, confirmed in `bot_config`), and
that counter counts **rows on the active track**. So every city sits at **23/60 → earliest
possible promotion ~2026-09-23**, and only then if an operator manually sets
`ready_for_promotion=1` (no automated path does).

**M3 therefore resolves roughly a month before EMOS could serve anything.** The recurring worry —
"is M3 decisive if EMOS sharpens the model afterwards?" — is settled by arithmetic, not by
judgement: they were never in a race, and no change to training cadence moves it, because the
guard counts days.

The `fixed` track's 453 rows stopped counting the moment `USE_ENSEMBLE_SIGMA` flipped on
2026-07-25 (#851 keys the counter on `sigma_source`). That is ~15 days per city of promotion
evidence discarded — the real EMOS clock reset in this project's history, and it happened
silently through a config flag. It is the precedent for freezing `FORECAST_STACK`.

##### The #885 pooling hazard — DECIDED: hold #885 until after the gate

The 621 rows tagged `sigma_source='ensemble'` were **scored against fixed sigma**, because
`ensemble_sigma_f` is never populated (above). The label tracks the flag, not the actual input.

So when #885 lands and `ensemble_sigma_f` starts carrying real spread, the sigma input changes
materially while the track label does not. **The counter will not reset**, and pre-#885 and
post-#885 evidence will pool — a city could reach 60 on evidence mostly drawn from a different
sigma regime. This is exactly the train/serve-evidence skew #851 exists to prevent, arriving
through the unpopulated-field door instead of the flag.

Three options were weighed:

1. Land #885 now and retag or delete the 621 rows — honest, but resets EMOS to day zero
   (~mid-November).
2. Land #885 after promotion — worst case: a promoted city's sigma input shifts underneath live
   coefficients.
3. **Hold #885 until after the M3 gate, then do (1) as one deliberate reset**, bundled with the
   `FORECAST_STACK` switch.

**Decision: (3).** It costs nothing extra — EMOS cannot promote before ~2026-09-23 regardless —
and it collapses the stack switch, the sigma fix, and the counter reset into a single post-gate
event with one clock instead of three. The cost is accepted knowingly: **the 621 `ensemble`-tagged
CRPS rows are mislabelled and must be discarded, not pooled, when #885 lands.** If that discard
is skipped, the promotion decision is made on evidence that does not mean what its label says.

##### Post-gate sequence (one event, one clock)

1. M3 gate resolves on the current model.
2. Switch `FORECAST_STACK` to whichever of `hrrr_nbm` / `intl_ecmwf_icon` the backtest picks.
3. Land #885 (and #893).
4. Discard the pre-#885 `sigma_source='ensemble'` CRPS rows.
5. Retrain shadow on the new stack + real ensemble sigma; the promotion counter restarts once.

##### Coverage note — the three cities on `fixed` but not `ensemble`

`Wuhan (ZHHH)`, `Jinan (ZSJN)`, `Zhengzhou (ZHCC)`. All three are `training_eligible: false` in
`config/source_priority.yaml` per the 2026-07-01 audit — ZHHH/ZHCC because a 2-hourly real-world
METAR cadence undershoots the true daily high by 1–3 °F, ZSJN because the feed went fully dead on
2026-07-17 (#732, `METAR_SKIP_STATIONS`). Their absence from the `ensemble` track is the audit
working as intended, not a coverage gap: 27/27 is full coverage of the eligible set.

##### Runbook — confirm nothing is serving EMOS

```bash
DB=data/meteoedge.db
sqlite3 -readonly "$DB" "SELECT 'overrides=' || (SELECT COUNT(*) FROM emos_mode_override) || ' primary_rows=' || (SELECT COUNT(*) FROM emos_calibration WHERE model_mode='emos_primary');"
```

`overrides=0 primary_rows=0` ⇒ shadow is inert and the window is homogeneous. Anything else means
EMOS is live and the window must be re-examined from the date that row was written.

```bash
# promotion distance on the active track (mirrors get_emos_crps_count exactly)
sqlite3 -readonly -header -box "$DB" "SELECT city, COUNT(*) AS n, MIN(date) AS since, MAX(date) AS latest FROM emos_crps_log WHERE model_mode='emos_shadow' AND forecast_source='baseline' AND sigma_source='ensemble' GROUP BY city ORDER BY n DESC;"
```

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

### Pre-registration — signed off 2026-08-20, before any powered run

The verdict table above fixes *what the number means*. It does not fix the population, the
power treatment, or what a negative result licenses. Those are specified here, with the gate
~4 days out and **no BSS figure yet read** for the 2026-08-06 window. Nothing below may change
after the first powered run.

#### The question

Do the model's bracket probabilities beat the market's own prices, on the population the
scanner evaluates, over the clean window? Nothing else — not profitability, not execution,
not whether a better model could exist.

#### Population — fixed now

- `--population all-bracket --since 2026-08-06`.
- Both certainty exclusions **kept** (#909, decided blind 2026-08-10).
- De-duplication: one row per `(station, ticker, end_date)`, lowest `minutes_to_settlement`.
- **The 12 dawn-closing station-days are INCLUDED** — KATL/KHOU/KORD/SBGR, 11:00Z final poll,
  near-uniform ladders (#1021). They are a different information regime: the final poll sits
  ~12 h from the outcome where every other station-day's sits minutes away. Both including and
  excluding them is defensible, so the choice is made **now, blind**. They are additionally
  reported as a **separate sensitivity line** — BSS with and without the cohort. The inclusive
  number is the verdict; the exclusive number is recorded, never substituted. A divergence
  beyond 0.05 is noted in the record and changes nothing about which number governs.
  *Rationale for including: dropping rows because the model did badly on them is the failure
  mode this document exists to prevent, and 3.0% cannot move the aggregate either way.*
- Power bar: **≥ 300 resolved station-days**, read off the `[bss] PASS 2 / M3 GATE` stderr
  line — not the pre-resolution count, which runs ~28 station-days ahead of it (see
  "Confirmed 2026-08-18"). The report's `UNDERPOWERED` guard is never overridden.

#### Power and uncertainty

The verdict is read against a **95% bootstrap CI resampled over station-days**, not over
bracket-rows — all ~11 brackets on a station-day share one daily high and are not independent
draws. A point estimate on the correct side of a boundary with a CI straddling it is reported
as straddling, not as clearing.

#### ⚠️ One band boundary is NOT yet settled

A stricter and a looser reading are both on the table, and this document must not quietly
adopt the looser one:

| | `BSS ≤ 0` | `−0.15 ≤ BSS < −0.05` |
|---|---|---|
| **Verdict table above (governs today)** | **Stop the thesis** — "it was a dream" | Stop |
| **Proposed 2026-08-20 (NOT adopted)** | — | Retest, if #967 is fixed **and** EMOS clears the bar below |

**The existing stricter rule governs.** The proposal to open a retestable band between −0.15
and −0.05 is recorded here as *considered and not adopted*, because adopting it would loosen a
pre-registration that was already fixed — the precise move this section exists to prevent.
Overriding it requires an explicit, dated decision recorded here **before** the gate runs; after
the number exists, it is not available at all.

#### What a PASS licenses

`BSS > 0.05` licenses **M4 — rebuilding the entry rule**. It does not license live trading;
M5 is separately gated on M4 holding, through the #559 promotion machinery.

#### The EMOS retest bar

Where the verdict table permits a re-test ("re-test after the σ work bites"), CRPS improvement
alone is **not sufficient**. Shadow CRPS is already 1.33 vs legacy 2.04 (d = +0.72), and CRPS
measures distance to the observation while BSS measures skill against the market — the two can
move independently, and assuming otherwise is how a re-test becomes a second bite.

A re-test requires EMOS to show **BSS improvement on the same window**: EMOS-derived
probabilities scored against the same market prices and outcomes, beating the market where the
legacy probabilities did not. **That capability does not exist today** — the shadow path logs
μ/σ at debug level (`scanner.py:773`) and never computes a ladder. Building shadow-BSS scoring
is therefore a **prerequisite** for any EMOS-based re-test, named here so it cannot be waived
later. Note also that EMOS cannot serve before ~2026-09-23 regardless (23/60 CRPS days on the
active track), so this is not on the critical path to M3.

#### Named alternative explanations

Declared in advance so they cannot be invented afterwards:

- **#967** — forecast capture halved on leads 12 and 24 since 2026-07-28; affects the whole
  window uniformly (already recorded above as a knowingly accepted cost)
- **The 12 dawn station-days** — 3.0% of last polls, near-uniform, included per above
- **#1021** — one unexplained mass deficiency (0.8952, KORD 2026-08-19); and same-day σ ≈ 10 °F
  from the climb floor against next-day σ ≈ 2 °F from `next_day_probability_yes`, a ~5×
  disagreement about the same uncertainty
- **#885** — serving σ is the fixed `FORECAST_STDDEV_F = 2.0`; `ensemble_sigma_f` is never
  assigned, so ensemble spread does not reach the scanner
- **#893** — 5 US stations train on a constant σ

**Declaring them does not license dismissing a negative result.** A negative result stands on
its own. They are named solely so that the re-test conditions are specified in advance, and
only the pre-specified conditions count.

#### One look

The gate runs **once**, at ≥ 300 resolved station-days. Progress checks before that read the
stderr station-day count only, never the score (see the Pass 2 runbook). An underpowered run is
not a verdict in either direction and is not argued from, in either direction. Looking at an
underpowered BSS and then looking again at 300 is two looks, and it inflates the false-positive
rate whatever the first look appeared to show.

---

## Open work, in dependency order

*Status as of 2026-08-06. M0 and M2 are complete. The plan is data-bound again — but on a
**second** clean-data window, restarted 2026-08-06 after two probability-mass defects voided the
first. The only thing between here and M3 is station-days accruing on the new window.*

| Issue | What | Stage | Status |
|---|---|---|---|
| #820 | Evening entries price tomorrow with today's observations | M0 | ✅ Merged |
| #826 | Persist all evaluated-bracket snapshots | M0 (parallel) | ✅ Merged — started a clock on 2026-07-24, but **that window is void** (#917/#920). Clock restarted **2026-08-06** |
| #917 | °F dash-range brackets integrated at half width | M3 | ✅ Merged, live 2026-08-01 (#919) — **verified in production**: °F ladder mass 0.53 → 0.99, gaps 100% → 0% |
| **#920** | **Truncation without renormalisation.** Filed against the bottom cut; measurement showed the **top** cut cost ~5× more (°C ladders 1.04 untruncated → 0.85 top-cut → 0.80 both) | **was the last M3 blocker** | ✅ Merged 2026-08-05 (#934), verified live 2026-08-06 — all ladders read 1.00. Replaced three shortcuts with one conditional that renormalises by construction |
| #921 | Mild two-sided excess — untruncated °C ladders average 1.04, one poll 1.156 | after #920 | Open — **stood down as immaterial**; explicitly NOT a gate precondition |
| #935 | `time_to_settlement_boost` rescales an already-normalised ladder | after M3 | Open — left deliberately untouched by #934 so the mass fix stayed attributable |
| #942 | One `scan_decisions` ladder sums 0.4644 **after** the #920 deploy, unexplained by parameter sweep or deployment lag | **before the gate** | Open — `bracket_evals` is clean, and that is what M3 scores, but the mechanism is not understood |
| #944 | Two systemd units failed ~427,000 times unnoticed — nothing queries unit health | **now** | Open — the monitoring gap, not the units themselves (#945 removed those) |
| #865 | Re-point Pass 1 at `resolve_bracket_outcomes` (n=20 → 404) | M1 | ✅ Merged — Pass 1 complete |
| #867 | Gamma resolves DISJOINT brackets as YES on one station-day | before M3 | ✅ Merged (#875) — root cause was direction-blindness, not wrong-market reads; 4 → 0 collisions |
| #869 | Post-fix model health — rail concentration, artifact rate, σ identifiability | **now, no waiting** | Shipped — run it |
| #870 | Ground-truth quality — Gamma-vs-METAR rate, zero-YES days, ladder completeness | **now, no waiting** | Shipped — run it |
| #822 | Market-vs-model skill test | M1 · M3 | Pass 1 ran (**BSS −0.28**, direction-contaminated — re-run pending, see M1); Pass 2 built and run 2026-07-29 (**underpowered: 139/300 station-days**); **Pass 2 = the decision** |
| — | **Re-run Pass 1** with the direction-aware resolver (#875) — same archive, no new data | M1 | ✅ **Done (2026-07-29)** — BSS −0.2752, clean. Direction dispatch verified working (#867), test coverage added (#902 → #903) |
| #909 | **Decide the certainty exclusions** — blind, before the powered Pass-2 run | M3 | ✅ **DECIDED 2026-08-10 — KEEP both.** n=1049 resolved, 1.0% YES, Wilson 0.5–1.7% (clear of the 2% bar, so a decision not an underpowered read). Calibration passes, provenance fails on all 1105 bit-exact zeros. See the M3 section |
| #799 | σ unidentifiable — switch on ensemble spread, retrain | M2 | ✅ Merged |
| #798 | Partial pooling instead of hard 60-sample cutover | M2 | ✅ Merged |
| #823 | Recompute promotion bars excluding artifact rows | M2 | ✅ Merged |
| #824 | Capture ECMWF ensemble spread (2,750 rows have none) | M2 | ✅ Merged |
| #861 | Bracket-boundary convention (**adjacent** brackets both YES) | before M3 | ✅ Merged (#881) — `[lo, hi)` matches Gamma on 96.9% of 2,619 settlements (vs 48.8% inclusive); 70 → 0 collisions |
| #871 | `bracket_evals.emos_mode` overwritten with `next_day` on half the rows | **now** | ✅ Merged (#880) — verified live. **10,503 pre-fix rows keep the corrupted value permanently** |
| #872 | M2 premise check — constant-σ rows show median `d` = 0.88, not 0.001 | before citing #799 | ✅ Resolved — premise CORRECT. On a constant σ, `c`/`d` lie on a flat ridge (`c + d·σ`), so a fitted `d` is optimizer position, not evidence. #869's check 3 measured nothing |
| #450 | Calibration backtest: reliability + CRPS over 30 days | M3 | ✅ Merged (#874) — HOLD ensemble sigma, CRPS delta −0.0041 (within noise; see #885 for why) |
| **#885** | **`ensemble_sigma_f` never populated — `USE_ENSEMBLE_SIGMA` is a no-op at serving** | **M2 (real remaining work)** | **Open — M2 BLOCKER** |
| #886 | No city ever promoted to `emos_primary` | M2 | ✅ Resolved (#890) — bar unmet by design, ~mid-Sept, needs manual promotion |
| #887 | 63% of GEFS σ below the 1 °F floor; raw-vs-calibrated decision | before #885 | ✅ Resolved (#892) — train raw, serve calibrated |
| #888 | `p_normal_between()` ZeroDivisionError on σ=0 | before #885 | ✅ Merged (#891) |
| **#967** | **Forecast capture killed by a 45-min timeout — the 12:02 and 21:02 UTC runs die daily since 2026-07-28. `model_forecast_log` 712 → 575 rows/day, entirely in lead 12 (143→74) and lead 24 (142→77)** | **after M3 — see the sequencing note** | Open — real data loss, degrades EMOS training input. NOT an M3 mass contaminant (the gate scores `bracket_evals`), but it does mean M3 scores a model on degraded input |
| **#969** | **Rail and `p_yes=0.0` WARN thresholds calibrated on the gate-selected Pass-1 archive, applied to the full `scan_decisions` ladder — so they fire unconditionally** | **now, before the gate** | Open — on an 11-bracket ladder most brackets sit at the 1¢ rail by construction, so `rail_pct < 20` can essentially never pass. Same left-behind pattern as #943. A WARN that always fires is how "Healthy" printed over two days of data loss (#913) |
| #893 | EMOS training σ chosen by sort order — 5 US stations train on a constant | after M3 | Open — triaged; fix is Simple (exclude constant σ sources). **Held until after M3**, but the reasoning weakened on 2026-08-10: it was rejected when a second window looked like ~5 days, and the measured rate makes it ~19 — see "What M3 will and will not have tested" |
| #894 | 329 legacy clamped σ rows in the training window | done | ✅ Merged (#896) — inert under `baseline`, live under `full` |
| #895 | KORD eats the GEFS cold-start timeout (sorts first in `STATIONS`) | done | ✅ Merged (#896) |
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

- **Run M3 on the M0-fixed model as-is** (~2026-08-24, counting from the 2026-08-06 clock
  restart at the measured ~16 resolved station-days/day -- see the counting note in M3). Cheapest, and a clean read on what is actually
  deployed. If it fails, the σ lever is still untried, so the verdict condemns the current
  serving stack rather than the thesis.
- **Or wire #885/#886/#887/#888 first, then start a fresh collection window.** Tests the model
  the plan intended, but resets the clean-data clock — switching serving σ mid-collection
  would split the sample and invalidate the accrued station-days.

Doing both in sequence is the only way to attribute a result to the σ work at all.

**Decided 2026-08-06: the first option.** The second was reconsidered while #920 was open — the
clock was being reset anyway, so bundling looked free — and then rejected once accrual was
measured. A second window costs ~5 days, not the ~12 assumed, and five days is cheap for keeping
a negative M3 attributable.

---

*Compiled 2026-07-24 from a live production extract (`meteoedge.db`, `bot.log`) and the
Polymarket Gamma API. Investigation thread: #810, #811, #819, #820, #822–#825.*
