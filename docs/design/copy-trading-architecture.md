# Copy-Trading Strategy — Architecture & Requirements

Status: draft, informed by spike #1097 / PR #1098. Not yet broken into
leaf implementation issues — see "Epics" below for the issue-level
breakdown and `docs/design/copy-trading-dashboard.md` for the frontend spec.

## Background

The spike (#1097) validated the hypothesis "identify profitable Polymarket
traders and copy their trades" against live data. Finding: naively
mirroring a profitable wallet's trades and position sizes does not hold up
(dominated by tail-bet variance, fragile to the source trader's own bet
sizing). The refined form does: **screen wallets by median ROI + trade
volume, then copy their trade *selection* with flat/capped position sizing
instead of mirroring their bet sizes.** That version was net positive on
7/7 candidate wallets in the final replay, vs. 5/7 under mirrored sizing.

This document scopes what it takes to run that refined strategy for real,
starting in paper mode, alongside (never mixed with) the existing weather
strategy.

## Non-goals (v1)

- Not a latency-accurate market-tape replay of historical trades — the
  150bps flat-slippage assumption from the spike carries forward as the
  paper-mode cost model. Revisit only if paper-mode P&L diverges
  meaningfully from the backtest.
- Not mirrored-size copying. Flat-stake was the only sizing mode that held
  up; v1 does not implement mirrored sizing as a live option.
- Not integrated with the weather strategy's capital, risk limits, or
  trading loop. Fully isolated — see "Isolation" below.
- Not multi-exchange. Polymarket only, same as today.

## Isolation from the weather strategy

The spike was deliberately kept out of `src/scripts/run.py`'s live/paper
loop. v1 keeps that boundary: copy-trading gets its own scheduler loop,
its own capital allocation/env vars, its own kill switch, and its own risk
tables (or a `strategy` discriminator column added to the existing risk
tables — decide in Epic 1, see open questions). A bug or bad screening
result in copy-trading must not be able to affect weather positions, and
vice versa.

The one thing that IS genuinely shared and must be budgeted deliberately:
`src/http_client.py`'s per-domain `DomainRateLimiter` is process-global
(1 req/sec default). If copy-trading runs as a second process hitting
`data-api.polymarket.com` / `gamma-api.polymarket.com`, it either needs
its own rate-limit budget on those hosts or the two strategies need to
agree on a shared ceiling. Flag this explicitly in Epic 1 rather than
discovering it as a production incident.

## Known limitation carried over from the spike

`get_wallet_trades()` (`src/data/polymarket_traders.py`) is capped at
`MAX_TRADE_PAGES * page_size` = 20,000 trades. For wallets whose true
history exceeds that, results are not stable run-to-run (confirmed on
`0xd3b034d7...` in the spike — resolved-trade count and median ROI both
changed dramatically between two runs 15 hours apart). Any wallet the
screening pipeline is about to start following needs either (a) a trade
count comfortably under the cap, or (b) a stability check — re-screen and
require the numbers to hold before following. This is Epic 1's problem to
solve, not something to patch around downstream.

## Backend architecture

Reuses existing infrastructure rather than parallel-building it:

| Concern | Reuse | New |
|---|---|---|
| Rate-limited HTTP | `src/http_client.py` | — |
| Wallet trade tape / leaderboard | `src/data/polymarket_traders.py` | — |
| Market resolution truth | `src/data/polymarket.py::fetch_market_resolution` | — |
| Backtest / screening scoring | `src/scripts/copy_trade_backtest.py` (median ROI, flat-stake) | Scheduled runner, persistence |
| DB schema/migrations | `src/data/db.py` pattern (`CREATE TABLE IF NOT EXISTS` + versioned `_migrate`) | New tables (see below) |
| Order execution | `src/execution/live_trader.py`, `src/paper_trader.py` | Copy-trading signal → order adapter |
| Config | `CONFIG_DEFAULTS` + `_CONFIG_META` pattern (per project convention: new strategy params must reach the dashboard config tab) | Copy-trading param block |
| Dashboard | `src/dashboard/api.py` (FastAPI) + `src/dashboard/static/index.html` (vanilla JS/Chart.js, no framework) | New tab + endpoints |

### Data model (new tables, `src/data/db.py` conventions)

- `copy_wallet_candidates` — one row per (wallet, screening_run): address,
  window, n_resolved, win_rate, mean_roi, median_roi, mirrored $ PnL,
  flat-stake $ PnL, screened_at. Append-only — this is what makes the
  stability check in Epic 1 possible (compare a wallet's row across runs).
- `copy_wallets_followed` — the subset of candidates promoted to "actively
  copy": address, stake_per_trade, status (active/paused), paused_reason,
  added_at.
- `copy_signals` — one row per detected BUY from a followed wallet:
  source trade id, market/condition id, detected_at, order placed (bool),
  skip_reason (rate-limited, market already closed, risk limit hit, etc).
- `copy_positions` / `copy_settlements` — mirror the shape of the existing
  `open_positions` / `settlements` tables if a `strategy` discriminator
  column is added there instead of new tables (decide in Epic 1 — reusing
  the existing tables gets shared risk/PnL rollups for free but couples
  schemas; separate tables keep isolation clean but duplicate settlement
  logic). **Open question, needs a decision before Epic 1 starts.**

### Backend epics

**Epic A — Wallet screening pipeline (productionize the spike).**
Turn `copy_trade_backtest.py`'s logic into a scheduled job that screens
the leaderboard on a cadence, persists every run to
`copy_wallet_candidates`, and implements the stability check (a wallet
must screen consistently across ≥2 runs before it's eligible to follow).
Also resolves the rate-limit-budget question from "Isolation" above.
No live/paper trading in this epic — output is a stored, queryable
candidate list only.

**Epic B — Signal detection & flat-stake execution (paper mode only).**
Poll followed wallets for new BUY trades, generate `copy_signals`, size
each at the configured flat stake, submit through `paper_trader.py`.
Depends on Epic A (need followed wallets) and the config epic (need
stake/risk parameters wired in before anything executes, even in paper
mode).

**Epic C — Settlement & P&L tracking.**
Extend the settlement flow (`settle.py` pattern) to resolve copy-trading
positions and record realized P&L per wallet and in aggregate — this is
what Epic D's dashboard reads. Depends on Epic B.

**Epic D — Risk controls & wallet health monitoring.**
Max exposure per wallet / total, circuit breakers, and an auto-pause rule
tied to Epic A's stability check (if a followed wallet's rolling median
ROI turns negative or a re-screen no longer reproduces, pause it and
record `paused_reason`). Depends on Epic A and C (needs both screening
history and live P&L to evaluate against).

**Epic E — Config & isolation wiring.**
Copy-trading params into `CONFIG_DEFAULTS`/`_CONFIG_META`, a dedicated
kill switch independent of the weather strategy's, and the capital
allocation env vars. This is small but blocking — Epic B cannot go live
(even in paper mode) without it. Should land first or in parallel with
Epic A.

### Frontend epic

**Epic F — Copy-trading dashboard tab.** See
`docs/design/copy-trading-dashboard.md` for the full spec. Summary: a new
tab in the existing dashboard (same FastAPI + vanilla JS/Chart.js stack,
no new framework) showing screened candidates, followed-wallet management,
live copy-trading positions/P&L, and a signal activity feed. Depends on
Epic A for candidate data and Epic C for P&L data; the candidate-browsing
view can ship as soon as Epic A has data, before execution exists.

## Suggested phasing

1. **Epic E** (config/isolation) + **Epic A** (screening pipeline) in
   parallel — neither depends on the other, both block everything else.
2. **Epic F, candidate-browsing view only** — ships as soon as Epic A has
   persisted data, gives a real UI to evaluate screening quality before
   building execution on top of it.
3. **Epic B** (paper execution) once A and E are done.
4. **Epic C** (settlement/P&L) once B is producing positions.
5. **Epic D** (risk/health monitoring) once C gives it something to
   monitor.
6. **Epic F, remaining views** (followed-wallet management, live P&L,
   activity feed) alongside C/D.
7. **Go/no-go gate**: run paper mode for a deliberate observation window,
   compare realized paper P&L to the backtest's flat-stake numbers. Only
   then consider live capital — and given the backtest's own aggregate
   edge was modest (+$13,273 across 7 wallets at $5/trade), start live
   capital small and scale deliberately, not at weather-strategy capital
   levels.

## Open questions (need a decision before Epic 1 starts)

1. Reuse `open_positions`/`settlements`/`risk_state` with a `strategy`
   discriminator column, or fully separate tables for copy-trading? Reuse
   is less duplication; separate tables are cleaner isolation.
2. Rate-limit budget split between the weather strategy and copy-trading
   on `data-api.polymarket.com`/`gamma-api.polymarket.com` if both run as
   separate processes. **Decision:** See `docs/OPERATIONS.md`'s
   `meteoedge-copy-screening.service / .timer` section, "Rate-limit decision".
3. Screening cadence (how often to re-run the leaderboard scan and
   re-validate followed wallets) — the spike's own re-run 15 hours apart
   was enough to catch instability on one wallet; needs a concrete number
   (hourly? daily?) traded off against `data-api.polymarket.com` load.
   **Decision:** Daily at 03:00 UTC. See `docs/OPERATIONS.md`'s
   `meteoedge-copy-screening.service / .timer` section, "Why 03:00 UTC".
4. What "sizing" means beyond flat $5 in v1 — fixed forever, or does the
   Epic D health-monitoring output ever adjust an individual wallet's
   stake (e.g., scale down a wallet whose edge is degrading before fully
   pausing it)? v1 as scoped above is fixed-stake-until-paused; scaling is
   explicitly deferred, flagged here so it isn't silently assumed later.
