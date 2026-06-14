# Epic — Per-Station Shadow Tier & Unified Promotion Pipeline

**Date:** 2026-06-14
**Status:** Planned (Ready for issue creation)
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
per-station **shadow** tier, surfaced on the existing stations page, with real-vs-shadow
performance tracked separately per side so it can drive promotion decisions.

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

## Decision — binary per-station status

Per-station status collapses to a **binary toggle: `enabled` ↔ `shadow`**.

| Status | Polls / observes | Live orders | Shadow trades logged (YES + NO) |
|---|---|---|---|
| **enabled** | ✅ | ✅ (within active hours) | only YES when `ENABLE_YES_TRADES=false` |
| **shadow**  | ✅ | ❌ | ✅ both sides |

A truly dark station (no polling at all) is achieved by **removing it from `STATIONS`** —
no DB state required.

### Why no `disabled` tier

The previously-considered third tier (`disabled` = observe but never trade) is redundant
once `shadow` exists, and **strictly worse** for our purposes:

- Demoting a bad station (e.g. RKSI) is now `enabled → shadow`. That keeps the data warm
  **and** keeps the win-rate signal alive, so we can see if/when it recovers and re-promote.
  The old `disabled` went dark on the trade signal.
- The only thing `disabled` did that `shadow` does not is "stop simulating too" — handled
  by dropping the station from `STATIONS`.

### Candidate-is-shadow rule

```
shadow = (station.status == 'shadow') OR (side == 'YES' AND not ENABLE_YES_TRADES)
```

This generalizes the existing YES-only behavior: an **enabled** station still shadow-logs
its YES side while `ENABLE_YES_TRADES=false`; a **shadow** station shadow-logs both sides.

### Implementation: reuse the existing bool

Redefine `station_overrides.enabled`: `1` = live (**enabled**), `0` = **shadow**. No new
enum, minimal schema churn. The env seed `DISABLED_STATIONS` migrates to `SHADOW_STATIONS`
(keep `DISABLED_STATIONS` as a back-compat alias). DB override wins over the env baseline,
as today.

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

---

## Settlement — NO-side shadow

Shadow settlement currently assumes YES ($1 notional, win if the YES bracket is hit).
It must branch on the existing `trades.side` column so NO shadow rows settle correctly
(NO wins when the bracket is missed). $1 notional for both sides; rows stay excluded from
live P&L.

---

## Issue breakdown

| # | Issue | Area | Complexity | Depends on |
|---|---|---|---|---|
| 1 | **Binary station status (enabled ↔ shadow) + scanner.** Redefine `station_overrides.enabled` (1=live, 0=shadow). Migrate `DISABLED_STATIONS` → `SHADOW_STATIONS` shadow seed (alias kept). Scanner shadow-logs **both** YES + NO candidates on shadow stations; drop the old no-trade branch. | backend | Mid | — |
| 2 | **Exclude `mode='shadow'` from live P&L & win-rate.** Filter shadow out of `status()` capital, `_today_pnl`, `_compute_win_rate`, and `stations()` totals. Correctness prerequisite for surfacing shadow stations. | backend | Simple | 1 |
| 3 | **NO-side shadow settlement.** Branch shadow settlement on `side`; $1 notional both sides. Extend `test_settle_shadow.py` / `test_db_shadow.py`. | backend | Mid | 1 |
| 4 | **Bulk-import 39 archive cities as shadow.** Port station tuples + IANA TZ + active hours from `archive/polymarket-shadow/config.py` into `src/config.py` `STATIONS` at shadow; verify °C bracket parsing; handle the Hong Kong omission; deprecate `archive/polymarket-shadow` once parity confirmed. | backend | Mid | 1 |
| 5 | **Promotion metrics — 2×2 mode×side endpoint.** Per-station `{real,shadow}×{YES,NO}`: count, win rate, P&L, avg entry price, days of data. Extend `/api/stations/overview`. | backend | Mid | 1, 2, 3 |
| 6 | **Stations page: enabled/shadow toggle + 2×2 perf table.** Status badge, live toggle (PATCH → DB override, no restart), real/shadow × YES/NO performance columns, promotion-readiness indicator. **Designer review required (frontend).** | frontend | Mid | 5 |

**Sequencing:** #1 → #2 → (#3, #4 in parallel) → #5 → #6.

---

## Acceptance criteria (epic-level)

- [ ] A station's status is `enabled` or `shadow`, settable from the dashboard with no restart.
- [ ] Shadow stations collect observations and log **both** YES and NO shadow trades; they
      place no orders.
- [ ] No `mode='shadow'` row ever affects live capital, today's P&L, or live win rate.
- [ ] NO-side shadow rows settle correctly ($1 notional, win on bracket miss).
- [ ] The stations page shows, per station, a 2×2 `{real,shadow}×{YES,NO}` breakdown of
      count, win rate, P&L, and avg entry price.
- [ ] The 39 non-US cities run in-app as shadow stations; `archive/polymarket-shadow` is
      deprecated.
- [ ] Demoting a station is `enabled → shadow`; it keeps collecting data and signal.

---

## Out of scope / Backlog

- **EMOS feed (fast-follow epic).** A shadow station produces exactly what EMOS calibration
  needs — (forecast, realized-obs) pairs per city — independent of whether trades are real.
  A city can be EMOS-calibrated *while in shadow*, so its `emos_shadow → emos_primary`
  promotion runs in parallel with `shadow → enabled` station promotion. Guardrail: EMOS
  trains on **forecast error, not trade P&L**; the hypothetical shadow P&L must never feed
  it (they are already separated). Logged in Backlog so it is not forgotten.
- `archive/improved_spike.py` and `archive/early-spike-results-may-2026/` are older artifacts
  unrelated to the shadow loop and are left untouched.
