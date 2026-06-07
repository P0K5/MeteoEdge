# Epic 2 — Live Observation Report

**Period:** 2026-05-08 19:29 UTC → 2026-05-10 20:54 UTC (~49 hours)  
**Mode:** Paper trading (no real orders placed)  
**Author:** André Freixo  
**Verdict:** ✅ GO — proceed to Epic 3

---

## 1. System uptime

| Metric | Result | Target | Pass? |
|---|---|---|---|
| Total runtime | ~49 hours | ≥ 48h | ✅ |
| Scans completed | 537 | — | ✅ |
| Avg scan interval | 5.3 min | 5 min | ✅ |
| Missed polls | 0 | 0 | ✅ |
| Unhandled crashes | 0 | 0 | ✅ |
| Snapshot entries | 111,634 | ≥ 100 | ✅ |

The process ran on a remote Ubuntu machine (Mac Mini) managed via `systemctl`. No interruptions across the full 49-hour window.

---

## 2. Market coverage

| Metric | Result | Target | Pass? |
|---|---|---|---|
| Stations monitored | 9 | 9 | ✅ |
| Avg snapshots per scan | ~198 | — | ✅ |
| Markets parsed successfully | >99% | >90% | ✅ |
| Edges flagged per day | ~300–500 raw / ~115–144 unique | ≥ 10 | ✅ |

Stations: KLGA, KORD, KMIA, KAUS, KLAX, KATL, KHOU, KSFO, KSEA.  
All stations produced candidates on every day of the run.

---

## 3. Settlement results (3 days)

Settlements were scored against 48-hour METAR history using `src/scripts/settle.py`.  
Deduplication applied: one entry per (date × station × bracket) — the same bracket can be flagged across many scans; only the first is counted as a trade.

### By day

| Date | Unique trades | Win rate | PnL (€5/trade) | Capital |
|---|---|---|---|---|
| Start | — | — | — | €500.00 |
| 2026-05-08 | 43 | 95.3% | +€63.70 | €563.70 |
| 2026-05-09 | 144 | 88.2% | +€227.45 | €791.15 |
| 2026-05-10 | 115 | 91.3% | +€200.40 | €991.55 |
| **Total** | **302** | **90.4%** | **+€491.55** | **€991.55** |

Starting capital €500, position size €5/trade, daily loss limit €50, drawdown stop 15%.  
Neither the daily loss limit nor the drawdown stop was triggered on any day.

### By side

| Side | Trades | Win rate | PnL |
|---|---|---|---|
| NO | 275 | **94.2%** | €453.45 |
| YES | 27 | **51.9%** | €38.10 |

The NO side is the reliable edge source. YES calls are inconsistent (36%–67% day-to-day) and barely above chance in aggregate — they will be **disabled for Epic 3**.

### By station

| Station | Trades | Win rate | PnL |
|---|---|---|---|
| KLAX | 40 | 100% | €83.20 |
| KMIA | 37 | 100% | €73.50 |
| KORD | 36 | 97.2% | €72.20 |
| KHOU | 37 | 89.2% | €62.85 |
| KAUS | 26 | 100% | €62.65 |
| KATL | 33 | 87.9% | €46.10 |
| KLGA | 38 | 78.9% | €35.90 |
| KSEA | 30 | 76.7% | €32.55 |
| KSFO | 25 | 80.0% | €22.60 |

---

## 4. Bugs found and fixed during the run

### 4.1 Confidence clamp (`src/model/envelope.py`)

**Symptom:** 15 candidate rows had `flagged_confidence > 1.0` (range 1.004–1.061), clustered in a ~30-minute window each day around 11:00–11:42 UTC.

**Root cause:** `time_to_settlement_boost()` adds a nudge proportional to `(p - 0.5)` when fewer than 60 minutes remain to settlement. When `p` was already close to 1.0, the nudge pushed the result above 1.0.

**Fix:** Clamped the return value to `[0.0, 1.0]` in `src/model/envelope.py`. Committed in `b35a588`.

### 4.2 KSFO removed from active stations (`src/config.py`)

**Symptom:** KSFO produced a 64.5% raw win rate and -€21.76 net PnL over two days — the only station with negative expected value. On a unique-trade basis the NO side was only 64.8% accurate vs 92%+ for all other stations.

**Root cause:** Structural — San Francisco's marine layer and coastal fog create temperature variance that NWS + Open-Meteo forecasts consistently misprice.

**Fix:** KSFO commented out of `STATIONS` in `src/config.py`, same pattern as the earlier KBKF/KDAL removal. Committed in `50c36c6`.

---

## 5. Manual spot-checks

Five candidates were reviewed to verify physical plausibility:

| # | Station | Bracket | Side | Market price | Model view | Verdict |
|---|---|---|---|---|---|---|
| 1 | KORD | 64°F–∞ YES | YES | 42¢ | Current high already 65°F → p=1.0 | ✅ Correct — market lagging reality |
| 2 | KSEA | 66°F–∞ NO | NO | 6¢ | Forecast high 58°F, no path to 66°F | ✅ Correct — obvious NO |
| 3 | KHOU | 86°F–∞ NO | NO | 18¢ | Forecast 82°F, marine influence low | ✅ Correct — solid NO |
| 4 | KLGA | -50–63°F YES | YES | 1¢ | Current high 62.4°F settling in bracket | ✅ Correct — catch-all underpriced |
| 5 | KSFO | 58–59°F YES | YES | 44¢ | Forecast 63°F → bracket miss | ❌ Wrong — model overconfident on narrow bracket |

Spot-check 5 confirmed the KSFO YES problem already identified in settlements.

---

## 6. Known issues and mitigations for Epic 3

| Issue | Severity | Mitigation |
|---|---|---|
| YES side 51.9% win rate | High | Disable YES trades in Epic 3 until ≥ 7 days of settlement data available |
| KSEA weak at 76.7% | Medium | Keep active, watch for 3 more days before deciding |
| KLGA weak at 78.9% | Medium | Keep active, concentrated in YES calls which are already disabled |
| Confidence clamp bug | Fixed | Deployed `b35a588` |
| KSFO marine bias | Fixed | Station removed in `50c36c6` |
| `settle.py` hardcoded to yesterday | Low | Date arg added in `b35a588` — backfill with `python3 -m src.scripts.settle YYYY-MM-DD` |
| Cron for daily settlement | Low | Crontab entry added on Ubuntu: `0 8 * * *` |

---

## 7. Go / no-go recommendation

**GO.**

The 90.4% win rate across 302 unique trades over 3 live days significantly exceeds the 55–70% live expectation documented in `docs/IMPLEMENTATION_PLAN.md`. The NO side alone (94.2%, 275 trades) is a reliable, consistent edge. Capital doubled in 3 days of paper trading with risk limits never threatened.

Proceed to Epic 3 with the following constraints:

- **Week 1 capital cap: €100** (as specified in Epic 3)
- **NO side only** — disable YES trades via `MIN_CONFIDENCE_YES = 1.1` (unreachable threshold) or a dedicated flag until YES win rate is validated over ≥ 7 days
- **Position size: €5/trade** — consistent with this paper trading run
- **Monitor KSEA and KLGA** — if either drops below 70% win rate over the next week, recalibrate or remove

### Epic 3 entry checklist

- [ ] Polymarket API key obtained (polymarket.com → wallet → API settings)
- [ ] E3-1: `src/execution/auth.py` implemented and health-checked
- [ ] E3-2: `src/execution/live_trader.py` implemented and testnet-verified
- [ ] E3-3: Order lifecycle wired into `run.py`
- [ ] YES trades disabled in config for Week 1
