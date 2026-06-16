# Design Spec — Per-City MAE Monitoring Panel

**Issue:** #307 — Per-city warm-bias residual correction + monitoring
**Endpoint:** `GET /api/residual-stats`
**Status:** Draft — awaiting Tech Lead PM feasibility review

---

## User goal

The operator needs to monitor forecast accuracy per city in real time so they can see which stations have an unsafe warm bias (high MAE), understand whether the MAE gate is suppressing live entries for that city, and validate that the residual correction is working as the model evolves. Without this panel, bias problems are invisible until they show up as P&L losses — this panel makes the signal visible before trades are placed.

---

## User flow

1. Operator opens the dashboard → Stations tab (natural location: city-level detail lives here).
2. The MAE panel appears above the station cards as a summary section, giving a portfolio-level view of model accuracy across all cities before drilling into per-station trade controls.
3. On first visit, the panel fetches `/api/residual-stats`. It then polls every 5 minutes (shared with the stations polling cycle).
4. The table renders with per-city rows. Cities where the MAE gate is active are visually highlighted so the operator immediately sees which cities are suppressed.
5. Operator can click column headers to re-sort (e.g. sort by MAE descending to see worst-performing cities first).
6. Operator can hover/tap a city row for a tooltip with the full city name and station code if truncated.

---

## Screen layouts

### Per-City MAE Panel

- **Purpose:** Show rolling forecast MAE (mean absolute error in °F), mean signed error (warm bias), gate status, and sample count per city so the operator can monitor model quality and understand which cities are in shadow-only mode due to the `MAX_RESIDUAL_MAE_F_FOR_LIVE` gate.
- **Key components:**
  - Section header with icon (`activity` or `bar-chart-2`) and title "Model Accuracy — Per City"
  - Subtitle/hint: "Rolling MAE of forecast vs observed high (°F)" (muted, right of title)
  - Sortable data table with 5 columns: City, MAE (°F), Warm Bias, Gate Status, n
  - MAE threshold indicator (the current `MAX_RESIDUAL_MAE_F_FOR_LIVE` value displayed as a caption below the table header, e.g. "Gate threshold: 5.0°F")
  - Loading skeleton (shimmer)
  - Error state (inline banner)
  - Empty state

**Layout — ASCII diagram:**

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ▦ Model Accuracy — Per City         Rolling MAE of forecast vs observed high    │
│                                                                  Gate: 5.0°F    │
├──────────────────────┬──────────────┬─────────────┬───────────────────┬────────┤
│ City            ↕    │   MAE (°F) ↕ │  Warm Bias  │       Gate        │      n │
├──────────────────────┼──────────────┼─────────────┼───────────────────┼────────┤
│ ⬤ Busan              │  10.1°F      │   +4.6°F    │  SHADOW (gated)   │     90 │
│ ⬤ Tokyo              │   8.4°F      │   +5.9°F    │  SHADOW (gated)   │     14 │
│ ⬤ Singapore          │   3.7°F      │   +2.1°F    │  LIVE             │     70 │
│ ⬤ Seoul              │   2.4°F      │   +1.4°F    │  LIVE             │     89 │
│ ⬤ Los Angeles        │   1.2°F      │   −0.3°F    │  LIVE             │    142 │
│ ⬤ Chicago            │   1.8°F      │   +0.6°F    │  LIVE             │     98 │
│   (no data yet)      │    —         │     —       │  —                │      0 │
└──────────────────────┴──────────────┴─────────────┴───────────────────┴────────┘
  Last updated: 12:04:31
```

- **Layout description:**
  - Panel is a full-width card (`surface` background, `border`, `radius-xl`, `shadow-sm`) within the existing `main` container, placed inside `#tab-stations` above the existing station cards section.
  - Header uses `.section-hdr` + `.section-title` pattern. The rolling-window note and gate threshold are rendered as `.config-hint` text on the right side of the header row.
  - The gate threshold value is rendered inline in the header hint, e.g. `Gate: 5.0°F`, in `font-family:var(--font-mono)`. This value is supplied by the API response so it reflects live config.
  - Table uses the same `perf-table` CSS pattern (already established for the Stations tab perf matrix). `width:100%; border-collapse:collapse; font-size:var(--text-xs); font-variant-numeric:tabular-nums`.
  - Column widths: City ~35% (left-aligned), MAE (°F) ~15% (right-aligned), Warm Bias ~15% (right-aligned), Gate ~20% (centre or left), n ~10% (right-aligned).
  - The coloured dot (`⬤`) preceding the city name is a small `status-dot`-style indicator (6×6px, `border-radius:50%`) whose colour encodes the gate status (see Colour coding below). It provides an at-a-glance scan without requiring the operator to read the Gate column.
  - Rows with `n = 0` (no data yet) are displayed in `var(--muted)` and the dots are `var(--faint)`, visually de-emphasised.
  - Last-updated timestamp rendered below the table in `pos-ts` style.

- **Interactions:**
  - Clicking a column header sorts ascending; clicking again sorts descending. Default sort: MAE descending (worst accuracy at top — the highest operational risk).
  - The `▲`/`▼` sort glyph appears in the active column header. Inactive headers show a neutral `⇅` glyph.
  - Rows are not drillable in this iteration — they are read-only. If a future spec adds per-city time-series charts, rows can become expandable (follow the closed-position chart expand pattern already in the codebase).
  - The global topbar "Refresh" button triggers a re-fetch of this panel alongside stations.

- **States:**

  **Loading:**
  ```
  ┌────────────────────────────────────────────────┐
  │ ▦ Model Accuracy — Per City                   │
  │  [██████████████░░░░]  [████░░]  [████████░░] │  ← shimmer rows
  │  [██████████████░░░░]  [████░░]  [████████░░] │
  │  [██████████████░░░░]  [████░░]  [████████░░] │
  └────────────────────────────────────────────────┘
  ```
  Four shimmer skeleton rows, matching column widths. Uses existing `.skeleton` keyframe.

  **Empty (no residual data computed yet):**
  ```
  ┌────────────────────────────────────────────────┐
  │ ▦ Model Accuracy — Per City                   │
  │                                                │
  │          [activity icon]                       │
  │          No residual data yet                  │
  │          MAE statistics will appear here       │
  │          once the correction pipeline has      │
  │          processed at least one observation.   │
  └────────────────────────────────────────────────┘
  ```
  Uses existing `.empty` component: `data-lucide="activity"` icon, `<h3>`, `<p>`.

  **Error:**
  ```
  ┌────────────────────────────────────────────────┐
  │ ▦ Model Accuracy — Per City                   │
  │ [!] Could not load MAE data — retrying…        │
  └────────────────────────────────────────────────┘
  ```
  Uses `.error-banner` pattern scoped within this panel card. Does not affect the station cards below.

  **Success (normal):** Full table as shown in the layout diagram above.

  **Gated city row (highlight):**
  Cities where the gate is active get a subtle left border accent (matching the `station-card` accent pattern) using `var(--warn)` amber to signal "suppressed, attention needed". This is applied to the `<tr>` via an inline style or a CSS class `mae-row-gated { border-left: 2px solid var(--warn); }`. The dot is amber.

---

## Colour coding

| Element | Rule | Token |
|---|---|---|
| Status dot (live) | MAE below gate threshold → green | `var(--yes)` |
| Status dot (gated) | MAE at or above gate threshold → amber | `var(--warn)` |
| Status dot (no data) | n = 0 → faint | `var(--faint)` |
| MAE value | < gate threshold → green; ≥ gate threshold → amber; no data → muted | `var(--yes)` / `var(--warn)` / `var(--muted)` |
| Warm Bias (positive = under-prediction) | > +2°F → amber/warn; > +5°F → red; negative → muted; near-zero (±1°F) → muted | `var(--warn)` / `var(--no)` / `var(--muted)` |
| Gate column | "LIVE" → `var(--yes)` with `yes-bg` badge; "SHADOW (gated)" → `var(--warn)` with `warn-bg` badge | See badge spec below |
| n column | < 20 (low sample, unreliable) → `var(--muted)` with a `(low)` suffix | `var(--muted)` |

**Gate badge spec:**

The Gate column renders a small inline badge (not a full `.status-badge` — that is reserved for station operational status):

```html
<!-- LIVE -->
<span class="mae-gate-badge mae-gate-live">LIVE</span>

<!-- SHADOW (gated) -->
<span class="mae-gate-badge mae-gate-shadow">SHADOW</span>
```

```css
.mae-gate-badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: var(--radius-full);
  font-size: var(--text-xs); font-weight: 700; letter-spacing: .04em;
}
.mae-gate-live   { background: var(--yes-bg);  color: var(--yes);  }
.mae-gate-shadow { background: var(--warn-bg); color: var(--warn); }
```

The Warm Bias column displays `+4.6°F` / `−0.3°F` with the sign always shown. Zero is displayed as `0.0°F` in `var(--muted)`. Values with a high positive bias (>+5°F) are shown in `var(--no)` red with a `!` prefix as an additional visual signal that this is a critical under-prediction issue.

---

## API contract (expected response shape)

```json
GET /api/residual-stats

{
  "gate_threshold_f": 5.0,
  "updated_at": "2026-06-16T12:04:31Z",
  "cities": [
    {
      "city": "Busan",
      "metar": "RKPK",
      "rolling_mae_f": 10.1,
      "mean_signed_error_f": 4.6,
      "n": 90,
      "gated": true
    },
    {
      "city": "Tokyo",
      "metar": "RJTT",
      "rolling_mae_f": 8.4,
      "mean_signed_error_f": 5.9,
      "n": 14,
      "gated": true
    },
    {
      "city": "Singapore",
      "metar": "WSSS",
      "rolling_mae_f": 3.7,
      "mean_signed_error_f": 2.1,
      "n": 70,
      "gated": false
    },
    {
      "city": "Los Angeles",
      "metar": "KLAX",
      "rolling_mae_f": 1.2,
      "mean_signed_error_f": -0.3,
      "n": 142,
      "gated": false
    }
  ]
}
```

- `gate_threshold_f` is the live value of `MAX_RESIDUAL_MAE_F_FOR_LIVE` from the config DB — displayed in the panel header so the operator always sees what threshold is in effect.
- `gated: true` means the city is currently suppressing live NO entries because `rolling_mae_f >= gate_threshold_f`.
- Cities with `n = 0` (no residual data yet) should still appear in the response (with `null` for numeric fields) so the panel always shows a complete city list.
- `mean_signed_error_f > 0` means the model under-predicts the actual high (warm bias); `< 0` means over-prediction.

---

## Design tokens / references

All values reference existing CSS custom properties:

| Token | Usage |
|---|---|
| `var(--surface)` | Panel card background |
| `var(--border)` | Card border |
| `var(--radius-xl)` | Card corner radius |
| `var(--sh-sm)` | Card box shadow |
| `var(--divider)` | Table row dividers |
| `var(--text)` | Default cell text |
| `var(--muted)` | Column headers, null/faint values |
| `var(--faint)` | No-data row dots |
| `var(--yes)` / `var(--yes-bg)` | Low-MAE / live gate badge |
| `var(--warn)` / `var(--warn-bg)` | High-MAE / gated badge, positive bias |
| `var(--no)` | Critical warm bias (>+5°F) |
| `var(--font-mono)` | Numeric cells, gate threshold value |
| `var(--text-xs)` | Table text size |
| `.skeleton` | Loading shimmer animation |
| `.error-banner` | Error state |
| `.empty` | Empty state container |
| `.section-hdr` / `.section-title` | Panel header |
| `.config-hint` | Threshold hint text in header |
| `.perf-table` | Base table styles |

New CSS classes introduced by this panel:
- `.mae-gate-badge`, `.mae-gate-live`, `.mae-gate-shadow` — gate status pill (specified above)
- `.mae-row-gated` — amber left border on gated rows (`border-left: 2px solid var(--warn)`)
- `.mae-bias-critical` — applied to Warm Bias cell when `mean_signed_error_f > 5`: `color: var(--no); font-weight: 700`

---

## Placement within the Stations tab

The MAE panel is inserted **above** the station cards list (above the `.section-hdr` for "Stations"), as an additional sub-section within `#tab-stations`. The HTML structure becomes:

```
#tab-stations
  .main
    .error-banner#stations-error-banner        ← existing
    .mae-panel#mae-panel                       ← NEW: full-width card
      .section-hdr
      .mae-table-wrap
    .section-hdr (Stations)                    ← existing
    #stations-list                             ← existing station cards
```

This placement is chosen because:
1. MAE directly drives which stations suppress live entries — the operator should see model quality before the per-station toggles.
2. The Stations tab is the right semantic home (city-level signal, not portfolio-level).
3. The panel is narrow enough (one table, no interactive controls) that it does not crowd the station cards below.

---

## Accessibility notes

- Table must have `role="table"` and `aria-label="Forecast MAE per city"`.
- Column headers use `<th scope="col">` with `aria-sort` updated on interaction.
- Sort buttons within `<th>` must be keyboard-focusable with visible focus ring.
- The status dot colour is supplemented by the Gate column badge text — colour is never the sole signal.
- Gated rows: `aria-label="Busan — gated, SHADOW only"` on the row or Gate cell so screen readers convey gate status.
- ARIA live region (`aria-live="polite"`) on the table container so data refresh is announced.
- `n < 20` suffix "(low)" is screen-reader-visible text, not CSS-only.
- Contrast: `var(--warn)` on `var(--warn-bg)` — verify both themes meet WCAG AA. If `--warn` fails on `--warn-bg` at `text-xs` size, increase font weight to 700 or use a darker shade.

---

## Open questions

1. **Tab placement vs dedicated Analytics tab:** This spec places the panel on the Stations tab. If issue #304's close-reason panel also moves to the Stations tab (or a new Analytics tab), the two panels should be co-located. Recommend deferring this layout decision until both panels exist. [Tech constraint: requires PM decision before implementation begins.]

2. **Rolling window size:** The issue does not specify the rolling window for MAE/bias computation (e.g. last 30 obs, last 90 days). The API response should include a `window_label` string (e.g. `"trailing 90 observations"`) so the panel can display it without hard-coding. Backend PM to confirm window.

3. **Low-sample warning threshold:** The spec uses `n < 20` as "low sample, unreliable." The correct threshold depends on the rolling window; the backend should ideally supply a `low_sample` boolean per city so the frontend doesn't need to hard-code the threshold.

4. **Warm Bias sign convention:** The spec uses `mean_signed_error_f > 0` = model under-predicts (warm bias, bad for NO). Confirm this matches the backend's `delta_f = observed − model` convention (positive delta = observed higher than model = under-prediction = warm bias). If the sign is inverted at the API layer, the colour-coding logic reverses.

5. **Gate override visibility:** Should the operator be able to manually override the gate (force a city to LIVE despite high MAE) from this panel? Recommend no for this iteration — the Config tab's `MAX_RESIDUAL_MAE_F_FOR_LIVE` parameter controls the threshold, and per-city overrides add complexity. Log as a future issue.

6. **Sort persistence:** Same question as the close-reason panel — should sort survive data refreshes? Recommend yes.
