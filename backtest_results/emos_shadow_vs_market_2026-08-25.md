# EMOS-Shadow-vs-Market Skill Test (issue #1041)

**Run date:** 2026-08-25  
**Data source:** `logs/bracket_evals.*.jsonl` (issue #826, same population M3 Pass 2 scores)  
**Probability source:** reconstructed EMOS-shadow `(mu_final, sigma_cal)` via `apply_emos`/`resolve_sigma_raw` + `true_probability_yes` -- see `src.scripts.emos_shadow_reconstruction`  
**Poll-date window:** rows polled on or after **2026-08-06** (same `--since` M3 uses, for direct comparability)  

> **NOT THE M3 DECISION GATE -- A PARALLEL, EXPLORATORY READ (issue #1041).**
> This scores a RECONSTRUCTED EMOS-shadow probability (EMOS shadow's current
> coefficients, applied offline to the same population M3 scores) against the
> market, using the identical BSS methodology M3 uses. It answers a different
> question than M3: "if EMOS shadow's forecasts had been served, how would
> they have scored?" -- not "how did the legacy served model score?"
>
> **1. Undertrained model.** Every city sits at roughly 28 of the 60
> `EMOS_MIN_SAMPLES_PROMOTION` CRPS-logged shadow days required for
> promotion in this window. Treat any BSS below the same way Pass 1 is
> treated: directional, not a verdict, and must not be argued from in
> either direction.
>
> **2. Fixed-sigma mislabeling.** The `sigma_source='ensemble'` coefficients
> used here were fit against rows that were actually scored with FIXED
> sigma=2.0 (`ensemble_sigma_f` was never populated in this window). This
> result therefore reflects EMOS's `(a, b)` mu-correction ONLY -- it cannot
> test the sigma lever `(c, d)` was meant to calibrate, and says nothing
> about the sharper-sigma model issues #885/#893 are meant to eventually
> produce.


---

## Reconstruction funnel

Reconstruction runs on the de-duplicated, `--since`-filtered population, BEFORE the row-exclusion cascade below (which then runs unchanged on the substituted probability).

| Stage | Count |
|---|---|
| De-duplicated rows considered | 6539 |
| Reconstructed (EMOS-shadow p available) | 6017 |
| Out of scope: next-day rows | 165 |
| Out of scope: non-"high"-direction rows | 0 |
| Unreconstructable: missing required fields or no `model_forecast_log`/timezone data | 357 |

## Exclusion funnel (identical cascade to `bss_market_vs_model_report.apply_exclusions`)

| Stage | Count | Share (of population) |
|---|---|---|
| Input rows (post reconstruction) | 6539 | 100% |
| Excluded: missing/unreconstructable EMOS probability | 522 | 8.0% |
| Excluded: EMOS probability == 0.0 (certainty artifact) | 4293 | 65.7% |
| Excluded: missing market price | 0 | 0.0% |
| Excluded: fabricated 50/50 price | 6 | 0.1% |
| Excluded: 1c/99c rail | 835 | 12.8% |
| Kept after row exclusions | 883 | 13.5% |
| Excluded: no outcome resolvable | 0 | 0.0% |
| **Final de-duplicated sample (n)** | **883** | **100.0%** |
| **Effective sample size (station-days)** | **293** | station-days, not bracket-rows |

## Global result

| Metric | Value |
|---|---|
| n | 883 |
| BS_model (EMOS-shadow-implied) | 0.1846 |
| BS_market | 0.1357 |
| BSS | -0.3603 |
| Directional reading (NOT a verdict -- see caveats above) | no edge over the market (BSS <= 0) |

## Reliability


=== EMOS-shadow-implied ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02      3      1.2%      0.0%   -1.2%
  0.02-0.05     21      3.6%     28.6%  +25.0%
  0.05-0.10    122      8.7%     13.9%   +5.2%
  0.10-0.20    313     13.9%     22.0%   +8.2%
  0.20-0.35    257     26.1%     28.0%   +1.9%
  0.35-0.50     86     41.9%     39.5%   -2.3%
  0.50-0.65     47     56.2%     59.6%   +3.4%
  0.65-0.80     15     69.4%     73.3%   +3.9%
  0.80-0.90      5     87.0%    100.0%  +13.0%
  0.90-0.95      1     91.6%    100.0%   +8.4%
  0.95-1.00     13    100.0%     92.3%   -7.7%

=== Market (symmetrized implied P(YES)) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.02-0.05    172      2.6%      2.3%   -0.3%
  0.05-0.10    115      6.6%      4.3%   -2.3%
  0.10-0.20    128     14.0%     14.8%   +0.8%
  0.20-0.35    148     26.4%     27.7%   +1.3%
  0.35-0.50    127     41.9%     37.8%   -4.1%
  0.50-0.65     98     57.9%     62.2%   +4.3%
  0.65-0.80     51     70.8%     66.7%   -4.2%
  0.80-0.90     12     83.6%     91.7%   +8.1%
  0.90-0.95     11     92.8%    100.0%   +7.2%
  0.95-1.00     21     97.0%    100.0%   +3.0%

## Sharpness


=== EMOS-shadow-implied ===
     bucket      n   share
  0.00-0.02      3    0.3%
  0.02-0.05     21    2.4%
  0.05-0.10    122   13.8%
  0.10-0.20    313   35.4%
  0.20-0.35    257   29.1%
  0.35-0.50     86    9.7%
  0.50-0.65     47    5.3%
  0.65-0.80     15    1.7%
  0.80-0.90      5    0.6%
  0.90-0.95      1    0.1%
  0.95-1.00     13    1.5%

=== Market (symmetrized implied P(YES)) ===
     bucket      n   share
  0.00-0.02      0    0.0%
  0.02-0.05    172   19.5%
  0.05-0.10    115   13.0%
  0.10-0.20    128   14.5%
  0.20-0.35    148   16.8%
  0.35-0.50    127   14.4%
  0.50-0.65     98   11.1%
  0.65-0.80     51    5.8%
  0.80-0.90     12    1.4%
  0.90-0.95     11    1.2%
  0.95-1.00     21    2.4%

---

## Methodology notes

- `p_model` = reconstructed EMOS-shadow-implied P(YES). See `src.scripts.emos_shadow_reconstruction` for the exact reconstruction (reuses `apply_emos`/`resolve_sigma_raw`/`true_probability_yes` directly, never re-derives their math).
- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- identical to M3's `market_p_yes()` (imported, not reimplemented).
- Exclusion funnel, de-duplication, and outcome resolution (`resolve_bracket_outcomes`, Gamma-first with observed-daily-high fallback) are IMPORTED from `bss_market_vs_model_report` unchanged -- identical to M3 Pass 2 except for the probability column.
- Reconstruction is scoped to same-day, high-direction rows only (see the reconstruction funnel above and `emos_shadow_reconstruction.reconstruct_bracket_row`'s docstring for what is out of scope and why).
