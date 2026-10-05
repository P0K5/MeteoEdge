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

> **Owner decision (via Tech Lead PM): #1290 ships as scoped below.** Section 1
> (Live status unavailable) is binding and unchanged. The inline stake editor,
> the inline `followed-stake-error` element and the sticky icon-only 375px
> actions row are **DEFERRED to a follow-up issue** and are NOT part of #1290.
> Sections 2-4 below give the interim behaviour that #1290 implements; the
> deferred design is kept at the end of this section for the follow-up.

### 2. Stake validation errors (interim, #1290)

Validation errors (empty/<=0, over cap) appear in the row's existing
`<p class="followed-live-msg" role="status" aria-live="polite">` element
(`live-followed-live-msg-{safeId}`), not in the page banner. The banner stays
for server/network failures only.

- Add a modifier class `.followed-live-msg-error{color:var(--no);}` while an
  error is shown; remove it when the message is cleared. The text itself must
  start with `Could not set live stake: ` so the error is never colour-only.
- Copy (replaces the banner text for validation):
  - Not a number / <=0: **`Could not set live stake: enter an amount above $0, or clear the field to follow the paper stake.`**
  - Over cap: **`Could not set live stake: $X.XX is over this wallet's $CAP live cap. Enter $CAP or less.`**
- One message at a time per row; cleared at the start of the next attempt and
  on success. Nothing is written and no `window.confirm` opens on a validation
  failure; the operator re-clicks the action (native `prompt` cannot re-ask).
- Entry stays on `window.prompt` (stake) then `window.confirm` (final), exactly
  as in `copy-trading-live-views.md`. Paper-stake editor copy is unchanged.

### 3. Inheriting-wallet clarity (interim, #1290)

No new helper element. The inherit case is made explicit in the prompt and by
never writing an unchanged value.

**Prompt copy, wallet currently inheriting** (`live_stake_is_override=false`;
prefill = resolved stake = paper stake):

```
Set a live stake per trade for {address}.

This wallet currently follows its paper stake ($5.00 per trade) and will change automatically when the paper stake changes.

Leave the amount as it is (or clear it) to keep following the paper stake.
Enter a different dollar amount to fix the live stake at that amount.

Live per-wallet exposure cap: $CAP.
```

**Prompt copy, wallet has an override** (prefill = override, e.g. $2.00):

```
Set a live stake per trade for {address}.

This wallet's live stake is fixed at $2.00 per trade (its paper stake is $5.00).

Keep the amount to leave it unchanged. Enter a different amount to change it, or clear the field to follow the paper stake instead.

Live per-wallet exposure cap: $CAP.
```

**Write rule (amends the earlier "accepting the pre-fill = explicit override"):**
compare the entered value with the resolved stake shown. Unchanged value ->
keep the wallet's current state (inheriting stays inheriting; an override stays
the same override); send no stake change. Changed value -> explicit override.
Cleared field -> inherit. This stays safe for the re-enable hazard (override
persists and is the prefill), and a wallet never silently flips from inherit
to a fixed value just by pressing OK.

Roster stake line (replaces the 10px `(inherits paper)` span), unchanged ruling:
**`Live: $5.00 · follows paper stake`** with the second part at `--text-xs`,
`--text`. Override case shows only `Live: $5.00`.

Final `window.confirm` copy when inheriting:
**`Go live for {address}?\n\nReal trades of $5.00 each, following this wallet's paper stake (it will change if the paper stake changes), up to $CAP live exposure for this wallet.\n\nPaper trading continues unchanged.`**
When overriding: replace the middle clause with `Real trades of $X.XX each (fixed), up to $CAP live exposure for this wallet.`

### 4. 375px actions (interim, #1290): stack like the Paper roster

No sticky column, no icon-only buttons. Below `max-width:600px` the Live roster
uses the **same stacked-block rule the Paper roster already has** (the
"Narrow screens" `.copy-table--roster` block): extend that selector to the Live
roster rather than writing new CSS.

- Each wallet row becomes a wrapping block; the Actions cell is
  `display:flex;flex-wrap:wrap;gap:var(--space-2);` with full-text labelled
  buttons (`Edit live stake`, `Revert to paper`, `Unfollow`; `Go live ->` in
  Paper), each `min-height:44px` so wrapping rows are tappable.
- `Unfollow` is last in DOM order, with `margin-left:var(--space-3)` and its
  existing `--no` styling.
- At 601px and above nothing changes.
- No horizontal page scroll; the block fits the viewport (#1286 guarantee).

### DEFERRED (follow-up issue, not in #1290): inline editor + sticky actions

Kept for the follow-up; implementers of #1290 must not build any of this.
- Inline `.followed-stake-edit` editor replacing `window.prompt` for Go live /
  Edit live stake, with `<p class="followed-stake-error" role="alert">` under
  the input (`aria-invalid`, `aria-describedby`, `alert-circle` icon), field
  helper `Leave blank to follow this wallet's paper stake ($5.00). It changes
  automatically when the paper stake changes.` and placeholder
  `Follow paper ($5.00)`. Final `window.confirm` stays.
- 375px sticky-right Actions column, icon-only 44px buttons with `aria-label`/
  `title`, `Go live ->` as a full-width 44px labelled button under the address,
  open editor as a full-width block under the row.

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
4. Roster Actions column: stays as shipped by #1290 section 4 (stacked, labelled
   buttons below 600px). The sticky-right icon-only Actions column is part of the
   DEFERRED roster redesign; #816 must not assume it. Roster tables therefore keep
   their #1290 stacked layout under 600px until the follow-up lands; the sticky
   first-column pattern applies to all other tables.
5. Cells stay `white-space:nowrap`; min column widths not forced. No card/stacked layout anywhere.
6. Wrapper gets `tabindex="0"` and `role="region"` with `aria-label` = the
   table's label so keyboard users can scroll it.

Dev order: the #1291 column order is already shipped. Do not replace the
`.copy-table--roster` stacked block for the rosters in #816 (see item 4); only
non-roster tables adopt the sticky-first-column pattern.

[Tech constraint: UNVERIFIED. No feasibility check on the #816 table pattern
(sticky first column inside the `overflow-x:auto` wrapper, interplay with the
#1286 `position:relative` wrap and the `.copy-table-wrap:has(.copy-copy-btn)`
padding, and the `.copy-feedback-label` overlay clipping) has been done. The
Tech Lead PM must confirm it, and the agreed approach be recorded here, BEFORE
#816 starts.]

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
