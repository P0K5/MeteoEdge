# Graph Report - .  (2026-06-16)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 837 nodes · 1400 edges · 46 communities (42 shown, 4 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 52 edges (avg confidence: 0.64)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `06d5f53c`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Database Core Functions|Database Core Functions]]
- [[_COMMUNITY_MSS Data Collector|MSS Data Collector]]
- [[_COMMUNITY_Spike Detection|Spike Detection]]
- [[_COMMUNITY_Risk Management|Risk Management]]
- [[_COMMUNITY_Settlement Logic Tests|Settlement Logic Tests]]
- [[_COMMUNITY_Agent Roles & Docs|Agent Roles & Docs]]
- [[_COMMUNITY_Model Retraining|Model Retraining]]
- [[_COMMUNITY_EMOS Mode Management|EMOS Mode Management]]
- [[_COMMUNITY_Bracket Market Parsing|Bracket Market Parsing]]
- [[_COMMUNITY_Envelope Tests|Envelope Tests]]
- [[_COMMUNITY_Weather Envelope Model|Weather Envelope Model]]
- [[_COMMUNITY_JMA AMeDAS Collector|JMA AMeDAS Collector]]
- [[_COMMUNITY_Freshness Monitoring|Freshness Monitoring]]
- [[_COMMUNITY_Intraday Correction Backtest|Intraday Correction Backtest]]
- [[_COMMUNITY_METAR Data Fetcher|METAR Data Fetcher]]
- [[_COMMUNITY_TAF Disruption Check|TAF Disruption Check]]
- [[_COMMUNITY_Live Trading Execution|Live Trading Execution]]
- [[_COMMUNITY_Market Scanner & Dashboard|Market Scanner & Dashboard]]
- [[_COMMUNITY_Candidate & TAF Disruption|Candidate & TAF Disruption]]
- [[_COMMUNITY_CRPS Score Calculation|CRPS Score Calculation]]
- [[_COMMUNITY_AMOS Collector|AMOS Collector]]
- [[_COMMUNITY_Weather Envelope & Ensemble|Weather Envelope & Ensemble]]
- [[_COMMUNITY_Forecast Data Fetching|Forecast Data Fetching]]
- [[_COMMUNITY_Data Source Adapters|Data Source Adapters]]
- [[_COMMUNITY_Settlement Report Tests|Settlement Report Tests]]
- [[_COMMUNITY_EMOS API & Status|EMOS API & Status]]
- [[_COMMUNITY_Risk Manager Core|Risk Manager Core]]
- [[_COMMUNITY_Project Documentation & Plans|Project Documentation & Plans]]
- [[_COMMUNITY_Take-Profit Exit Logic|Take-Profit Exit Logic]]
- [[_COMMUNITY_Scan Market Tests|Scan Market Tests]]
- [[_COMMUNITY_Settlement Recording|Settlement Recording]]
- [[_COMMUNITY_Decay Function Library|Decay Function Library]]
- [[_COMMUNITY_Source Priority Configuration|Source Priority Configuration]]
- [[_COMMUNITY_Order State Management|Order State Management]]
- [[_COMMUNITY_Shadow Backtest|Shadow Backtest]]
- [[_COMMUNITY_TAF Database Schema|TAF Database Schema]]
- [[_COMMUNITY_Risk PnL Recording Tests|Risk PnL Recording Tests]]
- [[_COMMUNITY_Spike Detection Core|Spike Detection Core]]
- [[_COMMUNITY_Settlement Pipeline Tests|Settlement Pipeline Tests]]
- [[_COMMUNITY_Stations API Tests|Stations API Tests]]
- [[_COMMUNITY_Climb Rate Tables|Climb Rate Tables]]
- [[_COMMUNITY_Dashboard API Tests|Dashboard API Tests]]
- [[_COMMUNITY_Database Thread Safety|Database Thread Safety]]
- [[_COMMUNITY_LiveTrader DB Integration|LiveTrader DB Integration]]
- [[_COMMUNITY_EMOS Coefficients Persistence|EMOS Coefficients Persistence]]

## God Nodes (most connected - your core abstractions)
1. `LiveTrader` - 46 edges
2. `_write_jsonl()` - 26 edges
3. `_position_state()` - 23 edges
4. `poll_once()` - 19 edges
5. `scan_markets()` - 18 edges
6. `Implementation Plan` - 18 edges
7. `TestStationsOverviewEndpoint` - 17 edges
8. `TestStationsPerfEndpoint` - 17 edges
9. `rotated_path()` - 17 edges
10. `housekeep()` - 17 edges

## Surprising Connections (you probably didn't know these)
- `Implementation Plan` --references--> `Alerts`  [EXTRACTED]
  docs/IMPLEMENTATION_PLAN.md → src/monitoring/alerts.py
- `Implementation Plan` --references--> `Climb Rates Model`  [EXTRACTED]
  docs/IMPLEMENTATION_PLAN.md → src/model/climb_rates.py
- `Implementation Plan` --references--> `Config Module`  [EXTRACTED]
  docs/IMPLEMENTATION_PLAN.md → src/config.py
- `Implementation Plan` --references--> `Fee Model`  [EXTRACTED]
  docs/IMPLEMENTATION_PLAN.md → src/strategy/fee.py
- `Implementation Plan` --references--> `Logger`  [EXTRACTED]
  docs/IMPLEMENTATION_PLAN.md → src/monitoring/logger.py

## Import Cycles
- None detected.

## Communities (46 total, 4 thin omitted)

### Community 0 - "Database Core Functions"
Cohesion: 0.05
Nodes (61): _execute_live(), Single-order execution lifecycle for live trading.  _execute_live is extracted f, Place one order and wait for fill/timeout. open_position() already called by cal, _check_metar_exits(), _check_stop_loss_exits(), _log_open_position_snapshots(), Position tracking helpers — snapshot logging and exit checks.  These functions w, Exit NO positions when the live model itself no longer supports the entry. (+53 more)

### Community 1 - "MSS Data Collector"
Cohesion: 0.06
Nodes (47): get_live_config(), Return current bot_config values as a typed dict.      Reads all rows from the b, Bracket, WeatherState, Bracket, WeatherState, Candidate, _decode_json_string() (+39 more)

### Community 2 - "Spike Detection"
Cohesion: 0.08
Nodes (44): CI Workflow, Alerts, Climb Rates Model, Config Module, DEB Weighting Engine, EMOS Calibration Trainer, Weather Envelope Model, Fee Model (+36 more)

### Community 3 - "Risk Management"
Cohesion: 0.07
Nodes (21): _position_state(), Triggered but bid under STOP_LOSS_MIN_BID_CENTS — hold, keep strikes., Triggered but best-bid depth too thin — skip this poll, keep strikes., sell_position_immediate -> None (cancelled unfilled): no resting order,, Model eval failed (fair None) — never strike or sell on missing data., Generic sell exception → warning logged, position NOT marked sold (will retry ne, Hotfix guards added 2026-06-13 after 3 wins were cut in one day., current_high 1.8F below bracket_low (>0.5F buffer) -- hold, keep strikes. (+13 more)

### Community 4 - "Settlement Logic Tests"
Cohesion: 0.11
Nodes (24): Database, Path, _fresh_db(), _insert_settled_trade(), _insert_stuck_trade(), _query_stuck_trades(), Tests for stuck-trade detection in src/scripts/settle.py.  Covers: - Detection o, Tests for stuck-trade detection logic. (+16 more)

### Community 5 - "Agent Roles & Docs"
Cohesion: 0.10
Nodes (32): scripts/build_climb_lookup.py, src/data/climb_lookup.py, src/model/climb_rates.py, src/model/envelope.py, Climb Rate Spike Report, Confidence Clamp Fix, Live Observation Report, KSFO Station Removal (+24 more)

### Community 6 - "Model Retraining"
Cohesion: 0.08
Nodes (20): Database, _mem_db(), Scenario:         1. Admin manually sets RKSI to both enabled in the DB., Scenario:         1. Startup 1: SHADOW_STATIONS not set (default="RKSI"). Seed c, After manual edit, multiple restarts preserve the edit., Verify that the scanner prioritizes DB rows over env vars., When a DB row exists, scanner reads from it, not from env vars.          This is, When no DB row exists, scanner falls back to env vars.          Direct test: KOR (+12 more)

### Community 7 - "EMOS Mode Management"
Cohesion: 0.12
Nodes (17): _common_ctx(), _make_live_trader(), _make_mock_db(), _make_mock_risk(), Tests for balance-check fix (issue #286).  Covers: - Exception path: balance che, _wallet_cooldown_until is set ~1800s ahead when balance falls below position siz, When cooldown is active, poll_once returns early without calling get_usdc_balanc, Once the cooldown window passes, normal scanning resumes. (+9 more)

### Community 8 - "Bracket Market Parsing"
Cohesion: 0.08
Nodes (16): Tests for the /api/stations/overview endpoint (issue #229)., Inject db and clear the stations overview cache., Endpoint must return 200 and a list., One entry per configured STATION must appear., Each record must include all required fields., A station in DISABLED_STATIONS must have status='disabled' and enabled=False., A station outside its active hours must have status='outside_hours'., A station with no observations and in active hours must have status='no_data'. (+8 more)

### Community 9 - "Envelope Tests"
Cohesion: 0.07
Nodes (27): bot_log(), _cash_usdc(), get_city_taf(), health_monitor(), _market_question(), _midpoint_cents(), portfolio(), PortfolioOut (+19 more)

### Community 10 - "Weather Envelope Model"
Cohesion: 0.10
Nodes (13): Tests for GET /api/stations/perf: {real, shadow} × {YES, NO} quadrants., Helper: insert a trade and update outcome/pnl., A station with no trades must not appear in the response., Each station entry must have real and shadow, each with YES and NO quadrants., An empty quadrant must return count=0, win_rate=null, pnl=0.0,         avg_entry, Real YES quadrant computes metrics correctly from live-mode YES trades., Real NO quadrant is computed independently of real YES., Shadow YES quadrant is computed from mode='shadow' trades only. (+5 more)

### Community 11 - "JMA AMeDAS Collector"
Cohesion: 0.18
Nodes (15): Database, _db(), _insert_settlement(), _insert_trade(), Tests for P&L scaling by position size (issue #292).  Verifies that the fix_stuc, Test P&L scaling for a losing trade (YES bracket missed).          Example:, Test P&L scaling for a NO-side trade (bracket NOT hit = win).          Example:, Test P&L scaling with fractional shares and rounding.          Example: (+7 more)

### Community 12 - "Freshness Monitoring"
Cohesion: 0.12
Nodes (18): _batch_midpoints(), ClosedPositionOut, _latest_model_probs(), _nws_forecast_for_title(), PositionOut, _positions_from_wallet(), Read live_state.json; return empty state if missing or corrupt., Return live_state.json open trades keyed by token_id for quick lookup. (+10 more)

### Community 13 - "Intraday Correction Backtest"
Cohesion: 0.17
Nodes (10): Database, _db(), _make_trader(), Tests for LiveTrader DB integration (Issue B: replace live_state.json with DB)., v2 API: cancel_order failures now raise exceptions (hard error)., v2 API: if response has an error field, cancel_order() returns False., place_order() must not write any .json state files., Return a LiveTrader with a mock ClobClient. (+2 more)

### Community 14 - "METAR Data Fetcher"
Cohesion: 0.21
Nodes (10): _make_trader(), _mock_orderbook(), Tests for sell_position_immediate cancel-error vs cancel-refused distinction (#2, #203 — cancel_order False must not mark position sold without fill confirmation., cancel_order() raises (network error) → exception propagates to caller., cancel_order() returns False (error response) + check_fill confirms filled → ret, cancel_order() returns False + check_fill shows open → sell_id is None, not sold, Happy path: order fills immediately → returns (order_id, price), cancel never ca (+2 more)

### Community 15 - "TAF Disruption Check"
Cohesion: 0.16
Nodes (7): _orderbook(), Tests for the model-confidence stop-loss (issue #177).  Covers: - LiveTrader.sel, Limit is priced through the bid (70c - 2c = 68c) and fill is reported., If the order does not cross it must be cancelled — returns (None, order_id)., Cancel refused + check_fill confirms filled → treat as sold., Aggression below the 1c tick floor clamps to 1c, never 0 or negative., TestSellPositionImmediate

### Community 16 - "Live Trading Execution"
Cohesion: 0.14
Nodes (9): ClobClient, LiveTrader, Live order execution via Polymarket CLOB., Return the shares matched/filled for *order_id* so far (0.0 on error)., Return available USDC in the CLOB (internal balance, not on-chain)., Place a GTC limit order. Returns order_id string., Sell NO tokens at the current best bid price.          Used for METAR-triggered, LiveTrader (+1 more)

### Community 17 - "Market Scanner & Dashboard"
Cohesion: 0.21
Nodes (14): fetch_daily_climate_high(), _open_db(), Run once a day after NWS publishes the Daily Climate Report (typically ~9am loca, For each candidate from yesterday, record whether it would have won., Return a Database handle, or None if the DB cannot be opened., Upsert one settlement row per market resolved on *target*.      Market identity, Write actual P&L back to live_trades.jsonl for a given date.      Two cases:, Pull the actual daily high for a station on target_date using 48h METAR history. (+6 more)

### Community 18 - "Candidate & TAF Disruption"
Cohesion: 0.13
Nodes (8): Tests for GET /api/emos/status., Status endpoint returns one entry per city in STATIONS., Each status entry has the required keys., shadow and primary are null when no calibration rows exist., shadow block is populated when a shadow calibration row exists., effective_mode defaults to EMOS_DEFAULT_MODE when no override is set., Returns 503 when _db is None., TestEmosStatusEndpoint

### Community 19 - "CRPS Score Calculation"
Cohesion: 0.23
Nodes (3): Tests for _should_log_weather in src.weather.builder., After a suppressed log, state should stay unchanged., TestWeatherDeltaLogging

### Community 20 - "AMOS Collector"
Cohesion: 0.16
Nodes (14): _compute_win_rate(), _latest_capital(), _open_positions_count(), Compute win rate over the last *n* settled trades (excluding shadow).      Only, Sum PnL for trades whose timestamp falls on today (UTC), excluding shadow rows., Count trades whose timestamp falls on today (UTC)., Return the most recent capital value (excluding shadow). Prefers DB; falls back, Return today's open position count from risk_state, or 0. (+6 more)

### Community 21 - "Weather Envelope & Ensemble"
Cohesion: 0.16
Nodes (9): Return settled hold-to-expiry trades from live_trades.jsonl.      settle.py writ, _settled_jsonl_positions(), Test exit_reason field on closed positions (early exits and settled)., _stopped_positions() should set exit_reason='take_profit' for take_profit@ trigg, _stopped_positions() should set exit_reason='stop_loss' for stop_loss@ triggers., _settled_jsonl_positions() should set exit_reason='won' when pnl > 0., _settled_jsonl_positions() should set exit_reason='lost' when pnl <= 0., _settled_jsonl_positions() should set exit_reason='lost' when pnl == 0. (+1 more)

### Community 22 - "Forecast Data Fetching"
Cohesion: 0.14
Nodes (6): client(), Unit tests for the consolidated dashboard at src/dashboard/api.py.  Tests use tm, POST /api/positions/{token_id}/sell — operator-triggered manual sell., Create a fresh TestClient for each test to avoid state leakage.      Also resets, TestReadJsonl, TestSellPositionEndpoint

### Community 23 - "Data Source Adapters"
Cohesion: 0.16
Nodes (7): Tests for POST /api/emos/{city}/promote., Promote succeeds when shadow exists and ready_for_promotion=1., Promote returns 409 when no shadow row exists., Promote returns 409 when shadow exists but ready_for_promotion=0., Promote returns 404 for an unknown city name., Promote works with URL-encoded city names (spaces → %20)., TestEmosPromoteEndpoint

### Community 24 - "Settlement Report Tests"
Cohesion: 0.14
Nodes (8): Test that shadow mode trades are excluded from live metrics., _dashboard_load_trades() must filter out shadow rows., _today_pnl() must exclude shadow rows from P&L sum., stations() endpoint must exclude shadow rows from total_pnl., status() endpoint must report win_rate without shadow trades., A station with both live and shadow trades sees only live trades (shadow filtere, _latest_capital() must find capital from non-shadow row., TestShadowModeFiltering

### Community 25 - "EMOS API & Status"
Cohesion: 0.17
Nodes (13): BaseModel, AnalysisOut, DebOut, DebWeightOut, _emos_row_to_coefficients(), EmosCoefficients, get_city_analysis(), get_city_deb() (+5 more)

### Community 26 - "Risk Manager Core"
Cohesion: 0.22
Nodes (4): Path, TestStationsEndpoint, TestTradesEndpoint, _write_jsonl()

### Community 27 - "Project Documentation & Plans"
Cohesion: 0.15
Nodes (7): Tests for POST /api/emos/{city}/mark-ready., mark-ready toggles ready_for_promotion from 0 to 1., mark-ready toggles ready_for_promotion from 1 back to 0., mark-ready returns 409 when no shadow row exists., mark-ready returns 404 for unknown city., Calling mark-ready twice returns to the original state., TestEmosMarkReadyEndpoint

### Community 28 - "Take-Profit Exit Logic"
Cohesion: 0.18
Nodes (12): Any, _build_param_entry(), ConfigPatchRequest, get_config(), patch_config(), Validate and coerce *raw_value* for *key*.      Returns ``(serialised_str, None), Cast raw DB string to the correct Python type for the API response., Build the parameter object returned by GET /api/config. (+4 more)

### Community 29 - "Scan Market Tests"
Cohesion: 0.26
Nodes (12): emos_demote(), emos_mark_ready(), emos_promote(), emos_status(), EmosCityStatus, _get_emos_city_status(), Return EMOS calibration state for all cities defined in STATIONS.      For each, Resolve a URL city name to (canonical_city, station).      Performs a case-insen (+4 more)

### Community 30 - "Settlement Recording"
Cohesion: 0.18
Nodes (6): Tests for POST /api/emos/{city}/demote., Demote sets effective mode to legacy., Demote is safe to call when already in legacy mode., Demote returns 404 for an unknown city name., Demote preserves shadow/primary calibration rows., TestEmosDemoteEndpoint

### Community 31 - "Decay Function Library"
Cohesion: 0.22
Nodes (4): win_rate must be null when all trades have no settled pnl outcome., Issue G acceptance criteria for /status capital and open_positions_count., All original keys must still be present plus open_positions_count., TestStatusDbCapital

### Community 32 - "Source Priority Configuration"
Cohesion: 0.20
Nodes (4): _compute_win_rate() must exclude shadow rows from calculation., status() endpoint must report today_pnl without shadow trades., TestStatusEndpoint, _today()

### Community 33 - "Order State Management"
Cohesion: 0.20
Nodes (10): _build_perf_quadrant(), PerfQuadrantOut, Compute the 5 metrics for a single (mode, side) quadrant.      Args:         tra, Return a 2×2 performance matrix per station: {real, shadow} × {YES, NO}.      Re, Metrics for one (mode, side) quadrant of the performance matrix., Per-side breakdown for one mode (real or shadow)., 2×2 performance matrix for a single station: {real, shadow} × {YES, NO}., StationPerfOut (+2 more)

### Community 34 - "Shadow Backtest"
Cohesion: 0.20
Nodes (6): Verify that src.monitoring.dashboard correctly delegates to src.dashboard.api., Setting last_poll_ts via the bridge stub updates src.dashboard.api., set_db() via the bridge stub injects into src.dashboard.api., _load_trades() from bridge stub returns a list., _compute_win_rate() from bridge stub computes correctly., TestBridgeStubCompat

### Community 35 - "TAF Database Schema"
Cohesion: 0.50
Nodes (8): Trader Project, GitHub Governance Protocol, Graphify Knowledge Graph, Designer Agent, Graphify Update Workflow, Junior Developer Agent, Mid Developer Agent, Tech Lead PM Agent

### Community 37 - "Spike Detection Core"
Cohesion: 0.29
Nodes (4): place_order() must persist the open position to DB., place_order() without DB must not raise., Even if DB write fails, place_order() must still return the order_id., TestPlaceOrderWritesPosition

### Community 38 - "Settlement Pipeline Tests"
Cohesion: 0.33
Nodes (6): _dashboard_load_trades(), Return all trade records (excluding shadow), newest first. Prefers DB when avail, Last 50 trade records, newest first., Per-station trade count, win rate, and total PnL (excluding shadow rows)., stations(), trades_list()

### Community 39 - "Stations API Tests"
Cohesion: 0.33
Nodes (3): Sell NO tokens immediately or not at all — never leaves a resting order., Cancel an open order. Returns True if cancelled.          Uses py_clob_client_v2, Return current status of an order. Never raises.

### Community 40 - "Climb Rate Tables"
Cohesion: 0.40
Nodes (5): fix_stuck_trades(), _open_db(), Data-fix script for stuck trades from 2026-06-07.  Five trades have outcome IS N, Return a Database handle, or None if unavailable., Settle the five stuck trades from 2026-06-07 by attempting automatic resolution.

### Community 41 - "Dashboard API Tests"
Cohesion: 0.40
Nodes (5): _derive_station_status(), Derive the station status string from config and latest observation timestamp., Return config metadata and trade stats for all configured stations.      Respons, StationOverviewOut, stations_overview()

## Knowledge Gaps
- **30 isolated node(s):** `ClobClient`, `Path`, `Database`, `Path`, `Dashboard UI` (+25 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LiveTrader` connect `Live Trading Execution` to `Database Core Functions`, `Order State Management`, `Risk Management`, `Spike Detection Core`, `Stations API Tests`, `Envelope Tests`, `Dashboard API Tests`, `LiveTrader DB Integration`, `Freshness Monitoring`, `Intraday Correction Backtest`, `METAR Data Fetcher`, `TAF Disruption Check`, `EMOS API & Status`, `Take-Profit Exit Logic`, `Scan Market Tests`?**
  _High betweenness centrality (0.259) - this node is a cross-community bridge._
- **Why does `_settled_jsonl_positions()` connect `Weather Envelope & Ensemble` to `Envelope Tests`, `Freshness Monitoring`, `Forecast Data Fetching`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._
- **Why does `_stopped_positions()` connect `Freshness Monitoring` to `Envelope Tests`, `Weather Envelope & Ensemble`, `Forecast Data Fetching`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Are the 24 inferred relationships involving `LiveTrader` (e.g. with `Any` and `AnalysisOut`) actually correct?**
  _`LiveTrader` has 24 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Unified config for Polymarket weather arbitrage. Environment vars override defau`, `Seed bot_config from env vars / hardcoded defaults on first run.      For each k`, `Seed station_overrides for RKSI on first run, if missing.      RKSI is seeded wi` to the rest of the system?**
  _320 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Database Core Functions` be split into smaller, more focused modules?**
  _Cohesion score 0.0506155950752394 - nodes in this community are weakly interconnected._
- **Should `MSS Data Collector` be split into smaller, more focused modules?**
  _Cohesion score 0.05628415300546448 - nodes in this community are weakly interconnected._