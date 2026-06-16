# Design Spec — Close-Reason P&L Breakdown Panel

**Issue:** #304 — Exit discipline: forced pre-settlement exit + take-profit tuning
**Endpoint:** `GET /api/close-reason-stats`
**Status:** Draft — awaiting Tech Lead PM feasibility review

---

## User goal

The operator needs to understand at a glance which exit path is generating (or destroying) P&L. The live-trade analysis shows the early-exit path (`take_profit`) is the entire alpha while `settled` and `timeout` (now `forced_exit`) are net-negative. This panel makes that breakdown permanently visible on the dashboard so the operator can validate tuning changes and spot regressions without running a separate script.

---

## User flow

1. Operator opens the dashboard → Portfolio tab (default view).
2. The panel appears below "Recent Closed" and above (or replacing the existing) "Bot Log" section — it is always visible, not behind a tab switch, because it is a key performance signal.
3. On page load, the panel fetches `/api/close-reason-stats` (polled every 30 s alongside the portfolio call to stay in sync).
4. The table renders immediately with the latest data. Rows are colour-coded green/red.
5. Operator can click any column header to re-sort the table by that metric (client-side sort, no re-fetch).
6. When data is unavailable or the endpoint errors, the panel shows a clear inline error state — it does not collapse silently.

---

## Screen layouts

### Close-Reason P&L Panel

- **Purpose:** Show P&L, win rate, average P&L, and worst loss per close reason so the operator can see at a glance whether the exit-discipline changes (take_profit, forced_exit, stop_loss, settled) are working as intended.
- **Key components:**
  - Section header with icon (`git-branch` or `arrow-right-circle`) and title "Exit Breakdown"
  - Last-updated timestamp (small, muted, right-aligned)
  - Sortable data table with 6 columns: Close Type, n, Win Rate, Total P&L, Avg P&L, Worst
  - Loading skeleton (shimmer, matching table structure)
  - Error state (red banner, inline within panel)
  - Empty state (when 0 closed trades exist)

**Layout — ASCII diagram:**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ↗ Exit Breakdown                                        updated 12:04:21   │
├──────────────────┬────┬──────────┬────────────┬──────────┬─────────────────┤
│ Close Type  ↕    │  n │ Win Rate │ Total P&L  │  Avg P&L │          Worst  │
├──────────────────┼────┼──────────┼────────────┼──────────┼─────────────────┤
│ take_profit      │ 39 │    90%   │   +€29.79  │   +0.76  │          −4.09  │
│ settled          │ 22 │    64%   │    −€1.72  │   −0.11  │          −5.00  │
│ forced_exit      │  —  │    —    │    −€1.89  │    —     │          −5.00  │
│ stop_loss        │  4 │     0%   │    −€3.20  │   −0.80  │          −2.10  │
├──────────────────┼────┼──────────┼────────────┼──────────┼─────────────────┤
│ Total            │ 65 │    80%   │   +€22.98  │   +0.35  │               — │
└──────────────────┴────┴──────────┴────────────┴──────────┴─────────────────┘
```

- **Layout description:**
  - Panel is a full-width card (`surface` background, `border`, `radius-xl`, `shadow-sm`) within the existing `main` container (max-width 980px).
  - Header row uses the existing `.section-hdr` + `.section-title` pattern. Last-updated is a `<span class="pos-ts">` aligned right.
  - Table uses `width:100%; border-collapse:collapse` — matches `.perf-table` pattern already in the codebase.
  - Column widths: "Close Type" ~30% flex, remaining columns fixed-width right-aligned (tabular nums).
  - A "Total" summary row appears at the bottom of the table body, visually separated by a `divider` top border and rendered with `font-weight:700`.

- **Interactions:**
  - Clicking a column header sorts ascending; clicking again sorts descending. The active sort column shows a `▲`/`▼` glyph. Default sort: Total P&L descending (most profitable exit type first).
  - Rows are not clickable — they are read-only summary rows.
  - The panel auto-refreshes every 30 s (shared with the portfolio poll cycle). No manual refresh control needed — the global "Refresh" button in the topbar triggers it.

- **States:**

  **Loading:**
  ```
  ┌─────────────────────────────────────────────────────┐
  │ ↗ Exit Breakdown                                    │
  │  [████████████░░░░]  [████░░░]  [██████░░░░░░░░░]  │  ← shimmer skeletons
  │  [████████████░░░░]  [████░░░]  [██████░░░░░░░░░]  │
  │  [████████████░░░░]  [████░░░]  [██████░░░░░░░░░]  │
  └─────────────────────────────────────────────────────┘
  ```
  Three shimmer skeleton rows at fixed height (40px each), same column widths as the real table. Use the existing `.skeleton` keyframe animation.

  **Empty (no closed trades yet):**
  ```
  ┌─────────────────────────────────────────────────────┐
  │ ↗ Exit Breakdown                                    │
  │                                                     │
  │          [clock icon]                               │
  │          No closed trades yet                       │
  │          P&L by exit type will appear here          │
  │          as positions are resolved.                 │
  └─────────────────────────────────────────────────────┘
  ```
  Uses existing `.empty` component: `data-lucide="clock"` icon, `<h3>`, `<p>`.

  **Error:**
  ```
  ┌─────────────────────────────────────────────────────┐
  │ ↗ Exit Breakdown                                    │
  │ [!] Could not load exit breakdown — retrying…       │
  └─────────────────────────────────────────────────────┘
  ```
  Uses existing `.error-banner` pattern scoped inside the panel (not the page-level banner). `data-lucide="alert-circle"` icon + inline message. The rest of the panel is hidden; only the error message shows.

  **Success (normal):** Full table as shown in the layout diagram above.

---

## Colour coding

Colour is applied at the cell level, not the row level, to maintain readability regardless of sort order:

| Cell | Rule | Token |
|---|---|---|
| Total P&L | positive → green, negative → red | `var(--yes)` / `var(--no)` |
| Avg P&L | positive → green, negative → red | `var(--yes)` / `var(--no)` |
| Worst | always negative or zero → `var(--no)` | `var(--no)` |
| Win Rate | ≥ 80% → `var(--yes)`, 50–79% → `var(--warn)`, < 50% → `var(--no)`, `—` → `var(--muted)` | |
| Close Type badge | `take_profit` → `result-take_profit` (primary blue), `stop_loss` → `result-stop_loss` (amber), `settled` → `result-won`/`result-lost` based on net P&L, `forced_exit` → new badge `result-forced_exit` (muted/surface-dyn) | |

The "Close Type" column renders the reason as a small badge (matching the existing `.result-badge` pattern) alongside the plain text label so it is visually consistent with the closed-positions list above.

The `forced_exit` badge needs a new CSS class:
```css
.result-forced_exit { background: var(--surface-dyn); color: var(--text); }
```

Win rate is shown as `—` (em dash) for `forced_exit` / `timeout` rows where settlement did not occur (these are pre-settlement exits with no binary win/loss outcome).

---

## API contract (expected response shape)

```json
GET /api/close-reason-stats

[
  {
    "close_reason": "take_profit",
    "n": 39,
    "win_rate": 0.90,       // null when not applicable (forced_exit)
    "total_pnl": 29.79,
    "avg_pnl": 0.76,
    "worst_pnl": -4.09
  },
  {
    "close_reason": "settled",
    "n": 22,
    "win_rate": 0.64,
    "total_pnl": -1.72,
    "avg_pnl": -0.11,
    "worst_pnl": -5.00
  },
  {
    "close_reason": "forced_exit",
    "n": 36,
    "win_rate": null,
    "total_pnl": -1.89,
    "avg_pnl": null,
    "worst_pnl": -5.00
  },
  {
    "close_reason": "stop_loss",
    "n": 4,
    "win_rate": 0.0,
    "total_pnl": -3.20,
    "avg_pnl": -0.80,
    "worst_pnl": -2.10
  }
]
```

The frontend computes the "Total" summary row client-side by summing `n`, `total_pnl`, and re-computing `avg_pnl = total_pnl / n`.

---

## Design tokens / references

All values reference the existing CSS custom properties defined in `index.html`:

| Token | Usage |
|---|---|
| `var(--surface)` | Panel card background |
| `var(--border)` | Card border |
| `var(--radius-xl)` | Card corner radius |
| `var(--sh-sm)` | Card box shadow |
| `var(--divider)` | Table row dividers, footer border |
| `var(--text)` | Default cell text |
| `var(--muted)` | Header labels, "—" placeholders |
| `var(--yes)` | Positive P&L, high win rate |
| `var(--no)` | Negative P&L, low win rate |
| `var(--warn)` | Medium win rate (50–79%) |
| `var(--primary)` | `take_profit` badge |
| `var(--surface-dyn)` | `forced_exit` badge background |
| `var(--font-mono)` | Numeric cells (tabular-nums) |
| `var(--text-xs)` | Table cell text size |
| `.skeleton` | Loading shimmer animation |
| `.error-banner` | Error state styling |
| `.empty` | Empty state container |
| `.section-hdr` / `.section-title` | Panel header row |
| `.result-badge` | Close-reason badge pills |

Typography: table headers use `text-xs`, `font-weight:700`, `letter-spacing:.05em`, `color:var(--muted)`, `text-transform:uppercase` — matching `.perf-th-side`. Numeric cells use `font-variant-numeric:tabular-nums` and `font-family:var(--font-mono)`.

---

## Accessibility notes

- Table must have `role="table"` and `aria-label="P&L breakdown by exit type"`.
- Column headers use `<th scope="col">` with `aria-sort="ascending"` / `"descending"` / `"none"` updated on sort.
- Sort buttons within `<th>` must be focusable (keyboard accessible) with a visible focus ring (`:focus-visible { outline: 2px solid var(--primary); }`).
- Colour is never the sole signal: `—` text is used alongside muted colour for null values; `▲`/`▼` glyphs supplement colour-coded sort indicators.
- ARIA live region on the panel (`aria-live="polite"`) so screen readers announce when data refreshes.
- Contrast: `var(--yes)` on `var(--surface)` must meet WCAG AA (4.5:1 for text-xs). Verify both dark and light themes — the existing theme tokens are already validated for this.

---

## Open questions

1. **Panel placement:** Proposed position is below "Recent Closed" on the Portfolio tab. Alternative: add it to a new "Analytics" tab alongside the MAE panel (#307). Depends on how many analytics panels we accumulate. **Recommend** keeping it on Portfolio for now (it is operationally relevant there) and moving to an Analytics tab if a third panel is added.

2. **`forced_exit` win rate:** These are pre-settlement exits, so there is no binary market outcome. The spec renders `—` for win rate. If the backend can compute "would have won at settlement" retroactively, we could show a parenthetical estimated win rate. Requires PM decision.

3. **Currency symbol:** The table above uses `€`. The existing dashboard uses `$` (USDC) for portfolio values. The trade P&L is in EUR per the backend. Confirm with Tech Lead PM whether to display `€` or `$` here, or let the backend send a `currency` field.

4. **History window:** Should the breakdown be all-time or rolling (last 30/90 days)? Rolling would make regime changes visible but requires a date filter. **Default:** all-time (simplest, matches the live-trade analysis in the issue).

5. **Sort persistence:** Should the user's chosen sort survive a data refresh (re-fetch)? Recommend yes — preserve sort column/direction across refreshes, reset only on page load.
