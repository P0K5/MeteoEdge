# Design Spec — Copy-Trading Live/Paper Dashboard Views (Epic J)

Addendum to `docs/design/copy-trading-dashboard.md` (Epic F, shipped) and
`docs/design/copy-trading-architecture.md` (Epic J, #1161). Read the Epic F
spec first — this document only covers the delta: what changes in
**Positions & P&L**, **Followed Wallets**, and **Activity Feed** now that
Epic H (live order execution, `copy_live_positions`) and Epic I (live
settlement/reconciliation, live circuit breaker) exist and are merged.
Candidates and the rest of Epic F's layout, interactions, and states are
unchanged and not repeated here.

## Why this exists (read before designing anything new here)

Per the epic's own acceptance criteria: live and paper numbers must **never
be blended into one total**, and mistaking a paper figure for a live one is
a real trust/safety failure, not a cosmetic issue. Every design decision
below is evaluated against one question: *if an operator glances at this for
two seconds, could they mistake a paper number for a live one, or vice
versa?* If the answer isn't a confident "no," the design isn't done.

**Precedent to reuse, not reinvent** (already in
`src/dashboard/static/index.html`, verified before writing this spec):

- `.side-mode-badge` + `.side-mode-live` (green, `var(--yes-bg)`/`var(--yes)`)
  / `.side-mode-shadow` (amber, `var(--warn-bg)`/`var(--warn)`) — uppercase
  pill badges, `display:inline-flex;gap:4px`, used for the weather
  strategy's per-side live/shadow state (lines ~613-621).
- Every such badge carries an explicit `aria-label` stating the state in
  words (`"YES side: live"` / `"YES side: shadow"`) — never color-only, per
  that code's own comment (lines ~2337-2338).
- Trade-history text convention: `execution_mode === 'live'` → **"Traded
  live"**, anything else (including missing/unrecognized) → **"Traded
  (paper)"** — the conservative reading is the default; "live" is never
  claimed without an explicit, confirmed signal (lines ~3205-3247,
  `gateTooltip()` / `gateChipHTML()`).

This spec reuses the **visual pattern** (colors, pill shape, uppercase
weight, explicit aria-label, "assume paper unless proven live" default) but
**not the literal word "SHADOW"** — copy-trading's own domain vocabulary,
established consistently across Epics A–I (`copy_positions` vs.
`copy_live_positions`, "paper mode," the Epic F spec's own "Traded (paper)"
convention, the issue title itself), already says **"paper," never
"shadow."** Carrying "SHADOW" over here would introduce a second word for
the same concept inside one product. New class names, to avoid coupling to
the weather strategy's per-*side* (YES/NO) semantics, which isn't the axis
copy-trading badges vary on:

```css
.mode-badge{ /* structurally identical to .side-mode-badge */
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 8px;border-radius:var(--radius-full);
  font-size:var(--text-xs);font-weight:700;letter-spacing:.05em;
  flex-shrink:0;
}
.mode-badge-live{background:var(--yes-bg);color:var(--yes);}
.mode-badge-paper{background:var(--warn-bg);color:var(--warn);}
```

Badge text is always the word **LIVE** or **PAPER**, uppercase, never an
abbreviation or icon alone.

## Global: a tab-wide live/paper posture banner

Because `COPY_LIVE_TRADING_ENABLED` (Epic G) is a single **global** kill
switch — not per-wallet — read once per cycle in
`src/scripts/copy_signal_loop.py` and applied uniformly to every active
followed wallet, the Copy-Trading tab needs one persistent, unmissable
indicator of the platform-wide posture, visible across all four views (not
just Positions & P&L), so an operator on Followed Wallets or the Activity
Feed never loses track of whether live is even switched on:

- A banner/pill pinned near the Copy-Trading tab header (same sticky region
  as the existing dashboard's `.live-pill`, but a distinct element — that
  existing pill denotes data-connection freshness, not trading mode, and
  must not be repurposed): **"LIVE TRADING ON"** (green, `mode-badge-live`
  styling) or **"LIVE TRADING OFF — paper only"** (amber,
  `mode-badge-paper` styling, plus the literal words "paper only" so the OFF
  state reads as a statement about current behavior, not just an inert
  switch position).
- This banner is a landmark region for screen readers (`role="status"` or
  equivalent, `aria-live="polite"` if it can change without a page reload —
  it can, via the config API / `halt_live_copy_trading.py`).

## Positions & P&L

**Decision: twin panels, not a toggle.** A single view with a toggle risks
an operator misreading whichever regime happens to be selected as "the"
number, especially after a page refresh where the toggle's prior state
isn't obvious at a glance. Twin panels — both always visible, always
labeled — make blending structurally impossible rather than just
discouraged. `[Tech constraint: twin panels means two independent sets of
API calls/renders instead of one toggled dataset, and needs a call on
narrow-viewport stacking order — flagging for Tech Lead PM feasibility
confirmation before implementation; if two live API round-trips per poll is
a real cost concern, the fallback is a toggle with a hard-coded persistent
banner announcing which regime is showing, but twin panels is the design
default given the "never blended" requirement.]`

- **Layout:** two side-by-side columns on wide viewports — **Live always
  left, Paper always right**, a fixed order repeated everywhere in this
  epic so operators build one stable mental model. Stacked vertically on
  narrow viewports, Live column first. Each column has its own heading
  (`<h3>LIVE</h3>` / `<h3>PAPER</h3>`) using the `.mode-badge` styling,
  its own open-positions table, its own realized-P&L chart, and its own
  per-wallet breakdown table — the exact shape Epic F already specified,
  just not sharing a single aggregate number across the two.
- **Summary strip:** the existing "# active / # paused / aggregate P&L"
  strip splits into two qualified numbers — **"Paper aggregate P&L"** and
  **"Live aggregate P&L"** — never a single "aggregate P&L" anywhere in
  this view, including any future CSV/API export: no server-side sum across
  `copy_positions` and `copy_live_positions` should ever be computed or
  returned as one figure. `[Tech constraint: confirm with Tech Lead PM that
  the API layer never introduces a combined-total field even as a
  convenience — agreed approach: two independent response keys, always.]`
- **Backtest-comparison toggle** (Epic F's phase-7 go/no-go feature) stays
  in the **Paper** column only — the backtest was a flat-stake paper
  projection; it has no live counterpart yet and must not appear to.
- **Click-through / date range:** unchanged from Epic F, applied
  independently per column.

### States (both columns, independently)

- **Default:** populated, as above.
- **Loading:** skeleton per column — a slow live fetch must not block or
  blank the paper column, and vice versa (isolation applies to rendering,
  not just data).
- **Empty, live trading OFF (global switch off):** the Live column shows a
  **dedicated off-state**, not "$0.00" and not a plain empty table — a
  live P&L of literal zero and "live isn't running" must never look the
  same. Render it dimmed/neutral (not green, not the live accent) with the
  text **"Live trading is off"** plus a link to wherever the switch lives
  (config panel or docs), and no numeric figures at all in that column
  (not even a zero).
- **Empty, live trading ON but no live positions yet:** Live column keeps
  its live accent (green) but body text reads **"No live positions yet."**
  This is deliberately visually distinct from the OFF state above — same
  accent color as populated live data, different text, no numbers pretending
  to be a real zero-P&L reading versus an absent one.
- **Paper empty / unresolved-only:** unchanged from Epic F ("No
  copy-trading positions yet" / realized-P&L-chart-has-nothing-to-plot
  distinct empty state).
- **Error:** each column gets its own stale-data banner — a live-fetch
  failure shows the Live column's last-known-good data with its own banner;
  it must never blank the page or, worse, silently fall back to showing
  paper data in the Live column's slot.
- **Reconciliation required (Epic I balance-drift):** if the backend
  surfaces a live wallet-balance drift beyond
  `COPY_LIVE_BALANCE_DRIFT_TOLERANCE_USD` (`check_wallet_balance_drift` in
  `src/scripts/copy_live_settle.py`), the Live column shows a high-priority
  warning banner above its content: **"Live balance mismatch detected —
  manual reconciliation required"** (do not soften this language; it is a
  real-money discrepancy). `[Open question — needs Tech Lead PM
  confirmation: is a drift-status read endpoint in scope for Epic J, or is
  this a fast-follow? If out of scope for this epic's PRs, this state ships
  as a documented but not-yet-wired UI affordance, and the open question
  below tracks it explicitly rather than silently dropping it.]`

## Followed Wallets

Add a **second, independent** status badge per wallet row — paper
follow-status (existing: active/paused) and live-eligibility status (new)
are two separate axes, exactly as the issue specifies: a wallet can be
paper-followed without being live-enabled.

`[Tech constraint: there is no per-wallet `live_enabled` column in
`copy_wallets_followed` today. `COPY_LIVE_TRADING_ENABLED` (Epic G) is a
single global switch, and per-wallet live exposure
(`COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD`, Epic G) is enforced at
signal-execution time in `copy_signal_loop.py`, not stored as a queryable
per-row flag — agreed approach, pending Tech Lead PM feasibility
confirmation: derive the badge rather than add new schema. Two acceptable
derivations, in order of preference — Tech Lead PM to confirm which is
cheaply queryable for a table view:
(a) **preferred** — LIVE when the global switch is on AND this wallet's
current committed live exposure is below its per-wallet cap (i.e., it would
actually be eligible to execute a live order right now);
(b) **fallback** — LIVE when the global switch is on AND the wallet's
paper status is `active` (coarser: doesn't reflect a temporarily
exhausted per-wallet cap, but is honest about the global gate and requires
no new query). This spec is written against (a) but ships as (b) if (a)
isn't feasible without a schema/query change — either way, the badge must
never claim LIVE for a wallet that is `paused`.]`

- **Badge:** `.mode-badge-live`/`.mode-badge-paper`, text **LIVE** / **PAPER**,
  placed next to the existing paper active/paused status in the row (not
  replacing it — a wallet can be simultaneously "Active" (paper) and
  "PAPER" (live-badge), or "Active" (paper) and "LIVE" (live-badge); a
  paused wallet is always "PAPER" regardless of the global switch, since a
  paused wallet executes nothing in either mode).
- **aria-label** spells out the reason, not just the state, e.g. `"Live
  status: paper only — live trading is currently off"` or `"Live status:
  paper only — this wallet's live exposure limit is currently reached"` or
  `"Live status: live — eligible for live execution"`. Never rely on the
  badge color alone (same accessibility rule as the Epic F instability
  badge).
- **Table-level banner vs. per-row noise:** when the global switch is off,
  every row would otherwise show an identical PAPER badge with an identical
  tooltip — noisy. Add one banner above the table: **"Live trading is
  currently off — all wallets are paper-only."** The per-row badges stay
  present regardless (a screenshot of a single row must still be
  unambiguous on its own — never rely on surrounding page context for a
  safety-relevant signal).
- **Summary strip:** extend the existing "# active / # paused / aggregate
  P&L" strip with **# live-eligible** / **# paper-only**, and split
  "aggregate P&L" the same way as Positions & P&L (paper aggregate / live
  aggregate, never combined).

### States

Unchanged from Epic F (default/loading/empty/row-level action error) with
one addition: the live badge and table-level banner degrade to "PAPER" /
"off" language whenever the live-status derivation can't be computed (e.g.
API error fetching live config) — never guess LIVE when the source of truth
is unavailable, same conservative-default rule as the `execution_mode`
precedent.

## Activity Feed

Every event row gains a required **mode indicator** — LIVE or PAPER — using
the same `.mode-badge` component, plus explicit event text (never rely on
the badge alone; the badge and the text must independently make the mode
clear). Default to PAPER when a mode can't be determined for an event,
mirroring the `execution_mode` fallback rule verbatim.

**Paper events (Epic F, relabeled for clarity now that live exists
alongside them):**
- "Signal detected"
- "Order placed (paper)"
- "Order skipped (paper) — `{skip_reason}`"
- "Wallet auto-paused — `{paused_reason}`"

**Live events (new, from `copy_live_positions` / live gate reasons in
`copy_signal_loop.py` / `copy_risk_manager.py`):**
- "Live order pending"
- "Live order filled"
- "Live order partially filled"
- "Live order rejected — `{rejected_reason}`"
- "Live order skipped — `{live_gate_reason}`" (covers both the Epic G
  startup sanity-check and the Epic I live circuit breaker —
  `live_circuit_breaker_daily_loss` / `live_circuit_breaker_drawdown` —
  surfaced as plain-language reason text, not the raw constant)
- "Live position settled — `{settled_pnl_usd}`"
- "Live circuit breaker tripped — `{reason}`"
- "Live trading halted" / "Live trading resumed" (global switch flips, via
  the config API or `halt_live_copy_trading.py`)
- "Live balance mismatch detected — reconciliation required" (Epic I
  drift check) — a distinct, high-visibility event type, never folded into
  a generic "error" row, matching the language `copy_live_settle.py` itself
  uses internally. `[Open question — same one as Positions & P&L above:
  needs Tech Lead PM confirmation that a drift event is actually emitted
  somewhere queryable for this epic's scope.]`

**Visual treatment:** a colored left-border accent (green/live, amber/paper)
on each row, in addition to the badge and text — three redundant signals
(border, badge, text), because this is exactly the kind of risk-relevant
information the Epic F accessibility notes already insist can't rely on
color alone. Live rejections/skips additionally get a small warning icon
(not a separate panel — keep "why didn't this get copied" in one place per
Epic F's own purpose statement) since a live rejection has real capital
implications a paper skip doesn't.

**Filters:** add a **Mode** filter (Live / Paper / All) alongside the
existing wallet and event-type filters.

### States

Unchanged from Epic F (default/loading/empty/auto-refresh-paused) plus:
- **Empty with Mode=Live selected:** distinguish "live trading has never
  been switched on" ("Live trading hasn't been turned on yet") from "live
  is on, but nothing happened in this window" ("No live activity in this
  range") — the same off-vs-on-but-empty distinction used throughout this
  spec, for the same reason: "No live activity" must never read as
  reassurance if live isn't even running.

## Design tokens / references

- Reuses `var(--yes)` / `var(--yes-bg)` (live/green) and `var(--warn)` /
  `var(--warn-bg)` (paper/amber) exactly as the weather strategy's
  `.side-mode-badge` already does — no new color tokens.
- New class names (`.mode-badge`, `.mode-badge-live`, `.mode-badge-paper`)
  are structurally identical to `.side-mode-badge`/`.side-mode-live`/
  `.side-mode-shadow` but kept distinct because they vary on a different
  axis (live/paper execution regime) than the weather strategy's per-side
  (YES/NO) live/shadow axis — do not literally alias the two, to avoid a
  future weather-strategy CSS change silently altering copy-trading's
  badges or vice versa.
- No new typography, spacing, or layout primitives beyond what Epic F
  already established.

## Accessibility notes

- Every LIVE/PAPER badge carries a full-sentence `aria-label`, not just the
  two-word visible text — never color-only, per Epic F's own precedent.
- Live is always ordered/positioned first (left column, first in stacking,
  first in reading order) everywhere in this epic, consistently, so the
  mental model ("live always comes first") stays stable across views.
- The global live/paper posture banner is a landmark/status region,
  announced on change (`aria-live="polite"`), not a purely visual cue.
- Numbers are never shown with color as the only mode indicator — the word
  LIVE or PAPER (or the qualified label, e.g. "Live aggregate P&L") is
  always adjacent to any dollar figure, so the information survives being
  read aloud, printed in grayscale, or viewed by someone colorblind.
- Table-level "all wallets are paper-only" banners do not replace per-row
  badges (redundancy is intentional here, not noise, given the trust/safety
  stakes) — see Followed Wallets above.

## Open questions

1. **Per-wallet live-eligibility derivation** (Followed Wallets) — needs
   Tech Lead PM feasibility confirmation on whether the "currently within
   its per-wallet live exposure cap" check (preferred) is cheaply queryable
   per row for a table view, or whether the coarser "global switch +
   active" fallback ships instead. See the `[Tech constraint ...]` block
   above.
2. **Reconciliation/balance-drift surface** (Positions & P&L, Activity
   Feed) — Epic I's `check_wallet_balance_drift` exists in
   `copy_live_settle.py`, but is its result exposed anywhere the dashboard
   API can read from today? If not, is adding a minimal read endpoint in
   scope for this epic, or a fast-follow? Both states above are specified
   either way, but this needs a Tech Lead PM call before implementation.
3. **Twin panels vs. toggle** (Positions & P&L) — this spec defaults to
   twin panels (always both visible) as the safer choice against the
   "never blended" requirement; flagging for Tech Lead PM sign-off given
   the added API/render cost of two simultaneous live queries per poll.
4. **Live aggregate P&L when live trading has never been turned on for any
   wallet** — should "Live aggregate P&L" render as the off-state (no
   number) or as a genuine "$0.00" once at least one wallet has ever gone
   live, even if currently off? Leaning toward: once live has ever run
   (any `copy_live_positions` row exists), show the real historical
   aggregate even while currently off, with the off-banner layered on top
   to make clear no *new* live activity is occurring — needs confirmation
   this reads unambiguously in practice, not just on paper.
