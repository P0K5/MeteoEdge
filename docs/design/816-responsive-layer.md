# Design Spec — Responsive Layer (Mobile Breakpoints, Collapsing Grids, Wide-Table Treatment)

Status: **Draft — pending Tech Lead PM feasibility review**
Related: Epic #805 (Group D — responsive/mobile pass), Issue #816
Builds on / generalizes: #809 (Edge table `overflow-x:auto` stopgap, merged), #1286 (topbar/copy-table overflow fix, merged)

## User goal

The operator needs to run the full dashboard — review positions, read the Edge
decision table, adjust Config, check EMOS/Promotion status — from a phone or
tablet without the page scrolling sideways, without losing any control, and
without the desktop layout changing for existing users.

## User flow

1. Operator opens the dashboard on a phone (375px, iPhone SE baseline) or
   tablet (768px).
2. Topbar renders compactly: logo/title truncates (already shipped, #1286),
   Live pill, Refresh, and the theme toggle all stay visible — nothing is
   pushed off-screen.
3. Tab bar renders as a horizontally-scrollable strip of all 9 tabs (already
   shipped behavior — see "Tab bar" below). Tapping any tab scrolls it fully
   into view.
4. Inside a tab, KPI/prob/wallet grids reflow to fewer columns instead of
   staying desktop-wide; station/EMOS card headers wrap onto a second line
   instead of overflowing their card.
5. Any table wider than the viewport (Edge decisions, Stations' per-station
   sub-tables, Promotion, and any future EMOS/Config table) scrolls
   horizontally **inside its own container** — the page itself never
   scrolls sideways.
6. Every action available on desktop (sell position, promote/demote EMOS,
   edit a config value) remains reachable and legible at 375px.

## Breakpoint system

The codebase already has 16 media queries at `max-width:600px` (the de facto
phone cutover) plus a handful of ad hoc tablet-ish values (`800px`, `900px`
in the Copy tabs). Rather than introduce a new, parallel breakpoint and
rewrite 16 proven rules — regression risk for no user-visible benefit — this
spec **keeps 600px as the primary phone breakpoint** and adds two more:

| Name | Width | Role | Status |
|---|---|---|---|
| Tablet | `max-width:768px` | New. For grids/headers that are only cramped between desktop and phone (e.g. 5-column KPI/prob rows), not yet broken enough to need the full phone treatment. | **New** |
| Phone (primary) | `max-width:600px` | Existing, dashboard-wide convention. Grids go to 2-up, headers wrap, tables scroll in their wrap. | **Existing — keep** |
| Small phone | `max-width:480px` | New. For the few elements that are still tight at exactly 600px → 480px (e.g. 3-up wallet row, topbar Refresh label). Covers the 375px test width with margin. | **New** |

`[Tech constraint: issue #816 suggested 768px tablet / 480px-or-600px phone as if choosing one phone value — agreed approach: keep 600px (already proven across 16 rules) as the phone breakpoint, add 768px and 480px as secondary refinements rather than replacing it. Needs Tech Lead PM confirmation before merge.]`

All three are documented here as a **convention**, not a CSS custom property
— media query features can't reference `var()` in this codebase's browser
target, so each rule still hardcodes the px value; new rules must use one of
these three values, not an arbitrary fourth number.

---

## Screen layouts

### 1. Topbar

- **Purpose:** persistent brand + live status + global actions (refresh, theme).
- **Key components:** logo/title block, live pill, Refresh button, theme toggle.
- **Layout:** unchanged structurally (`display:flex`, sticky). Already handles
  375px via #1286 (`min-width:0` + ellipsis on the title block, `flex-shrink:0`
  on the pill/buttons).
- **New rule (480px):** `.btn-outline` ("Refresh") drops its text label and
  renders icon-only below 480px, recovering ~60px for the title block and
  live pill. The refresh icon is universally understood and already paired
  with the existing `refresh-cw` Lucide icon — add `aria-label="Refresh"` so
  it stays announced to screen readers once the visible text is gone.
  ```css
  @media(max-width:480px){
    .btn-outline .btn-outline-label{display:none;} /* wrap existing text in a span if not already */
  }
  ```
  `[Tech constraint: confirm whether "Refresh" text is currently a bare text
  node or already wrapped in a span the CSS can target — if not, the dev
  wraps it as part of implementation, no visual change above 480px.]`
- **States:** no new states. Live pill's existing blink animation is already
  covered by the dashboard's `prefers-reduced-motion` rule elsewhere in the
  file — no separate handling needed here.

### 2. Tab bar

- **Purpose:** primary navigation across the 9 tabs (Portfolio, Stations,
  Edge, EMOS, Promotion, Copy · Wallets/Paper/Live, Config).
- **Current state (verified in code, not a gap):** `.tab-bar` already has
  `overflow-x:auto` + `scroll-padding-inline` + `-webkit-overflow-scrolling:touch`,
  and `switchTab()` already calls `scrollIntoView({behavior:'smooth',
  inline:'nearest', block:'nearest'})` on the clicked tab. This means the
  active tab is never left scrolled out of view, and all 9 tabs are reachable
  without crowding.
- **Decision: keep the scrollable-strip pattern. Do not switch to icon-only
  tabs or condensed-spacing-only.**
  - *Icon-only* is rejected: three of the nine tabs are "Copy · Wallets" /
    "Copy · Paper" / "Copy · Live" — there is no unambiguous icon triad for
    those, and guessing icons for a trading tool's nav actively hurts
    Clarity (principle #1). Text labels stay.
  - *Condensed-spacing-only* (shrink `gap`/`padding` and hope everything
    fits) is rejected: at 9 tabs, no amount of px-trimming makes all labels
    fit in 375px without becoming illegible; it only delays the problem to
    the next tab added.
  - *Scrollable strip* wins: it's the standard mobile nav pattern for
    overflowing tab sets, it's already 90% implemented and shipped, and it
    costs nothing further to make correct.
- **Gap to close:** the strip currently has **no visual affordance** signaling
  there's more to scroll — a user landing on Portfolio has no cue that
  Config is six tabs to the right until they swipe. Recommended addition
  (pure CSS, no JS): an edge fade using the two-background-attachment trick
  (`background-attachment:local,local,scroll,scroll` with gradients), the
  same family of techniques already used for scroll affordances elsewhere on
  the web and compatible with this codebase's plain-CSS style. Flagged as
  **[suggestion — not a blocker for AC]**: the acceptance criteria only
  requires the tab bar be "usable... no forced scroll of critical nav,"
  which the existing `scrollIntoView` already satisfies. Log as a Backlog
  follow-up if the Tech Lead PM agrees it's out of scope for this issue.
- **States:** none new.

### 3. Grids — wallet row, prob row, EMOS/KPI rows

- **Purpose:** compact at-a-glance metric cards.
- **`.wallet-row`** (3-up desktop): already drops to 2-up at 600px. **New:**
  when 3 cards collapse to a 2-column grid, the 3rd card is orphaned alone on
  row 2 at a half-width that looks accidental. Reuse the existing
  `:last-child:nth-child(odd){grid-column:1/-1}` trick already shipped for
  `.copy-paper-kpis` so the lone card spans full width:
  ```css
  @media(max-width:600px){.wallet-row>.wallet-card:last-child:nth-child(3n){grid-column:1/-1;}}
  ```
- **`.prob-row`** (5-up desktop): currently jumps straight from 5-up to 2-up
  at 600px — the exact "still cramped" complaint in the issue. **New:** add
  a 768px step to 3-up first (mirroring the already-shipped `.edge-kpi-row`
  pattern of 5→3→2), so the collapse is gradual:
  ```css
  @media(max-width:768px){.prob-row{grid-template-columns:1fr 1fr 1fr;}}
  @media(max-width:600px){.prob-row{grid-template-columns:1fr 1fr;}}
  ```
  (second rule already exists; only the 768px step is new.) 5 items in a
  2-column grid orphan the 5th card the same way — apply the same span fix:
  `.prob-row>.prob-item:last-child:nth-child(5n){grid-column:1/-1;}` at 600px.
- **States:** none new — these are static layout cards, no loading/error
  state changes.

### 4. Station / EMOS card headers

- **Purpose:** per-station / per-EMOS-model identity row (code, city/name,
  status badges, local time).
- **Problem (code-verified):** both `.station-header` and `.emos-header` are
  `display:flex` with **no `flex-wrap`**. Each has a `flex:1 min-width:0`
  left block and a `flex-shrink:0` right block — `min-width:0` lets the left
  block's *text* truncate, but it does not let the row as a whole narrow
  below the right block's natural width. On a real station with side badges
  + METAR + city on the left and local-time + timezone + status badge
  stacked on the right, 375px is tight enough that this is a page-width-overflow
  risk, not just a cosmetic one — exactly the class of bug #816 exists to
  close.
- **Fix:** allow the header to wrap onto two lines at the phone breakpoint,
  consistent with the `#tab-copy-paper .main > .section-hdr{flex-wrap:wrap;
  row-gap:var(--space-2);}` pattern already shipped for an analogous heading
  row:
  ```css
  @media(max-width:600px){
    .station-header,.emos-header{flex-wrap:wrap;row-gap:var(--space-2);}
    .station-header-right,.emos-header-right{align-items:flex-start;}
  }
  ```
  (`.emos-header-right` — confirm exact class name in `emosCardHTML()`
  during implementation; this spec names the equivalent-role element by the
  `.station-header-right` analogy.) Badges/time move to their own row under
  the code/city line rather than being clipped or forcing the card wider
  than the viewport.
- **States:** none new.

### 5. Config rows

- **Purpose:** key/description/control row per tunable parameter.
- **Current state:** `.config-param` is `flex` with `info{flex:1;min-width:0}`
  + `ctrl{flex-shrink:0}`; the description text already wraps (no
  `white-space:nowrap`), so this one is lower-risk than the headers above.
  **Defensive rule anyway** (the issue explicitly calls out "config headers"
  as unaudited, and a long key name + a `<select>` control is a plausible
  future overflow):
  ```css
  @media(max-width:480px){.config-param{flex-wrap:wrap;row-gap:var(--space-2);}}
  ```
- **States:** none new — `.config-err` inline error text already wraps.

---

## 6. Wide tables — the one dashboard-wide pattern

**Decision: horizontal-scroll-in-its-own-container, generalizing the pattern
#809 already shipped for the Edge table and that already exists for
Promotion (`.promo-table-wrap`) and Copy (`.copy-table-wrap`).**

Applies to every current and future wide table: Edge decisions
(`.edge-table-wrap` — done), Promotion (`.promo-table-wrap` — done),
Stations' per-station `.perf-table` / `.bias-table` / `.deb-table` (**not
yet wrapped — gap this issue closes**), and any table added later to EMOS or
Config.

```css
.table-wrap{overflow-x:auto;position:relative;}
```

- **Why horizontal scroll, not the stacked-card pattern:** the codebase
  already has a *second* wide-table treatment — `.copy-table--roster` turns
  each row into a wrapping, labeled block below 600px. That pattern exists
  because the copy-wallet roster is an **action list** (each row carries
  Pause / Edit stake / Unfollow buttons where 44px touch targets and
  reachability matter more than column alignment). Edge, Stations,
  Promotion, and Config tables are **comparison/scan tables** — the user's
  task is reading across a row to compare Polymarket % vs Model % vs Edge,
  or one station's bias-correction stats against another's. Collapsing those
  into stacked key/value blocks would force the eye to re-scan vertically to
  compare two numbers that are currently one glance apart — a real Clarity
  regression for a 480px screen-width saving. Horizontal scroll keeps true
  column alignment and headers intact; it is also the lower-risk choice
  since it is already proven in production for Edge and Promotion with zero
  reported regressions. **The stacked-card pattern stays reserved for
  action-row tables (Copy roster); this spec does not touch it.**
- **Container must be `position:relative`:** per the #1286 postmortem (test
  `test_copy_table_wrap_scrolls_and_contains_absolute_descendants`), an
  unpositioned scroll container lets any `position:absolute` descendant
  (e.g. `.sr-only` header labels, hover overlays) lay out against the page
  instead of the container, silently re-introducing page-level
  `scrollWidth` overflow. Every new `*-table-wrap` must include
  `position:relative`, not just `overflow-x:auto` — the existing
  `.promo-table-wrap` / `.edge-table-wrap` rules **predate** this fix and
  should be checked/patched to match while this issue is in flight, since
  they are exactly the shape of container the #1286 bug hit.
- **No font-shrinking or column-hiding:** columns keep their current
  `white-space:nowrap` widths; the container scrolls, the table does not
  compress. This matches the already-shipped Edge/Promotion behavior and
  avoids a second new failure mode (illegible shrunk text).
- **Naming:** the issue and existing code use three different class names
  for the identical rule (`.edge-table-wrap`, `.promo-table-wrap`,
  `.copy-table-wrap`). **Open question for Tech Lead PM** below on whether
  to consolidate to one shared `.table-wrap` class now (small, low-risk
  refactor touching 2 shipped rules) or keep per-tab names and just repeat
  the identical rule body for Stations' three new wraps — functionally
  identical either way, this is a DRY/naming call, not a UX call.

### States

No table in scope gains a new loading/empty/error state from this issue —
each table's existing skeleton-row / `.empty` / `.error-banner` treatment is
unchanged. The only requirement is that none of those three states push the
*container* wider than the viewport:

| State | Requirement |
|---|---|
| Loading (skeleton rows) | Skeleton `<td>` placeholders use the same column widths as real data — must not force a column artificially wider than the final render. |
| Empty | Empty-state block (icon + heading + paragraph) renders inside `.main`'s normal width, not inside the scrolling `*-table-wrap` — it should never itself need to scroll. |
| Error | `.error-banner` replaces the table, full-width within `.main`, wraps normally — never inherits the table's horizontal scroll. |

---

## Acceptance criteria (restated, testable)

- [ ] **No page-level horizontal scroll at 375px or 768px, on every tab**
      (Portfolio, Stations, Edge, EMOS, Promotion, Copy · Wallets/Paper/Live,
      Config): `document.documentElement.scrollWidth <=
      document.documentElement.clientWidth` holds on each tab after it's
      activated and its data has loaded (loading/empty/error state included).
- [ ] **Topbar** at 375px: logo/title, live pill, Refresh, theme toggle all
      visible, none clipped off-screen or wrapped onto a second row. Below
      480px, Refresh is icon-only with `aria-label="Refresh"` still present
      in the DOM.
- [ ] **Tab bar** at 375px: all 9 tabs reachable by horizontal swipe/scroll;
      clicking any tab (including ones currently scrolled out of view)
      results in that tab fully visible within the strip afterward
      (regression check on existing `scrollIntoView` call — must not break).
- [ ] **Grids:** `.wallet-row` and `.prob-row` never render a card narrower
      than ~140px at 375px; an orphaned last card in an odd collapse spans
      the full row width (no lone half-width card).
- [ ] **Headers:** `.station-header` and `.emos-header` wrap onto two lines
      at ≤600px without any child being clipped or the card exceeding
      viewport width.
- [ ] **Tables:** every `<table>` wider than its container (Edge, Stations'
      perf/bias/deb, Promotion; any EMOS/Config table added later) is
      wrapped in a `position:relative;overflow-x:auto` container — zero
      tables rely on page-level scroll, verified at 375px and 768px.
- [ ] **Desktop unchanged:** at ≥1280px, no visible difference in any grid,
      header, or table versus current production — this issue adds rules
      scoped to `max-width` media queries only; it must not alter any
      unqualified (desktop) rule.

---

## Design tokens / references

No new color/spacing/typography tokens. Reuses:
- `--space-2` / `--space-4` (new `row-gap` values on wrapped headers/rows)
- `--text-xs` / `--text-sm` (unchanged)
- Existing breakpoint convention extended per the Breakpoint system table above.

## Accessibility notes

- Icon-only Refresh (≤480px) keeps `aria-label="Refresh"` — no loss of
  screen-reader affordance from removing the visible label.
- Tab bar's horizontal scroll container is natively keyboard-focusable
  (standard `overflow:auto` + native scroll) — no new ARIA required; this is
  unchanged from current shipped behavior.
- Table scroll containers (`position:relative;overflow-x:auto`) don't change
  any table's existing `<th scope="col">` semantics — screen-reader table
  navigation is unaffected by adding a scrolling ancestor.
- If the tab-bar edge-fade affordance (§2, suggestion) is implemented, it
  must be a pure visual cue layered on top of — not a replacement for —
  the native scrollbar; color/gradient alone must not be the only signal
  (contrast/low-vision users still get the real scrollbar).

## Open questions

1. **Breakpoint convention (§ Breakpoint system):** confirm with Tech Lead
   PM that keeping 600px as the primary phone breakpoint (rather than
   migrating everything to the issue's suggested 768/480 pair) is an
   acceptable compromise — flagged as `[Tech constraint]` above.
2. **`.table-wrap` naming:** consolidate `.edge-table-wrap` /
   `.promo-table-wrap` / new Stations wraps into one shared `.table-wrap`
   class now, or keep per-tab class names with a duplicated rule body? Pure
   implementation/DRY call for the Tech Lead PM and developer — no UX impact
   either way.
3. **Tab-bar scroll affordance (edge fade):** is it in scope for #816, or
   logged as a Backlog follow-up issue? Not required by the acceptance
   criteria as written; recommended as a Clarity improvement.
4. **Existing `.edge-table-wrap` / `.promo-table-wrap` missing
   `position:relative`:** confirm whether patching these two (pre-#1286-fix)
   rules is in scope for this issue (cheap, same PR) or should be a separate
   fast-follow — raised because they're the exact shape of container the
   #1286 bug previously hit elsewhere in the dashboard.
