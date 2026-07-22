# Design Spec — Edge Tab: Bot's-Eye Per-Bracket Decision View

Status: **Draft — pending Tech Lead PM technical feasibility review**
Related: Epic #754, Issues #755 (this spec), #756 (backend: gate verdict + persistence),
#757 (backend: serve endpoint), #758 (frontend: rebuild)
Supersedes: `docs/design/edge-tab.md` (Epic #510) — that spec's station selector and
Today/D+1 toggle patterns are **reused** below; its ensemble-distribution chart and
KPI strip are **replaced** by this spec.

## Why this redesign

Product feedback (2026-07-21): the current Edge tab "looks really confusing and has
no use." Two root causes, per epic #754:

1. The tab's numbers come from a **parallel recompute** (`get_ensemble_distribution`
   / `get_bracket_analysis`) that can disagree with the probability the scanner
   **actually traded on**.
2. The tab shows **no reason** a bracket did or didn't trade — an operator asking
   "why so few trades today?" has to go read logs.

This spec fixes both: the tab becomes a read-only mirror of the scanner's last poll,
one row per evaluated bracket, with a **gate-verdict chip** that names the exact
reason each bracket did or didn't trade.

## User goal

Given a station and a date, answer in one glance: *"What did the bot see on its last
poll, and for each bracket, why did it trade, not trade, or paper-trade?"* — without
reading logs.

## User flow

1. Operator opens the **Edge** tab (lands on last-selected station via
   `localStorage`, defaulting to the first configured station; date = today).
   `[Implementation correction, #782/#787/PR #783: "date = today" is no longer
   unconditional. Every Polymarket temperature market closes at noon UTC on its
   label date, so the Today partition is empty by construction for the
   12:00–24:00 UTC half of every day. The tab now auto-advances to D+1 when
   Today comes back empty and D+1 has data — see the amended §1 below for the
   persistent in-content indicator that makes this state visible. The
   semantic fix (re-anchoring the day boundary to the market's settlement
   window instead of the UTC calendar date) is tracked separately in #782,
   intentionally left open; this is the UX mitigation.]`
2. Operator reads the **scan summary bar**: when the last poll ran, the model's
   forecast high vs the current observed high. This is scan-level context shared by
   every bracket below it.
3. Operator scans the **per-bracket table**: for each bracket, market price on both
   sides, the model's probability, EV on both sides, and the **gate verdict chip**.
   One row is visually emphasized — the trade that happened, or (if none did) the
   bracket that came closest.
4. Operator hovers/focuses a gate chip to see the exact threshold vs actual value
   that produced the verdict (e.g. "edge 12.4¢ < min 15¢").
5. Operator optionally expands **Forecast inputs** (collapsed by default) for the
   raw ensemble mean/range/member count, if they want the research-grade detail that
   used to be the KPI strip.
6. Operator optionally toggles **D+1** to see tomorrow's evaluation (always
   shadow-routed — chip explains this, doesn't imply something is wrong).
7. (Future, #528) Operator clicks a row to jump to the matching Polymarket market.

## Screen layout

```
┌─ tab-bar: Portfolio | Stations | Edge | EMOS | Config ──────────────────────┐
│                                                                                │
│  Station pill row:  [ KJFK ] [ KLGA ] [ KEWR ] [ KBOS ] …                    │
│                                                                                │
│  ┌─ Scan summary bar ──────────────────────────────────────────────────────┐│
│  │  🕐 Last scan 14:32:05 (3m ago)   Today ▐ D+1                            ││
│  │  Model forecast high 71°F  ·  Current observed high 68°F                 ││
│  └────────────────────────────────────────────────────────────────────────┘│
│  ▸ Forecast inputs (collapsed — ensemble mean / range / members)             │
│                                                                                │
│  ┌─ Per-bracket decision table ─────────────────────────────────────────────┐│
│  │ Bracket   Market (Y/N)   Model p_yes     EV (Y/N)        Gate           ││
│  │ 68–70°F   58¢ / 45¢      ● 41%           −3.2¢ / +1.1¢   ● Edge too small││
│  │ 71–73°F   34¢ / 68¢      ● 62%           +9.8¢ / −4.0¢   ● Traded  ★     ││
│  │ 74–76°F   12¢ / 90¢      ● 22% (raw 9%)  −0.5¢ / +2.0¢   ● Confidence low││
│  │ …                                                                        ││
│  └────────────────────────────────────────────────────────────────────────┘│
│  ▸ Gate legend (collapsed — what each chip means)                            │
└────────────────────────────────────────────────────────────────────────────┘
```

All components live inside `.main` (max-width 980px), matching Portfolio/Stations —
unchanged from the prior spec.

---

## Components

### 1. Tab bar, station selector, Today/D+1 toggle — reused unchanged

These three pieces are **not** redesigned; `docs/design/edge-tab.md` §1, §2, §6
still apply verbatim (`.edge-station-row` / `.edge-station-pill`,
`.edge-date-toggle` / `.edge-date-btn`). No new visual language needed here.

One behavior change: **D+1 is always shadow-routed** by policy (per epic #754's
`next_day_shadow` verdict and issue #687), so unlike v1's "disabled until a market
exists" framing, D+1 should be **enabled whenever a D+1 market exists**, and every
row will simply show the `next_day_shadow` chip rather than a mix of verdicts. The
disabled/tooltip pattern for "no D+1 market" is unchanged.

`[Implementation correction, #787/PR #783: two further amendments to this
"reused unchanged" section, both driven by the Designer's change request on
PR #783.
First — auto-advance: the tab now lands on D+1 automatically when Today comes
back empty and D+1 has data (see User flow step 1 above), not just on a
manual toggle click.
Second — persistent in-content D+1 indicator: the toggle's
.edge-date-btn.active tint alone was judged insufficient signal for trading
UI once the tab could land on D+1 by default rather than only by a deliberate
click. Whenever the D+1 partition is showing — auto-advanced OR manually
toggled — a persistent textual banner renders inside the content area itself
(.edge-d1-indicator, between the date toggle and the scan summary bar),
reading "Today's markets closed — showing D+1 (shadow-only)". It survives
every re-render and is visible without hovering or clicking anything.
"Shadow-only" is factual, not hedged wording — scanner.py forces
shadow=(shadow_* or is_next_day_eval) for every D+1 evaluation (issue #687),
so a D+1 row is never a live position.]`

### 2. Scan summary bar (replaces the 5-card KPI strip)

The old KPI strip (Ensemble Mean / Bias-Corrected / Members / Range / Best Edge)
is **removed in full** — the Bias-Corrected value is gone because there is no
longer a parallel model to compute it from, and Members/Range move into the
collapsed "Forecast inputs" block (§3) since they're research detail, not a
trading decision. What replaces it is a single-line scan context bar:

```css
.edge-scan-bar{
  display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-2) var(--space-5);
  padding:var(--space-3) var(--space-4);
  background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius-lg);
  margin-bottom:var(--space-4);font-size:var(--text-sm);
}
.edge-scan-freshness{display:flex;align-items:center;gap:6px;color:var(--muted);font-family:var(--font-mono);font-variant-numeric:tabular-nums;}
.edge-scan-freshness i{width:14px;height:14px;}
.edge-scan-freshness.stale{
  background:var(--warn-bg);color:var(--warn);border-radius:var(--radius-full);
  padding:3px 10px;font-weight:600;
}
.edge-scan-forecast{color:var(--text);font-weight:600;}
.edge-scan-forecast .label{color:var(--muted);font-weight:500;margin-right:4px;}
```

- **Poll-freshness indicator:** `clock` icon + `poll_ts` formatted as `HH:MM:SS
  (Nm ago)`. Default (fresh) styling: plain `.edge-scan-freshness` (muted text).
  **Stale** styling (`.edge-scan-freshness.stale`, amber pill — reuses the exact
  visual treatment of the existing `.prob-sub-stale` badge) applies when `poll_ts`
  is older than **2× the configured `POLL_INTERVAL_SECONDS`** (fallback: 10 minutes
  if that config isn't exposed to the frontend — see Open Questions). Tooltip:
  "Last scan was Nm ago — the scanner may be paused or the station outside its
  active window."
- **Forecast context:** `Model forecast high {forecast_high}°F` and
  `Current observed high {current_high}°F`, plain text, `--text` / `--muted` label
  pairing (mirrors `.wc-label`/`.wc-value` pattern's color roles but inline, not
  card-based — this is context, not a KPI to scan for, so it shouldn't compete
  visually with the table below it).
- **No data yet today:** bar shows `No scan recorded for {station} today` in
  `var(--muted)`, freshness clock hidden, table renders its own empty state (§6).

### 3. Forecast inputs (collapsed secondary block)

```html
<details class="edge-forecast-inputs">
  <summary>Forecast inputs</summary>
  <div class="edge-forecast-inputs-body"> … ensemble mean / range / members … </div>
</details>
```

- Native `<details>`/`<summary>` — no new disclosure component, free keyboard and
  screen-reader support, closed by default (`[Tech constraint: this is optional
  per #757's acceptance criteria ("may be retained") — agreed approach: collapsed
  by default so it never competes with the primary decision table, per Clarity]`).
- **Body content**, plain key-value rows (not KPI cards — this is de-emphasized
  detail now): `Ensemble mean`, `Range (min–max)`, `Members`. Reuses
  `.bias-th`/`.deb-th` label styling already present for similar secondary
  tabular detail elsewhere in the dashboard.
  `[Implementation correction, #758/PR #779: relabelled from "Range (5th–95th pct)"
  to "Range (min–max)" — forecast_inputs.ensemble_range_low/high is the raw
  min/max across contributing models, not a percentile; the original wording
  claimed a statistic the data doesn't represent. Accepted — more accurate
  labeling serves Clarity better than the original copy.]`
- If the backend has retired this data entirely (per #757's "retire or demote"
  choice), the `<details>` element is omitted rather than rendered empty — confirm
  final call with Tech Lead PM before D ships.

### 4. Per-bracket decision table

```css
.edge-decision-table{width:100%;border-collapse:collapse;font-size:var(--text-sm);font-variant-numeric:tabular-nums;}
.edge-decision-table th{text-align:left;padding:var(--space-2) var(--space-3);font-size:var(--text-xs);font-weight:700;letter-spacing:.05em;color:var(--muted);border-bottom:1px solid var(--divider);}
.edge-decision-table td{padding:var(--space-3);border-bottom:1px solid var(--divider);vertical-align:top;}
.edge-decision-table tr:last-child td{border-bottom:none;}
.edge-decision-table tr.row-traded{background:var(--yes-bg);border-left:3px solid var(--yes);}
.edge-decision-table tr.row-near-miss{background:var(--no-bg);border-left:3px solid var(--no);}
.edge-decision-table tr.row-best-signal{background:var(--edge-bg);border-left:3px solid var(--edge);}
.edge-decision-table tr.row-thin{opacity:.55;}
```

**Columns, in order** (exactly the five from the acceptance criteria — no new
columns; supplementary detail lives inside cells, not as extra columns):

| # | Header | Cell content |
|---|---|---|
| 1 | `Bracket` | `68–70°F` — `var(--font-mono)`, en-dash, matches `.footer-bracket` convention |
| 2 | `Market (YES · NO)` | Two values, e.g. `58¢ / 45¢` — YES ask first, NO ask second, plain `--text` color, `/` separator (no bar chart — the old `.edge-prob-bar` is dropped, it added visual noise for a single scalar) |
| 3 | `Model p_yes` | Bold capped value, e.g. `62%`. If `raw_p_yes` differs from the capped/served value (the 0.05/0.95 clamp), a small caption below reads `raw 9%` in `var(--muted)`/`var(--text-xs)` — reuses the `.prob-sub`/`.fallback-tag` micro-caption pattern. Below that, the `emos_mode` inline chip (§5). |
| 4 | `EV (YES · NO)` | Two signed values, e.g. `+9.8¢ / −4.0¢`. Each half colored independently: positive `var(--yes)`, negative `var(--no)`, within ±0.5¢ `var(--muted)` (reuses the near-zero threshold concept from the v1 spec, now per-side rather than per-row) |
| 5 | `Gate` | The gate-verdict chip — see §5 |

- **Row structure:** one row per bracket the scanner evaluated in the persisted
  scan (`scan_decisions`), in ascending bracket order.
- **Row emphasis** (mutually exclusive, in this precedence order — only one class
  applies per table):
  1. `row-traded` (green accent) — if any bracket has `traded_live`.
  2. `row-near-miss` (red accent) — else, if any bracket has `entry_guard` or
     `timeout_today`, the one with the higher of `ev_yes`/`ev_no` (its own flagged
     side) gets this class. This is the single most useful row on the page for
     "why so few trades" — the model wanted to trade and something operational
     stopped it.
  3. `row-best-signal` (purple accent, reusing `--edge`/`--edge-bg` exactly as v1's
     "best edge" highlight) — else, the bracket with the single highest
     `max(ev_yes, ev_no)` across all rows, regardless of verdict. Gets a small
     `★ Best signal today` caption next to its Gate chip (text, not icon-only, so
     it's announced to screen readers, not just implied by background color).
     `[Implementation correction, #787/PR #783: the caption is day-aware — reads
     "Best signal today" when viewing Today and "Best signal D+1" when viewing
     D+1 (auto-advanced or manually toggled). The original wording was
     hardcoded to "today" regardless of which partition was showing, a
     pre-existing bug (reachable since v1's manual D+1 toggle) whose severity
     PR #783 raised from a deliberate-click edge case to the default view for
     roughly half of every day, since it now sits in that PR's blast radius.]`
  4. No highlight if the table is empty or all EVs are deeply negative (nothing
     resembling a signal) — don't manufacture a highlight from noise.
- **Thin-signal de-emphasis:** brackets with negligible liquidity or EV near zero
  on both sides keep the `row-thin` treatment from v1 (`opacity:.55`), applied
  independent of the emphasis classes above.

### 5. The gate-verdict chip

Reuses the exact `.status-badge` + `.status-dot` anatomy already used for station
status and promotion status (`badge-active`, `badge-shadow`, etc.) — no new
component shape, just a new set of `badge-*` modifiers. This keeps the chip
familiar on sight before the operator has even read this spec.

```css
.gate-chip{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:var(--radius-full);font-size:var(--text-xs);font-weight:700;letter-spacing:.02em;white-space:nowrap;}
.gate-chip .status-dot{width:6px;height:6px;border-radius:var(--radius-full);}

/* Family 1 — executed for real */
.gate-traded_live{background:var(--yes-bg);color:var(--yes);}
.gate-traded_live .status-dot{background:var(--yes);}

/* Family 2 — passed the model/market gates, blocked or missed at execution */
.gate-entry_guard,.gate-timeout_today{background:var(--no-bg);color:var(--no);}
.gate-entry_guard .status-dot,.gate-timeout_today .status-dot{background:var(--no);}

/* Family 3 — shadow-routed by policy (paper only, not a rejection) */
.gate-shadow_only,.gate-next_day_shadow{background:var(--warn-bg);color:var(--warn);}
.gate-shadow_only .status-dot,.gate-next_day_shadow .status-dot{background:var(--warn);}

/* Family 4 — rejected on the numbers (the routine, majority case) */
.gate-below_min_edge,.gate-above_max_edge,.gate-below_min_price,
.gate-below_min_confidence,.gate-margin_gate,.gate-mae_gate{background:var(--surface-off);color:var(--muted);}
.gate-below_min_edge .status-dot,.gate-above_max_edge .status-dot,.gate-below_min_price .status-dot,
.gate-below_min_confidence .status-dot,.gate-margin_gate .status-dot,.gate-mae_gate .status-dot{background:var(--muted);}
```

**Why four color families, not eleven:** eleven distinct hues would be
indistinguishable at a glance and would fight the table's real job (reading down
one column fast). Instead, color encodes the *category* an operator actually
needs at a glance, and the label text + tooltip carry the specific reason.
The four families deliberately reuse color meanings **already established
elsewhere in this dashboard**, not new ones:

- Green (`--yes`/`--yes-bg`) already means "live/real" (`.badge-primary` on the
  EMOS tab). Reused here for `traded_live`.
- Amber (`--warn`/`--warn-bg`) already means "running in shadow/paper mode"
  (`.badge-shadow` on the EMOS tab). Reused here for `shadow_only` /
  `next_day_shadow` — an operator who already knows "amber = shadow" from the
  EMOS tab reads this instantly, no new vocabulary to learn.
- Gray (`--surface-off`/`--muted`) already means "legacy/routine baseline"
  (`.badge-legacy`). Reused here for the six numeric-threshold rejections — this
  is deliberately the **least visually loud** family because it is the
  **majority, expected case** on most polls. Painting six-elevenths of a table
  red every scan would train operators to ignore red, which is the opposite of
  what we want for family 2 below.
- Red (`--no`/`--no-bg`) is reserved for the two verdicts where the model
  actually **wanted** to trade and something else stopped it
  (`entry_guard`, `timeout_today`) — the rarest and most actionable case, and the
  direct answer to "why so few trades" when the KPI strip's Best Edge number used
  to be positive but nothing happened. This is a **new** use of `--no` (previously
  only "market moved against us" / errors) but it's semantically consistent:
  red = "this needs a look."

| Verdict | Chip label | Family | Tooltip (native `title`) |
|---|---|---|---|
| `traded_live` | `Traded` | green | `Traded — cleared every gate on the {side} side.` (deliberately drops "live" — see #758/PR #779 discussion and Backlog #780) |
| `entry_guard` | `Blocked` | red | `{guard reason text from backend}` e.g. "Already have an open position in this bracket today — duplicate-entry guard." |
| `timeout_today` | `Timed out` | red | `Order placed but not filled before the settlement window closed.` |
| `shadow_only` | `Shadow` | amber | `Would trade, but this station/side runs in shadow (paper) mode — no live order placed.` |
| `next_day_shadow` | `Next-day` | amber | `D+1 evaluations are shadow-only by policy — informs tomorrow, no live order today.` |
| `below_min_edge` | `Edge too small` | gray | `edge {actual}¢ < min {threshold}¢` |
| `above_max_edge` | `Edge too large` | gray | `edge {actual}¢ > max {threshold}¢ — flagged as a pricing-error risk, not traded` |
| `below_min_price` | `Price too low` | gray | `ask {actual}¢ < min {threshold}¢ — too thin to fill reliably` |
| `below_min_confidence` | `Confidence low` | gray | `model p_yes {actual} < min confidence {threshold} required for YES` |
| `margin_gate` | `Margin too thin` | gray | `YES/NO margin {actual} < required {threshold} — model isn't decisive enough here` |
| `mae_gate` | `Forecast error high` | gray | `rolling MAE {actual}°F > {threshold}°F — recent accuracy too poor for a live NO entry` |

Tooltip content needs `{actual}`/`{threshold}` values from the API — see Open
Questions §1 for the exact fields this requires from story B/C.

### 6. Gate legend (collapsed disclosure)

Eleven verdicts is a lot to memorize on first use. A second `<details>` element,
this one below the table:

```html
<details class="edge-gate-legend">
  <summary>Gate legend — what each chip means</summary>
  <ul> … one line per verdict, dot + label + one-sentence meaning, grouped by the four families … </ul>
</details>
```

Same native disclosure pattern as §3, closed by default, zero new component
machinery. Content is the four family groups from §5's table, in the same order,
so the legend and the chips teach the same taxonomy.

### 7. Loading / empty / stale / error states

| Component | Loading | Empty (no scan today) | Stale (scan exists, but old) | Error |
|---|---|---|---|---|
| Scan summary bar | `.skeleton` block in place of freshness text | Bar reads `No scan recorded for {station} today` in `var(--muted)`, no freshness pill | `.edge-scan-freshness.stale` amber pill (§2) — **data still shown**, never hidden | `.error-banner.visible`: "Couldn't load the scan summary. Retry." |
| Decision table | 3–5 skeleton rows (`.edge-table-skeleton`, unchanged from v1) | `.empty` block (icon `inbox`, heading `No scan for {station} today`, paragraph `MeteoEdge hasn't evaluated this station yet today — check back after the next poll.`) | Table renders normally with the last known rows — staleness is communicated once, in the scan bar, not repeated per row | `.error-banner.visible` replacing the table: "Couldn't load the decision table. Retry." (reuse `.pg-btn` "Retry" affordance) |
| D+1 with no market | N/A | `D+1` button `disabled` + tooltip "No D+1 market available for this station yet" (unchanged from v1) | — | — |

Key difference from v1: **stale data is never hidden.** The old spec's KPI/chart
empty-vs-error states didn't have a "stale but still show it" case; this view's
entire purpose is diagnosing scanner behavior, so showing the last known decision
with a clear "this is N minutes old" flag is more useful than blanking the screen.

---

## Design tokens / references

No new color tokens. Every family above reuses existing `--yes`/`--no`/`--warn`/
`--muted`+`--surface-off`/`--edge` pairs, exactly as catalogued in
`docs/design/edge-tab.md`'s Design tokens section (still accurate — repeated here
for the tokens this spec newly recruits into gate-chip duty):

- `--yes` / `--yes-bg` — traded chip, row-traded emphasis
- `--no` / `--no-bg` — entry-guard/timeout chip, row-near-miss emphasis
- `--warn` / `--warn-bg` — shadow chips (already means "shadow" via `.badge-shadow`)
- `--muted` / `--surface-off` — numeric-rejection chips (already means "legacy/routine" via `.badge-legacy`)
- `--edge` / `--edge-bg` — best-signal row emphasis only (unchanged role from v1)
- `--font-mono` — bracket labels
- `--text-xs` / `--text-sm` — chip and cell typography

New CSS **classes**: `.edge-scan-bar`, `.edge-scan-freshness`, `.edge-forecast-inputs`,
`.edge-decision-table`, `.gate-chip` + eleven `.gate-{verdict}` modifiers,
`.edge-gate-legend`. All compose existing tokens; no new visual primitives.
`[Implementation correction, #787/PR #783: adds `.edge-d1-indicator` —
reuses `--warn`/`--warn-bg` exactly as the amber shadow-chip family above, no
new token.]`

**Removed** (do not carry forward from v1): `.edge-kpi-row`/`.wallet-card` KPI strip
usage on this tab, `.edge-dist-chart`, `.edge-prob-bar`/`.edge-prob-bar-fill`,
`.edge-edge-value`/`.edge-table tr.best-edge-row`/`.best-edge-chip` (superseded by
`row-best-signal`), the "Bias-Corrected" KPI card specifically.

## Accessibility notes

- **Gate chip:** rendered as `<span class="gate-chip gate-{verdict}"
  aria-label="{label}: {tooltip text}">` — the full explanation is available to
  screen readers via `aria-label`, not gated behind a hover-only `title`. The
  `title` attribute is kept too (native tooltip on hover/focus for sighted mouse
  and keyboard users, consistent with the rest of the app's tooltip convention —
  see `docs/design/edge-tab.md`'s reliance on native `title`).
- **Row emphasis:** the `★ Best signal today` / `★ Best signal D+1` caption (§4,
  day-aware per the #787/PR #783 correction) uses visible text, not color/icon
  alone, same requirement carried over from v1's best-edge chip.
- **D+1 indicator:** the persistent `.edge-d1-indicator` banner (§1) is text, not
  color/icon alone — same rationale as the gate chip and best-signal caption above.
- **Disclosures:** native `<details>`/`<summary>` for both Forecast inputs (§3)
  and the Gate legend (§6) — free keyboard toggle (Enter/Space on the summary),
  free screen-reader semantics, no ARIA authoring needed.
- **Table:** `<th scope="col">` on all five headers, same as v1.
- **Contrast — #602 constraint:** the amber (`--warn`/`--warn-bg`) and green
  (`--yes`/`--yes-bg`) chip pairs are the same token pairs flagged in #602 as
  borderline in light theme (~4.47–4.60:1 at small bold text). This spec does
  **not** fix #602 — it's a shared-token issue, out of scope here — but it must
  not make it worse: gate chips use `var(--text-xs)` at `font-weight:700`,
  matching the sizing already in use for `.status-badge`/`.badge-active`, not a
  smaller size that would push contrast further under AA. If/when #602 lands a
  token nudge, every gate chip inherits the fix automatically since nothing here
  is a bespoke color. `[Tech constraint: #602 light-theme --yes/--warn badge
  contrast is a known, separately-tracked issue — agreed approach: reuse the
  tokens as-is here rather than block this spec on it, since a token-level fix
  in #602 propagates to this view for free]`.
- **Stale-state pill:** the amber `.edge-scan-freshness.stale` pill inherits the
  same #602 contrast note above.

## Open questions / flags for Tech Lead PM

1. **Traded side is not derivable from `gate_verdict` alone.** `traded_live` says
   *that* a bracket traded, not *which side* (YES or NO) — but the scanner's
   `Candidate` already carries a `side` field (`src/strategy/scanner.py` L364).
   Recommend story B additionally persist `side: "YES"|"NO"|null` per row so the
   UI can indicate the traded side (e.g. bold/underline the correct half of the
   `Market`/`EV` columns) instead of leaving the operator to infer it from which
   EV is positive.
2. **Tooltip data contract for the six numeric-rejection verdicts** needs
   `{actual}` and `{threshold}` values per row (e.g. `gate_actual`,
   `gate_threshold`, optionally `gate_unit`) — these are the exact numbers the
   scanner already compares (`MIN_EDGE_CENTS`, `MAX_EDGE_CENTS`,
   `MIN_PRICE_CENTS`, `MIN_CONFIDENCE_YES`, the margin gate, and rolling MAE vs
   `MAX_RESIDUAL_MAE_F_FOR_LIVE` — all named in `src/strategy/scanner.py`).
   For `entry_guard`, the reason is prose, not a threshold comparison (see
   `src/scripts/run.py` L593/597/601-604) — recommend the API pass that string
   through as `gate_detail` rather than the frontend reconstructing it.
3. **Enum naming:** this spec adopts the eleven snake_case verdicts as listed in
   issue #756/#757/epic #754 verbatim (`traded_live`, `entry_guard`,
   `timeout_today`, `shadow_only`, `next_day_shadow`, `below_min_edge`,
   `above_max_edge`, `below_min_price`, `below_min_confidence`, `margin_gate`,
   `mae_gate`). No renaming requested — confirming this closes the naming
   coordination the epic asked for between story A and B.
4. **Row-emphasis precedence** (traded → near-miss guard/timeout → best-signal
   among rejections) is a UX judgment call, not something the backend needs to
   precompute — the frontend can derive it client-side from the returned bracket
   list (`max(ev_yes, ev_no)` per row). Flagging only so story C doesn't
   accidentally also try to flag a "best" row server-side and conflict with this
   logic.
5. **Staleness threshold** proposes 2× `POLL_INTERVAL_SECONDS` (fallback 10 min).
   Confirm whether that config value is available to the frontend (currently
   backend-only) or whether a fixed 10-minute threshold is simpler for v1 — either
   is fine UX-wise, just needs a decision before D.
6. **#528 click-through:** if story D implements it, recommend the whole `<tr>`
   be the click target (not just the Bracket cell) for a larger hit area — not a
   blocker for this spec either way.
