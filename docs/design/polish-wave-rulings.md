# Design Rulings — Frontend Polish Wave (#1290, #1291, #602, #816)

Status: approved by Design-Agent; feasibility check with Tech Lead PM is the
PR review of this file. All files: `src/dashboard/static/index.html`.
No new design language: every ruling reuses an existing token or class.
Where this file conflicts with `copy-trading-live-views.md` or
`copy-trading-tab-split.md`, this file wins (those specs are amended in the
same PR).

---

## #1290 — Live-status unavailable, stake errors, inherit copy, 375px actions

### 1. "Live status unavailable" (neutral, never PAPER)

Today an unknown live status (reason missing, or the wallets payload failed)
falls through to the amber `PAPER` badge. That asserts a fact we do not
have. Ruling: unknown is its own state, visually neutral (not amber, not
green, not red).

- **Badge** `.mode-badge-unknown`:
  `background:var(--surface-off);color:var(--text);border:1px solid var(--border);`
  `letter-spacing:0;text-transform:none;font-weight:600;`
  Icon `help-circle` 12px, then text exactly **`Live status unavailable`**
  (may wrap; do not abbreviate).
  `aria-label="Live status unavailable — could not load live eligibility, retrying automatically"`.
  Contrast: `--text` on `--surface-off` is 14.3:1 light / 10.4:1 dark. Do NOT
  use `--muted` for the text: dark `--muted` on `--surface-off` is 3.35:1 (fails).
- **Banner** (Live tab and Paper roster header, shown while the state holds):
  reuse `.warn-banner` geometry but neutral colours via a modifier
  `.info-banner`: `background:var(--surface-2);border:1px solid var(--border);color:var(--text);`
  icon `info` (not `alert-circle`/`cloud-off`). `role="status"` (not `alert`).
  Copy: **`Live status unavailable. Could not load live eligibility for your wallets, so no LIVE or PAPER label is shown. Retrying automatically.`**
  Auto-hides on the next successful load. No dismiss button, no retry button.
- **Behaviour while unavailable:** every roster row shows the unknown badge;
  `Go live` is `disabled` with `title="Live status unavailable"`; `Revert to
  paper`, Pause, Unfollow stay enabled (risk-reducing actions are never blocked).
- Mapping rule (replaces the "conservative fallback to PAPER" in
  `copy-trading-live-views.md`): `live_status_reason` null/undefined, or the
  fetch errored -> unknown badge. Known reasons keep their current badges.
  Unknown *reasons* (a string we do not recognise) with `live_enabled=false`
  still render `PAPER`.

### 2. Per-row stake error

Validation errors (empty/<=0, over cap) appear **in the row, directly under
the stake input**, not in the page banner. The banner stays for
server/network failures only.

- Element: `<p class="followed-stake-error" id="followed-stake-err-{safeId}" role="alert">`
  inside `.followed-stake-edit`'s cell, below the input+Save row;
  `font-size:var(--text-xs);color:var(--no);margin-top:var(--space-1);`
  with a leading `alert-circle` 12px icon (error is never colour-only).
- Input gets `aria-invalid="true"` and `aria-describedby` -> the error id; the
  input keeps focus and its value. Error clears on next `input` event or
  successful save. One error at a time per row.
- Copy:
  - Empty/not a number/<=0: **`Enter an amount above $0, or clear the field to follow the paper stake.`** (clear-to-inherit applies to the live-stake editor only; in the paper stake editor use **`Enter an amount above $0.`**)
  - Over cap: **`$X.XX is over this wallet's $CAP live cap. Enter $CAP or less.`**
- Consequence: the live-stake entry (Go live, Edit live stake) moves from
  `window.prompt` (cannot host an inline error) to the existing inline
  `.followed-stake-edit` editor in the row. The final real-money
  `window.confirm` stays unchanged in structure (copy below).

### 3. Inheriting-wallet copy

Field helper (below the input, `--text-xs`, `color:var(--text)` in both themes;
`--muted` fails 4.5:1 in dark):

- Input `placeholder`: `Follow paper ($5.00)` (paper stake substituted).
- Helper: **`Leave blank to follow this wallet's paper stake ($5.00). It changes automatically when the paper stake changes.`**

Roster stake line (replaces the 10px `(inherits paper)` span): 
**`Live: $5.00 · follows paper stake`** — second part at `--text-xs`
(not 10px), `--text`. Override case shows only `Live: $5.00`.

Final confirm (`window.confirm`) when inheriting:
**`Go live for {address}?\n\nReal trades of $5.00 each, following this wallet's paper stake (it will change if the paper stake changes), up to $CAP live exposure for this wallet.\n\nPaper trading continues unchanged.`**
When overriding: replace the middle clause with `Real trades of $X.XX each (fixed), up to $CAP live exposure for this wallet.`

### 4. 375px action layout (Paper and Live rosters)

Single rule, shared with the #816 wide-table pattern (no stacked-block layout):
at `max-width:600px` the Actions cell is the **sticky-right** column and renders
icon-only buttons in one row: `min-width:44px;min-height:44px`, `gap:var(--space-2)`,
`aria-label` and `title` keep the full text ("Pause", "Edit stake", "Unfollow").
Order left to right: `Pause/Resume`, `Edit stake`, then `Unfollow` last, with
`margin-left:var(--space-3)` extra separation and `--no` styling (destructive
action never adjacent-by-accident). Live roster: `Edit live stake`,
`Revert to paper`, `Unfollow`.

- `Go live` / `Go live ->` is a labelled full-width 44px button placed inside the
  first (Wallet) cell under the address, not an icon, so the primary action is
  never ambiguous and the sticky-right cell stays at three icons (~148px).
- Stake editor open: it replaces the row's actions with a full-width block under
  the row: input full width, `Save | Cancel` 50/50 beneath, error (section 2)
  under that.
- At 601px and above the Actions cell keeps today's wrapping label buttons.

---

## #1291 — Go-live highlight, reduced motion, Recent Closed addresses, P&L order

### 1. Neutral highlight (replace amber)

Amber means "paper". A row we jump to is not paper. Replace the keyframes:

```css
@keyframes copy-row-flash{
  0%,60%{background:var(--primary-bg);box-shadow:inset 3px 0 0 var(--primary);}
  100%{background:transparent;box-shadow:none;}
}
```
`--primary-bg`/`--primary` are the neutral "focus/selection" pair already used
for active tabs/pills. Duration/ease unchanged (2.4s ease-out).

### 2. Reduced motion

```css
@media (prefers-reduced-motion: reduce){
  html{scroll-behavior:auto;}
  .copy-row-highlight{animation:none;background:var(--primary-bg);box-shadow:inset 3px 0 0 var(--primary);}
  .live-dot,.skeleton,.spinner{animation:none;}
}
```
- JS: if `matchMedia('(prefers-reduced-motion: reduce)').matches`, call
  `scrollIntoView({block:'center'})` (no `behavior:'smooth'`; applies to the
  go-live jump and the other smooth-scroll sites) and remove
  `copy-row-highlight` after 2400 ms via `setTimeout` so the static highlight
  does not persist.
- Spinner: under reduced motion keep the control's text label visible
  (`Saving…`) so state is not conveyed by rotation alone. Skeleton: static
  `--surface-off` block.

### 3. Recent Closed address cell — tap/keyboard

Both Recent Closed tables (Paper and Live) use the **same** cell
(`_followedAddressCellHtml`); Paper currently renders bare text, so a copy
control is added there.

- Address text is not interactive; no row click action.
- Copy is the native `<button class="copy-copy-btn">` (focusable via Tab,
  Enter/Space activate, `aria-label="Copy wallet address {truncated}"`).
- Hit area: visual icon stays 12px; at `max-width:600px` the button gets
  `min-width:44px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;margin:-12px 0` so row height does not grow.
- Feedback: existing `.copy-feedback-label` plus an `aria-live="polite"`
  `.copy-sr-status` announcement `Address copied`. Failure text unchanged
  (fallback select-input).
- Focus ring: existing `outline:2px solid var(--primary);outline-offset:1px`.
- Truncated address keeps `title` for hover; on touch, the full address is
  reachable by copy (no long-press tooltip dependency).

### 4. Column order (both Recent Closed tables, all widths)

One order everywhere, no breakpoint-dependent reordering:
**Market | Settled P&L | Stake | Closed | Wallet**.

(Paper header text: `Market | Paper settled P&L | Paper stake | Settled | Wallet`;
Live: `Market | Settled P&L | Stake | Closed | Wallet`.) At 375px the sticky
first column (Market) plus P&L are both visible without scrolling (see #816
pattern). Paper and Live now share the same order; today they differ.

---

## #602 — Light-theme contrast tokens (11px bold badge text, need 4.5:1)

WCAG relative luminance, sRGB. Current light values:

| Pair (text on bg) | Ratio | Result |
|---|---|---|
| `--yes #1e7a42` on `--yes-bg #d8f0e3` | **4.47** | FAIL |
| `--yes #1e7a42` on `--yes-dim #e8f7ee` (pending badge) | 4.84 | pass |
| `--warn #9a6010` on `--warn-bg #fef0d8` | 4.60 | pass, thin |
| `--warn` on `--surface-dyn #e3e8f2` | 4.22 | FAIL (if ever used there) |

**Ruling (light theme only; dark theme untouched, already 7.26 / 7.80):**

```css
[data-theme="light"]{
  --yes:#1a6f3a;      /* was #1e7a42 */
  --warn:#8a5309;     /* was #9a6010 */
  /* --yes-bg #d8f0e3, --yes-dim #e8f7ee, --warn-bg #fef0d8 unchanged */
}
```

Computed ratios for the new values:

| Text | on `--yes-bg` | on `--yes-dim` | on `--warn-bg` | `--surface` #fff | `--surface-2` | `--surface-off` | `--bg` | `--surface-dyn` |
|---|---|---|---|---|---|---|---|---|
| `--yes #1a6f3a` | **5.18** | 5.61 | n/a | 6.22 | 5.86 | 5.45 | 5.55 | 5.06 |
| `--warn #8a5309` | n/a | n/a | **5.62** | 6.32 | 5.96 | 5.55 | 5.65 | 5.14 |

Every pair >= 5.06:1, so all surfaces clear 4.5:1 with margin. Hue is
preserved (same green / same amber, ~10% darker). Backgrounds are unchanged
so badge look stays recognisable; no token rename (test
`test_dashboard_copy_trading_mode_badge.py` asserts only the `var(--yes...)` names).

**Consumer check** (grep of `var(--yes)`/`var(--warn)` in `index.html`; no
hard-coded copies of the old hex values exist anywhere in the repo; five
chart sites read tokens via `getPropertyValue`, so they follow automatically):

- Text on tinted bg (badges/pills): live-pill, `.gate-traded_live`,
  `.gate-shadow_only`, `.side-yes`, `.result-won`, `.result-stop_loss`,
  `.badge-active/disabled/shadow/primary`, `.side-mode-live/shadow`,
  `.mode-badge-live/paper/live-pending`, `.bias-badge-*`, `.followed-status-*`,
  `.copy-followed-tag`, `.badge-unstable/truncated`, `.edge-d1-indicator`,
  `.edge-scan-freshness.stale`, `.btn-follow`, `.btn-followed-golive`,
  `.btn-toggle.enable`, `.btn-emos.btn-promote`, `.btn-cfg.btn-save` — all on
  `--yes-bg`/`--yes-dim`/`--warn-bg`/hover `--yes-dim`: >= 5.18:1. Pass.
- Text on neutral surfaces (`.edge-ev-value.positive`, `.wc-value.pos`,
  `.val-target`, `.closed-pnl.win`, `.prob-sub.up/down`, `.perf-th-side`,
  `.perf-cell-pnl.pos`, `.perf-ready-tag` (10px bold), `.copy-pnl-pos`,
  `.copy-follow-reason`, `.copy-followed-note`, `.followed-stake-live`,
  `.copy-feedback-label.success`): on `--surface`/`-2`/`-off`/`--bg` >= 5.45:1. Pass.
- `.edge-decision-table tr.row-traded` (`--yes` text over `--yes-bg`): 5.18. Pass.
- Non-text (borders, dots, fills, progress bars, accent rails, 3px top rule
  on `.wallet-card--paper`): need 3:1; darker values only increase contrast. Pass.
- Hover state `--yes-dim` backgrounds with `--yes` text: 5.61. Pass.
- Visual-regression note for QA: only `--yes`/`--warn` get ~10% darker in
  light theme; no layout change.

---

## #816 — Responsive design spec

### Breakpoints (three, one set)

| Name | Range | Rule |
|---|---|---|
| phone | `max-width:600px` | single column content, 2-up KPI cards, 44px touch targets |
| tablet | `601px–900px` | 2-col grids, tables scroll if needed |
| desktop | `>900px` | current desktop layout; `.main` 980px, `.main--wide` 1280px |

Consolidate the stray `800px` media queries (`.copy-search-*`, `.copy-detail-grid`)
into `900px`. No other widths are introduced. Add `viewport-fit=cover` to the
meta viewport and use `env(safe-area-inset-*)` in topbar/tab-bar side padding.
Declare `:root{--topbar-h:58px;--tabbar-h:52px}` (phone: `52px`/`48px`) and
use them for the tab-bar `top` offset instead of the literal `58px`.

### Topbar (phone)

- Height `var(--topbar-h)` 52px, side padding `var(--space-3)`.
- Logo: show `.logo-img` only; hide `.logo-text`/`.logo-sub`.
- Live pill: keep dot + short text; padding `4px 8px`; never wraps.
- Theme button: 44x44 hit area. Spacer stays flex:1.
- Nothing else lives in the topbar; no hamburger (the tab bar is the nav).

### Tab bar (phone and tablet) — keep top, scrollable, not a bottom bar

Nine tabs do not fit a bottom bar; the current horizontal scroller is kept
and made discoverable.

- `.tab-btn`: `min-height:44px;padding:0 var(--space-3)`; gap `var(--space-2)`.
- `scroll-snap-type:x proximity` on the bar, `scroll-snap-align:center` on tabs.
- On `switchTab`, call `activeBtn.scrollIntoView({inline:'center',block:'nearest'})`
  (instant under reduced motion) so the active tab is never hidden.
- Edge affordance: `mask-image:linear-gradient(to right,transparent 0,#000 16px,#000 calc(100% - 24px),transparent 100%)`
  applied only when the bar overflows (JS toggles `.is-overflowing`). Hide
  the scrollbar (`scrollbar-width:none`).
- Active state unchanged (primary text + 2px underline).

### Page chrome

- `.main` side padding `var(--space-3)` at phone, `var(--space-4)` at tablet, unchanged above.
- `.wallet-row` / `.wallet-row--live`: 2 columns at phone and tablet; 1 column at `max-width:360px`.
- Section headers already `flex-wrap:wrap`; keep. Pagers wrap and use `.pg-btn{min-height:44px}`.
- Charts: fixed height (`220px` phone), width 100%; legends below.
- No horizontal page scroll at 320px is an acceptance criterion; only table
  wrappers scroll.

### ONE wide-table pattern: scroll container + sticky first column

Replaces the stacked-block roster pattern (`.copy-table--roster` at <=600)
and any bare overflow. Applies to every data table (`.copy-table`, perf, edge decision, closed lists).

1. Table sits in `.table-wrap` (existing `.copy-table-wrap`):
   `overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain;`
   with a right-edge shadow cue `background:linear-gradient(to left,var(--surface) 0,transparent 16px)` shown while scrollable.
2. First column is sticky: `th:first-child,td:first-child{position:sticky;left:0;background:var(--surface);z-index:1;max-width:40vw;overflow:hidden;text-overflow:ellipsis;}`
   plus `box-shadow:1px 0 0 var(--divider)`. First column = the row identity
   (Market, or Wallet in rosters).
3. Column order is fixed by priority, key figure second (P&L / status), never
   reordered per breakpoint. Secondary columns trail and scroll.
4. Roster Actions column: sticky **right** at phone, icon-only, exactly as #1290 section 4.
5. Cells stay `white-space:nowrap`; min column widths not forced. No card/stacked layout anywhere.
6. Wrapper gets `tabindex="0"` and `role="region"` with `aria-label` = the
   table's label so keyboard users can scroll it.

Dev order: implement #816 table pattern together with #1290 section 4 and
the #1291 column order; do not ship the old stacked `.copy-table--roster` block.

[Tech constraint: none identified; pending PM feasibility confirmation on
replacing `.copy-table--roster` — agreed approach to be recorded here.]

### States (all new surfaces)

- Loading: existing `.skeleton` rows sized to final row height.
- Empty: existing `.empty` block inside the wrapper (no scroll chrome).
- Error: existing `.error-banner`/inline error; unknown-live-status uses the neutral banner from #1290.

### Acceptance checklist

- 320, 375, 768, 1024, 1280 widths: no page-level horizontal scroll.
- All interactive targets >= 44x44 CSS px at phone.
- Active tab visible after every tab switch.
- Every table passes keyboard scroll and shows the scroll cue.
- Reduced-motion rules (#1291 section 2) honoured by all new transitions.
