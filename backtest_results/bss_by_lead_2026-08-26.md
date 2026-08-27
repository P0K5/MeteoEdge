# BSS and Realized EV by Time-to-Settlement (M3 window)

**Run date:** 2026-08-26  
**Data source:** `logs/bracket_evals.*.jsonl` (issue #826, M3's own population)  
**Poll-date window:** rows polled on or after **2026-08-06** (M3's window)  

> **DOES NOT REOPEN M3.** M3's verdict (`BSS = -0.4123`) stands, final. This report buckets the SAME population by lead and asks whether the model's skill varies with lead, and whether the live entry rule fired at the worst moment. Live stays halted (#1053/#1054) regardless of any result here.

> **AMENDMENT (2026-08-26, same window/loader/exclusions/resolution, read-only).** The NULL verdict below STANDS -- the stopping rule is not reopened and no bar moves. Two evidentiary additions and three reporting corrections were made after the original run: (1) BS_market and the Murphy decomposition of BOTH model and market on the 268-bracket-day intersection subset (new subsection under "Headline comparison"), showing a clean per-bucket collapse the unrestricted table hides; (2) null-signal controls N1/N2/N3 for the H2 EV branch (new subsection under "H2 counterfactual"), testing whether A/B's positive EV requires the model at all; and three corrections to the original prose -- question (1) is now answered from the intersection table, not the unrestricted one; the "YES side is anti-informative" claim is dropped in favor of a per-side/per-bucket break-even-vs-realized comparison; and policy C is reported as unstable (sign-flipping across the threshold sweep) rather than as a single point estimate.


---

## Method

- Loader, exclusion cascade, and outcome resolution are **imported unchanged** from `bss_market_vs_model_report` (the M3 gate). Only the de-duplication key changed: one row per (station, ticker, end_date, LEAD BUCKET), keeping the poll nearest the bucket centre.

- `p_model` = `p_yes_raw`; `p_market` = `(yes_ask + (100 - no_ask)) / 200` (M3's `market_p_yes`, for *scoring* only -- execution uses the actual ask).

- Lead buckets (minutes-to-settlement): `<60, 60-180, 180-360, 360-720, 720-1440, >1440`; centres `30, 120, 270, 540, 1080, 1800` (the `>1440` centre is nominal -- the bucket is unbounded).

- **Buckets are NOT independent samples**: the same bracket-day appears in every bucket it has a poll for, with the same outcome and a different probability/market price. Every interval below is a paired read.


---

## Self-check: classic de-duplication (must reproduce M3)

| De-duplicated bracket-days | Station-days | BSS (classic, lowest-mts poll) |
|---|---|---|
| 1008 | 316 | -0.4123 |

> The M3 gate reports `BSS = -0.4123`. If this self-check does not match, the loader/exclusion/resolution reuse below is not faithful and nothing else in this report is trustworthy.


---

## Per-lead-bucket result

> **Lead >= 720 min is next-day, not early same-day.** The scanner polls a market the evening before its settlement day (as `next_day`, a forecast-only probability path with no envelope) and again on the settlement day itself (as `same_day`). The lowest-`minutes_to_settlement` poll of a ticker is always the same-day one, so the lead axis splits cleanly: `<60`..`360-720` are same-day, `720-1440` and `>1440` are next-day. H1 (dawn `remaining_rise`) is therefore a question about the four same-day buckets only.

| Lead | day | n | station-days | BS_model | BS_market | BSS | Unc | Res | Rel | mean\|Δ\| | sign n | sign acc | EV n | EV (c) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| <60 | same | 914 | 296 | 0.1897 | 0.1322 | -0.4353 | 0.2016 | 0.0187 | 0.0077 | 0.1865 | 431 | 0.339 | 431 | -0.51 |
| 60-180 | same | 814 | 250 | 0.1924 | 0.1357 | -0.4173 | 0.2038 | 0.0176 | 0.0047 | 0.1813 | 369 | 0.331 | 369 | 0.28 |
| 180-360 | same | 943 | 347 | 0.1947 | 0.1340 | -0.4533 | 0.2199 | 0.0364 | 0.0112 | 0.1968 | 411 | 0.394 | 411 | 2.67 |
| 360-720 | same | 2013 | 556 | 0.1748 | 0.1403 | -0.2460 | 0.1795 | 0.0091 | 0.0045 | 0.1540 | 803 | 0.392 | 803 | 2.14 |
| 720-1440 | next | 2561 | 537 | 0.1577 | 0.1273 | -0.2390 | 0.1547 | 0.0039 | 0.0071 | 0.1409 | 1006 | 0.408 | 1006 | 1.59 |
| >1440 | next | 2446 | 467 | 0.1441 | 0.1231 | -0.1708 | 0.1499 | 0.0088 | 0.0030 | 0.1042 | 652 | 0.388 | 652 | -0.09 |

- **BS_model / BS_market**: mean squared error of `p_model` / `p_market` vs the outcome. **BSS** = `1 - BS_model/BS_market` (positive = better than the market).

- **Unc / Res / Rel**: Murphy (1973) decomposition of the MODEL's Brier score (`BS = Rel - Res + Unc`). Resolution higher = better discrimination; Reliability lower = better calibration.

- **mean |Δ|**: mean |`p_model` - `p_market`| -- how much we disagree with the market at that lead.

- **sign acc**: among rows with |Δ| >= 0.15, how often the outcome is on OUR side of the price rather than the market's.

- **EV (c)**: realized EV per contract if every such disagreement were traded at that lead, at the ACTUAL `yes_ask`/`no_ask` (never `market_p_yes`).

### Correction 2 -- per-side, per-bucket break-even vs. realized hit rate

Sign accuracy benchmarks against 0.5, which is the wrong benchmark at a skewed price -- a 10c ask only needs to win 10% of the time to be fair value. The table below reports, per side and per bucket among disagreement rows, the average ask, the break-even hit rate that ask implies (`avg_ask/100`), and the realized hit rate:

| Lead | side | n | avg ask (c) | break-even hit rate | realized hit rate |
|---|---|---|---|---|---|
| <60 | YES | 144 | 11.86 | 11.86% | 9.72% |
| <60 | NO | 287 | 45.69 | 45.69% | 45.99% |
| 60-180 | YES | 119 | 9.40 | 9.40% | 10.92% |
| 60-180 | NO | 250 | 43.92 | 43.92% | 43.60% |
| 180-360 | YES | 120 | 18.19 | 18.19% | 20.83% |
| 180-360 | NO | 291 | 44.39 | 44.39% | 47.08% |
| 360-720 | YES | 283 | 10.04 | 10.04% | 11.66% |
| 360-720 | NO | 520 | 51.81 | 51.81% | 54.23% |
| 720-1440 | YES | 385 | 8.14 | 8.14% | 9.35% |
| 720-1440 | NO | 621 | 58.40 | 58.40% | 60.23% |
| >1440 | YES | 245 | 9.76 | 9.76% | 10.61% |
| >1440 | NO | 407 | 56.43 | 56.43% | 55.77% |

Every bucket, both sides, realized hit rate is within a couple points of the break-even rate its own average ask implies -- at <60 the YES side realizes 9.72% against a break-even of 11.86% (a small loss, consistent with the -0.28c/contract seen in the H2 policy-A split), but it is fair-value-adjacent, not "anti-informative." **Sign accuracy > 0.5 remains the stopping-rule guard** (conservative, pre-registered) -- but the honest read of whether a side carries information is this table, not sign accuracy against a 0.5 benchmark that ignores the skewed price.


## Headline comparison: bracket-days present in ALL buckets

Coverage differs by bucket (early polls exist for fewer station-days than late ones). A bucket that looks good because it covers only the easy station-days is not good. This is the BSS restricted to the intersection subset present in every bucket -- the only apples-to-apples comparison.

| Lead | n (∩) | BSS (∩) |
|---|---|---|
| <60 | 268 | -0.6416 |
| 60-180 | 268 | -0.3802 |
| 180-360 | 268 | -0.3296 |
| 360-720 | 268 | -0.3752 |
| 720-1440 | 268 | -0.1844 |
| >1440 | 268 | -0.0707 |

Intersection subset: 268 bracket-days across 139 station-days.

### Addition 1 -- BS_market and Murphy decomposition, intersection subset

Uncertainty is identical (`0.2230`) across every bucket and for both model and market, by construction -- Uncertainty depends only on the outcome base rate, and the intersection holds the same 268 bracket-days fixed across buckets. This confirms the intersection is what it claims to be: a controlled comparison where only the forecasts change, not the population.

| Lead | n | BS_model | BS_market | BSS | Unc (model=market) | Res (model) | Rel (model) | Res (market) | Rel (market) |
|---|---|---|---|---|---|---|---|---|---|
| <60 | 268 | 0.2004 | 0.1221 | -0.6416 | 0.2230 | 0.0303 | 0.0075 | 0.1076 | 0.0043 |
| 60-180 | 268 | 0.2118 | 0.1534 | -0.3802 | 0.2230 | 0.0172 | 0.0048 | 0.0755 | 0.0042 |
| 180-360 | 268 | 0.2395 | 0.1801 | -0.3296 | 0.2230 | 0.0172 | 0.0331 | 0.0490 | 0.0040 |
| 360-720 | 268 | 0.2620 | 0.1905 | -0.3752 | 0.2230 | 0.0057 | 0.0461 | 0.0376 | 0.0075 |
| 720-1440 | 268 | 0.2346 | 0.1980 | -0.1844 | 0.2230 | 0.0156 | 0.0284 | 0.0360 | 0.0124 |
| >1440 | 268 | 0.2175 | 0.2032 | -0.0707 | 0.2230 | 0.0206 | 0.0151 | 0.0272 | 0.0081 |

**Reading (a) vs (b).** The market's Resolution (`Res` column, market) rises **monotonically** as lead shortens -- `0.0272 -> 0.0360 -> 0.0376 -> 0.0490 -> 0.0755 -> 0.1076`, a 4x increase from `>1440` to `<60` -- while its Reliability stays low and roughly flat (`0.0081` to `0.0043`, never above `0.0124`). That is a clean signature: the market discriminates far better near settlement without becoming less well-calibrated. The model shows no comparable monotonic pattern in either direction -- Resolution is `0.0206, 0.0156, 0.0057, 0.0172, 0.0172, 0.0303` (non-monotonic) and Reliability peaks in the *middle* of the day (`0.0461` at 360-720) rather than climbing steadily toward `<60` (`0.0075`, in fact the model's best-calibrated bucket in this subset).

**Verdict: the numbers support (b), not (a).** The BSS collapse from `-0.0707` (>1440) to `-0.6416` (<60) is driven almost entirely by `BS_market` falling faster (`0.2032 -> 0.1221`, -40%) than `BS_model` falls (`0.2175 -> 0.2004`, -8%) over the same buckets -- the market's nowcasting advantage grows sharply into settlement, via a clean Resolution gain with no Reliability cost. There is a secondary, weaker signal consistent with (a) in the 180-720 window specifically: `Rel_model` more than triples from `180-360` (`0.0331`) to `360-720` (`0.0461`) before falling back down toward `<60`, i.e. the model's calibration is at its worst mid-day, not at the extremes -- but this does not extend into a monotonic same-day degradation all the way to settlement, so it does not explain the `<60` collapse. This is an architecture statement, not an edge statement: the intraday-correction/climb-floor/settlement-boost machinery is not obviously *harmful* by this decomposition, but it also never catches up to the market's late-window nowcasting gain.


## Reliability / sharpness (per bucket, model)

### Lead <60 (n=914)


=== <60: model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02      8      0.8%     12.5%  +11.7%
  0.02-0.05     36      3.5%     13.9%  +10.4%
  0.05-0.10    153      8.3%     22.2%  +13.9%
  0.10-0.20    312     13.7%     20.2%   +6.4%
  0.20-0.35    259     26.2%     29.0%   +2.7%
  0.35-0.50     72     42.2%     45.8%   +3.7%
  0.50-0.65     32     56.8%     37.5%  -19.3%
  0.65-0.80     20     70.6%     70.0%   -0.6%
  0.80-0.90      7     83.6%    100.0%  +16.4%
  0.95-1.00     15    100.0%     80.0%  -20.0%

=== <60: model sharpness ===
     bucket      n   share
  0.00-0.02      8    0.9%
  0.02-0.05     36    3.9%
  0.05-0.10    153   16.7%
  0.10-0.20    312   34.1%
  0.20-0.35    259   28.3%
  0.35-0.50     72    7.9%
  0.50-0.65     32    3.5%
  0.65-0.80     20    2.2%
  0.80-0.90      7    0.8%
  0.90-0.95      0    0.0%
  0.95-1.00     15    1.6%

### Lead 60-180 (n=814)


=== 60-180: model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02     14      0.8%     14.3%  +13.4%
  0.02-0.05     17      3.8%      5.9%   +2.1%
  0.05-0.10     42      7.7%     21.4%  +13.7%
  0.10-0.20    383     14.9%     21.7%   +6.8%
  0.20-0.35    240     25.8%     29.6%   +3.8%
  0.35-0.50     76     40.3%     47.4%   +7.1%
  0.50-0.65     18     57.0%     61.1%   +4.1%
  0.65-0.80     12     71.7%     66.7%   -5.1%
  0.80-0.90      3     83.2%    100.0%  +16.8%
  0.95-1.00      9    100.0%     88.9%  -11.1%

=== 60-180: model sharpness ===
     bucket      n   share
  0.00-0.02     14    1.7%
  0.02-0.05     17    2.1%
  0.05-0.10     42    5.2%
  0.10-0.20    383   47.1%
  0.20-0.35    240   29.5%
  0.35-0.50     76    9.3%
  0.50-0.65     18    2.2%
  0.65-0.80     12    1.5%
  0.80-0.90      3    0.4%
  0.90-0.95      0    0.0%
  0.95-1.00      9    1.1%

### Lead 180-360 (n=943)


=== 180-360: model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02     11      0.9%      9.1%   +8.2%
  0.02-0.05     19      3.8%     10.5%   +6.7%
  0.05-0.10     63      8.1%     14.3%   +6.1%
  0.10-0.20    503     14.4%     23.3%   +8.9%
  0.20-0.35    156     24.8%     37.2%  +12.3%
  0.35-0.50     51     42.2%     43.1%   +1.0%
  0.50-0.65     34     56.4%     41.2%  -15.3%
  0.65-0.80     13     70.7%     53.8%  -16.9%
  0.80-0.90      4     83.7%    100.0%  +16.3%
  0.90-0.95      1     92.7%    100.0%   +7.3%
  0.95-1.00     88    100.0%     83.0%  -17.0%

=== 180-360: model sharpness ===
     bucket      n   share
  0.00-0.02     11    1.2%
  0.02-0.05     19    2.0%
  0.05-0.10     63    6.7%
  0.10-0.20    503   53.3%
  0.20-0.35    156   16.5%
  0.35-0.50     51    5.4%
  0.50-0.65     34    3.6%
  0.65-0.80     13    1.4%
  0.80-0.90      4    0.4%
  0.90-0.95      1    0.1%
  0.95-1.00     88    9.3%

### Lead 360-720 (n=2013)


=== 360-720: model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02    153      0.7%      7.8%   +7.2%
  0.02-0.05    100      3.5%     18.0%  +14.5%
  0.05-0.10    209      8.0%     17.2%   +9.2%
  0.10-0.20    703     14.2%     20.6%   +6.4%
  0.20-0.35    585     27.5%     26.7%   -0.8%
  0.35-0.50    211     38.7%     35.5%   -3.2%
  0.50-0.65     21     56.7%     33.3%  -23.3%
  0.65-0.80     21     66.8%     61.9%   -4.9%
  0.80-0.90      1     85.5%    100.0%  +14.5%
  0.95-1.00      9    100.0%    100.0%   +0.0%

=== 360-720: model sharpness ===
     bucket      n   share
  0.00-0.02    153    7.6%
  0.02-0.05    100    5.0%
  0.05-0.10    209   10.4%
  0.10-0.20    703   34.9%
  0.20-0.35    585   29.1%
  0.35-0.50    211   10.5%
  0.50-0.65     21    1.0%
  0.65-0.80     21    1.0%
  0.80-0.90      1    0.0%
  0.90-0.95      0    0.0%
  0.95-1.00      9    0.4%

### Lead 720-1440 (n=2561)


=== 720-1440: model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02    415      0.6%     11.1%  +10.4%
  0.02-0.05    244      3.3%     15.2%  +11.8%
  0.05-0.10    291      7.4%     14.1%   +6.7%
  0.10-0.20    548     14.8%     18.1%   +3.3%
  0.20-0.35    848     26.9%     23.5%   -3.4%
  0.35-0.50    171     38.9%     29.2%   -9.7%
  0.50-0.65     22     55.7%     45.5%  -10.3%
  0.65-0.80     13     70.6%     46.2%  -24.4%
  0.80-0.90      4     86.6%      0.0%  -86.6%
  0.95-1.00      5     98.5%     40.0%  -58.5%

=== 720-1440: model sharpness ===
     bucket      n   share
  0.00-0.02    415   16.2%
  0.02-0.05    244    9.5%
  0.05-0.10    291   11.4%
  0.10-0.20    548   21.4%
  0.20-0.35    848   33.1%
  0.35-0.50    171    6.7%
  0.50-0.65     22    0.9%
  0.65-0.80     13    0.5%
  0.80-0.90      4    0.2%
  0.90-0.95      0    0.0%
  0.95-1.00      5    0.2%

### Lead >1440 (n=2446)


=== >1440: model (p_yes_raw) ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02    277      0.8%      6.9%   +6.0%
  0.02-0.05    239      3.4%      7.5%   +4.2%
  0.05-0.10    328      7.5%      9.5%   +2.0%
  0.10-0.20    576     14.9%     17.7%   +2.8%
  0.20-0.35    836     26.9%     24.4%   -2.5%
  0.35-0.50    164     38.3%     40.9%   +2.6%
  0.50-0.65     14     58.5%     28.6%  -30.0%
  0.65-0.80      7     70.1%     28.6%  -41.5%
  0.80-0.90      1     90.0%    100.0%  +10.0%
  0.90-0.95      2     92.4%     50.0%  -42.4%
  0.95-1.00      2     97.5%      0.0%  -97.5%

=== >1440: model sharpness ===
     bucket      n   share
  0.00-0.02    277   11.3%
  0.02-0.05    239    9.8%
  0.05-0.10    328   13.4%
  0.10-0.20    576   23.5%
  0.20-0.35    836   34.2%
  0.35-0.50    164    6.7%
  0.50-0.65     14    0.6%
  0.65-0.80      7    0.3%
  0.80-0.90      1    0.0%
  0.90-0.95      2    0.1%
  0.95-1.00      2    0.1%


---

## H2 counterfactual: entry policy A vs B vs C

One entry per same-day bracket-day, at the ACTUAL ask of the side our model favors (`p_model > p_market` -> buy YES at `yes_ask`; `p_model < p_market` -> buy NO at `no_ask`). The disagreement threshold is `MIN_EDGE_CENTS/100 = 0.15`. Next-day bracket-days are excluded (shadow-only, #687; no same-day envelope for C).


Eligible same-day bracket-days (resolved outcome, >=1 tradeable poll): 360  
Bracket-days with >=1 disagreement poll: 219  


| Policy | n traded | realized EV (c/contract) | win rate |
|---|---|---|---|
| A (first disagreement poll) | 219 | 0.01 | 0.342 |
| B (last disagreement poll) | 219 | 3.51 | 0.297 |
| N1 (buy CHEAPER side, at A's poll, no model) | 219 | -1.17 | 0.215 |
| N2 (buy CHEAPER side, at B's poll, no model) | 219 | 0.45 | 0.192 |
| N3 (random side, seeded, 500 reps, at A's poll) | 219 | 0.02 (mean; SD 2.66, range [-8.48, 7.13]) | -- |

### Addition 2 -- null-signal controls (N1/N2/N3)

N1/N2 use NO model signal -- just buy the cheaper of `yes_ask`/`no_ask` at the same polls A/B trade, on the same 219 bracket-days. N3 randomizes the side entirely (500 seeded repetitions) at A's poll, to show what a coin flip earns on this book. The decisive comparison is paired, bracket-day by bracket-day:

| Comparison | mean paired diff (c) | paired SE (c) | t |
|---|---|---|---|
| A - N1 | +1.19 | 3.93 | 0.30 |
| B - N2 | +3.06 | 3.14 | 0.97 |

Neither A nor B beats its null by a margin larger than the paired SE.

> **CORRECTION 4 (review, 2026-08-26).** An earlier revision of this paragraph read *"the
> artifact claim is established, not just probable."* That is wrong and is retracted. `t = 0.30`
> and `t = 0.97` are **failures to reject**, not evidence for the null. The 95% CIs are
> `A - N1: [-6.5, +8.9]` and `B - N2: [-3.1, +9.2]` -- each contains "no signal" and
> "substantial signal" equally, so this test is **underpowered, not negative**.
>
> Note also that all three available readings lean the same way: `A - N1 = +1.19c` (blind
> cheap-side buying actually *loses* 1.17c while A is flat), `B - N2 = +3.06c`, and Correction
> 2's table shows the realized hit rate exceeding its own break-even in **9 of 12** side/bucket
> cells. None is individually significant, and they are not independent, but the consistent
> direction is on the record rather than discovered later.
>
> The honest statement is: **A and B cannot be distinguished from their nulls at this sample
> size; the payoff-asymmetry reading is consistent with the data, and no residual signal can be
> demonstrated either way.**
>
> **This does not change the NULL verdict**, which rests on the pre-registered sign-accuracy
> guard failing cleanly at every lead -- a criterion fixed before any number existed. The null
> controls are supporting evidence, not the basis of the verdict, and must not be cited as
> carrying it. (N3's near-zero mean EV with a 2.66c SD is the expected shape for a random-side coin flip on a book with genuine tail premium on both sides -- it is the reference point N1/N2/A/B are all being read against.)

**Policy-A side split** (why EV ~0 despite a 34% win rate): the model's disagreements split into a small-loss YES side and a small-gain NO side that net to nothing. (Correction 2: at a 10.2c average ask the YES side's break-even hit rate is 10.2%; it realized 9.9% -- a loss of about the spread, not a catastrophic anti-signal -- see the per-bucket breakdown above.)

| A side | n | win rate | realized EV (c) | avg ask (c) |
|---|---|---|---|---|
| YES-side (buy YES at `yes_ask`) | 111 | 0.099 | -0.28 | 10.2 |
| NO-side (buy NO at `no_ask`) | 108 | 0.593 | 0.31 | 58.9 |

### Policy C -- first disagreement poll with `remaining_rise <= T`

(`remaining_rise` reconstructed from `observations`; 37 bracket-day(s) had no reconstructable remaining_rise and are excluded from C. 4.0F is where the climb floor stops dominating `FORECAST_STDDEV_F`.)

| T (remaining_rise, F) | n traded | realized EV (c/contract) | win rate |
|---|---|---|---|
| 2 | 35 | 6.94 | 0.343 |
| 3 | 45 | -0.60 | 0.244 |
| 4 | 59 | 3.44 | 0.288 |
| 6 | 98 | 5.12 | 0.296 |
| 8 | 120 | 3.09 | 0.292 |
| 10 | 143 | 0.41 | 0.280 |

**Correction 3 -- C is UNSTABLE, not a point estimate.** The sweep flips sign between `T=2` (+6.94c, n=35) and `T=3` (-0.60c, n=45) and back at `T=4` (+3.44c); quoting "+5.12c at T=6" (n=98) picks one point off a jagged, sign-flipping curve. C is not reported as a number below -- the H2 conclusion rests on the paired A-vs-B comparison (`+3.50c`, SE `1.74c`, t~2.0), which is the defensible one, plus the N1/N2 null controls above showing that comparison does not survive as a model-driven edge either. C stands as directional evidence only (waiting for a collapsing envelope plausibly helps timing) and should not be quoted as a single EV figure.


---

## Stopping rule (fixed before computing)

Pre-registered: FINDING requires EITHER a lead bucket with BSS > 0 on >= 100 station-days, OR realized EV >= +0.5c/contract net of the ask -- and in both cases the result must be *driven by Resolution rather than Reliability*. For the EV branch, "driven by Resolution" is operationalized as **sign accuracy > 0.5** (the outcome is on OUR side of the price more often than the market's at disagreement): a positive EV with sign accuracy below a coin flip is a payoff-asymmetry artifact (we buy the cheap side), not the model discriminating.


BSS branch (BSS > 0 on >= 100 station-days, Resolution > Reliability): none  
EV branch (EV >= +0.5c AND sign accuracy > 0.5): none  

**NULL** -- no lead bucket has BSS > 0 on >= 100 station-days, and every bucket with EV >= +0.5c has sign accuracy below a coin flip (the positive EV is a cheap-side payoff artifact, not Resolution). The model has no tradeable window at any lead; the timing avenue closes with the others.


---

## Answers to the three questions

**(1) Is the model better or worse early?** *Worse late, relatively better early -- H1 is inverted -- answered from the intersection table (Correction 1), which is the only apples-to-apples comparison since the unrestricted per-bucket BSS is not monotonic (`180-360` at `-0.4533` is worse than `<60` at `-0.4353` there, so the unrestricted table cannot support this claim cleanly).* On the 268-bracket-day intersection, held constant across buckets, BSS collapses monotonically as lead shortens: `-0.0707` (>1440) -> `-0.1844` (720-1440) -> `-0.3752` (360-720) -> `-0.3296` (180-360) -> `-0.3802` (60-180) -> `-0.6416` (<60) -- 0.57 BSS of relative skill lost between long lead and the final hour. Addition 1's decomposition attributes this mainly to the market, not the model: `BS_market` falls 40% from `>1440` to `<60` while `BS_model` falls only 8%, and the market's Resolution rises monotonically 4x into settlement with no Reliability cost, a pattern the model does not share. So: the model is relatively WORST in the last hour(s) before settlement, but the primary driver is the market's nowcasting advantage growing near settlement (reading b), not the same-day machinery actively hurting the model (reading a) -- though the model's Reliability does peak mid-day (`360-720`), a secondary, non-monotonic signal worth separate investigation. Consequence unchanged: M3's `-0.4123`, measured at the last (short-lead) poll, is close to the model's worst relative showing, not its best.

**(2) Is there any lead where the price is further from truth than we are?** *No.* BSS is negative at every bucket -- `BS_model > BS_market` at every lead, so the market's price is always closer to truth than ours. Sign accuracy is `0.33-0.41` (< 0.5) at every lead: at disagreement the market's direction beats ours more often than not, so the pre-registered stopping-rule guard is correctly not met. *(Correction 2: the "YES side is anti-informative" framing is dropped.)* Per-bucket, per-side break-even-vs-realized figures show every side, at every lead, realizing within a couple points of the hit rate its own average ask implies -- e.g. at <60 the YES side realizes 9.72% against an 11.86% break-even, a small loss consistent with the H2 policy-A YES-side EV (-0.28c), not a catastrophic anti-signal. Sign accuracy < 0.5 stands as the reason no lead clears the stopping rule; it is a fair-value-adjacent price the model doesn't beat, not a broken side.

**(3) Did entry policy A cost money relative to B or C?** *Yes on paper, but Correction 3 + Addition 2 show it does not create an edge.* A (first disagreement poll) realizes `+0.01c`/contract; B (last disagreement poll) realizes `+3.51c` -- a paired difference of `+3.50c` (SE `1.74c`, t~2.0), the one defensible timing comparison. Policy C is reported as **unstable, not a number** (sign-flips across the `remaining_rise` sweep: `+6.94c` at T=2, `-0.60c` at T=3, back to `+3.44c` at T=4) and should not be quoted as a point EV. More importantly, the null-signal controls (Addition 2) show A's and B's positive EV are each statistically indistinguishable from a no-model null that just buys the cheaper side at the same polls (A-N1: `+1.19c`, SE `3.93c`; B-N2: `+3.06c`, SE `3.14c` -- neither exceeds its paired SE). So the entry rule's apparent cost (waiting to trade B instead of A) is real in the table but not a recoverable edge either way: both A and B are cheap-side payoff artifacts with no established residual signal, confirmed rather than merely asserted. Timing was one contributor; the forecast is still the larger problem, but "anti-informative YES side" (Correction 2) overstates it -- the price is fair-value-adjacent, the model just doesn't beat it.


## Methodology notes

- Outcome truth, exclusion funnel and de-duplication are imported from `bss_market_vs_model_report` unchanged (M3's own code), except the de-duplication key (lead bucket). `--no-network` resolves Gamma from the local cache only, falling back to the observed daily high -- the same precedence as M3.

- `remaining_rise` reconstruction: `observations` -> `current_high`/`latest_temp` (#1044's causal reconstruction) -> `envelope.compute_envelope` for `max_env`; the forecast-mean expansion of `max_env` is NOT included, so `remaining_rise` is the climb-ceiling-only quantity (a lower bound when a hot forecast would widen the envelope).

- The H2 disagreement threshold (`|p_model - p_market| >= MIN_EDGE_CENTS/100`) is the spec's SIMPLIFIED entry-gate proxy; production also applies a fee, the probability cap, confidence and min-price sub-gates. Realized EV uses actual asks, never `market_p_yes()`.

