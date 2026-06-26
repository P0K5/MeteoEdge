# Operations Reference

## Environment Variables

### Data Retention

| Variable | Default | Component | Effect |
|---|---|---|---|
| `SNAPSHOT_RETAIN_DAYS` | `365` | `src/utils/log_rotation.py` | How many days of snapshot JSONL files (`snapshots.jsonl`, `position_snapshots.jsonl`) to retain before deletion. Overrides `LOG_ROTATION_RETAIN_DAYS` for snapshot and position-snapshot log files only. |
| `ARCHIVE_DB_PATH` | `data/analytics.db` | `src/data/archive_db.py` | Path to the analytics SQLite database used by the archive pipeline. |

---

## Forecast Stacks

### FORECAST_STACK config flag

The `FORECAST_STACK` DB config key controls which ingestion channels are active:

| Value | Channels active | Use case |
|---|---|---|
| `baseline` | NWS + Open-Meteo | Default. Safe fallback. |
| `hrrr_nbm` | + HRRR + NBM (US stations) | After HRRR/NBM pass the promotion gate. |
| `intl_ecmwf_icon` | + ECMWF + ICON-EU (international stations) | After ECMWF/ICON pass the promotion gate. |
| `full` | All channels | Both stacks live. |

Change via the dashboard config panel or directly in the DB:

```bash
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='baseline' WHERE key='FORECAST_STACK';"
```

---

### HRRR + NBM stack

**Run cadence:**
- HRRR: hourly runs (t00z–t23z). Ingested by `src/data/hrrr_ingest.py`.
- NBM: issued 4× daily (~00/06/12/18z). Ingested by `src/data/nbm_ingest.py`.
- Forecasts are written to `model_forecast_log` with `model='hrrr'` / `model='nbm'`.

**Failure modes:**
- NOMADS GRIB server outage: HRRR/NBM ingestion silently skips the cycle. The bot falls back to NWS + Open-Meteo for the affected hours. Check `logs/bot.log` for `[WARN] HRRR fetch failed` messages.
- Stale GRIB cache: delete `.grib_cache/` to force a fresh fetch on the next cycle.
- Missing `model_forecast_log` rows: run `python -m src.scripts.hrrr_nbm_backtest` to verify data presence.

**Roll back:**
```bash
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='baseline' WHERE key='FORECAST_STACK';"
```

---

### ECMWF + ICON-EU stack

**Run cadence:**
- ECMWF Open Data: 2× daily (00z + 12z). Ingested by `src/data/ecmwf_ingest.py`.
- ICON-EU (DWD): 8× daily (every 3 h). Ingested by `src/data/icon_ingest.py`.
- Forecasts are written to `model_forecast_log` with `model='ecmwf'` / `model='icon'`.
- Applies to international stations only (EU: EGLC, LFPB, LIMC, EFHK, EPWA, LTFM, LTAC; non-EU: RKSI, WMKK, RKPK, ZGSZ, WSSS, MPMG).

**Failure modes:**
- ECMWF Open Data S3 outage: ingestion skips the cycle. The bot falls back to Open-Meteo for affected international stations.
- DWD ICON server throttling: icon_ingest retries up to 3× with exponential backoff. Persistent failures are logged as `[WARN] ICON fetch failed`.
- Missing rows: run `python -m src.scripts.ecmwf_icon_backtest` to verify data presence.

**Roll back:**
```bash
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='baseline' WHERE key='FORECAST_STACK';"
```

---

### Promotion gate procedure

Run after at least 30 days of live ingestion data has accumulated.

**Step 1 — Re-run backtests with real DB data:**
```bash
python -m src.scripts.hrrr_nbm_backtest
python -m src.scripts.ecmwf_icon_backtest
```
Reports are saved to `backtest_results/`.

**Step 2 — Check the gate:**
```bash
python -m src.scripts.check_promotion_gate
```
Prints `PROMOTE / HOLD / KILL` for each stack. Exit code 0 = all pass, 1 = any fail.

**Step 3 — Promote if gate passes (MAE improvement ≥ 0.3 °F):**
```bash
# Promote HRRR+NBM only:
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='hrrr_nbm' WHERE key='FORECAST_STACK';"

# Promote ECMWF+ICON only:
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='intl_ecmwf_icon' WHERE key='FORECAST_STACK';"

# Promote both:
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='full' WHERE key='FORECAST_STACK';"
```

**Step 4 — Monitor for 7 days** after promotion. Watch per-station MAE in the dashboard DEB panel. Roll back immediately if MAE regresses by > 0.5 °F vs pre-promotion baseline.
