# Envelope (max_env) Sweep Tradability Report (issue #1069, M3c)

**Run date:** 2026-08-26  
**DOES NOT REOPEN M3 OR M3b.** M3's verdict (BSS = -0.4123) and M3b's verdict (FAIL, concentrated -- #1063/#1064) both stand, final. See the module docstring (`src/scripts/envelope_sweep_tradability_report.py`) and `docs/REMEDIATION_PLAN.md`'s "M3c" section.  

**No live trading regardless of result.** #1053/#1054's halt is unconditional and unaffected by this report either way.  

**Window:** rows polled on or after `2026-08-06`, restricted to the pre-registered held-out split -- fit `2026-08-06..2026-08-15`, evaluate `2026-08-16..2026-08-25`.  


---

## Amendment -- diagnostic-first sequencing

Per the Tech Lead PM's amendment on issue #1069: a cheap price-distribution diagnostic runs before the full EV machinery, with a legitimate stop-early path. This does NOT change the pre-registered FINDING/NULL stopping rule in `docs/REMEDIATION_PLAN.md` -- it only sequences the work so an unsatisfiable-by-construction sweep is caught early.

## Population funnel

| Stage | Count |
|---|---|
| De-duplicated, in-window bracket-days | 6374 |
| Same-day/high-direction (in scope) | 6374 |
| Reconstructed (current_high/latest_temp/now_local) | 6149 |
| Resolved to an outcome | 6149 |
| Unresolvable (dropped, never guessed) | 0 |
| Station-days (resolved) | 559 |

## V0 control check (must reproduce M3b before scoring other variants)

Reconstructed V0 class (pooled fit+evaluate, this module's own geometric reclassification -- see module docstring): n=4490, n_resolved=4490, observed YES=0.9%. M3b reference (#1063/#1064, full population, no direction restriction): n~=4368, observed YES~=1.0%.

**Control check: OK**

## Variant V0 -- Current p95 CLIMB_LOOKUP -- CONTROL

### Fit half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 110 | 5.0% | 0 | 0.0% |
| 96 | 11 | 0.5% | 0 | 0.0% |
| 97 | 4 | 0.2% | 0 | 0.0% |
| 98 | 26 | 1.2% | 0 | 0.0% |
| 99 | 2050 | 93.1% | 0 | 0.0% |
| **total** | **2201** | -- | **0** | -- |

### Evaluate half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 113 | 4.9% | 0 | 0.0% |
| 96 | 13 | 0.6% | 0 | 0.0% |
| 97 | 7 | 0.3% | 0 | 0.0% |
| 98 | 42 | 1.8% | 0 | 0.0% |
| 99 | 2114 | 92.4% | 0 | 0.0% |
| **total** | **2289** | -- | **0** | -- |

## Variant V1 -- p90 climb (DB-derived, all available observations)

### Fit half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 118 | 5.7% | 45 | 38.1% |
| 96 | 11 | 0.5% | 3 | 27.3% |
| 97 | 6 | 0.3% | 2 | 33.3% |
| 98 | 31 | 1.5% | 9 | 29.0% |
| 99 | 1910 | 92.0% | 35 | 1.8% |
| **total** | **2076** | -- | **94** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 118 | 60 | yes |
| 96 | 11 | 75 | **unpassable at this n** |
| 97 | 6 | 100 | **unpassable at this n** |
| 98 | 31 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

### Evaluate half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 129 | 5.9% | 52 | 40.3% |
| 96 | 16 | 0.7% | 6 | 37.5% |
| 97 | 7 | 0.3% | 3 | 42.9% |
| 98 | 38 | 1.7% | 6 | 15.8% |
| 99 | 1986 | 91.3% | 31 | 1.6% |
| **total** | **2176** | -- | **98** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 129 | 60 | yes |
| 96 | 16 | 75 | **unpassable at this n** |
| 97 | 7 | 100 | **unpassable at this n** |
| 98 | 38 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

## Variant V2 -- p85 climb (DB-derived, all available observations)

### Fit half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 144 | 6.7% | 61 | 42.4% |
| 96 | 12 | 0.6% | 3 | 25.0% |
| 97 | 6 | 0.3% | 2 | 33.3% |
| 98 | 33 | 1.5% | 10 | 30.3% |
| 99 | 1959 | 90.9% | 47 | 2.4% |
| **total** | **2154** | -- | **123** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 144 | 60 | yes |
| 96 | 12 | 75 | **unpassable at this n** |
| 97 | 6 | 100 | **unpassable at this n** |
| 98 | 33 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

### Evaluate half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 157 | 7.0% | 72 | 45.9% |
| 96 | 18 | 0.8% | 6 | 33.3% |
| 97 | 8 | 0.4% | 3 | 37.5% |
| 98 | 42 | 1.9% | 9 | 21.4% |
| 99 | 2024 | 90.0% | 35 | 1.7% |
| **total** | **2249** | -- | **125** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 157 | 60 | yes |
| 96 | 18 | 75 | **unpassable at this n** |
| 97 | 8 | 100 | **unpassable at this n** |
| 98 | 42 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

## Variant V3 -- Anomaly-conditioned climb (fit-window observations only)

### Fit half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 129 | 6.1% | 59 | 45.7% |
| 96 | 10 | 0.5% | 1 | 10.0% |
| 97 | 6 | 0.3% | 2 | 33.3% |
| 98 | 38 | 1.8% | 16 | 42.1% |
| 99 | 1947 | 91.4% | 59 | 3.0% |
| **total** | **2130** | -- | **137** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 129 | 60 | yes |
| 96 | 10 | 75 | **unpassable at this n** |
| 97 | 6 | 100 | **unpassable at this n** |
| 98 | 38 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

### Evaluate half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 147 | 6.8% | 73 | 49.7% |
| 96 | 16 | 0.7% | 5 | 31.2% |
| 97 | 8 | 0.4% | 3 | 37.5% |
| 98 | 43 | 2.0% | 9 | 20.9% |
| 99 | 1960 | 90.2% | 31 | 1.6% |
| **total** | **2174** | -- | **121** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 147 | 60 | yes |
| 96 | 16 | 75 | **unpassable at this n** |
| 97 | 8 | 100 | **unpassable at this n** |
| 98 | 43 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

## Variant V4 -- p95 regenerated from accumulated observations (scratch, never written to src/data/climb_lookup.py)

### Fit half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 93 | 4.7% | 38 | 40.9% |
| 96 | 11 | 0.6% | 3 | 27.3% |
| 97 | 6 | 0.3% | 2 | 33.3% |
| 98 | 26 | 1.3% | 5 | 19.2% |
| 99 | 1839 | 93.1% | 20 | 1.1% |
| **total** | **1975** | -- | **68** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 93 | 60 | yes |
| 96 | 11 | 75 | **unpassable at this n** |
| 97 | 6 | 100 | **unpassable at this n** |
| 98 | 26 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

### Evaluate half

| no_ask bucket | class n | class share | newly-certain n | newly-certain share of class bucket |
|---|---|---|---|---|
| <=95 | 85 | 4.1% | 27 | 31.8% |
| 96 | 14 | 0.7% | 5 | 35.7% |
| 97 | 7 | 0.3% | 3 | 42.9% |
| 98 | 35 | 1.7% | 6 | 17.1% |
| 99 | 1935 | 93.2% | 27 | 1.4% |
| **total** | **2076** | -- | **68** | -- |

**Certification adequacy (sub-99 buckets), this half:**

| bucket | n | n needed to certify (rule-of-three) | thickened? |
|---|---|---|---|
| <=95 | 85 | 60 | yes |
| 96 | 14 | 75 | **unpassable at this n** |
| 97 | 7 | 100 | **unpassable at this n** |
| 98 | 35 | 150 | **unpassable at this n** |

*"Unpassable at this n" means even zero adverse events could not clear that bucket's breakeven at the 95% upper bound -- a sample-size finding, not evidence against the edge.*

## Stop-early decision (evaluate half only)

**CONTINUE.** At least one variant materially thickened a sub-99 `no_ask` bucket on the evaluate half -- proceeding to the full pre-registered EV analysis below.

---

## Full EV analysis (evaluate half decides the verdict)

### V1 -- p90 climb (DB-derived, all available observations)

Class n (evaluate half): 2176; newly-certain n: 98.

Concentration: total priced EV 3443.0 over 2176 contracts.

**Verdict: NULL**

TOTAL held-out EV: V0=3734.0, V1=3443.0 (< +25% required). Below-breakeven buckets: ['99', '98', '97'] (>= 3 required). Newly-certain observed YES 16.3% vs. V0 class observed YES 0.8% (WORSE).

### V2 -- p85 climb (DB-derived, all available observations)

Class n (evaluate half): 2249; newly-certain n: 125.

Concentration: total priced EV 3405.0 over 2249 contracts.

**Verdict: NULL**

TOTAL held-out EV: V0=3734.0, V2=3405.0 (< +25% required). Below-breakeven buckets: ['99', '98'] (< 3 required). Newly-certain observed YES 17.6% vs. V0 class observed YES 0.8% (WORSE).

### V3 -- Anomaly-conditioned climb (fit-window observations only)

Class n (evaluate half): 2174; newly-certain n: 121.

Concentration: total priced EV 3141.0 over 2174 contracts.

**Verdict: NULL**

TOTAL held-out EV: V0=3734.0, V3=3141.0 (< +25% required). Below-breakeven buckets: ['99', '98'] (< 3 required). Newly-certain observed YES 16.5% vs. V0 class observed YES 0.8% (WORSE).

### V4 -- p95 regenerated from accumulated observations (scratch, never written to src/data/climb_lookup.py)

Class n (evaluate half): 2076; newly-certain n: 68.

Concentration: total priced EV 2807.0 over 2076 contracts.

**Verdict: NULL**

TOTAL held-out EV: V0=3734.0, V4=2807.0 (< +25% required). Below-breakeven buckets: ['99', '98', '97'] (>= 3 required). Newly-certain observed YES 11.8% vs. V0 class observed YES 0.8% (WORSE).
