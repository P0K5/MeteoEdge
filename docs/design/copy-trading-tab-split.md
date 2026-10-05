# Design Spec — Copy-Trading Tab Split (Wallets / Paper / Live)

Re-arrangement of the existing single `#tab-copy-trading` panel into three
tabs. **No new design language**: every component below already exists in
`src/dashboard/static/index.html` (`.wallet-card`, `.copy-table`,
`.copy-slots-pill`, `.pagination`/`.pg-btn`/`.pg-info`, `.mode-badge-*`,
`.warn-banner`/`.error-banner`, `.skeleton`, `.empty`, `.section-hdr`).
Read first, unchanged and still binding: `copy-trading-dashboard.md` (Epic F)
and `copy-trading-live-views.md` (Epic J). Where this spec is silent, those
specs win. The overriding rule of Epic J is kept verbatim: **live and paper
figures are never blended or toggled into one slot; a paper number must never
be mistakable for a live one.**

## User goal

Operator wants to (1) browse a heavy screened-wallet list quickly, (2) watch
the paper study without live noise, and (3) run and monitor real-money
copy-trading in a view that looks and behaves like the familiar Portfolio
tab, with zero chance of confusing it with paper.

## User flow

1. **Wallets** tab: search/sort/page the screened list, expand a row for
   history, copy an address, Follow with a stake (paper follow).
2. **Paper** tab: monitor followed (paper) roster, paper P&L, paper
   positions, paper activity; pause/resume/edit paper stake/unfollow.
3. Paper-proven wallet: operator clicks "Go live" **on the Live tab** (see
   placement decision), sets live stake, confirms (flow unchanged from Epic J).
4. **Live** tab: monitor live KPIs, open positions, recent closed, live
   activity; adjust live stake or revert to paper.

## Tab names, order, global mode banner

- Order: `Portfolio | Stations | Edge | EMOS | Promotion | Wallets | Paper | Live | Config`
  (the three new tabs replace "Copy-Trading", same slot, in the order of the
  workflow: discover -> study -> real money).
- **Label collision risk:** the weather Portfolio is also real-money, so a bare
  "Live" tab is ambiguous. Recommended visible labels: **"Copy · Wallets"**,
  **"Copy · Paper"**, **"Copy · Live"** (ids `tab-btn-copy-wallets|paper|live`,
  `switchTab('copy-wallets'|'copy-paper'|'copy-live')`). If the operator
  prefers the bare names, keep them and rely on the page heading
  (`Copy-Trading — Live`) plus aria-labels (`"Copy-trading live"`). See Open Q1.
- **Global posture banner** (`LIVE TRADING ON` / `LIVE TRADING OFF — paper
  only`, existing `#copy-trading-mode-banner`) is a single shared component
  rendered at the top of **all three** tabs (same markup, same updater,
  `role="status" aria-live="polite"`; ids become class-based or per-tab
  suffixed so they stay unique). One fetch feeds all three instances.
- Each tab also has a **permanent mode heading badge** (`.mode-badge`) next
  to its title: Wallets = none (neutral, mode-agnostic screening);
  Paper = `PAPER` (amber); Live = `LIVE` (green). The badge is on the Live
  and Paper tab for the whole panel, so a screenshot of any section is
  unambiguous. Every dollar figure keeps its adjacent "Live"/"Paper" word per
  Epic J.
- The Live tab never renders paper figures, and the Paper tab never renders
  live figures. The old twin-panel layout is retired because the tab itself is
  now the hard separator. `[Tech constraint: the Epic J "never blended"
  guarantee previously came from side-by-side panels; it now comes from
  separate tabs, each with its own mode badge and API-key set — agreed
  approach: pending Tech Lead PM confirmation, no combined total anywhere,
  including no "all modes" KPI card.]`
- Cross-link only, never numbers: Paper header shows text "N wallets opted
  into live -> Live tab"; Live header shows "Paper study -> Paper tab".

## Screen layouts

### Tab 1 — Wallets (screened candidates)

- **Purpose:** discover and evaluate screened wallets; start a paper follow.
- **Key components:** posture banner; `.section-hdr` "Candidates" +
  `.copy-slots-pill` ("N slots remaining"); toolbar (search, page size);
  existing candidates `.copy-table` (columns unchanged: Wallet, Resolved
  trades, Win %, Mean ROI, Median ROI, Mirrored $ PnL, Flat-stake $ PnL, Last
  screened, Stability, History, row-details chevron); `.pagination` footer.
  Followed Wallets and Activity do **not** appear here (a followed wallet
  keeps its existing `.copy-followed-tag` in the row, with a link to Paper).
- **Layout:**
  ```
  [posture banner]
  Candidates  [N slots remaining]
  [Search address…      ] [Rows: 25 v]        Showing 1–25 of 312
  | Wallet (copy) | Trades | Win% | ... | Stability | History | v |
  | rows ...
  [ <- Prev ]  Page 1 of 13  [ Next -> ]
  ```
  Pagination footer is shown top-right (count text) and bottom (buttons) so
  the operator never scrolls 25+ rows to page.
- **Pagination UX** (reuses `.pagination`, `.pg-btn`, `.pg-info`; matches the
  Portfolio "Recent Closed" Prev/Next pattern):
  - Page sizes: **25 (default), 50, 100** via a `config-edit-select`;
    persisted in `localStorage` (`copyWalletsPageSize`).
  - Info text: `Page 2 of 13` in `.pg-info`; the toolbar shows `Showing
    26–50 of 312` (and `of 312 (filtered from 1,204)` when searching).
    Prev disabled on page 1, Next disabled on last page. Hidden entirely if
    results <= smallest page size.
  - **Sort is global, not per-page:** sorting (existing header buttons, default
    Median ROI desc) is applied to the full (filtered) dataset, then sliced.
    Sort key/dir persist across page changes. Changing sort, search, or page
    size resets to page 1; background refresh does **not** change page, sort,
    search, or expanded row.
  - **Search:** worth it — operators paste an address from elsewhere. One
    text input, case-insensitive substring match on full address (min 3
    chars, 200 ms debounce), clear (x) button, `aria-label="Search wallets by
    address"`. No other filters in v1 (see Open Q4 for "hide followed").
  - **Expanded-row detail:** unchanged (chevron/row click, sparkline,
    Follow arming). Expansion is keyed by address, so it survives refresh and
    re-sort while the row is on the current page; paging away collapses it
    (single expanded row at a time, as today). An armed Follow input with
    typed stake is also preserved across refresh. History/sparkline stays
    lazy: fetched on first expand only (as today).
  - `[Tech constraint: GET /api/copy-trading/candidates returns the full list
    in one response (no limit/offset). v1 = client-side sort/search/slice
    (cuts DOM cost, the heavy part); server-side `limit/offset/sort/q` with a
    `total` count is a follow-up if payload size itself is the bottleneck —
    agreed approach: pending Tech Lead PM feasibility call.]`
- **States:**
  - *Loading:* existing 3 skeleton rows; on page/sort change no skeleton
    (instant, client-side).
  - *Empty (never screened):* "No wallets screened yet" (existing).
  - *Empty (screened, none):* last screening-run timestamp (existing).
  - *No search results:* in-table row "No wallets match “abc123”" + "Clear
    search" `.copy-empty-link`. Pagination hidden.
  - *Error:* `.error-banner` + last-known-good table kept (existing).
  - *Success:* Follow confirmation uses the existing `.copy-followed-note`.
- **Mobile (<= 800 px):** toolbar stacks (search full width, size select
  below); table keeps `.copy-table-wrap` horizontal scroll with Wallet as the
  first column; detail grid already collapses to one column (`max-width:800px`);
  `.pagination` wraps, buttons keep min 44 px touch height; "Showing…" text
  moves under the toolbar.

#### Copy-address button (bug fix, applies to every `.copy-copy-btn` on all tabs)

Root cause (confirmed in `copyAddressToClipboard`, ~L4051):
`navigator.clipboard` is `undefined` outside secure contexts (plain-HTTP
dashboard); `await navigator.clipboard.writeText` throws a TypeError and the
`catch` only `console.error`s, so the button looks dead. Spec:

- **Single shared function** for all 6 copy buttons (L4208, 4716, 5390, 5452,
  5718, 5764 and their delegated handlers); returns a result
  `copied | failed`.
- **Fallback chain:** (1) `navigator.clipboard.writeText` only if
  `window.isSecureContext && navigator.clipboard`; (2) otherwise/on rejection a
  hidden, off-screen `<textarea>` + `document.execCommand('copy')` (works on
  HTTP; focus and selection restored afterwards); (3) if both fail, the
  manual fallback below. Never swallow silently.
- **Button states** (icon swap uses existing lucide `copy`/`check`; no new
  icons beyond `x`):

  | State | Icon | Colour | Text/aria | Duration |
  |---|---|---|---|---|
  | Idle | `copy` | `--muted` | aria-label "Copy full wallet address to clipboard" | — |
  | Copied | `check` | `--yes` | visible tooltip-style label "Copied" beside icon; aria-live "Address copied" | 1.5 s then idle |
  | Failed | `x` | `--no` | visible label "Copy failed — select manually"; aria-live "Could not copy address" | until dismissed / 4 s |

  The "Copied"/"Copy failed" label is a small `--text-xs` inline span right of
  the button inside `.copy-address-cell` (no popover framework). One shared
  visually-hidden `role="status" aria-live="polite"` region announces results
  for screen readers (icon swaps alone are silent). Rapid re-click resets the
  timer. Timers cleaned up if the row re-renders (check `isConnected`, as
  today).
- **Manual fallback (state: Failed):** the address cell swaps the truncated
  text for a read-only `<input class="copy-stake-input copy-address-mono">`
  containing the full address, auto-selected, with hint "Press Ctrl/Cmd+C".
  Blur or Esc restores the truncated text. This guarantees the operator can
  always get the address.
- Full address stays in `title`/`data-address`; truncated display unchanged.
- Recommended (not blocking): serve the dashboard over HTTPS so path (1) works
  everywhere; the fallback must still ship.

### Tab 2 — Paper

- **Purpose:** study paper-followed wallets; everything here is `PAPER`.
- **Key components / order (top to bottom):**
  1. Posture banner + title `Copy-Trading` + `PAPER` mode badge. When live is
     off, nothing extra; when on, one muted line "Live positions are on the
     Live tab".
  2. **KPI cards** (`.wallet-row` + `.wallet-card`, same markup as Portfolio):
     *Paper aggregate P&L* (`wc-sub`: realized / unrealized if available),
     *Open paper positions* (`wc-sub`: cost basis), *Followed wallets*
     (`wc-sub`: "N active · M paused"), *Win rate / resolved* only if the
     positions payload already carries it, else omit (no new data invented).
  3. **Followed Wallets (paper roster)** `.copy-table`: Wallet (copy), Paper
     stake/trade, Status (active/paused + reason), Date added, Running P&L
     (paper), Live opt-in indicator (read-only `LIVE`/`PAPER` badge from
     `_derive_live_eligibility`, with its aria-label), Actions. Actions here:
     **Pause/Resume, Edit stake (paper), Unfollow** (confirm text unchanged).
     Roster pills in the section header: `# active`, `# paused`, `# paper-only`,
     `Paper aggregate P&L` (the live-only pills move to the Live tab).
  4. **Paper P&L**: realized-P&L chart (Chart.js, existing) with the date-range
     select and **Compare to backtest (paper)** toggle (stays Paper-only per
     Epic J).
  5. **Open Positions** table + **per-wallet breakdown** table (existing
     two-column grid `.copy-positions-tables-grid`; click row = signal detail,
     unchanged).
  6. **Recent Closed (paper)** with Prev/Next (see Live tab, same component,
     page size 15) — only if the positions payload exposes closed paper rows;
     otherwise out of scope (Open Q in issue breakdown).
  7. **Paper Activity** feed: same list component, filtered `mode=paper`
     server-side; the Mode select is **removed**; Wallet and Event-type selects
     stay (event options limited to paper events). Paginated Prev/Next, 25
     per page, newest first (feed currently scrolls unbounded).
- **Go-live entry point:** each active wallet row shows a text link
  "Go live ->" that switches to the Live tab and scrolls/highlights that
  wallet in "Ready to go live". It does **not** open the live prompt here.
  The jump highlight is neutral (`--primary-bg` + `--primary` rail, not amber)
  and honours `prefers-reduced-motion`; see `polish-wave-rulings.md` (#1291).
- **States:** KPI cards use `.skeleton` blocks while loading; roster/positions/
  activity use skeleton rows. Empty roster: "Nothing followed yet — go to
  **Wallets**" (`.copy-empty-link` switching tabs). Empty positions: "No
  copy-trading positions yet"; unresolved-only chart empty state per Epic F.
  Error: per-section `.error-banner` with last-known-good data (a failed
  activity fetch must not blank the roster).
- **Mobile:** KPI cards stack per existing `.wallet-row` behaviour; tables
  scroll in `.copy-table-wrap`; roster Actions wrap (`.followed-actions-cell`
  `flex-wrap`); positions grid collapses at 900 px; filter selects wrap.

### Tab 3 — Live (modelled on Portfolio)

- **Purpose:** run and monitor real-money copy-trading. Everything here is
  `LIVE`; no paper figure appears.
- **Layout (mirrors Portfolio: wallet row -> Open Positions -> Recent Closed):**
  ```
  [posture banner]   Copy-Trading  [LIVE]
  [drift banner — red, only if mismatch]
  [KPI cards: Live P&L | Open exposure | Open positions | Live wallets]
  Live Wallets (opted in)   [pills]
  Ready to go live (paper-active, not opted in)
  Open Positions (live)      [count-pill]
  Recent Closed (live)       [count-pill]  <- Prev  Page 1 of N  Next ->
  Live Activity
  ```
- **KPI cards** (`.wallet-card`, label/value/sub, same as Portfolio):
  - *Live aggregate P&L* — realized; sub "since first live trade".
  - *Open live exposure* — sum of cost basis of open live positions; sub
    "cap $X per wallet".
  - *Open live positions* — count; sub "across N wallets".
  - *Live wallets* — `# live-eligible` value; sub "N opted in · M capped".
  - No cash/USDC card (no live balance data in the dashboard API today);
    add later if a balance endpoint exists.
- **Live Wallets table** (opted-in wallets only): Wallet (copy), Live badge
  (`LIVE` / `LIVE (switch off)` / `LIVE (cap reached)` per Epic J), Stake
  (`Live: $X` with "(inherits paper)" note — paper stake is **not** shown
  here), Open positions, Live P&L, Status, Actions: **Edit live stake**
  (#1262), **Revert to paper** (no confirm), **Pause/Resume** (pause is
  duplicated here deliberately: stopping real-money trades must never require
  switching tabs). Per-row `followed-live-msg` status region stays.
- **Ready to go live** (collapsed `<details>` by default if > 5 rows): active,
  not-opted-in wallets with Wallet (copy), paper P&L labelled "Paper P&L"
  (single deliberately-labelled exception to "no paper figures", needed to
  justify promotion — amber text, `PAPER` badge in the column header), Go
  live button. Go live runs the Epic J prompt+confirm flow unchanged.
  `[Open Q2: whether this paper reference column is acceptable.]`
- **Open Positions (live):** same card/table shape as existing live column
  (click row = signal detail). `count-pill` shows N.
- **Recent Closed (live):** same shape as Portfolio Recent Closed;
  **Prev / Next + `Page X of Y`** using `.pagination` / `.pg-btn` / `.pg-info`,
  15 per page (same as `CLOSED_PAGE_SIZE`), most-recent first, hidden when
  <= 1 page; Prev disabled on page 1. Columns: Market/position, Wallet,
  Opened, Closed, Settled P&L (pos/neg classes `.copy-pnl-pos/-neg`).
- **Live Activity:** feed filtered `mode=live` (Mode select removed; Wallet and
  live event-type selects remain), 25 per page Prev/Next; includes global
  events (halted/resumed, circuit breaker, balance mismatch) with the existing
  left-border + badge + text triple signal.
- **States (Epic J rules preserved):**
  - *Live trading OFF, no live history:* KPI cards replaced by one dimmed
    neutral card "Live trading is off" + link to config; **no numbers, not
    even $0.00**. Sections below show their own "off" text.
  - *Live OFF but history exists:* real historical figures + the off banner
    on top (resolves Epic J Open Q4 in the leaning direction).
  - *Live ON, nothing yet:* green accent, "No live positions yet." / "No live
    activity in this range." per section.
  - *Loading:* `.skeleton` KPI cards + skeleton rows, per section, so a slow
    live fetch doesn't blank other sections.
  - *Error:* per-section `.error-banner`, last-known-good data kept; **never
    fall back to paper data in a live slot**.
  - *Drift:* red banner "Live balance mismatch detected — manual
    reconciliation required" (unchanged copy), above KPI cards.
- **Mobile:** KPI cards stack; Live Wallets/Open Positions render as
  `.copy-table-wrap` scroll tables (card layout deferred, consistent with
  other tabs); action buttons wrap, **Revert to paper** and **Pause** stay
  reachable without horizontal scroll (actions column first-wrapped under
  the wallet cell below 600 px); pagination wraps, 44 px targets.

## Where things go (decisions)

| Component | Wallets | Paper | Live |
|---|---|---|---|
| Candidates + Follow | yes | — | — |
| Followed roster, pause/resume, paper stake, unfollow | tag only | **yes (owner)** | pause/resume only on opted-in rows |
| Go live / Revert / live stake / live KPIs | — | link only | **yes (all money actions)** |
| Activity Feed | — | paper only | live only |
| Mode select in feed | — | removed | removed |
| Global posture banner | yes | yes | yes |

Rationale: split by mode, not "kept together" — the feed already tags mode and
the API takes `mode=`; a single mixed feed is the main place paper/live could
visually blend. Real-money controls live only on the Live tab so the Paper tab
can be browsed with no risk of an accidental real-money click.

## Performance and loading (applies to all three)

- **Lazy per tab:** first activation of a tab fetches only that tab's data
  (replaces the single `copyTradingLoaded` fetch-everything block).
- **No polling of hidden tabs:** `switchTab` clears the intervals of the tab
  being left (today they are never cleared) and starts the new tab's; polling
  also pauses on `document.hidden` and does one catch-up fetch on return.
- **Cadences:** posture 30 s (any of the 3 tabs active, one shared fetch);
  followed-wallets 30 s (Paper/Live only); positions + live data 30 s on Live
  (real money), 5 min on Paper; candidates **no auto-poll** (screening is
  periodic) — fetch on activation + manual Refresh (existing header button) +
  5 min while active; activity 30 s on Live, 5 min on Paper.
- **Shared payloads:** `/followed-wallets` and `/positions` return both modes;
  fetch once per cycle into a cache with a ~10 s TTL and let Paper/Live render
  only their slice, so switching Paper <-> Live is instant and never
  double-fetches. Rendering uses only the owning mode's keys.
- **Skeletons** everywhere on first load (existing `.skeleton`); later polls
  update silently with a small "Updated hh:mm:ss" in each section header.
- Client-side pagination keeps DOM <= page size rows (25/15), rather than
  rendering the whole list.

## Design tokens / references

- Colours: `--yes`/`--yes-bg`/`--yes-dim` live, `--warn`/`--warn-bg` paper,
  `--no` errors/negative, `--muted`, `--primary`; no new tokens.
- Existing classes only; the **only** new CSS is optional layout glue: a
  toolbar flex row (search + select + count text, built from
  `.copy-activity-filters`) and `.copy-copy-feedback` (an inline `--text-xs`
  span). The retired `.copy-positions-twin` markup is deleted.
- Spacing/typography: `--space-*`, `--text-xs/sm` as in surrounding code.

## Accessibility notes

- Tab buttons get `role="tab"`-compatible labels (aria-label "Copy-trading
  wallets/paper/live"); active tab marked as today.
- Pagination: buttons real `<button>`s, disabled state native; `.pg-info`
  `aria-live="polite"` so page changes are announced; focus stays on the
  pressed button (do not move focus to table top); table gets
  `aria-label` including range ("Screened wallet candidates, rows 26 to 50
  of 312").
- Sortable headers unchanged (keyboard-reachable `.copy-th-btn`, add
  `aria-sort`).
- Copy feedback is announced via a shared `aria-live` region, never icon/colour
  alone; manual-copy input receives focus and selection.
- LIVE/PAPER badges keep full-sentence aria-labels; mode is never colour-only.
- Skeletons `aria-busy="true"` on their container; search input has a label;
  contrast tokens as existing.

## Open questions (for the operator)

1. Tab labels: "Copy · Wallets / Copy · Paper / Copy · Live" (recommended,
   avoids confusion with the weather Portfolio's real-money data), or bare
   "Wallets / Paper / Live"?
2. May the Live tab's "Ready to go live" list show each wallet's paper P&L
   (clearly labelled PAPER) to help the promote decision, or must the Live tab
   contain zero paper numbers?
3. Should Pause/Resume also appear on the Live tab (recommended for fast
   emergency stop), or only on the Paper tab?
4. Candidates list: is client-side pagination/search enough for v1 (list size
   today?), and do you want a "hide already-followed" filter on Wallets?

## Proposed issue breakdown (by tab; Tech Lead PM to size and sequence)

0. **Foundation (do first, unblocks all):** split `#tab-copy-trading` into 3
   panels + tab bar + `switchTab` routing; shared posture banner component;
   per-tab lazy load and interval start/stop (incl. hidden-tab pause); shared
   payload cache; remove/relocate Mode select and twin-panel markup. (mid-dev)
1. **Wallets tab:** move Candidates; client-side pagination (25/50/100),
   Prev/Next + info, global sort across pages, address search, expanded-row
   and armed-follow persistence, empty/no-results states. (mid-dev)
2. **Copy-button fix (can ship independently, first):** shared
   `copyAddressToClipboard` with secure-context check, `execCommand`
   fallback, manual-select fallback, visible success/failure states, aria-live;
   apply to all 6 call sites. (junior-dev)
3. **Paper tab:** KPI cards, paper roster with paper controls, P&L chart +
   backtest toggle, open positions + per-wallet breakdown, paper activity
   (paged, mode-locked), Go-live link. (mid-dev)
4. **Live tab:** KPI cards + off/empty states, Live Wallets table with Edit
   live stake/Revert/Pause, Ready-to-go-live list with the Epic J flow,
   Open Positions, Recent Closed with Prev/Next, live activity (paged,
   mode-locked), drift banner. (mid-dev; backend sub-issue if closed live
   positions need a paged endpoint)
5. **Follow-up (optional, backend):** server-side pagination/sort/search for
   `/api/copy-trading/candidates` and paged closed-positions/activity.

## PM decisions (Tech Lead PM sign-off)

- Open Q1: labels are "Copy · Wallets", "Copy · Paper", "Copy · Live".
- Open Q2: "Ready to go live" may show a paper P&L figure, always carrying an
  adjacent PAPER badge/word; no other paper number appears on the Live tab.
- Open Q3: Pause/Resume is duplicated on the Live tab.
- Open Q4: candidates pagination is **server-side** (optional `page`,
  `page_size`, `sort`, `dir`, `q` query params; backward compatible when
  omitted) because the payload and per-row stability computation are the
  heavy part. Closed positions and activity feed page client-side in v1
  (data already in `/api/copy-trading/positions` `*_realized_pnl_history`).
- Twin-panel retirement confirmed; no combined/"all modes" total anywhere.
- Performance gates: no polling of hidden tabs or `document.hidden`; each
  tab lazy-loads; one shared fetch/cache for followed-wallets + positions
  payloads; list renders must not rebuild the DOM when data is unchanged.
