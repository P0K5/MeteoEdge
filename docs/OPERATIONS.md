# MeteoEdge Operations Guide

This document describes operational procedures for MeteoEdge — tasks that require
human judgment and manual execution, rather than automated pipeline steps.

---

## EMOS Retrain Procedure

**Rule: Whenever `FORECAST_STACK` changes, retrain EMOS before promoting the new stack to live.**

EMOS (Ensemble Model Output Statistics) coefficients correct the raw forecast mean and spread
for each city. They are trained per `(city, forecast_source)` pair. When a new forecast stack
lands (e.g. HRRR/NBM for US cities, ECMWF/ICON for international), the existing coefficients
are stale — they were fitted on a different input distribution.

### When to retrain

- When `FORECAST_STACK` changes (controlled by `#437`/`#444`).
- After ≥ 30 days of `intraday_corrections` rows exist for the new stack.
- Before setting `ready_for_promotion=1` on any coefficients.
- Before calling `set_active_source()` for any city.

### Step 1 — Verify sufficient data

```bash
python - <<'EOF'
from src.data.db import Database
from src.model.emos_calibration import MIN_TRAINING_ROWS

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"  # change to your new stack identifier

with Database() as db:
    for city in CITIES:
        n = db._conn.execute(
            "SELECT COUNT(*) FROM intraday_corrections WHERE city=?", (city,)
        ).fetchone()[0]
        status = "OK" if n >= MIN_TRAINING_ROWS else f"INSUFFICIENT ({n}/{MIN_TRAINING_ROWS})"
        print(f"  {city}: {n} rows — {status}")
EOF
```

Do not proceed to Step 2 until all cities show `OK`.

### Step 2 — Fit coefficients

```bash
python - <<'EOF'
from src.data.db import Database
from src.model.emos_calibration import fit_emos, save_coefficients, InsufficientDataError

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"  # change to your new stack identifier

with Database() as db:
    for city in CITIES:
        try:
            fit = fit_emos(db, city=city, forecast_source=SOURCE)
            save_coefficients(db, city=city, forecast_source=SOURCE,
                              a=fit.a, b=fit.b, c=fit.c, d=fit.d, crps_score=fit.crps_score)
            print(f"  {city}: fitted CRPS={fit.crps_score:.4f} (shadow, not yet promoted)")
        except InsufficientDataError as e:
            print(f"  {city}: SKIP — {e}")
EOF
```

All rows are stored with `model_mode='emos_shadow'` and `ready_for_promotion=0`.
The live correction path is unchanged until Step 4.

### Step 3 — Validate calibration

Review CRPS scores and compare with the legacy coefficients for the same cities:

```bash
python - <<'EOF'
from src.data.db import Database

SOURCE = "hrrr_nbm"
LEGACY = "nws_open_meteo"

with Database() as db:
    print("New stack vs legacy CRPS:")
    rows = db._conn.execute(
        "SELECT city, forecast_source, crps_score, fitted_at FROM emos_calibration ORDER BY city, forecast_source"
    ).fetchall()
    for r in rows:
        print(f"  {r['city']:20s}  {r['forecast_source']:20s}  CRPS={r['crps_score']:.4f}  fitted={r['fitted_at'][:10]}")
EOF
```

**Promotion criteria (all must be met):**
- New CRPS ≤ legacy CRPS + 0.5°F for every city in scope.
- No city has `InsufficientDataError` — all rows fitted.
- PnL / win-rate gate in `#437`/`#444` passes (separate check).

If any city fails, investigate the training data before proceeding.

### Step 4 — Promote (manual, human-in-the-loop)

Only after Step 3 criteria are met:

```bash
python - <<'EOF'
from src.data.db import Database
from src.model.emos_calibration import check_ready_for_promotion
from src.model.emos_mode import set_active_source

CITIES = ["Chicago", "Miami", "Los Angeles", "Atlanta", "Houston"]
SOURCE = "hrrr_nbm"

with Database() as db:
    # Mark each city as ready_for_promotion=1
    for city in CITIES:
        db._conn.execute(
            "UPDATE emos_calibration SET ready_for_promotion=1 WHERE city=? AND forecast_source=?",
            (city, SOURCE),
        )
    db._conn.commit()

    # Verify gate
    if not check_ready_for_promotion(db, SOURCE, CITIES):
        print("ERROR: promotion gate failed — some cities missing")
    else:
        # Switch active source
        for city in CITIES:
            set_active_source(db, city, SOURCE)
        print(f"Promoted {SOURCE} for all cities. Live corrections now use new coefficients.")
EOF
```

### Step 5 — Post-promotion monitoring

- Watch `emos_calibration.crps_score` vs. trailing observed CRPS for the first 7 days.
- If live CRPS degrades significantly vs. shadow, roll back by calling `set_active_source(db, city, "nws_open_meteo")` for affected cities.
- Do not delete old coefficients — they remain in the table and can be re-activated.

### Notes

- Legacy `nws_open_meteo` coefficients are **never deleted**. The `(city, forecast_source)` PK
  ensures rows for different sources coexist independently.
- EMOS sigma retrain (for new ensemble spread inputs from #449) follows the same procedure
  but targets the `c`/`d` parameters specifically. If both #449 and a stack change land near
  each other, a single pass of `fit_emos` covers both axes simultaneously.
- Do not run `fit_emos` in CI or in the live trading loop. It is an offline, operator-initiated step.

---

## Shadow Trade Review

Shadow trades are stored with `mode='shadow'` and never sent to the exchange.
Review them weekly via:

```bash
python - <<'EOF'
from src.data.db import Database
with Database() as db:
    rows = db._conn.execute(
        "SELECT station, direction, COUNT(*) as n, "
        "AVG(CASE WHEN outcome='win' THEN 1.0 ELSE 0.0 END) as win_rate "
        "FROM trades WHERE mode='shadow' AND created_at >= date('now', '-7 days') "
        "GROUP BY station, direction"
    ).fetchall()
    for r in rows:
        print(f"  {r['station']:6s}  {r['direction']:4s}  n={r['n']:3d}  win_rate={r['win_rate']:.1%}")
EOF
```

Shadow-to-live promotion criteria (per station/direction):
- ≥ 5 shadow trades
- Win rate ≥ 55%
- Minimum 3 calendar days of data
