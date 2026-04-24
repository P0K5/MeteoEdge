# FundingEdge — Implementation Plan

**Project:** Binance Funding-Rate Arbitrage Bot
**Author:** Tech Lead PM (André / FundingEdge team)
**Date:** 2026-04-24
**Status:** Ready for GitHub issue creation
**Source specs:** `funding-edge-spec.md`, `funding-edge-mvp-spike.md`, `funding-edge-backtest-harness.md`
**Predecessor:** See `archive/` for MeteoEdge implementation plan — many patterns are reused verbatim.

---

## Complexity Legend

| Label | Model level | When to use |
|---|---|---|
| **Easy** | Haiku | Boilerplate, well-defined templates, scripting, config, docs |
| **Mid** | Sonnet | Moderate complexity — integrations, data pipelines, standard modules |

> **No Opus-level stories.** All formerly Hard stories have been decomposed into Mid or Easy sub-stories. The Tech Lead PM provides the exact interface, formula, pseudocode, and test cases in each story so the implementer executes rather than designs.

## Designer Involvement

The only frontend surface is the **local operator dashboard** (FastAPI + HTMX, `localhost:8080`). All dashboard stories require Designer review before implementation and Designer PR approval before merge. No other epics have a visual component.

---

## Critical Path & Epic Dependencies

```
Epic 0 (Spike)  ─────────────────────────────────────────────────────► GATE: proceed?
                                                                              │
Epic 1 (Infra)  ──────────────────────────────────────────────────────────────┤
Epic 9 (Security) ─► (parallel with Epic 1)                                   │
                                                                              │
Epic 2 (Ingestion) ─► depends on Epic 1 ──────────────────────────────────────┤
                                                                              │
Epic 3 (Strategy Engine) ─► depends on Epic 2                                 │
Epic 7 (Backtest) ─► depends on Epic 3 (injectable modules) ──► GATE          │
                                                                              │
Epic 4 (Risk Mgmt + Withdrawal) ─► depends on Epic 3                          │
Epic 5 (LLM Sanity Check) ─► depends on Epic 3 + Epic 4                       │
                                                                              │
Epic 6 (Hedge Execution) ─► depends on Epic 4 + Epic 5                        │
Epic 8 (Operational Tooling) ─► depends on Epic 6 ────────────────────────────┤
                                                                              │
Epic 10 (Rollout) ─► depends on Epic 7 Stage 1 gate pass ◄────────────────────┘
```

**Parallel streams possible from day 1:**
- Stream A: Epic 0 (Spike) → premise validation
- Stream B: Epic 1 + Epic 9 (Infra + Security) → platform foundation

**Critical path (longest chain blocking revenue):**
Epic 0 → Epic 1 → Epic 2 → Epic 3 → Epic 7 → Stage 1 gate → Epic 4 → Epic 5 → Epic 6 → Epic 8 → Epic 10

---

## Epic 0 — MVP Spike (Premise Validation)

**Goal:** Validate that the funding-rate carry edge survives real Binance conditions before committing to the full build. Observe-only, no orders, no database, no services.
**Gate:** ≥ 60% win rate on ≥ 30 virtual hedge cycles over 2 weeks, with positive median net yield.
**Blocks:** All epics (do not invest in the full build if the gate fails).
**Designer needed:** No
**Spike code:** Already designed in `funding-edge-mvp-spike.md` — implementation is largely transcription.

---

### E0-S1: Bootstrap spike environment

**Complexity:** Easy
**Designer needed:** No
**Assigned to:** Junior Dev
**Depends on:** None
**Blocks:** E0-S2, E0-S3, E0-S4

**Description:**
Set up the `fundingedge-spike/` directory structure, Python venv, install dependencies, and create a Binance read-only API key.

**Acceptance criteria:**
- [ ] `fundingedge-spike/` directory exists with layout from spike §4
- [ ] `.venv` created with all dependencies installed (`python-binance`, `httpx`, `pandas`, `python-dateutil`, `pytz`)
- [ ] Binance API key created with **read-only** permissions (trading and withdrawal disabled)
- [ ] `keys/binance_keys.env` exists with `chmod 600`
- [ ] `logs/` directory created
- [ ] Running `python spike.py --smoke-test` exits cleanly after 1 poll cycle

---

### E0-S2: Implement polling loop + virtual hedge bookkeeping

**Complexity:** Mid
**Designer needed:** No
**Assigned to:** Mid Dev
**Depends on:** E0-S1
**Blocks:** E0-S5

**Description:**
Implement the main observe-only loop per `funding-edge-mvp-spike.md §5.4`: every 60s, fetch funding + spot + perp state for each symbol, run the scorer, open virtual hedges when entry rules fire, accrue funding on 8h boundaries, close on rule hits or target hold, log to CSV.

**Acceptance criteria:**
- [ ] All functions from `spike.py`, `binance_client.py`, `scorer.py` implemented
- [ ] `signals.csv`, `cycles.csv`, `snapshots.jsonl`, `open_hedges.json` correctly written on each poll
- [ ] Binance API errors caught and logged; loop always continues
- [ ] Funding accrual fires exactly once per 8h boundary per open hedge (unit test with fake clock)
- [ ] `open_hedges.json` writes are atomic (tmp + rename) so a crash mid-poll doesn't corrupt state
- [ ] One manual 24h run documented in PR description with log excerpts

---

### E0-S3: Implement report.py

**Complexity:** Easy
**Designer needed:** No
**Assigned to:** Junior Dev
**Depends on:** E0-S2
**Blocks:** E0-S5

**Description:**
Implement `report.py` per spec §5.5. Computes win rate, median/mean net yield, per-symbol breakdown, and prints Green/Yellow/Red light decision per §7 table.

**Acceptance criteria:**
- [ ] Computes: n, wins, win rate, total net P&L, median & mean bps yield, stdev
- [ ] Per-symbol breakdown printed
- [ ] Green/Yellow/Red decision logic applied per spike §7
- [ ] Handles empty `cycles.csv` gracefully

---

### E0-S4: Dry-run fixture tests

**Complexity:** Easy
**Designer needed:** No
**Assigned to:** Junior Dev
**Depends on:** E0-S2
**Blocks:** E0-S5

**Description:**
Build a fixture replay harness for the spike: feed a pre-recorded sequence of premium-index and book-ticker JSONs into `poll_once`, verify the expected signals fire and funding accrues correctly. This is the safety net that lets us ship the spike with confidence that the math is right.

**Acceptance criteria:**
- [ ] 3 fixture scenarios: (a) steady positive funding for 3 days, (b) funding flip to negative mid-hold, (c) basis blow-out
- [ ] Each fixture has expected `signals.csv` and `cycles.csv` outputs that the test asserts against
- [ ] Tests run in CI via `pytest`

---

### E0-S5: Run spike for 2 weeks and produce retro

**Complexity:** Easy
**Designer needed:** No
**Assigned to:** Operator (André)
**Depends on:** E0-S2, E0-S3, E0-S4
**Blocks:** All remaining epics (gate decision)

**Description:**
Operator runs the spike 24/7 for 2 weeks. If green light (≥ 30 cycles, ≥ 60% win rate, positive median): commit artifacts and proceed. If not: halt all work and revisit the spec.

**Acceptance criteria:**
- [ ] ≥ 30 closed virtual hedge cycles accumulated
- [ ] `report.py` shows ≥ 60% win rate AND positive median bps → Green light to proceed
- [ ] Spike artifacts committed to `docs/spike-retro/`: `signals.csv`, `cycles.csv`, snapshot sample, written retro
- [ ] Retro covers: which symbols worked, funding regime observed, anomalies, parameter instincts
- [ ] **Decision gate:** win rate < 55% → halt all work, do not open Epic 1+ issues

---

## Epic 1 — Infrastructure & Data Platform

**Goal:** Production-grade host setup, storage, process management, logging, and dashboard skeleton.
**Prerequisite:** Epic 0 green light (can start in parallel if operator is confident).
**Blocks:** Epic 2, Epic 8.
**Designer needed:** Yes (E1-S6 only).

---

### E1-S1: Ubuntu host hardening and user setup

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** None

**Description:**
Create `fundingedge` system user, all required directories, and correct file permissions. Document as an idempotent setup script.

**Acceptance criteria:**
- [ ] `fundingedge` system user (no login shell, no sudo)
- [ ] Directories: `/opt/fundingedge/`, `/etc/fundingedge/`, `/var/log/fundingedge/`, `/var/run/fundingedge/`
- [ ] `/etc/fundingedge/` mode `0750` owner `root:fundingedge`
- [ ] `/var/log/fundingedge/` mode `0755` owner `fundingedge`
- [ ] Python 3.11 venv at `/opt/fundingedge/.venv`
- [ ] `scripts/setup_host.sh` committed, idempotent, executable

---

### E1-S2: PostgreSQL 15 + Redis 7 install + migration tooling

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E1-S1

**Acceptance criteria:**
- [ ] PostgreSQL 15 as systemd service; `fundingedge` role, no superuser
- [ ] `pg_hba.conf` restricts to localhost only
- [ ] Redis 7 systemd service, bound to `127.0.0.1` only
- [ ] Alembic configured; `scripts/migrate.py` + `make migrate` work
- [ ] Python round-trip test: `psycopg2` + `redis-py` connect and execute

---

### E1-S3: Database schema — initial migration

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E1-S2

**Description:**
Alembic migration implementing all 8 tables from `funding-edge-spec.md §6.1`: `funding_snapshot`, `market_snapshot`, `decision`, `hedge_position`, `trade_order`, `funding_payment`, `withdrawal`, `risk_event`.

**Acceptance criteria:**
- [ ] All 8 tables with exact schema from §6.1 (columns, types, nullability)
- [ ] Indexes: `idx_funding_symbol_time`, `idx_market_symbol_time`, `idx_hedge_symbol_status`
- [ ] Primary keys and foreign keys per spec
- [ ] Migration runs cleanly on fresh DB and is fully reversible

---

### E1-S4: systemd unit scaffolding

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E1-S1

**Acceptance criteria:**
- [ ] 8 units created per spec §10.2 (funding-poller, price-poller, account-poller, engine, reconciler, dashboard, daily-report, weekly-withdrawal)
- [ ] All units: `User=fundingedge`, `Restart=on-failure`, `RestartSec=30s`, `EnvironmentFile=/etc/fundingedge/env`
- [ ] Weekly-withdrawal uses `OnCalendar=Fri 18:00:00 Europe/Lisbon`
- [ ] Unit files live under `deploy/systemd/` in the repo

---

### E1-S5: Structured logging framework

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E1-S1

**Acceptance criteria:**
- [ ] `structlog` configured for JSON output to `/var/log/fundingedge/<service>.jsonl`
- [ ] Standard fields on every log line: `ts`, `service`, `level`, `event`, `context`
- [ ] Logrotate config in `deploy/logrotate/fundingedge`: daily, 30-day retention, gzip
- [ ] Unit test verifies structure on a sample log call

---

### E1-S6: Dashboard skeleton

**Complexity:** Mid
**Designer needed:** Yes
**Assigned to:** Mid Dev (after Designer spec)
**Depends on:** E1-S2

**Description:**
FastAPI + HTMX app bound to `127.0.0.1:8080`. Skeleton pages for positions, funding schedule, basis, P&L, margin, risk events. Real data wiring is Epic 8; this story delivers the layout and routing.

**Acceptance criteria:**
- [ ] Designer spec approved (`docs/design/dashboard.md`)
- [ ] FastAPI app runs as systemd service on `localhost:8080`
- [ ] Pages: `/`, `/hedges`, `/funding`, `/risk`, `/withdrawals`
- [ ] HTMX partial refresh on a 10s interval for live numbers
- [ ] Basic auth or no auth (localhost only; dashboard not exposed)
- [ ] Designer PR approval before merge

---

## Epic 2 — Data Ingestion

**Goal:** Reliable pollers that keep PostgreSQL and Redis populated with funding, price, and account state.
**Prerequisite:** Epic 1 complete.
**Blocks:** Epic 3, Epic 7 (needs historical ingestion scripts).

---

### E2-S1: Binance client with HMAC signing + rate limiting

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E1-S2

**Description:**
Thin async wrapper around `python-binance` (or direct httpx) with: API key loading from env, HMAC-SHA256 request signing, Redis-backed token bucket for weight tracking, exponential backoff on 429/5xx, IP-restricted key enforcement.

**Acceptance criteria:**
- [ ] Supports all endpoints listed in spec §9.1 and §9.2
- [ ] Token bucket respects Binance's published weight budget per IP
- [ ] Retry policy: exponential backoff 1s → 2s → 4s → 8s → 16s, then fail
- [ ] Unit tests mock the HTTP layer and verify signing + retries
- [ ] Integration test runs against **testnet** to confirm signing works

---

### E2-S2: Funding poller

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E2-S1, E1-S3

**Description:**
Long-running service; every 60s fetches `/fapi/v1/premiumIndex` for the configured universe, writes to `funding_snapshot` and Redis cache.

**Acceptance criteria:**
- [ ] Writes one row per symbol per poll to `funding_snapshot`
- [ ] Updates `funding:{symbol}` Redis key with TTL 120s
- [ ] Graceful handling of partial failures (one symbol errors, rest still recorded)
- [ ] Integration test against testnet for 10 polls

---

### E2-S3: Price poller (spot + perp)

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E2-S1, E1-S3

**Description:**
Long-running service; every 10s fetches spot and perp book tickers, computes basis, writes to `market_snapshot` and Redis.

**Acceptance criteria:**
- [ ] Writes one row per symbol per poll to `market_snapshot`
- [ ] Computes `basis_bps` correctly from spot and perp mid prices
- [ ] Updates `price:{symbol}` Redis key with TTL 30s
- [ ] Integration test against testnet

---

### E2-S4: Account + margin poller

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E2-S1, E1-S3

**Description:**
Long-running service; every 30s fetches spot balances and perp account state (positions, margin ratio, unrealised P&L), writes to Redis hot cache and emits to logs. Used by risk manager and dashboard.

**Acceptance criteria:**
- [ ] `margin:ratio` Redis key updated every poll
- [ ] Position reconciliation signals (for Epic 6 reconciler) written to a dedicated log stream
- [ ] Alert if API key loses trading permission mid-run (signed request returns 403)

---

### E2-S5: Historical data ingestion script

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E2-S1

**Description:**
One-shot script per data source to pull 12 months of history into Parquet for the backtest (Epic 7). One script each: `ingest_funding.py`, `ingest_klines_spot.py`, `ingest_klines_perp.py`. Paginated, resumable, writes partitioned Parquet per `funding-edge-backtest-harness.md §6.1`.

**Acceptance criteria:**
- [ ] Each script resumable: skips dates already present in Parquet
- [ ] Writes partitioned layout `symbol=X/date=Y/part-0.parquet`
- [ ] `manifest.json` reports coverage and any gaps
- [ ] Full 12-month ingestion for the 4-symbol universe completes in under 2 hours on the Mac mini

---

## Epic 3 — Strategy Engine

**Goal:** Stateless modules that score funding opportunities and decide entries/exits. Importable by both the live engine and the backtest harness.
**Prerequisite:** Epic 2 complete (live data flows).
**Blocks:** Epic 4, Epic 7.

---

### E3-S1: Fee model

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** None

**Description:**
Pure function `fee_bps(venue, order_type, symbol, maker_or_taker, bnb_balance) -> float`. Versioned fee schedule in JSON; load the version active at a given timestamp. Unit-test against 15+ worked examples per version.

**Acceptance criteria:**
- [ ] Schedule versioning in `data/fee_schedule_v*.json`
- [ ] Function accepts a timestamp and returns the fee for the schedule valid at that time
- [ ] 15+ unit tests per schedule version
- [ ] Monthly reverification job: compare modelled fee against last 100 real fills, alert if median divergence > 10%

---

### E3-S2: Funding scorer

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E3-S1, E1-S3

**Description:**
Implement `scorer.py` with `should_enter(state) -> (bool, reason)` and `should_exit(state, hedge) -> (bool, reason)` per `funding-edge-spec.md §7.1–§7.2`. No wall-clock calls; clock injected.

**Acceptance criteria:**
- [ ] All entry rules implemented (threshold, persistence, basis ceiling, min-time-to-funding, liquidity)
- [ ] All exit rules implemented (threshold drop, negative streak, basis blow-out, max-hold, margin warning)
- [ ] 100% unit test coverage
- [ ] Clock and data source injected as parameters — no `datetime.now()` or HTTP calls inside

---

### E3-S3: Basis monitor

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E3-S2

**Description:**
Module that tracks basis evolution for each open hedge and flags tolerance breaches to the risk manager.

**Acceptance criteria:**
- [ ] `basis_tolerance_check(hedge, current_basis_bps) -> BasisStatus` returns one of `OK | WARN | BREACH`
- [ ] Unit tests for each transition

---

### E3-S4: Position sizing

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E3-S2

**Description:**
`compute_notional(symbol, bankroll_usd, depth_usd, available_margin_usd) -> float` applying the four caps from spec §7.5 and rounding down to Binance's lot-step.

**Acceptance criteria:**
- [ ] All four caps applied; returns the minimum
- [ ] Rounds down to exchange lot-step + notional precision from `/fapi/v1/exchangeInfo`
- [ ] Returns 0 if any cap is 0
- [ ] Unit tests with synthetic caps

---

## Epic 4 — Risk Management

**Goal:** Enforce every non-negotiable rule from spec §7.7, including the automated weekly withdrawal.
**Prerequisite:** Epic 3.
**Blocks:** Epic 5, Epic 6.

---

### E4-S1: Risk manager core

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E3-S2

**Description:**
`RiskManager.gate(proposed_action) -> (approved: bool, reasons: list[str])`. Every entry/exit flows through this before any LLM call or order placement.

**Acceptance criteria:**
- [ ] All 12 rules from spec §7.7 implemented
- [ ] 100% unit test coverage
- [ ] Halts are persisted to Redis (`halt:global = "1"`) and `risk_event` table
- [ ] Kill switch file check (`/var/run/fundingedge/STOP`) every gate call

---

### E4-S2: Margin ratio monitor + auto-unwind

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E4-S1, E2-S4

**Description:**
Continuous loop in the risk manager service: every 30s, check margin ratio for each open hedge. If > 0.50 → warn (alert). If > 0.70 → force-close that hedge (emergency unwind of that pair only).

**Acceptance criteria:**
- [ ] Warn trigger sends email alert within 60s
- [ ] Force-close trigger invokes hedge executor with `reason=MARGIN_RATIO_EXCEEDED`
- [ ] Integration test against testnet: manually push margin ratio via test orders, verify unwind

---

### E4-S3: Weekly withdrawal job

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E4-S1, E2-S1

**Description:**
Scheduled Fridays 18:00 Europe/Lisbon. Compute `operating_cap`, initiate withdrawal for excess to the whitelisted destination, log to `withdrawal` table, email operator. This is the platform-risk mitigation.

**Acceptance criteria:**
- [ ] Systemd timer fires every Friday at 18:00 local
- [ ] Withdrawal sent to a destination that must be present in `BINANCE_WITHDRAWAL_WHITELIST` (bot refuses otherwise)
- [ ] Failed withdrawal retries Saturday 18:00; persistent failure → HALT
- [ ] Integration test against testnet withdrawal API
- [ ] Dry-run mode prints what *would* withdraw without executing — operator enables real withdrawals via env flag after Stage 3

---

### E4-S4: Daily + weekly loss floors

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E4-S1

**Description:**
Two rolling counters: realised daily P&L and realised weekly P&L. HALT if thresholds breached.

**Acceptance criteria:**
- [ ] Daily P&L floor: -3% of bankroll → HALT 24h
- [ ] Weekly floor: -8% of bankroll → HALT until manual reset
- [ ] Alerts sent on HALT
- [ ] Unit tests with synthetic P&L streams

---

## Epic 5 — LLM Sanity Check

**Goal:** Port the MeteoEdge LLM sanity-check layer to FundingEdge's schema. Large portions are literally copy-paste.
**Prerequisite:** Epic 3 + Epic 4.
**Blocks:** Epic 6.

---

### E5-S1: Port LLM provider abstraction from MeteoEdge

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E3-S2

**Description:**
Copy `src/meteoedge/llm/` from the archive into `src/fundingedge/llm/`, update imports, keep the protocol unchanged. Only `anthropic_provider.py`, `deepseek_provider.py`, `openai_provider.py` need to stay.

**Acceptance criteria:**
- [ ] Protocol and factory match the archive spec
- [ ] All three provider implementations compile and pass their own unit tests (copied from archive)
- [ ] `sanity_check.py` refactored to take a FundingEdge `SanityCheckRequest` schema

---

### E5-S2: FundingEdge-specific sanity-check request schema

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E5-S1

**Description:**
New schema for the proposed hedge entry/exit + context per spec §8.2. Prompt template crafted to elicit warnings about macro events, exchange maintenance, regulatory actions, and basis anomalies.

**Acceptance criteria:**
- [ ] Request schema mirrors spec §8.2
- [ ] Response schema enforces `approve`, `confidence`, `reason`, `warnings` per §8.3
- [ ] Prompt committed to `prompts/sanity_check.md`
- [ ] 5+ fixture tests against mocked provider responses covering approve/reject/warning cases

---

### E5-S3: Wire sanity check into decision flow

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E5-S2, E4-S1

**Description:**
Every `RiskManager.gate()` approval that passes flows into `SanityChecker.check()`. Both must approve. Daily call cap enforced.

**Acceptance criteria:**
- [ ] AND-gate logic per spec §8.4
- [ ] Daily cap enforced via Redis counter (`llm:calls:YYYY-MM-DD`)
- [ ] Timeout: 10s per call, abort action on timeout
- [ ] Full decision flow unit-tested end-to-end with mocked provider

---

## Epic 6 — Hedge Execution

**Goal:** Safely execute paired spot+perp legs, handle partial fills, maintain hedge ratio, reconcile state against Binance truth.
**Prerequisite:** Epic 4 + Epic 5.
**Blocks:** Epic 8.

---

### E6-S1: Hedge executor (paired legs)

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E5-S3

**Description:**
Open a hedge: place perp short first (limits spot risk while awaiting perp fill), then spot buy. Close: reverse. Handle partial fills on one leg by chasing the other. Record every order to `trade_order` with links to `hedge_position`.

**Acceptance criteria:**
- [ ] Entry sequence: perp SELL → await fill → spot BUY → await fill → record `hedge_position`
- [ ] Exit sequence: perp BUY (cover) → await fill → spot SELL → await fill → mark `hedge_position` CLOSED
- [ ] Partial-fill handling: if perp fills 80%, spot scales to 80% to maintain the hedge ratio
- [ ] All orders persisted with `decision_id` linkage
- [ ] Integration test against testnet: 5 full entry + exit cycles

---

### E6-S2: Reconciler

**Complexity:** Mid
**Assigned to:** Mid Dev
**Depends on:** E6-S1

**Description:**
Every 60s compare internal state (open `hedge_position` rows + fills) against Binance's reported positions and balances. Divergence > 0.1% of notional → HALT.

**Acceptance criteria:**
- [ ] Pulls Binance spot balances and perp positions
- [ ] Cross-checks against internal `hedge_position` state
- [ ] Divergence threshold configurable; default 0.1% of notional
- [ ] HALT + alert on divergence
- [ ] Integration test against testnet: manually create a phantom order outside the bot, verify reconciler detects it

---

### E6-S3: Funding payment recorder

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E6-S1

**Description:**
Every 10 min, poll `/fapi/v1/income?incomeType=FUNDING_FEE` for the last hour. For each row, match to an open or recently-closed `hedge_position` by symbol+time and insert into `funding_payment`.

**Acceptance criteria:**
- [ ] All funding payments recorded exactly once (idempotent on Binance's `tranId`)
- [ ] Reconciles against `hedge_position.realised_funding` rolling total
- [ ] Unit test with fixture income log

---

### E6-S4: Emergency unwind script

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E6-S1

**Description:**
`scripts/emergency_unwind.py --confirm` closes all open hedges immediately (market orders). Logs to `risk_event`, emails operator.

**Acceptance criteria:**
- [ ] Requires `--confirm` flag (no accidental runs)
- [ ] Closes every row with `status=OPEN` in `hedge_position`
- [ ] Records a `risk_event` with `severity=HALT` and details
- [ ] Tested against testnet with 2 open hedges

---

## Epic 7 — Backtest Harness

**Goal:** Deliver the Stage 1 gate harness per `funding-edge-backtest-harness.md`.
**Prerequisite:** Epic 3 (modules importable).
**Blocks:** Epic 10 (production rollout) via the Stage 1 gate.

Stories per harness §9. Not re-listed here to avoid duplication; see the harness doc. Tech Lead PM breaks the numbered implementation steps into 15 GitHub issues, one per step, mostly Mid for Sonnet with data-ingestion and report stories assigned Easy for Haiku.

---

## Epic 8 — Operational Tooling

**Goal:** Operator-facing dashboard, alerts, daily P&L email, runbook docs.
**Prerequisite:** Epic 6 complete (real P&L flowing).
**Designer needed:** Yes on dashboard stories.

---

### E8-S1: Dashboard — positions + funding view

**Complexity:** Mid
**Designer needed:** Yes
**Assigned to:** Mid Dev
**Depends on:** E1-S6, E6-S1

**Acceptance criteria:**
- [ ] Designer spec approved (`docs/design/dashboard-positions.md`)
- [ ] `/hedges` page shows open and recent hedges with funding accrued, basis, margin ratio
- [ ] `/funding` page shows funding rate history + next-funding countdown per symbol
- [ ] HTMX refresh every 10s
- [ ] Designer PR approval

---

### E8-S2: Dashboard — risk + withdrawals view

**Complexity:** Mid
**Designer needed:** Yes
**Assigned to:** Mid Dev
**Depends on:** E4-S3

**Acceptance criteria:**
- [ ] Designer spec approved
- [ ] `/risk` page shows recent `risk_event` rows, global halt status, kill-switch state
- [ ] `/withdrawals` page shows withdrawal history and next scheduled withdrawal
- [ ] Designer PR approval

---

### E8-S3: Daily P&L email

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E6-S3

**Acceptance criteria:**
- [ ] Systemd timer fires at 23:00 UTC daily
- [ ] Email includes: today's realised P&L, open hedges with status, funding collected, any risk events
- [ ] Plain-text + HTML alternates
- [ ] Integration test against a local SMTP mock

---

### E8-S4: Alerter

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E4-S1

**Acceptance criteria:**
- [ ] Email alert on HALT, margin warning, reconciler divergence, withdrawal failure
- [ ] De-duplication window: same alert type within 15 min → suppress
- [ ] Alert volume test in staging

---

### E8-S5: Operator runbook

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E6-S1

**Description:**
Document startup, manual halt, resume, emergency unwind, incident response — per spec §13.

**Acceptance criteria:**
- [ ] `docs/runbook.md` covers all sections from spec §13
- [ ] Each command tested manually before docs are committed

---

## Epic 9 — Compliance & Security

**Goal:** Harden secrets management, API scoping, withdrawal whitelist, audit logging.
**Prerequisite:** Epic 1.
**Can run in parallel with Epic 2+.**

---

### E9-S1: Secrets management

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E1-S1

**Acceptance criteria:**
- [ ] `/etc/fundingedge/env` mode `0600` owner `fundingedge`
- [ ] Binance API key scoped to **spot+futures trading + restricted withdrawal** (explicitly NO transfer permission between accounts)
- [ ] API key IP-restricted to the Mac mini's static IP
- [ ] Withdrawal whitelist configured on Binance UI and documented in runbook

---

### E9-S2: Outbound network allowlist

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E1-S1

**Acceptance criteria:**
- [ ] `ufw` or nftables rules restrict outbound to: `api.binance.com`, `fapi.binance.com`, `testnet.binance.vision`, `testnet.binancefuture.com`, active LLM provider, SMTP host
- [ ] Documentation of allowlist in `docs/runbook.md`
- [ ] Integration test: attempt connection to a non-allowed host, verify blocked

---

### E9-S3: Audit logging

**Complexity:** Easy
**Assigned to:** Junior Dev
**Depends on:** E6-S1

**Acceptance criteria:**
- [ ] Every order placement logs: decision_id, timestamp, payload hash, response
- [ ] Every withdrawal logs: destination, amount, result, external ID
- [ ] Audit logs stored separately from operational logs, longer retention (1 year)

---

## Epic 10 — Production Rollout

**Goal:** Stage 2 (testnet) → Stage 3 (micro-live) → Stage 4 (scale). Gated by Epic 7 Stage 1 pass.

---

### E10-S1: Stage 2 testnet run (2 weeks)

**Complexity:** Easy (operator-led)
**Assigned to:** Operator (André)
**Depends on:** Epic 6 complete, Epic 7 Stage 1 gate passed

**Acceptance criteria:**
- [ ] Deploy to testnet, run 14 consecutive days with zero unhandled exceptions
- [ ] All risk halts manually triggered at least once
- [ ] Funding payments reconcile with Binance testnet reports to $0.01
- [ ] Operator writes weekly retros; final retro includes Go/No-Go decision

---

### E10-S2: Stage 3 micro-live (4 weeks)

**Complexity:** Easy (operator-led)
**Assigned to:** Operator (André)
**Depends on:** E10-S1 pass

**Acceptance criteria:**
- [ ] Production API with $200 bankroll, $50 per-hedge cap
- [ ] Run testnet and production in parallel, compare weekly
- [ ] Live-vs-testnet P&L divergence < 20%
- [ ] 4 consecutive Fridays executed clean withdrawals
- [ ] At least 15 hedges manually reviewed by operator

---

### E10-S3: Stage 4 scale-up checklist

**Complexity:** Easy
**Assigned to:** Tech Lead PM + Operator
**Depends on:** E10-S2 pass

**Description:**
Codify the scale-up rules from spec §11 Stage 4 into a committed runbook.

**Acceptance criteria:**
- [ ] `docs/scale-up.md` documents: doubling cadence, withdrawal discipline, per-quintile culling, monthly fee reverification, quarterly re-backtest
- [ ] Tech Lead PM and operator signoff on the document

---

## Governance Reminders

Per `CLAUDE.md`:

- Every agent uses `GITHUB_TOKEN_OPERATIONAL` (Tech Lead PM uses `GITHUB_TOKEN_SUPERVISOR` only when needed)
- Status updates via GraphQL `updateProjectV2ItemFieldValue` — **never** via labels
- Every PR uses `Closes #N` to link issues
- Issue comments mandatory at: work start, PR submission, blockers, review response
- Non-blocking review suggestions logged as new issues in Backlog

---

*End of implementation plan. Tech Lead PM creates GitHub issues from this doc and populates the project board with all stories in **Ready** status (except Epic 10, which stays in Backlog until the Stage 1 gate passes).*
