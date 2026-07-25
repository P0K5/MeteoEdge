# Market-vs-Model Skill Test -- Pass 1 (issue #822)

**Run date:** 2026-07-25  
**Data source:** `logs/candidates.*.csv.gz` (archived, gate-selected) joined to `settlements` (meteoedge.db)  

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
| Input rows (all polls, all dates) | 12230 |
| Excluded: missing `p_yes_raw` | 5892 |
| Excluded: `p_yes_raw == 0.0` (certainty-shortcut artifact, #820) | 2262 |
| Excluded: missing market price | 0 |
| Excluded: 1c/99c rail | 0 |
| Kept after row exclusions | 4076 |
| Excluded: no definitive settlement match | 360 |
| **Final de-duplicated sample (n)** | **20** |

## Global result

| Metric | Value |
|---|---|
| n | 20 |
| BS_model | 0.0998 |
| BS_market | 0.1051 |
| BSS | 0.0503 |
| Pass-1 reading (NOT the M3 verdict) | edge appears real (BSS > 0.05) |

Note on power (docs/REMEDIATION_PLAN.md): all brackets on a station-day share one daily-high outcome, so the effective sample size is station-days, not bracket-rows. This report states the de-duplicated bracket-row n; it does not further collapse to station-days.

## Segment: same-day vs. next-day

(Derived from station-local `ts` date vs. `end_date` -- `logs/candidates.csv` carries no `is_next_day` column.)

| Segment | n | BS_model | BS_market | BSS |
|---|---|---|---|---|
| next_day | 2 | 0.0000 | 0.0554 | 0.9994 |
| same_day | 18 | 0.1109 | 0.1106 | -0.0025 |

## Segment: UTC-offset bucket

| Bucket | n | BS_model | BS_market | BSS |
|---|---|---|---|---|
| UTC+2 | 4 | 0.2500 | 0.1884 | -0.3268 |
| UTC+8 | 8 | 0.0006 | 0.0496 | 0.9882 |
| UTC-4 | 2 | 0.0000 | 0.0600 | 1.0000 |
| UTC-5 | 6 | 0.1654 | 0.1387 | -0.1921 |

## Reliability

=== Model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02     13      0.2%     15.4%  +15.1%
  0.02-0.05      5      3.1%      0.0%   -3.1%
  0.95-1.00      2    100.0%    100.0%   +0.0%

=== Market (symmetrized implied P(YES)) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.20-0.35     18     23.5%     11.1%  -12.4%
  0.80-0.90      2     85.5%    100.0%  +14.5%

## Sharpness

=== Model (p_yes_raw) ===
     bucket      n   share
  0.00-0.02     13   65.0%
  0.02-0.05      5   25.0%
  0.05-0.10      0    0.0%
  0.10-0.20      0    0.0%
  0.20-0.35      0    0.0%
  0.35-0.50      0    0.0%
  0.50-0.65      0    0.0%
  0.65-0.80      0    0.0%
  0.80-0.90      0    0.0%
  0.90-0.95      0    0.0%
  0.95-1.00      2   10.0%

=== Market (symmetrized implied P(YES)) ===
     bucket      n   share
  0.00-0.02      0    0.0%
  0.02-0.05      0    0.0%
  0.05-0.10      0    0.0%
  0.10-0.20      0    0.0%
  0.20-0.35     18   90.0%
  0.35-0.50      0    0.0%
  0.50-0.65      0    0.0%
  0.65-0.80      0    0.0%
  0.80-0.90      2   10.0%
  0.90-0.95      0    0.0%
  0.95-1.00      0    0.0%

---

## Methodology notes

- `p_model` = `p_yes_raw` (pre-clamp, pre-#820-fix probability).
- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- symmetrized across both sides of the book (see `market_p_yes()`); using `yes_ask` alone is an equally defensible alternative and would read slightly differently on wide spreads.
- De-duplication keeps the final (lowest `minutes_to_settlement`) poll per (station, ticker, settlement date), mirroring `calibration_report.pick_samples`.
- Outcome truth: `settlements.resolved_yes` (meteoedge.db), keyed by ticker.
