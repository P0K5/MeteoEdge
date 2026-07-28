# Market-vs-Model Skill Test -- Pass 1 (issue #822)

**Run date:** 2026-07-28-postfix  
**Data source:** `logs/candidates.*.csv.gz` (archived, gate-selected)  
**Outcome truth:** `resolve_bracket_outcomes` -- Polymarket definitive resolution, falling back to the station-local observed daily high (issues #850/#860/#865)  

> **PASS 1 -- NOT THE DECISION GATE.** This scores the model's PRE-#820-FIX
> probabilities on a GATE-SELECTED sample (only brackets that already cleared
> live entry gates -- not the general population of evaluated brackets).
> It is a lower bound / directional read, weeks before the M3 decision gate
> (issue #822, Pass 2), which re-runs this same test on clean, post-fix,
> all-bracket data once #826 has accumulated enough history. **Do not treat
> the number below as settling whether the trading edge is real.**


---

## Exclusion funnel

| Stage | Count |
|---|---|
| Input rows (all polls, all dates) | 13126 |
| Excluded: missing `p_yes_raw` | 5892 |
| Excluded: `p_yes_raw == 0.0` (certainty-shortcut artifact, #820) | 2424 |
| Excluded: missing market price | 0 |
| Excluded: 1c/99c rail | 0 |
| Kept after row exclusions | 4810 |
| De-duplicated to one row per (station, ticker, settlement date) | 427 |
| Excluded: no outcome resolvable (no Gamma resolution, no observed high) | 2 |
| **Final de-duplicated sample (n)** | **425** |
| **Effective sample size (station-days)** | **317** |

## Global result

| Metric | Value |
|---|---|
| n | 425 |
| BS_model | 0.2144 |
| BS_market | 0.1660 |
| BSS | -0.2914 |
| Pass-1 reading (NOT the M3 verdict) | no edge over the market (BSS <= 0) |

Note on power (docs/REMEDIATION_PLAN.md): all brackets on a station-day share one daily-high outcome, so they are not independent draws -- the effective sample size is **317 station-days**, not the 425 bracket-rows the BSS above is computed over. Read the station-day figure against the decision rule's power requirement.

## Outcome ground truth

| Source | n | Share |
|---|---|---|
| Polymarket definitive resolution (`gamma`) | 422 | 99.3% |
| Observed daily high fallback (`metar`) | 3 | 0.7% |

Gamma lookups: 0 cache hits, 427 fetched (422 newly resolved, 5 indecisive, 0 errors, 0 skipped offline). Indecisive/errored tickers fall back to the observed high.

⚠️ **Impossible-outcome exposure: 4 station-day(s), 8 bracket-rows** resolved YES on more than one bracket. A station-day has one daily high, so at most one bracket can contain it -- these rows inflate the YES count and bias BS_model/BS_market. Diagnostic only; nothing is auto-corrected.

| Collision shape | Station-days | Issue |
|---|---|---|
| `boundary` — brackets touch or overlap | 0 | #861 (which interval convention is correct) |
| `disjoint` — brackets do not touch | 4 | #867 (no interval convention can produce this — the resolution source is wrong) |

| Station | Settlement date | Shape | YES brackets | Observed high |
|---|---|---|---|---|
| LFPB | 2026-07-03 | `disjoint` | 59.0-60.8 (gamma), 82.4-84.2 (gamma) | 82.4 |
| RJTT | 2026-07-06 | `disjoint` | 78.8-80.6 (gamma), 69.8-71.6 (gamma) | 78.8 |
| RKSI | 2026-07-05 | `disjoint` | 75.2-77.0 (gamma), 80.6-82.4 (gamma) | 80.6 |
| RKSI | 2026-07-09 | `disjoint` | 73.4-75.2 (gamma), 80.6-82.4 (gamma) | 80.6 |

## Segment: same-day vs. next-day

(Derived from station-local `ts` date vs. `end_date` -- `logs/candidates.csv` carries no `is_next_day` column.)

| Segment | n | BS_model | BS_market | BSS |
|---|---|---|---|---|
| next_day | 121 | 0.2358 | 0.1807 | -0.3049 |
| other | 2 | 0.4870 | 0.3284 | -0.4831 |
| same_day | 302 | 0.2041 | 0.1591 | -0.2827 |

## Segment: UTC-offset bucket

| Bucket | n | BS_model | BS_market | BSS |
|---|---|---|---|---|
| UTC+1 | 18 | 0.2546 | 0.1879 | -0.3551 |
| UTC+12 | 7 | 0.1429 | 0.1017 | -0.4047 |
| UTC+2 | 44 | 0.2257 | 0.1800 | -0.2536 |
| UTC+3 | 67 | 0.2555 | 0.1930 | -0.3235 |
| UTC+8 | 120 | 0.1265 | 0.1123 | -0.1260 |
| UTC+9 | 68 | 0.2161 | 0.1608 | -0.3446 |
| UTC-3 | 17 | 0.2870 | 0.2070 | -0.3864 |
| UTC-4 | 34 | 0.3567 | 0.2570 | -0.3878 |
| UTC-5 | 38 | 0.2539 | 0.1890 | -0.3430 |
| UTC-7 | 12 | 0.1646 | 0.1474 | -0.1171 |

## Reliability

=== Model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02    142      0.6%     26.1%  +25.4%
  0.02-0.05    127      3.7%     18.9%  +15.2%
  0.05-0.10      6      7.6%     50.0%  +42.4%
  0.10-0.20     11     17.1%     36.4%  +19.2%
  0.50-0.65      5     59.1%     20.0%  -39.1%
  0.65-0.80      8     71.9%     50.0%  -21.9%
  0.80-0.90      2     85.0%     50.0%  -35.0%
  0.90-0.95      3     93.4%    100.0%   +6.6%
  0.95-1.00    121    100.0%     81.8%  -18.2%

=== Market (symmetrized implied P(YES)) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.10-0.20      3     18.0%      0.0%  -18.0%
  0.20-0.35    274     23.5%     23.7%   +0.2%
  0.35-0.50     15     39.0%     33.3%   -5.7%
  0.50-0.65      7     58.6%     28.6%  -30.1%
  0.65-0.80     35     76.6%     54.3%  -22.3%
  0.80-0.90     52     86.0%     88.5%   +2.5%
  0.90-0.95     39     90.5%    100.0%   +9.5%

## Sharpness

=== Model (p_yes_raw) ===
     bucket      n   share
  0.00-0.02    142   33.4%
  0.02-0.05    127   29.9%
  0.05-0.10      6    1.4%
  0.10-0.20     11    2.6%
  0.20-0.35      0    0.0%
  0.35-0.50      0    0.0%
  0.50-0.65      5    1.2%
  0.65-0.80      8    1.9%
  0.80-0.90      2    0.5%
  0.90-0.95      3    0.7%
  0.95-1.00    121   28.5%

=== Market (symmetrized implied P(YES)) ===
     bucket      n   share
  0.00-0.02      0    0.0%
  0.02-0.05      0    0.0%
  0.05-0.10      0    0.0%
  0.10-0.20      3    0.7%
  0.20-0.35    274   64.5%
  0.35-0.50     15    3.5%
  0.50-0.65      7    1.6%
  0.65-0.80     35    8.2%
  0.80-0.90     52   12.2%
  0.90-0.95     39    9.2%
  0.95-1.00      0    0.0%

---

## Methodology notes

- `p_model` = `p_yes_raw` (pre-clamp, pre-#820-fix probability).
- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- symmetrized across both sides of the book (see `market_p_yes()`); using `yes_ask` alone is an equally defensible alternative and would read slightly differently on wide spreads.
- De-duplication keeps the final (lowest `minutes_to_settlement`) poll per (station, ticker, settlement date), mirroring `calibration_report.pick_samples`.
- Outcome truth: `resolve_bracket_outcomes.resolve_bracket_rows()` -- Polymarket's definitive resolution where available, else YES iff the station-local observed daily high falls in `[bracket_low, bracket_high]`. This is the same precedence `settle.py` applies and the same capability #822's Pass 2 uses. Brackets that neither source can resolve are dropped, never guessed.
- **Not joined to `settlements`** (issue #865). That table only covers brackets MeteoEdge actually traded (~156 rows all-time); joining it dropped 360 of 380 de-duplicated brackets on the 2026-07-25 run, leaving n=20. Whether a bracket resolved YES is a fact about the weather, not about whether we traded it. Use `--outcome-source settlements` to reproduce that earlier report.
