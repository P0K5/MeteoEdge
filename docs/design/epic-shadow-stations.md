# Epic — Per-Station Shadow Tier & Unified Promotion Pipeline

**Date:** 2026-06-14
**Status:** Planned (Issues created — #271 through #277)
**Owner:** Tech Lead PM
**Branch:** `claude/pm-agent-shadow-tags-s520eh`

---

## Problem

We evaluate new cities for live trading using a **separate, out-of-band process**
(`archive/polymarket-shadow/`) that runs as a second sibling checkout, never executes
orders, and writes per-(city × bracket × poll) snapshots to flat files. The 39 non-US
candidate cities live only there. Promotion decisions are made by hand-diffing those
files against informal rules encoded as comments in `src/config.py`
(e.g. "≥5 trades, 100% win, ≥3 days"; Shanghai was dropped because shadow entries
averaged ~57¢, below the 60¢ floor).

This is fragile:

- A whole parallel codebase to maintain, deploy, and keep in sync with the live model.
- Candidate cities never appear in the dashboard, so there's no operational visibility.
- Promotion criteria are tribal knowledge in code comments, not a tracked signal.
- There is no clean per-station, per-side view of what is *real* performance versus
  what is merely *shadowed* (hypothetical).

## Goal

Fold candidate-city data collection into the **main execution** as a first-class,
per-station, **per-side shadow** tier, surfaced on the existing stations page, with
real-vs-shadow performance tracked separately per side so it can drive promotion
decisions.

---

## Background — the three existing "shadow" concepts

These already exist and must not be confused. This epic builds on (1) and (2):

1. **Station presence** (`src/config.py`, `src/strategy/scanner.py:415`):
   a station in `STATIONS` is polled and its observations are collected.
   `DISABLED_STATIONS` (env) / the `station_overrides` DB table (binary `enabled` bool)
   currently excludes a station **from new entries only** — observations keep collecting.
   A station *not* in `STATIONS` is never polled.
2. **`mode='shadow'` trade rows** (`src/data/db.py`, `docs/DB_SCHEMA.md` §trades):
   a **side-level, global** mechanism. When `ENABLE_YES_TRADES=False`, YES candidates
   that pass all gates are logged as observational rows ($1 notional, no order, excluded
   from live P&L). This is the "yes analysis" loop.
3. **`emos_shadow`** (`emos_calibration.model_mode`): a per-city *forecasting-model* mode
   — compute EMOS, keep serving legacy. Out of scope here (see EMOS fast-follow).

---

## Decision — per-side, per-station status (Option A: two bool columns)

**Confirmed 2026-06-14.** Per-station status is tracked as **two independent booleans**:
`yes_enabled` and `no_enabled` on `station_overrides`. This replaces the original
binary per-station `enabled ↔ shadow` design.

| `yes_enabled` | `no_enabled` | YES side | NO side |
|---|---|---|---|
| 1 | 1 | Live orders | Live orders |
| 0 | 1 | Shadow-log only | Live orders |
| 1 | 0 | Live orders | Shadow-log only |
| 0 | 0 | Shadow-log only | Shadow-log only |

A truly dark station (no polling at all) is achieved by **removing it from `STATIONS`** —
no DB state required.

### Shadow-per-side rule

```python
shadow_yes = (station.yes_enabled == False) or (not ENABLE_YES_TRADES)
shadow_no  = (station.no_enabled == False)
```

`ENABLE_YES_TRADES=False` remains a global override that forces YES shadow on all
stations regardless of `yes_enabled` — existing global override is preserved.

### Schema change

Add `yes_enabled BOOL DEFAULT 1` and `no_enabled BOOL DEFAULT 1` to `station_overrides`.
The legacy `enabled` bool is deprecated but retained for back-compat migration:
translate `enabled=0` → `yes_enabled=0, no_enabled=0`.

### Env var seeding

- `DISABLED_STATIONS` (back-compat alias) → shadows both sides
- `SHADOW_STATIONS` → shadows both sides
- `SHADOW_STATIONS_YES` → shadows YES side only
- `SHADOW_STATIONS_NO` → shadows NO side only
- DB override wins over env baseline, as today.

### Why not a `disabled` tier

The previously-considered third tier (`disabled` = observe but never trade) is redundant.
Demoting a bad station is now `yes_enabled=0, no_enabled=0`. That keeps the data warm
**and** keeps both win-rate signals alive so we can see if/when either side recovers.

---

## Decision — separate real vs shadow performance (2×2)

**Latent correctness issue this fixes:** `_dashboard_load_trades()` calls
`get_trades(mode=None)` and every downstream aggregation — `status()` capital,
`_today_pnl`, `_compute_win_rate`, `stations()` `total_pnl` — sums P&L across **all**
modes with no filter. The moment shadow stations start writing NO rows with hypothetical
$1-notional P&L, those numbers will pollute *real* capital and win rate.

Performance is tracked as a **2×2 matrix per station** — `{real, shadow} × {YES, NO}` —
each quadrant reporting: count, win rate, P&L, average entry price, days of data.

- **Real** quadrants = live performance accounting (must exclude `mode='shadow'`).
- **Shadow** quadrants = promotion signal.

They never mix. Average entry price is a first-class metric because it is the gate that
removed Shanghai (entries below `MIN_PRICE_CENTS=60` are unprofitable in live conditions
even at a high shadow win rate).

> **Mixed-mode note:** a station may have live rows on one side and shadow rows on the
> other in the same time window. The filter is always on the *row's* `mode` column,
> never on the station's current status.

---

## Settlement — NO-side shadow

Shadow settlement currently assumes YES ($1 notional, win if the YES bracket is hit).
It must branch on the existing `trades.side` column so NO shadow rows settle correctly
(NO wins when the bracket is missed). $1 notional for both sides; rows stay excluded from
live P&L.

---

## Issue breakdown

| # | Issue | GitHub | Area | Complexity | Depends on |
|---|---|---|---|---|---|
| 1 | **Per-side station status (yes_enabled / no_enabled) + scanner.** Add `yes_enabled` and `no_enabled` bool columns. Migrate legacy `enabled`. Env vars `SHADOW_STATIONS`, `SHADOW_STATIONS_YES`, `SHADOW_STATIONS_NO`. Scanner shadow-logs per-side. | [#271](https://github.com/P0K5/MeteoEdge/issues/271) | backend | Mid | — |
| 2 | **Exclude `mode='shadow'` from live P&L & win-rate.** Filter shadow out of `status()` capital, `_today_pnl`, `_compute_win_rate`, and `stations()` totals. Mixed-mode stations handled by row-level mode filter. | [#272](https://github.com/P0K5/MeteoEdge/issues/272) | backend | Simple | #271 |
| 3 | **NO-side shadow settlement.** Branch shadow settlement on `side`; $1 notional both sides. Extend `test_settle_shadow.py` / `test_db_shadow.py`. | [#273](https://github.com/P0K5/MeteoEdge/issues/273) | backend | Mid | #271 |
| 4 | **Bulk-import 39 archive cities as shadow.** Port station tuples + IANA TZ + active hours from `archive/polymarket-shadow/config.py` into `src/config.py` `STATIONS` at `yes_enabled=0, no_enabled=0`; verify °C bracket parsing; handle the Hong Kong omission; deprecate `archive/polymarket-shadow` once parity confirmed. | [#274](https://github.com/P0K5/MeteoEdge/issues/274) | backend | Mid | #271 |
| 5 | **Promotion metrics — 2×2 mode×side endpoint.** Per-station `{real,shadow}×{YES,NO}`: count, win rate, P&L, avg entry price, days of data. Extend `/api/stations/overview`. | [#275](https://github.com/P0K5/MeteoEdge/issues/275) | backend | Mid | #271, #272, #273 |
| 6 | **Stations page: per-side YES/NO toggles + 2×2 perf table.** Independent status badges and live toggles for YES and NO sides. Mixed-state display. Real/shadow × YES/NO performance columns, promotion-readiness indicator per side. **Designer review required (frontend).** | [#276](https://github.com/P0K5/MeteoEdge/issues/276) | frontend | Mid | #275 |
| 7 | **[Backlog] Deprecate remaining archive artifacts.** Review and remove `archive/improved_spike.py`, `archive/early-spike-results-may-2026/`, `archive/polymarket-spike/` after epic merges. | [#277](https://github.com/P0K5/MeteoEdge/issues/277) | backend | — | after #276 |

**Sequencing:** #271 → #272 → (#273, #274 in parallel) → #275 → #276. #277 is post-epic backlog.

---

## Acceptance criteria (epic-level)

- [ ] Each station has independent `yes_enabled` and `no_enabled` flags, settable from the dashboard with no restart.
- [ ] Shadow stations/sides collect observations and log shadow trades; they place no orders.
- [ ] No `mode='shadow'` row ever affects live capital, today's P&L, or live win rate.
- [ ] NO-side shadow rows settle correctly ($1 notional, win on bracket miss).
- [ ] The stations page shows per-side YES/NO status badges, independent toggles, and a 2×2 `{real,shadow}×{YES,NO}` breakdown of count, win rate, P&L, and avg entry price.
- [ ] The 39 non-US cities run in-app as full-shadow stations (`yes_enabled=0, no_enabled=0`); `archive/polymarket-shadow` is deprecated.
- [ ] Demoting a station side is a single toggle; it keeps collecting data and signal.

---

## Out of scope / Backlog

- **EMOS feed (fast-follow epic).** A shadow station produces exactly what EMOS calibration
  needs — (forecast, realized-obs) pairs per city — independent of whether trades are real.
  A city can be EMOS-calibrated *while in shadow*, so its `emos_shadow → emos_primary`
  promotion runs in parallel with station promotion. Guardrail: EMOS trains on
  **forecast error, not trade P&L**; the hypothetical shadow P&L must never feed it.
  Logged in Backlog so it is not forgotten.
- **Archive cleanup (post-epic).** `archive/improved_spike.py`, `archive/early-spike-results-may-2026/`,
  and `archive/polymarket-spike/` are reviewed for removal after the epic merges (#277).
  `archive/improved_spike.py` and `archive/early-spike-results-may-2026/` are older artifacts
  unrelated to the shadow loop and are left untouched until then.
