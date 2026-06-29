# Design Spec — Edge Tab (Station-by-Station Model vs Market Analysis)

Status: **Draft — pending Tech Lead PM review**
Related: Epic #510, Issue #514

## User goal

Before placing or reviewing a position, the operator wants a fast, structured read of *where the model's ensemble forecast diverges from the live Polymarket odds, by how much, and at which station* — so they can decide whether to enter a trade. This is the analytical step that happens **before** the Portfolio tab's execution view.

## User flow

1. Operator clicks the **Edge** tab in the tab bar (lands on last-selected station, defaulting to the first configured station, date = today).
2. Operator selects a station from the pill row. The KPI strip, distribution chart, and edge table reload for that station/date.
3. Operator optionally toggles the date selector to **D+1** if a next-day Polymarket market exists for that station. All three components reload.
4. Operator reads the KPI strip for a fast summary, then the distribution chart to sanity-check the ensemble shape, then the Market vs Model table to find the best edge (already highlighted) and any other actionable brackets.
5. (Future / flagged) Operator could click a bracket row to jump to the matching Polymarket market — flagged in Open Questions, needs backend support.

## Screen layout

```
┌─ tab-bar: Portfolio | Stations | Edge | EMOS | Config ─────────────┐
│                                                                      │
│  Station pill row:  [ KJFK ] [ KLGA ] [ KEWR ] [ KBOS ] …          │
│                                                                      │
│  ┌─ Date selector ─────────────┐                                    │
│  │  [ Today ]  [ D+1 ]         │  (D+1 disabled if no market)       │
│  └──────────────────────────────┘                                   │
│                                                                      │
│  ┌─ KPI strip (5 cards) ──────────────────────────────────────────┐ │
│  │ Ensemble Mean │ Bias-Corrected │ Members │ Range │ Best Edge   │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Ensemble distribution (horizontal bars) ─────────────────────┐ │
│  │  62-64°F  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 38 members                       │ │
│  │  59-61°F  ▓▓▓▓▓▓▓▓▓▓ 22 members                               │ │
│  │  56-58°F  ▓▓▓ 9 members                                        │ │
│  │  …                                                              │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Market vs Model edge table ──────────────────────────────────┐ │
│  │ Bracket      Polymarket %   Model %   Edge (pp)                │ │
│  │ 53–55°F          18%           12%      -6.0 pp                │ │
│  │ 56–58°F          24%           31%      +7.0 pp  ★ best edge   │ │
│  │ 59–61°F          31%           29%      -2.0 pp                │ │
│  └────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

All components live inside `.main` (max-width 980px, same as Portfolio/Stations) so the tab feels native to the existing dashboard, not a bolt-on.

---

## Components

### 1. Tab bar integration

- **Placement:** insert a new `<button class="tab-btn" onclick="switchTab('edge')">Edge</button>` between **Portfolio** and **Stations** in the existing `<nav class="tab-bar">` (`src/dashboard/static/index.html` line ~669).
- **Label:** `Edge` (no icon in the tab button — matches existing tab buttons, which are text-only).
- **Active state:** reuse `.tab-btn.active` (color `var(--primary)`, 2px bottom border `var(--primary)`) — no new CSS needed.
- **Panel:** new `<div class="tab-panel" id="panel-edge">` following the existing `.tab-panel` / `.tab-panel.active` pattern used by `panel-portfolio`, `panel-stations`, etc. `switchTab('edge')` follows the same show/hide logic already implemented for other tabs.

### 2. Station selector (pill/chip row)

- **Layout:** horizontal row of pill buttons, one per station from `/api/stations/overview`, directly under the tab bar inside `.main`. Wrap in a new container `.edge-station-row` (flex, `gap:var(--space-2)`, `flex-wrap:wrap`, `margin-bottom:var(--space-4)`).
- **Pill component (`.edge-station-pill`):** reuses the same visual language as `.tab-btn` / `.count-pill`, not a new pattern:
  - Base: `background:var(--surface-off); color:var(--muted); border:1px solid var(--border); border-radius:var(--radius-full); padding:6px 14px; font-size:var(--text-xs); font-weight:600;`
  - Hover: `color:var(--text); border-color:var(--surface-dyn);`
  - **Active station:** `background:var(--primary-bg); color:var(--primary); border-color:var(--primary);` — same token pairing as `.result-take_profit` / `.side-yes` style badges elsewhere in the app.
  - **Disabled (no_data status):** apply existing `.station-card.accent-no_data` color logic — pill border-left or icon dot in `var(--no)`, pill itself at `opacity:.5` and `cursor:not-allowed`. Tooltip (native `title` attr) explains: "No data available for this station today."
- **Overflow behaviour:** the row wraps (`flex-wrap:wrap`) rather than scrolling horizontally, consistent with `.wallet-row`'s responsive wrapping at narrow widths. If the station count grows large enough to need pagination (more than ~20), that is an **open question** flagged below — out of scope for v1 given the current station count is small.
- **Selection persistence:** last-selected station persists in `localStorage` (`edgeTab.selectedStation`) so returning to the tab doesn't reset state — same pattern already used for theme persistence (`data-theme` on `<html>`).

### 3. KPI strip

Reuses the `.wallet-row` / `.wallet-card` grid pattern (3-up grid on desktop already exists for the wallet row; for 5 KPIs use a 5-column grid that collapses responsively):

```css
.edge-kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:var(--space-4);margin-bottom:var(--space-6);}
@media(max-width:900px){.edge-kpi-row{grid-template-columns:repeat(3,1fr);}}
@media(max-width:600px){.edge-kpi-row{grid-template-columns:1fr 1fr;}}
```

Each KPI card reuses `.wallet-card` exactly (`background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:var(--space-5); box-shadow:var(--sh-sm);`) with `.wc-label` / `.wc-value` / `.wc-sub` for label/value/sub-text — no new card style needed.

| # | Label (`.wc-label`, uppercase, `var(--muted)`) | Value formatting (`.wc-value`) | Sub text (`.wc-sub`) |
|---|---|---|---|
| 1 | `ENSEMBLE MEAN` | `56.7°F` — one decimal, `°F` suffix | `raw ensemble` |
| 2 | `BIAS-CORRECTED` | `57.9°F` — one decimal | `applied: +1.2°F` (or `no bias model` if none active) |
| 3 | `MEMBERS` | `173 members` | `of 51 ensemble runs` (or similar pipeline detail) |
| 4 | `RANGE` | `53–61°F` (en dash, no spaces) | `5th–95th pct` |
| 5 | `BEST EDGE` | `56–58°F` (bracket) + edge value `+7.0 pp` | `Bracket label` |

- **Best-edge highlight:** card #5's value uses `color:var(--edge)` (same token as `.prob-value.edge-pos` / `.edge-positive`) to visually flag it distinctly from the other four neutral-color KPIs. If best edge is negative for every bracket (no positive opportunity today), value falls back to `var(--no)` and sub-text reads "No positive edge today".
- **Tabular numerals:** all values use `font-variant-numeric:tabular-nums` like `.wc-value` already does, so digits don't jitter on reload.

### 4. Ensemble distribution chart

- **Orientation:** horizontal bars, one row per °F bucket, matching the reference screenshot (NYC ensemble, dark theme). Implemented with Chart.js (already a dependency — `chart.js@4` is loaded at the top of `index.html`) using a horizontal bar chart (`indexAxis:'y'`), consistent with the existing line charts built with `clr('--token')` helper (see lines ~1340–1490 for the existing `clr()` pattern that reads CSS variables at render time for light/dark parity).
- **Container:** reuse `.chart-section` / `.chart-wrap` pattern (`border-top:1px solid var(--divider); background:var(--surface-2); padding:var(--space-4) var(--space-5);` and `.chart-wrap{position:relative;height:180px;}` — bump height to ~260px for the bucket count typical in ensemble outputs, e.g. `.edge-dist-chart .chart-wrap{height:260px;}`).
- **Bar color:** default buckets use `clr('--primary')` at reduced opacity (e.g. `clr('--primary') + '99'`), consistent with how other charts borrow `--primary` for the main series.
- **Peak bucket highlight:** the bucket with the highest member count is rendered in `clr('--edge')` (full opacity) — this is the same token used for "edge" emphasis everywhere else in the app (`--edge` / `--edge-bg`), keeping the visual vocabulary consistent: edge = highlight = purple accent.
- **Member count label:** displayed at the end of each bar (Chart.js datalabels-style text, or a custom render), e.g. `38 members`, in `var(--muted)` text, `var(--text-xs)`, positioned just past the bar's end (outside the bar for short bars, inside-right-aligned for bars that reach near the chart edge — standard Chart.js label collision handling).
- **Axis labels:**
  - Y-axis (bucket labels): `53–55°F`, `56–58°F`, etc. — same en-dash bracket format as the edge table — font `var(--font-mono)` at `var(--text-xs)` color `var(--muted)`, consistent with `.footer-bracket` styling already used for bracket strings elsewhere.
  - X-axis (member count): tick labels in `var(--muted)`, gridlines in `var(--divider)` (Chart.js `scales.x.grid.color`), mirroring the muted/divider treatment of existing charts.
- **Tooltip:** on hover, show exact member count and % of total ensemble for that bucket (native Chart.js tooltip, themed via `clr('--surface-2')` background / `clr('--text')` text to match dark/light mode — same approach as existing charts' tooltip theming, if already implemented; otherwise default Chart.js tooltip is acceptable for v1).

### 5. Market vs Model edge table

- **Structure:** a standard HTML `<table>` reusing the visual language of `.perf-table` (the existing performance comparison table in the Stations tab) rather than introducing a new table style.

```css
.edge-table{width:100%;border-collapse:collapse;font-size:var(--text-sm);font-variant-numeric:tabular-nums;}
.edge-table th{text-align:left;padding:var(--space-2) var(--space-3);font-size:var(--text-xs);font-weight:700;letter-spacing:.05em;color:var(--muted);border-bottom:1px solid var(--divider);}
.edge-table td{padding:var(--space-3);border-bottom:1px solid var(--divider);}
.edge-table tr:last-child td{border-bottom:none;}
.edge-table tr.best-edge-row{background:var(--edge-bg);}
```

- **Column headers:** `Bracket` · `Polymarket %` · `Model %` · `Edge (pp)` — left-aligned header text per the acceptance criteria, font matching `.perf-th-side` styling (`font-weight:700; letter-spacing:.05em; color:var(--muted)`).
- **Row structure:** one row per Polymarket bracket for the selected station/date, in ascending bracket order (matches reference screenshot ordering, low to high temperature).
- **Bracket column:** font `var(--font-mono)` (mirrors `.footer-bracket`), e.g. `53–55°F`.
- **Polymarket % / Model % columns:** plain `var(--text)` color, `%` suffix, one decimal max (e.g. `18.0%` or `18%` if always whole numbers from the API — defer exact precision to backend response format).
- **Edge (pp) column — colour rules (per acceptance criteria, using only existing tokens):**
  - Positive edge (model > market): `color:var(--yes)` (this column specifically uses `--yes`/`--no` rather than `--edge`/`--no` because the acceptance criteria explicitly calls for green/red on the per-row edge value, reserving `--edge` purple for the *best edge* row/KPI highlight — see Note below.)
  - Negative edge: `color:var(--no)`
  - Near-zero edge (within ±1.0 pp, configurable threshold): `color:var(--muted)` — "near-zero = muted" per acceptance criteria.
  - Value format: signed, one decimal, `pp` suffix — e.g. `+7.0 pp`, `-2.0 pp`, `+0.3 pp`.
- **Best-edge row highlight:** the row with the single largest positive edge gets `.best-edge-row` (`background:var(--edge-bg)`) plus a `★ best edge` marker chip after the edge value, reusing `.edge-positive`'s pill treatment (`background:var(--edge-bg); color:var(--edge); border-radius:var(--radius-full); padding:2px 8px; font-size:var(--text-xs); font-weight:600;`). This mirrors `.pos-edge-badge.edge-positive` already defined for position cards.

> **Note — resolving the `--edge` vs `--yes`/`--no` tension:** the acceptance criteria says "positive = green `--yes`, negative = red `--no`" for the edge column, but the existing position-card pattern (`.edge-positive`/`.edge-negative`/`.edge-zero`) uses `--edge` (purple) for positive and `--no` (red) for negative with `--surface-off`/`--muted` for zero. **Decision for this spec:** follow the acceptance criteria literally for the per-row Edge (pp) column (green/red/muted), and reserve `--edge` (purple) exclusively for *emphasis* — the best-edge row background, the best-edge KPI card value, and the "★ best edge" chip. This keeps green/red doing double duty as "directional edge" (consistent with `--yes`/`--no` used everywhere else for outcome direction) while purple stays a pure "this is the standout" signal. Flagged as `[Tech constraint: acceptance criteria vs. existing edge-badge convention — agreed approach: green/red for row values, purple reserved for best-edge emphasis only]`.

### 6. Date selector (today / D+1 toggle)

- **Component:** two-button segmented toggle, reusing the `.pg-btn` (pagination button) visual treatment as a base, restyled as a connected pair:

```css
.edge-date-toggle{display:inline-flex;border:1px solid var(--border);border-radius:var(--radius-md);overflow:hidden;margin-bottom:var(--space-4);}
.edge-date-btn{padding:var(--space-2) var(--space-4);font-size:var(--text-sm);color:var(--muted);background:var(--surface);border:none;}
.edge-date-btn.active{background:var(--primary-bg);color:var(--primary);font-weight:600;}
.edge-date-btn:disabled{opacity:.35;cursor:not-allowed;color:var(--faint);}
```

- **Default:** `Today` active on tab load / station change.
- **Disabled state:** `D+1` button gets `disabled` attribute and the `:disabled` styling above when no next-day Polymarket market exists for the selected station — same disabled treatment already used for `.pg-btn:disabled`. Add a `title="No D+1 market available for this station yet"` tooltip.
- **Visual feedback:** clicking a non-disabled button immediately toggles `.active`, and triggers a reload of KPI strip + chart + table (all three sections show their loading state per §7 while the new data fetches).

### 7. Loading / empty / error states

Per component, reusing the dashboard's existing state vocabulary (`.skeleton`, `.empty`, `.error-banner`) rather than inventing new ones:

| Component | Loading | Empty | Error |
|---|---|---|---|
| Station pill row | Pills render with `.skeleton` shimmer blocks (fixed-width placeholders) while `/api/stations/overview` loads | N/A (no stations configured is a global empty state, out of scope here) | `.error-banner.visible` above the row: "Couldn't load stations." |
| KPI strip | Each `.wallet-card` value area shows a `.skeleton` block (height matching `.wc-value` line-height) instead of the number | If `/api/analysis/{station}` returns no ensemble data for the date: all 5 cards show `.empty`-style "—" placeholder text in `var(--faint)`, with one `.wc-sub` reading "No data for this date" | `.error-banner.visible` block above the KPI row: "Couldn't load analysis for {station}. Retry." with a retry affordance (reuse `.pg-btn` style for a "Retry" button) |
| Distribution chart | `.chart-wrap` shows centered `.skeleton` shimmer bars matching expected row count, or a centered spinner (existing `.spinner` keyframe/markup from `.btn-toggle .spinner`) | `.chart-empty` class already exists (`color:var(--muted); text-align:center; padding:var(--space-8) 0;`) — message: "No ensemble data available for this date." | Same `.error-banner` pattern inside `.chart-section`, replacing the chart canvas |
| Edge table | Table body rows replaced with 3–5 skeleton rows (`<td>` containing `.skeleton` blocks sized to column width) | `.empty` block (icon + heading + paragraph, same as `No open positions` / `No closed trades` patterns) — icon `bar-chart-2` or `inbox` (Lucide, already loaded), heading "No bracket data for this date", paragraph "Polymarket may not have published brackets for this station/date yet." | `.error-banner.visible` replacing the table: "Couldn't load the edge table. Retry." |
| Date selector | `D+1` button shows a small inline `.spinner` (reuse `.btn-toggle .spinner` keyframe) while checking market availability, with both buttons disabled during the check | N/A | If the availability check fails, `D+1` defaults to disabled with tooltip "Couldn't verify D+1 market — try again later." |

All error banners follow the existing `.error-banner` styling exactly (`background:var(--no-bg); border:1px solid var(--no-dim); color:var(--no);`), and all skeleton placeholders use the existing `.skeleton` shimmer animation — no new states are introduced.

---

## Design tokens / references

No new colour tokens are introduced. All components reference existing variables from `src/dashboard/static/index.html`:

- `--primary` / `--primary-bg` / `--primary-h` — active pill, active date toggle, section icons
- `--yes` / `--yes-bg` / `--yes-dim` — positive edge values
- `--no` / `--no-bg` / `--no-dim` — negative edge values, error banners
- `--edge` / `--edge-bg` — best-edge row/KPI/chip emphasis, peak distribution bucket
- `--muted` / `--faint` — labels, near-zero edge, empty-state text
- `--surface` / `--surface-2` / `--surface-off` / `--surface-dyn` / `--border` / `--divider` — card/table/chart backgrounds and borders
- `--text-xs` / `--text-sm` / `--text-base` / `--text-lg` / `--text-xl` — typography scale
- `--space-1` … `--space-10` — spacing scale
- `--radius-sm` / `--radius-md` / `--radius-lg` / `--radius-xl` / `--radius-full` — corner radii
- `--font-body` / `--font-mono` — typography (bracket/temperature values use `--font-mono`, consistent with `.footer-bracket`/`.pos-ts`)
- `--sh-sm` / `--sh-md` — card shadows

New CSS **classes** are introduced (e.g. `.edge-station-row`, `.edge-station-pill`, `.edge-kpi-row`, `.edge-date-toggle`, `.edge-table`, `.best-edge-row`) but they compose existing tokens and patterns rather than defining new visual primitives.

## Accessibility notes

- **Station pills:** implemented as `<button>` elements (native focus/keyboard activation), `aria-pressed="true"` on the active pill, `aria-disabled="true"` + `title` tooltip on no-data stations (mirrors existing `aria-label` usage on `.side-yes`/`.side-no` badges, e.g. `aria-label="YES side"` pattern at line ~1914).
- **Date toggle:** implemented as a two-button `role="radiogroup"` with `role="radio"` + `aria-checked` on each button, or simpler native `<button aria-pressed>` pair if radiogroup semantics add unnecessary complexity — defer exact ARIA pattern to implementer, but keyboard activation (Enter/Space) must work and disabled state must be conveyed via `aria-disabled` + `disabled` attribute, not just visual styling.
- **Edge table:** standard `<table>` with `<th scope="col">` headers so screen readers announce column context per cell. The best-edge row's `★ best edge` marker has accompanying text (not icon-only) so it's announced, not just visually implied by background color.
- **Contrast:** all color choices reuse existing tokens already vetted for both dark and light themes elsewhere in the dashboard (e.g. `--yes`/`--no` text on `var(--surface)` backgrounds already meets contrast requirements in the current Portfolio tab); no new contrast risk introduced.
- **Distribution chart:** Chart.js canvas has an `aria-label` summarizing the chart (e.g. "Ensemble distribution for KJFK, June 29") since canvas content isn't natively accessible to screen readers; consider a visually-hidden `<table>` fallback with the same bucket/count data as a future enhancement (flagged below — not required for v1 parity with the rest of the dashboard, which doesn't currently provide chart data-table fallbacks either).
- **Loading states:** skeleton shimmer elements get `aria-hidden="true"` (decorative) with a visually-hidden "Loading analysis…" live region (`aria-live="polite"`) announced once per fetch, consistent with how other async sections in the dashboard should ideally behave (note: this is a slight accessibility upgrade over current Portfolio/Stations behavior, which doesn't appear to announce loading state — flagged as a general improvement opportunity, not Edge-tab-specific).

## Open questions / flags for Tech Lead PM

1. **Click-through to Polymarket bracket** (mentioned in epic context as a "mirrors operational workflow" nicety): would require either a stored Polymarket market URL per bracket in the `/api/analysis/{station}` response, or a client-side URL construction rule. **Not included in this spec's v1** — flag as a follow-up backend-dependent enhancement if desired.
2. **Tooltip on edge value** with extra context (e.g. confidence interval, sample size contributing to that bracket's model %): the table spec above covers the visible value only. If a richer tooltip is wanted, the `/api/analysis/{station}` response needs to include per-bracket metadata beyond market %/model %/edge — flag to Tech Lead PM for B1–B3 scope.
3. **Station pill overflow at scale:** if station count grows past what comfortably wraps to 2–3 rows, a horizontal-scroll or "show more" affordance will be needed. Out of scope while station count is small; revisit if/when it grows.
4. **D+1 colour-mode parity:** confirm the disabled-button tooltip copy ("No D+1 market available…") matches actual API error semantics once B3 (`GET /api/analysis/{station}`) is implemented — may need adjustment based on actual 404 vs empty-array behavior for missing D+1 markets.
5. **Near-zero edge threshold (±1.0 pp)** is a reasonable default proposed in this spec but not specified in the acceptance criteria — confirm with Tech Lead PM/operator whether this threshold should be configurable or hardcoded.
