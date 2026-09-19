# Design Spec — Copy-Trading Dashboard Tab

Companion to `docs/design/copy-trading-architecture.md` (Epic F). Extends
the existing dashboard (`src/dashboard/api.py` + `src/dashboard/static/index.html`)
with a new tab — same stack, no new framework: vanilla JS/HTML/CSS,
Chart.js for charts, FastAPI JSON endpoints.

## User goal

Let the operator (currently just the account owner — no multi-user auth
exists in this dashboard today) see which wallets the screening pipeline
has found, decide which ones to actively copy, and monitor the resulting
positions and P&L, without touching the CLI or reading raw markdown
reports.

## User flow

1. Operator opens the "Copy-Trading" tab.
2. Reviews the **Candidates** view — every wallet the screening pipeline
   has scored, sorted by median ROI (matching the backtest's own
   methodology — never re-sort by $ PnL as the default, that's the exact
   mistake the spike had to correct).
3. Picks a wallet to follow → sets its flat stake per trade → confirms.
   Wallet moves into the **Followed Wallets** view.
4. Over time, checks **Positions & P&L** to see what's been copied and
   how it's performing, per-wallet and in aggregate.
5. Checks the **Activity Feed** when something looks off (a followed
   wallet stopped generating signals, an order was skipped) before
   digging into logs.

## Screen layouts

### Candidates

- **Purpose:** browse and evaluate screened wallets before deciding to
  follow one.
- **Key components:** sortable/filterable table (columns: wallet address
  (truncated + copy-to-clipboard), resolved trades, win %, mean ROI,
  median ROI, mirrored $ PnL, flat-stake $ PnL, last screened, stability
  flag); a "Follow" button per row; a detail panel/modal on row click
  showing the wallet's screening history (sparkline of median ROI across
  runs — this is what surfaces the `0xd3b034d7`-style instability
  directly in the UI instead of requiring a second manual re-run).
- **Layout:** full-width table, default sort by median ROI descending
  (mirrors the backtest report's own default — consistency matters here
  specifically because sorting by $ PnL was the mistake this whole spike
  had to correct).
- **Interactions:** click column header to re-sort; click a row to open
  the detail panel; "Follow" opens an inline stake-amount input (default
  $5, matching the backtest default) before confirming.
- **States:**
  - *Default:* populated table.
  - *Loading:* skeleton rows.
  - *Empty:* "No wallets screened yet" + last screening-run timestamp if
    the pipeline has run but produced nothing (distinct from "pipeline
    has never run").
  - *Error:* screening-pipeline fetch failed — show last-known-good data
    with a stale-data banner, not a blank screen.
  - *Instability flag:* a wallet whose median ROI has swung sign or
    magnitude across its last 2+ screening runs gets a visible warning
    badge in the table row, not just in the detail panel — this should be
    impossible to miss before clicking "Follow".

### Followed Wallets

- **Purpose:** manage the active copy-trading roster.
- **Key components:** table of followed wallets (address, stake/trade,
  status: active/paused, paused_reason if paused, date added, running
  P&L); pause/resume and unfollow controls per row; edit-stake control.
- **Layout:** full-width table below a summary strip (# active, # paused,
  aggregate P&L).
- **Interactions:** pause/resume toggles status immediately (optimistic
  UI, confirmed by the next poll); unfollow requires a confirm dialog
  (destructive — stops copying, existing open positions are NOT
  auto-closed, make that explicit in the confirm copy).
- **States:** default, loading, empty ("Nothing followed yet — go to
  Candidates"), a row-level error state if a pause/resume/unfollow action
  fails (toast + row stays in its prior state, not a silent no-op).

### Positions & P&L

- **Purpose:** monitor copy-trading performance, separate from the
  weather strategy's existing portfolio view — these must never be
  merged into one number given the isolation requirement in the
  architecture doc.
- **Key components:** open positions table (same shape as the existing
  weather dashboard's positions table, for visual consistency); realized
  P&L chart over time (Chart.js line chart, matching existing chart
  styling); per-wallet P&L breakdown table; a toggle to compare realized
  P&L against the original backtest's flat-stake projection for the same
  wallets (this is the paper-mode-vs-backtest check the architecture
  doc's phase 7 go/no-go gate depends on — it needs to be visible here,
  not computed ad hoc later).
- **Layout:** chart on top, two tables below side by side (open positions
  left, per-wallet breakdown right) on wide viewports, stacked on narrow.
- **Interactions:** click a position to see its source signal (which
  wallet, which trade it copied); date-range control on the P&L chart.
- **States:** default, loading, empty ("No copy-trading positions yet"),
  and a distinct empty state for "positions exist but none are resolved
  yet" (realized P&L chart has nothing to plot even though open positions
  aren't empty — don't render an empty chart with no explanation).

### Activity Feed

- **Purpose:** near-real-time visibility into what the signal/execution
  pipeline is doing, for diagnosing "why didn't this get copied."
- **Key components:** reverse-chronological list of events (signal
  detected, order placed, order skipped + reason, wallet auto-paused +
  reason); filter by wallet and by event type.
- **Layout:** single scrolling list, most recent first, auto-refreshing.
- **Interactions:** filter dropdowns at top; click an event to jump to
  the relevant wallet's detail (Candidates or Followed Wallets).
- **States:** default, loading, empty ("No activity yet"), and
  auto-refresh-paused indicator if the feed's polling fails (don't let it
  silently go stale with no indication).

## Design tokens / references

No existing design-token system in this dashboard — it's hand-styled CSS
in `index.html`. Match the existing visual language (fonts, colors,
spacing, card/table styling) rather than introducing a new look; if a
token system gets extracted later that's a separate, cross-cutting effort
and out of scope for this feature.

## Accessibility notes

- All tables need proper `<th>` headers and sortable-column controls
  reachable by keyboard (existing dashboard tables should be audited for
  this as a baseline — match whatever pattern they already use).
- Pause/resume/unfollow/follow controls need visible focus states and
  accessible labels (not icon-only buttons with no `aria-label`).
- The instability warning badge must not rely on color alone (icon + text,
  not just a red dot) — this is a genuinely risk-relevant signal, it needs
  to be legible to colorblind users too.

## Open questions

- Does the "Follow" action require any confirmation step (e.g., showing
  the wallet's stability history) or is a single click + stake amount
  enough? Given a bad follow decision has real financial consequences
  once execution (Epic B) exists, lean toward requiring the operator to
  at least see the stability sparkline before confirming — needs
  designer sign-off during Epic F execution, not decided here.
- Exact refresh cadence for the Activity Feed (poll interval) — depends on
  Epic A's screening cadence decision in the architecture doc.
