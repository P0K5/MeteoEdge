# Design Spec — Dashboard: Distinguish Live / Frozen / Stale Model Fair Value

**Issue:** #374 — Epic #366 (Sprint 3 polish)
**Author:** Design-Agent
**Date:** 2026-06-20
**Status:** Ready for implementation

---

## User goal

A trader holding a position across the overnight dead zone (23:00 – 06:00 local) currently sees "live: 78¢ (+2¢)" on the position card even though the model snapshot is many hours old. The user needs to know immediately — without digging into timestamps — whether the model value shown is fresh, intentionally frozen, or unexpectedly stale.

---

## User flow

1. User opens the dashboard and scans position cards.
2. Each card's model fair-value sub-line communicates freshness at a glance via icon + colour.
3. If the user wants detail, they hover the sub-line and read the tooltip.
4. On the Stations tab, any station outside its active window is visually dimmed so the user can correlate position card state with station state without switching back and forth.

---

## Screen layouts

### A. Position card — Model fair value sub-line (three states)

The sub-line lives inside `.prob-item` below `.prob-value` (line 911 of `index.html`, rendered by the `liveSub` variable at lines 886–888). The three states replace and extend the current single `prob-sub` element.

#### State 1 — Live (snapshot < 1 h old AND station `active`)

**Current behaviour is correct; no change to copy or colour.**

```
live: 78¢ (+2¢)          ← existing .prob-sub.up / .down class
```

- Class: `prob-sub` + `up` or `down` (existing)
- Icon: none (existing behaviour — the word "live:" is sufficient)
- Colour: `var(--yes)` for up, `var(--warn)` for down, `var(--muted)` for flat (existing)
- Tooltip: none (existing)

#### State 2 — Frozen by design (station status = `outside_hours`)

```
❄ frozen · 78¢           ← new class: prob-sub-frozen
```

- **Icon:** Unicode snowflake `❄` (U+2744) — universally supported, no extra asset needed. Alternative: clock symbol `🕐` — prefer ❄ as it reads as "paused/cold" rather than "delayed".
- **Copy:** `❄ frozen · {value}¢`
- **Class:** `prob-sub prob-sub-frozen` (new modifier, see CSS below)
- **Colour:** `var(--muted)` — neutral, not alarming, not positive
- **Delta suppressed:** do not render the `(+N¢)` delta suffix; the frozen value is not a live movement
- **Tooltip (exact text):** `"Station is outside its active scanning window. Model fair value will resume when the next local-window poll runs."`
  - Attach via `title` attribute on the `<div>` element
- **Rationale:** The ❄ glyph signals "intentionally paused" (as opposed to broken). Muting the delta removes noise — the value itself is still informative, the change direction is not.

#### State 3 — Stale (snapshot > 1 h old AND station status = `active`)

```
⚠ model stale · 6 h old  ← new class: prob-sub-stale (amber badge style)
```

- **Icon:** `⚠` (U+26A0) — conventional warning symbol
- **Copy:** `⚠ model stale · {N}h old` where `{N}` = `Math.round(ageMinutes / 60)`; for ages < 60 min but > threshold use `{N}m old`
- **Class:** `prob-sub prob-sub-stale` (new modifier, see CSS below)
- **Colour:** `var(--warn)` — amber, already used for warnings throughout the dashboard
- **Background:** `var(--warn-bg)` pill — use `display:inline-flex; padding: 1px 6px; border-radius: var(--radius-full)` to form a badge, consistent with how `.badge-disabled` renders on station cards
- **Delta suppressed:** same as frozen — the value is stale so the delta is misleading
- **Tooltip:** `"Model snapshot is older than 1 hour but the station should be active. Check the scanner logs."`
- **Rationale:** This is an unexpected failure path, not a designed pause, so it deserves a louder amber treatment vs. the neutral ❄.

#### ASCII layout — prob-item block (all three states)

```
┌─────────────────────────────┐
│ Model fair value            │ ← .prob-label
│ 78¢                         │ ← .prob-value.mine
│ ❄ frozen · 78¢              │ ← .prob-sub.prob-sub-frozen  (state 2)
│ ⚠ model stale · 6 h old    │ ← .prob-sub.prob-sub-stale   (state 3)
│ live: 78¢ (+2¢)             │ ← .prob-sub.up               (state 1, unchanged)
└─────────────────────────────┘
Only one sub-line is rendered at a time.
```

---

### B. Station card — `outside_hours` visual treatment

**Current situation:** The `status-badge-small` at line 1686 shows `"outside hours"` as a small muted text label in the card header. The `badge-outside_hours` CSS already sets `background: var(--surface-off); color: var(--muted)`. The card border-left accent classes at lines 401–403 cover `active`, `no_data`, and `disabled` but **not** `outside_hours` — that case falls through to `transparent`.

**Proposed change:** Two targeted additions:

1. **Border-left accent** — add `.station-card.accent-outside_hours { border-left-color: var(--muted); }` so the card has a visible left accent like all other states.
2. **Card background dimming** — add `.station-card.accent-outside_hours { background: var(--surface-off); }` (override the default `var(--surface)`). This creates a subtly darker card background that immediately separates outside-hours stations from active ones without being alarming.

The `accentClass` logic in JS (lines 1664–1668) must be extended to emit `' accent-outside_hours'` when `s.status === 'outside_hours'`.

**Do not** change the `status-badge-small` copy — "outside hours" is already clear.

**Rationale:** Dimming the whole card is the lowest-overhead treatment that achieves scan-speed differentiation. A border-left accent on its own is too subtle (only 3 px); combining it with a background change makes the card distinctly muted at a glance. The existing `.surface-off` value (`#1d222d` dark / `#edf0f8` light) is close enough to the card background to avoid harshness while still being perceptible.

---

## New CSS to add (after line 356 in `index.html`)

```css
/* ── MODEL FRESHNESS SUB-LINE MODIFIERS ── */
.prob-sub-frozen{
  color:var(--muted);
}
.prob-sub-stale{
  display:inline-flex;align-items:center;gap:4px;
  background:var(--warn-bg);color:var(--warn);
  padding:1px 6px;border-radius:var(--radius-full);
  font-size:10px;font-weight:600;font-family:var(--font-mono);
}
```

And after line 403 in the station-card accent block:

```css
.station-card.accent-outside_hours{
  border-left-color:var(--muted);
  background:var(--surface-off);
}
```

---

## JS logic for state selection (guide for developer)

The frontend needs `my_prob_now_ts` on the `PositionOut` payload (backend task). With that field available:

```js
function modelFreshnessState(p, stationStatus) {
  if (p.my_prob_now == null) return 'none';
  const ageMs = Date.now() - new Date(p.my_prob_now_ts).getTime();
  const ageMinutes = ageMs / 60000;
  if (stationStatus === 'outside_hours') return 'frozen';
  if (ageMinutes > 60) return 'stale';
  return 'live';
}
```

The developer needs to pass the station's `status` field alongside the position data to the card render function. Since positions already carry `p.station`, the status can be looked up from the stations array already fetched by the dashboard.

Rendered sub-line per state:

```js
// state === 'live'   → existing liveSub unchanged
// state === 'frozen'
const frozenSub = `<div class="prob-sub prob-sub-frozen"
  title="Station is outside its active scanning window. Model fair value will resume when the next local-window poll runs."
  >❄ frozen · ${p.my_prob_now}¢</div>`;

// state === 'stale'
const ageH = Math.round(ageMinutes / 60);
const ageLabel = ageMinutes >= 60 ? `${ageH}h old` : `${Math.round(ageMinutes)}m old`;
const staleSub = `<div class="prob-sub prob-sub-stale"
  title="Model snapshot is older than 1 hour but the station should be active. Check the scanner logs."
  >⚠ model stale · ${ageLabel}</div>`;
```

---

## Design tokens / references

| Token | Value (dark) | Usage |
|---|---|---|
| `--muted` | `#6a7290` | Frozen sub-line text, outside_hours border accent |
| `--warn` | `#e8a845` | Stale sub-line text |
| `--warn-bg` | `#2a1f08` | Stale sub-line badge background |
| `--surface-off` | `#1d222d` | Outside-hours station card background |
| `--yes` | `#4dc480` | Live sub-line up colour (unchanged) |

All tokens have corresponding light-theme values already defined in `:root [data-theme="light"]` — no new tokens are needed.

---

## Accessibility notes

1. **Colour-blind safety:**
   - The three states are differentiated by **icon + text** in addition to colour, so they are distinguishable without colour perception. State 1 = "live:", State 2 = "❄ frozen", State 3 = "⚠ model stale". A user with deuteranopia who cannot distinguish green (live) from amber (stale) can still read the label.
   - The station card dimming uses background lightness, not hue — it works for all forms of colour-blindness.

2. **Contrast:** `var(--warn)` `#e8a845` on `var(--warn-bg)` `#2a1f08` achieves ≈ 7.2:1 contrast (WCAG AA for small text at 4.5:1 required, AAA at 7:1 — passes both).

3. **ARIA / screen readers:** The `title` attribute on the sub-line `<div>` is announced by most screen readers on focus. Since these are non-interactive divs, also add `aria-label` with the same tooltip text for reliable SR support:
   ```html
   aria-label="Frozen: station outside active window"
   aria-label="Stale: model snapshot 6 hours old"
   ```

4. **Responsive:** The sub-line is already `font-size: 10px` inline with the existing `.prob-sub`. The stale badge pill adds ~12 px vertical height at most — acceptable within the existing `.prob-item` flex column. No layout changes required at any breakpoint.

5. **Motion:** No animation is introduced. The existing `.live-dot` blink animation (topbar pill) is unaffected.

---

## Open questions

1. **Station status lookup in position card render:** The `renderPosition()` function does not currently receive the full stations array. The developer should confirm the cleanest way to pass or look up `stationStatus` — options are (a) add `station_status` to `PositionOut`, (b) build a `Map<stationId, status>` in the page-level render function and pass it through, or (c) derive it inline. Option (b) is preferred to avoid a backend schema change beyond `my_prob_now_ts`.

2. **Threshold for "live":** The spec uses 1 h as the freshness boundary for "stale", matching the issue definition. Confirm with the Tech Lead PM whether this should be configurable or hardcoded in the frontend JS constant (`const STALE_THRESHOLD_MS = 60 * 60 * 1000`).

3. **Frozen value display:** The spec shows `❄ frozen · {value}¢` (the value is still shown). If the product preference is to hide the value entirely when frozen, the sub-line copy changes to `❄ outside active window` and the `.prob-value` above it remains as the sole value display. Current spec: show the value, since it is the last valid model probability and is useful for reference.
