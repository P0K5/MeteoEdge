# Market-vs-Model Skill Test -- PASS 2 / M3 DECISION GATE (issue #822)

**Run date:** 2026-08-25  
**Data source:** `logs/bracket_evals.*.jsonl` (issue #826, FULL evaluated-bracket population)  
**Outcome truth:** `resolve_bracket_outcomes` -- Polymarket definitive resolution, falling back to the station-local observed daily high (issues #850/#860/#865)  

**Poll-date window:** rows polled on or after **2026-08-06** (67674 of 170625 earlier rows excluded).  

> Filtered on POLL time, not settlement date: contamination is a property of when the probability was computed. Probability defects landed mid-window twice (#917 on 2026-08-01, #920 on 2026-08-05), so rows polled before 2026-08-06 came from a materially different model.

> **PASS 2 -- THIS IS THE M3 DECISION GATE (issue #822).** This scores the
> POST-FIX model on the FULL evaluated-bracket population (`bracket_evals`,
> issue #826), not the gate-selected archive Pass 1 used. The decision rule
> below was fixed in `docs/REMEDIATION_PLAN.md` **before** any number was seen,
> so it cannot be rationalised afterwards.
>
> **The two passes are not directly comparable.** Pass 1 answered *"on the
> brackets we chose to trade, were we better than the market?"*; Pass 2 answers
> the general calibration question over every bracket evaluated. A different
> number is expected from population alone.


---

## Exclusion funnel

Every row-exclusion stage's share below is of **rows in window** (the population column names it explicitly per stage) -- the cascaded count AFTER every stage above it has already removed its rows, per `apply_exclusions`'s single implementation. A row that is e.g. both model-certain and rail-clipped is attributed to whichever stage runs first, not counted in both (issue #1030).

| Stage | Count | Share (of population) |
|---|---|---|
| Input rows (all polls, all dates) | 170625 | 100% of all polls, all dates |
| Excluded: polled before `2026-08-06` (contaminated era) | 67674 | 39.7% of all polls, all dates |
| Rows in window | 6520 | 3.8% of all polls, all dates |
| Excluded: missing `p_yes_raw` | 0 | 0.0% of rows in window |
| Excluded: `p_yes_raw == 0.0` (certainty-shortcut artifact, #820) | 4368 | 67.0% of rows in window |
| Excluded: missing market price | 0 | 0.0% of rows in window |
| Excluded: fabricated 50/50 price (#1028) | 8 | 0.1% of rows in window |
| Excluded: 1c/99c rail | 1066 | 16.3% of rows in window |
| Kept after row exclusions | 1078 | 16.5% of rows in window |
| De-duplicated to one row per (station, ticker, settlement date) | 1078 | 100.0% of rows kept after row exclusions |
| Excluded: no outcome resolvable (no Gamma resolution, no observed high) | 70 | 6.5% of de-duplicated rows |
| **Final de-duplicated sample (n)** | **1008** | **93.5% of de-duplicated rows** |
| **Effective sample size (station-days)** | **316** | station-days, not bracket-rows -- not comparable to any row-population share above |

**Reconciling the rail share against `post_fix_model_health.rail_concentration`.** The two are DIFFERENT predicates over DIFFERENT columns, not two counts of the same quantity that should agree:

- `apply_exclusions`'s `rail_1c_99c` (1066 rows, 16.3% of rows in window) is `is_rail_price` -- the **market's** `yes_ask`/`no_ask` cents -- run LAST in the cascade, so it only counts rows that survived every earlier stage. Ignoring cascade order, `is_rail_price` alone is true for 5169 rows (79.3% of rows in window) -- the gap between that and the cascaded count is entirely rows an earlier stage (mostly `p_yes_raw == 0.0`) already removed.

- `post_fix_model_health.rail_concentration`'s rail share is a SEPARATE predicate on the **model's** `p_yes_raw` (`<= 0.02` or `>= 0.95`), computed over its own caller's population with no prior removal -- its low-rail bucket therefore FOLDS IN every `p_yes_raw == 0.0` artifact row this funnel excludes at an earlier, separate stage (4368 rows here; unconditionally, model-certain rows are 4368, 67.0% of rows in window). A model-probability rail share and a market-price rail share are not expected to agree: they read different columns for different purposes.

- **What would settle the residual gap exactly** (not established here, no `logs/` or `data/` available in this environment to compute it): re-run both this funnel and `rail_concentration` over the IDENTICAL windowed, de-duplicated population and print both alongside each other in one report -- today they are read off separate report runs, sometimes over different `--since` windows, which is enough by itself to move either figure independent of any exclusion-logic question.

- **The 1c/99c rail cannot distinguish a clamp from a genuine rail price.** `scanner.py`'s own `max(1, min(99, round(price * 100)))` clamp lands a genuinely sub-cent (e.g. 0.3c) or super-99-cent market on exactly 1 or 99 cents, bit-identical in `bracket_evals` to a market that was truly quoted at the rail. Nothing in the stored data distinguishes the two cases, and this funnel's `rail_1c_99c` count is therefore a count of clamped-OR-genuine rail prices together, not of genuine rail prices alone.


## Global result

| Metric | Value |
|---|---|
| n | 1008 |
| BS_model | 0.1864 |
| BS_market | 0.1320 |
| BSS | -0.4123 |
| **M3 VERDICT** | **no edge over the market (BSS <= 0)** |

Note on power (docs/REMEDIATION_PLAN.md): all brackets on a station-day share one daily-high outcome, so they are not independent draws -- the effective sample size is **316 station-days**, not the 1008 bracket-rows the BSS above is computed over. Read the station-day figure against the decision rule's power requirement.

## M3 decision gate -- the pre-registered rule

Fixed in `docs/REMEDIATION_PLAN.md` before this number was seen.

| Result | Verdict |
|---|---|
| **BSS > 0.05** | The edge is real. Proceed to M4. |
| **0 < BSS <= 0.05** | Marginal. Stay shadow-only; re-test after the sigma work bites. Do not re-enable live. |
| **BSS <= 0** | **It was a dream.** The public price forecasts weather at least as well as we do. Stop the thesis -- pivot the model materially or shut the live path down. |

### Power check

The rule requires **n >= 300**. All ~11 brackets on a station-day are determined by one daily high, so the effective sample size is **station-days**, not bracket-rows.

| Measure | Value | Required | Met |
|---|---|---|---|
| Station-days | **316** | 300 | YES |
| De-duplicated bracket-rows | 1008 | — | — |

### Verdict: no edge over the market (BSS <= 0)

BSS = **-0.4123** on 316 station-days, at or above the required power. Per the rule above, this is the M3 decision.

## Outcome ground truth

| Source | n | Share |
|---|---|---|
| Polymarket definitive resolution (`gamma`) | 946 | 93.8% |
| Observed daily high fallback (`metar`) | 62 | 6.2% |

Market direction (issue #867 -- the logged `direction` field, else `candidates.direction`, else the question text): 1078 high, 0 low, 0 unknown.

Gamma lookups: 946 cache hits, 132 fetched (0 newly resolved, 132 indecisive, 0 errors, 0 skipped offline). Indecisive/errored tickers fall back to the observed high.

Impossible-outcome check (issues #861 / #867): **0** station-days resolved YES on more than one bracket of the same direction.

## Segment: same-day vs. next-day

(From the `is_next_day` flag `bracket_evals` RECORDS -- not a reconstruction. Pass 1 had to derive this from station-local `ts` vs `end_date` because the candidates CSV carries no such column.)

| Segment | n | BS_model | BS_market | BSS |
|---|---|---|---|---|
| next_day | 6 | 0.0315 | 0.0560 | 0.4383 |
| same_day | 1002 | 0.1874 | 0.1325 | -0.4145 |

## Segment: UTC-offset bucket

| Bucket | n | BS_model | BS_market | BSS |
|---|---|---|---|---|
| UTC+1 | 69 | 0.2166 | 0.1527 | -0.4182 |
| UTC+2 | 199 | 0.1918 | 0.1282 | -0.4968 |
| UTC+3 | 230 | 0.1968 | 0.1212 | -0.6243 |
| UTC+8 | 7 | 0.1128 | 0.0324 | -2.4857 |
| UTC+9 | 9 | 0.2242 | 0.0380 | -4.9053 |
| UTC-3 | 65 | 0.1527 | 0.1603 | 0.0470 |
| UTC-4 | 142 | 0.1999 | 0.1361 | -0.4685 |
| UTC-5 | 199 | 0.1711 | 0.1352 | -0.2660 |
| UTC-7 | 88 | 0.1632 | 0.1356 | -0.2036 |

## Reliability

=== Model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02     22      0.8%      4.5%   +3.8%
  0.02-0.05     47      3.4%     14.9%  +11.5%
  0.05-0.10    165      8.3%     22.4%  +14.2%
  0.10-0.20    332     13.9%     20.5%   +6.6%
  0.20-0.35    285     26.4%     27.7%   +1.3%
  0.35-0.50     83     41.5%     43.4%   +1.9%
  0.50-0.65     32     56.8%     37.5%  -19.3%
  0.65-0.80     20     70.6%     70.0%   -0.6%
  0.80-0.90      7     83.6%    100.0%  +16.4%
  0.95-1.00     15    100.0%     80.0%  -20.0%

=== Market (symmetrized implied P(YES)) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.02-0.05    211      2.6%      2.8%   +0.2%
  0.05-0.10    136      6.7%      3.7%   -3.0%
  0.10-0.20    152     14.0%     15.1%   +1.1%
  0.20-0.35    168     26.5%     26.8%   +0.3%
  0.35-0.50    137     41.8%     36.5%   -5.3%
  0.50-0.65    105     57.7%     61.9%   +4.2%
  0.65-0.80     54     70.7%     64.8%   -5.9%
  0.80-0.90     12     83.6%     91.7%   +8.1%
  0.90-0.95     11     92.8%    100.0%   +7.2%
  0.95-1.00     22     97.0%    100.0%   +3.0%

## Sharpness

=== Model (p_yes_raw) ===
     bucket      n   share
  0.00-0.02     22    2.2%
  0.02-0.05     47    4.7%
  0.05-0.10    165   16.4%
  0.10-0.20    332   32.9%
  0.20-0.35    285   28.3%
  0.35-0.50     83    8.2%
  0.50-0.65     32    3.2%
  0.65-0.80     20    2.0%
  0.80-0.90      7    0.7%
  0.90-0.95      0    0.0%
  0.95-1.00     15    1.5%

=== Market (symmetrized implied P(YES)) ===
     bucket      n   share
  0.00-0.02      0    0.0%
  0.02-0.05    211   20.9%
  0.05-0.10    136   13.5%
  0.10-0.20    152   15.1%
  0.20-0.35    168   16.7%
  0.35-0.50    137   13.6%
  0.50-0.65    105   10.4%
  0.65-0.80     54    5.4%
  0.80-0.90     12    1.2%
  0.90-0.95     11    1.1%
  0.95-1.00     22    2.2%

---

## Methodology notes

- `p_model` = `p_yes_raw` (pre-clamp). **POST-#820-fix** -- `bracket_evals` logging began 2026-07-24, so every row here was produced by the fixed model.
- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- symmetrized across both sides of the book (see `market_p_yes()`); using `yes_ask` alone is an equally defensible alternative and would read slightly differently on wide spreads.
- De-duplication keeps the final (lowest `minutes_to_settlement`) poll per (station, ticker, settlement date), mirroring `calibration_report.pick_samples`. Exclusions are applied after de-duplication -- if the final poll is excluded, the bracket-day is dropped.
- Outcome truth: `resolve_bracket_outcomes.resolve_bracket_rows()` -- Polymarket's definitive resolution where available, else the station-local observed daily temperature, **dispatched on market direction**: a `high` market is YES iff the observed daily HIGH falls in the bracket, a `low` market iff the observed daily LOW does. A low market is never scored against the high (issue #867). The interval is `[bracket_low, bracket_high)` -- upper bound EXCLUSIVE, which #861 measured against live Gamma at 96.9% agreement vs. 48.8% for the inclusive reading. This is the same precedence `settle.py` applies and the same capability #822's Pass 2 uses. Brackets that neither source can resolve are dropped, never guessed.
- **Not joined to `settlements`** (issue #865). That table only covers brackets MeteoEdge actually traded (~156 rows all-time); joining it dropped 360 of 380 de-duplicated brackets on the 2026-07-25 run, leaving n=20. Whether a bracket resolved YES is a fact about the weather, not about whether we traded it. Use `--outcome-source settlements` to reproduce that earlier report.
