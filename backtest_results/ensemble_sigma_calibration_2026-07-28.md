# Ensemble-Sigma vs Fixed-Sigma EMOS Calibration Backtest

**Run date:** 2026-07-28  
**model_forecast_log window:** 2026-06-24 to 2026-07-28 (31 distinct dates)  
**Issue:** #450 (parent #445)  

---

## Recommendation: HOLD

CRPS delta (-0.0041) is within noise of fixed-sigma -- not enough signal yet to promote or kill.

---

## Methodology

- **Truth**: `fetch_training_data` joins each forecast date to `db.get_daily_obs_high` -- the post-#741 observations path (Open-Meteo fallback rows excluded). `settlements` is never read, decoupling this backtest from #867's Gamma wrong-market-read contamination.
- **Fit**: per (city, sigma_source), the SAME partial-pooling blend `scripts/run_emos_shadow.py` uses (issue #798) -- pure per-city `fit_emos` at >= 60 own triples, else a shrinkage blend against the city's cross-station pooling group, excluded entirely below 5 own triples (or if the pooling group is also thin).
- **This is an in-sample fit-then-score**, matching `emos_crps_log`'s own existing convention (see `scripts/run_emos_shadow.py::_persist`), NOT a walk-forward backtest. Numbers below should be read as "how well does each track's best fit describe the window it was fit on", not as an out-of-sample skill estimate.
- **Reliability diagram**: each calibrated triple is decomposed into 1-degree-F synthetic brackets spanning +/-4 sigma around the calibrated mean, scored with `p_normal_between` (the same primitive `next_day_probability_yes` uses in live trading) -- this avoids any dependency on real Polymarket bracket boundaries or settlement data.
- Regime: baseline stack (nws, open_meteo); lead_hours=24 (default).
- **Why per-station CRPS/sigma deltas are often tiny even though the raw sigma_f inputs genuinely differ per date**: `fit_emos` optimises (c, d) to minimise CRPS against each track's OWN residuals, so it partially re-absorbs whatever raw sigma proxy it is handed -- two tracks with different `sigma_raw` scales can converge to similar `sigma_cal` outputs simply because that is close to the CRPS-minimising spread for that city's residual distribution either way. A near-zero per-station delta is therefore a genuine result of EMOS's own recalibration, not a sign the backtest failed to pick up a real difference in `sigma_f`.

---

## Aggregate CRPS

| Track | Cities fitted | Cities excluded | Triples (n) | Mean CRPS |
|---|---|---|---|---|
| ensemble | 27 | 3 | 726 | 1.2692 |
| fixed | 27 | 3 | 726 | 1.2733 |
| **delta (ensemble - fixed)** | | | | **-0.0041** (negative = ensemble better) |

## Aggregate Sharpness (calibrated sigma, deg F -- narrower = sharper)

| Track | Mean sigma | Median sigma |
|---|---|---|
| ensemble | 2.159 | 2.132 |
| fixed | 2.142 | 2.128 |
| **delta** | **+0.017** | | 

## Aggregate Reliability + Brier (synthetic bracket decomposition, 0.05-wide buckets)

Bracket-probability pairs: ensemble n=13268, fixed n=13182.

=== Reliability -- ensemble sigma ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02   6615      0.4%      0.5%   +0.1%
  0.02-0.05   1578      3.3%      2.0%   -1.4%
  0.05-0.10   1645      7.4%      6.1%   -1.2%
  0.10-0.20   3156     14.8%     15.4%   +0.5%
  0.20-0.35    274     21.4%     25.5%   +4.1%


=== Reliability -- fixed sigma ===
  raw_p_yes      n mean_pred  obs_YES%     gap
  0.00-0.02   6562      0.4%      0.5%   +0.2%
  0.02-0.05   1572      3.3%      2.1%   -1.2%
  0.05-0.10   1604      7.4%      6.0%   -1.4%
  0.10-0.20   3223     15.0%     15.6%   +0.6%
  0.20-0.35    221     21.4%     24.0%   +2.6%

Brier score (ensemble): 0.0467  
Brier score (fixed): 0.0470  

---

## Per-station breakdown

Stations with >= 60 triples get their own reliability read below (0.10-wide buckets -- still coarser than the aggregate's 0.05 to keep bins populated). Everything else: CRPS/sharpness only -- per-station 0.05-bin reliability at these sample sizes is exactly the noise-as-signal problem that deferred this issue three times; not presented.

| City | n (ensemble) | Provenance (ens) | CRPS (ens) | CRPS (fixed) | Sigma (ens) | Sigma (fixed) | Reliability readable? |
|---|---|---|---|---|---|---|---|
| Ankara | 28 | blended(w=0.47, group=midlat_c) | 0.8775 | 0.8907 | 1.868 | 1.752 | no (thin) |
| Atlanta | 27 | blended(w=0.45, group=us_f) | 1.2146 | 1.2146 | 2.217 | 2.217 | no (thin) |
| Busan | 28 | blended(w=0.47, group=midlat_c) | 0.9461 | 0.9468 | 2.050 | 2.054 | no (thin) |
| Chicago | 27 | blended(w=0.45, group=us_f) | 2.2090 | 2.2090 | 3.068 | 3.068 | no (thin) |
| Guangzhou | 27 | blended(w=0.45, group=tropics_c) | 1.4752 | 1.4797 | 2.502 | 2.507 | no (thin) |
| Helsinki | 28 | blended(w=0.47, group=midlat_c) | 1.1497 | 1.1522 | 2.122 | 2.123 | no (thin) |
| Houston | 27 | blended(w=0.45, group=us_f) | 1.4070 | 1.4070 | 2.290 | 2.290 | no (thin) |
| Istanbul | 28 | blended(w=0.47, group=midlat_c) | 0.7948 | 0.8017 | 1.824 | 1.777 | no (thin) |
| Jeddah | 27 | blended(w=0.45, group=tropics_c) | 1.3680 | 1.3595 | 2.443 | 2.286 | no (thin) |
| Jinan | 0 | excluded (no training data) | n/a | n/a | n/a | n/a | no (thin) |
| Kuala Lumpur | 28 | blended(w=0.47, group=tropics_c) | 1.1150 | 1.1267 | 2.135 | 2.153 | no (thin) |
| London | 28 | blended(w=0.47, group=midlat_c) | 1.7720 | 1.7749 | 2.393 | 2.359 | no (thin) |
| Los Angeles | 27 | blended(w=0.45, group=us_f) | 1.1583 | 1.1583 | 2.245 | 2.245 | no (thin) |
| Manila | 27 | blended(w=0.45, group=tropics_c) | 1.2253 | 1.2581 | 2.169 | 2.128 | no (thin) |
| Miami | 27 | blended(w=0.45, group=us_f) | 0.8593 | 0.8593 | 1.896 | 1.896 | no (thin) |
| Milan | 28 | blended(w=0.47, group=midlat_c) | 1.3741 | 1.3816 | 2.054 | 1.979 | no (thin) |
| Panama City | 27 | blended(w=0.45, group=tropics_c) | 1.1046 | 1.1035 | 2.080 | 2.090 | no (thin) |
| Paris | 28 | blended(w=0.47, group=midlat_c) | 1.6626 | 1.6729 | 2.244 | 2.227 | no (thin) |
| Sao Paulo | 26 | blended(w=0.43, group=tropics_c) | 1.4285 | 1.4330 | 2.221 | 2.233 | no (thin) |
| Seoul | 28 | blended(w=0.47, group=midlat_c) | 2.3281 | 2.3300 | 2.393 | 2.392 | no (thin) |
| Shanghai | 27 | blended(w=0.45, group=midlat_c) | 1.0779 | 1.0768 | 1.849 | 1.848 | no (thin) |
| Shenzhen | 14 | blended(w=0.23, group=tropics_c) | 1.9079 | 1.9170 | 2.258 | 2.250 | no (thin) |
| Singapore | 28 | blended(w=0.47, group=tropics_c) | 0.8996 | 0.8998 | 2.050 | 2.065 | no (thin) |
| Taipei | 27 | blended(w=0.45, group=midlat_c) | 1.2328 | 1.2436 | 2.064 | 2.029 | no (thin) |
| Tel Aviv | 27 | blended(w=0.45, group=midlat_c) | 0.8757 | 0.8769 | 1.686 | 1.686 | no (thin) |
| Tokyo | 27 | blended(w=0.45, group=midlat_c) | 1.1658 | 1.1679 | 2.211 | 2.217 | no (thin) |
| Warsaw | 28 | blended(w=0.47, group=midlat_c) | 1.1269 | 1.1261 | 2.047 | 2.048 | no (thin) |
| Wellington | 27 | blended(w=0.45, group=midlat_c) | 0.8214 | 0.8239 | 1.987 | 1.985 | no (thin) |
| Wuhan | 0 | excluded (no training data) | n/a | n/a | n/a | n/a | no (thin) |
| Zhengzhou | 0 | excluded (no training data) | n/a | n/a | n/a | n/a | no (thin) |

### Per-station reliability (stations clearing the readable-bin threshold)

No station has >= 60 triples yet -- every per-station reliability table would be bin-thin. See the aggregate reliability diagram above for the pooled signal.

---

## emos_crps_log cross-check (production shadow-run evidence)

| sigma_source | model_mode | n_rows | n_cities | min_date | max_date | avg_crps |
|---|---|---|---|---|---|---|
| ensemble | emos_shadow | 81 | 27 | 2026-07-25 | 2026-07-27 | 1.2982 |
| ensemble | legacy | 81 | 27 | 2026-07-25 | 2026-07-27 | 2.0267 |
| fixed | emos_shadow | 453 | 30 | 2026-07-09 | 2026-07-25 | 1.4896 |
| fixed | legacy | 423 | 30 | 2026-07-10 | 2026-07-25 | 2.0768 |

**Caveat**: the `ensemble` sigma_source track in `emos_crps_log` only spans the days since `USE_ENSEMBLE_SIGMA` was flipped on for the shadow runner (recent) -- it is thin by construction, NOT because ensemble-sigma training data itself is scarce. `model_forecast_log.sigma_f` has been populated since 2026-06-24, so this backtest's own fit above uses the full 31-date window regardless of when the shadow runner's flag flipped.

