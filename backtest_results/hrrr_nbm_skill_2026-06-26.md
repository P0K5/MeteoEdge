# HRRR + NBM Ensemble Skill Backtest

**Run date:** 2026-06-26  
**Window:** last 30 days  
**Stations:** KATL, KHOU, KLAX, KMIA, KORD  
**Data source:** Synthetic simulation (no DB history available — illustrative only)  

---

## Global Skill Summary

| Metric | NWS-only | 2-model ensemble | 4-model ensemble | Delta (4m − NWS) |
|--------|----------|-----------------|-----------------|-----------------|
| MAE (°F)  | 1.915 | 1.580 | 1.663 | -0.252 |
| RMSE (°F) | 2.425 | 1.982 | 2.085 | -0.340 |
| CRPS      | 1.368 | 1.116 | 1.194 | -0.174 |
| N obs     | 150 | 150 | 150 | — |

---

## Per-Station MAE — Before vs After

| Station | City | MAE NWS-only (°F) | MAE 4-model (°F) | Delta | CRPS NWS | CRPS 4m |
|---------|------|------------------|-----------------|-------|----------|---------|
| KATL | Atlanta | 2.154 | 1.534 | -0.620 | 1.528 | 1.103 |
| KHOU | Houston | 1.738 | 1.575 | -0.163 | 1.252 | 1.102 |
| KLAX | Los Angeles | 1.838 | 1.755 | -0.083 | 1.301 | 1.276 |
| KMIA | Miami | 1.827 | 1.678 | -0.149 | 1.326 | 1.210 |
| KORD | Chicago | 2.017 | 1.775 | -0.242 | 1.435 | 1.278 |

---

## Edge-Sign Analysis

Candidate days where NWS-only and 4-model ensemble predicted **opposite** market edge directions:

- **Threshold:** ±2 °F from observed actual (proxy for bracket mid)
- **Flipped signs:** 31 / 150 candidate days (20.7%)

Interpretation: on 20.7% of days, adding HRRR + NBM would have changed the
YES/NO call direction. These are the days where model diversity has the highest impact.

---

## Simulated PnL Projection (US Stations)

Methodology: +1 unit profit on days where 4-model ensemble predicts the correct
temperature-change direction (up/down vs prior day); −1 unit on incorrect calls.

| Metric | Value |
|--------|-------|
| Total PnL (units) | +115 |
| Winning days | 130 |
| Losing days | 15 |
| Win rate | 89.7% |

---

## Recommendation

**Gate:** MAE improvement ≥ 0.3 °F required to promote.

| Criterion | Value | Pass? |
|-----------|-------|-------|
| MAE improvement (NWS → 4-model) | +0.252 °F | NO |
| RMSE improvement | +0.340 °F | YES |
| CRPS improvement | +0.174 | YES |

### **Decision: HOLD**

The 4-model ensemble shows marginal improvement (0.252 °F) below the 0.3 °F gate. Hold — collect 30 days of live HRRR + NBM forecasts and re-run this backtest with real data before promoting.

---

## Methodology Notes

- **HRRR proxy:** `open_meteo + N(0, 0.8°F)` — HRRR is a high-resolution NWP model
  expected to track GFS/open_meteo closely but with tighter spread.
- **NBM proxy:** `(NWS + open_meteo)/2 + N(0, 0.5°F)` — NBM blends multiple NWP
  models; modelled here as the average of our two existing sources with small noise.
- **Random seed:** 42 (reproducible).
- **4-model weights:** equal (0.25 each) — DEB has no history for new channels.
- **CRPS sigma:** NWS=2.5°F, 2-model=2.0°F, 4-model=1.6°F (ensemble compression).
- **Data source:** Synthetic simulation (no DB history available — illustrative only)
