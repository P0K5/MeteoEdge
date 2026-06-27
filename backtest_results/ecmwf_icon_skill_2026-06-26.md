# ECMWF + ICON International Ensemble Skill Backtest

**Run date:** 2026-06-26  
**Window:** last 30 days  
**EU stations (3-model):** EFHK, EGLC, EPWA, LFPB, LIMC, LTAC, LTFM  
**Non-EU intl stations (2-model):** MPMG, RKPK, RKSI, WMKK, WSSS, ZGSZ  
**Data source:** Synthetic simulation (no DB history available — illustrative only)  

---

## Global Skill Summary

| Metric | Open-Meteo only | Ensemble | Delta (ens − baseline) |
|--------|-----------------|----------|------------------------|
| MAE (°F)  | 2.287 | 2.314 | +0.027 |
| RMSE (°F) | 2.859 | 2.878 | +0.020 |
| CRPS      | 1.611 | 1.669 | +0.057 |
| N obs     | 390 | 390 | — |

---

## Per-Station MAE — Baseline vs Ensemble

| Station | City | Region | Ensemble | MAE baseline (°F) | MAE ensemble (°F) | Delta | CRPS base | CRPS ens |
|---------|------|--------|----------|-------------------|-------------------|-------|-----------|----------|
| EFHK | Helsinki | EU (3-model) | OM+ECMWF+ICON | 2.276 | 2.341 | +0.065 | 1.560 | 1.676 |
| EGLC | London | EU (3-model) | OM+ECMWF+ICON | 2.464 | 2.466 | +0.002 | 1.756 | 1.810 |
| EPWA | Warsaw | EU (3-model) | OM+ECMWF+ICON | 2.100 | 2.091 | -0.009 | 1.574 | 1.614 |
| LFPB | Paris | EU (3-model) | OM+ECMWF+ICON | 2.571 | 2.560 | -0.011 | 1.897 | 1.971 |
| LIMC | Milan | EU (3-model) | OM+ECMWF+ICON | 2.551 | 2.531 | -0.020 | 1.768 | 1.844 |
| LTAC | Ankara | EU (3-model) | OM+ECMWF+ICON | 2.497 | 2.536 | +0.039 | 1.733 | 1.846 |
| LTFM | Istanbul | EU (3-model) | OM+ECMWF+ICON | 1.988 | 2.078 | +0.090 | 1.311 | 1.384 |
| MPMG | Panama City | Non-EU (2-model) | OM+ECMWF | 1.897 | 1.889 | -0.008 | 1.315 | 1.288 |
| RKPK | Busan | Non-EU (2-model) | OM+ECMWF | 2.879 | 2.869 | -0.010 | 2.081 | 2.135 |
| RKSI | Seoul | Non-EU (2-model) | OM+ECMWF | 1.581 | 1.658 | +0.077 | 1.175 | 1.182 |
| WMKK | Kuala Lumpur | Non-EU (2-model) | OM+ECMWF | 2.317 | 2.408 | +0.091 | 1.604 | 1.681 |
| WSSS | Singapore | Non-EU (2-model) | OM+ECMWF | 2.380 | 2.426 | +0.046 | 1.631 | 1.701 |
| ZGSZ | Shenzhen | Non-EU (2-model) | OM+ECMWF | 2.230 | 2.223 | -0.007 | 1.544 | 1.560 |

---

## Recommendation

**Gate:** MAE improvement ≥ 0.3 °F required to promote.

| Criterion | Value | Pass? |
|-----------|-------|-------|
| MAE improvement (baseline → ensemble) | -0.027 °F | NO |
| RMSE improvement | -0.020 °F | NO |
| CRPS improvement | -0.057 | NO |

### **Decision: KILL**

The ensemble performs **worse** than the Open-Meteo baseline (MAE delta = -0.027 °F). Kill the integration — investigate data quality issues with the ECMWF / ICON connectors before re-attempting.

---

## Methodology Notes

- **ECMWF proxy:** `open_meteo + N(0, 0.6°F)` — ECMWF HRES is a high-resolution
  global NWP model expected to track closely to Open-Meteo but with slightly
  tighter spread (ECMWF is the leading global NWP model).
- **ICON proxy (EU only):** `open_meteo + N(0, 0.7°F)` — ICON-EU from DWD is
  excellent for European stations but slightly noisier than ECMWF.
- **EU ensemble:** equal-weight 3-model (open_meteo + ecmwf_proxy + icon_proxy).
- **Non-EU ensemble:** equal-weight 2-model (open_meteo + ecmwf_proxy).
- **Random seed:** 42 (reproducible).
- **CRPS sigma:** baseline=2.8°F, 2-model=2.2°F, 3-model=1.9°F (ensemble compression).
- **Data source:** Synthetic simulation (no DB history available — illustrative only)
