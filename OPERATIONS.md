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

---

## Station Shadow→Live Promotion (issue #559)

**This is the sole promotion path for moving a shadow station+side to live trading.**
It supersedes the ad-hoc thresholds proposed in issue #80 (`>=5 trades, 100% win
rate, >=3 days`, taken from an old `src/config.py` comment) — that bar was gameable
by noise (a handful of coin-flip wins reads as "100%") and never accounted for
trading costs. Issue #80 is **not closed**; see "Relationship to issue #80" below.

Do not confuse this with the **Forecast Stacks promotion gate** above — that gate
promotes a *forecast ingestion stack* (HRRR/NBM, ECMWF/ICON) based on backtested
MAE improvement. This section promotes a *station+side from shadow to live
trading* based on settled shadow P&L statistics.

### The rule

Implemented in `src/model/promotion_gate.py` (`compute_promotion_bar()`) and
surfaced on the dashboard's **Promotion** tab (`GET /api/promotion-bar`).

For each **station+side** with settled shadow trades, the tool computes:

- `n` — settled shadow trade count
- `win_rate` — wins / n
- `wilson_lower_bound` — the lower bound of the 95% Wilson score confidence
  interval on the win rate (see `wilson_lower_bound()`). The Wilson interval
  is used instead of the raw win rate specifically because it degrades
  gracefully for small `n` — a 5/5 or 9/9 streak reports a much lower bound
  than its headline win rate, which is what makes the #80 loophole
  statistically indefensible.
- `breakeven_win_rate` — the win rate required to break even at the station's
  average settled entry price, derived from `src/strategy/fee.py`'s taker fee
  model (`estimate_fee_cents()`), **not a bare 0.5**:
  `breakeven = (avg_entry_price_cents + fee_cents) / 100`.
- `days_coverage`, `avg_entry_price_cents`, `price_valid` (average entry price
  at/above `MIN_PRICE_CENTS` — below that, shadow data may not reflect live
  conditions; see the ZSPD comment in `src/config.py`).

**A station+side is eligible ⇔** `n >= PROMOTION_MIN_SETTLED_TRADES` **AND**
`wilson_lower_bound(wins, n) > breakeven_win_rate(avg_entry_price)`.

The dashboard reports one of three statuses per row:

| Status | Meaning |
|---|---|
| 🟢 `green` (ELIGIBLE) | Clears the bar: enough settled trades, Wilson lower bound beats break-even, and the average entry price is valid. |
| 🟡 `amber` (WATCH) | Directionally clears break-even but either `n` is still below the minimum, or the average entry price is below `MIN_PRICE_CENTS` (data-quality caveat). |
| 🔴 `red` (NOT YET) | No settled trades yet, or the Wilson lower bound does not clear break-even — the station+side has not demonstrated a statistically defensible edge net of costs. |

### Config

| Key | Default | Effect |
|---|---|---|
| `PROMOTION_MIN_SETTLED_TRADES` | `30` | Minimum settled shadow trades required for eligibility. |
| `PROMOTION_WILSON_CONFIDENCE` | `0.95` | Confidence level for the Wilson score lower bound. |
| `MIN_PRICE_CENTS` | `60` | Reused from the live entry gate to flag shadow data as price-valid. |

Editable via the dashboard **Config** tab (group: `promotion`) or directly in
the DB:

```bash
sqlite3 data/meteoedge.db "UPDATE bot_config SET value='30' WHERE key='PROMOTION_MIN_SETTLED_TRADES';"
```

### Advisory only — this tool never promotes anything

`compute_promotion_bar()` and `/api/promotion-bar` are **read-only**. They do
not write to `station_overrides`, `SHADOW_STATIONS`, or any other live-trading
config, and no automation acts on their output. A human (operator or Tech
Lead PM) must review the dashboard, judge whether the eligible station+side
also passes the data-coverage prerequisites (`/api/promotion-prerequisites` —
climb-rate history, ≥2 forecast models, TAF coverage, secondary observation
source, at least one settled loss), and then flip the station live manually,
e.g.:

```bash
sqlite3 data/meteoedge.db "UPDATE station_overrides SET no_enabled=1 WHERE station='WSSS';"
```

### Relationship to issue #80

Issue #80 proposed `scripts/station_promotion_check.py` (a standalone script)
plus the `>=5 trades / 100% WR / >=3 days` threshold and a
`/analytics/station_promotions` endpoint. Issue #559 supersedes the
*threshold* with the statistically defensible bar above, and the dashboard
surface with `/api/promotion-bar`. Issue #80 remains open — whether its
standalone script is still wanted (e.g. as a CLI wrapper around
`compute_promotion_bar()` for use outside the dashboard) is left as an open
question for the Tech Lead PM; see the comment thread on #80.
