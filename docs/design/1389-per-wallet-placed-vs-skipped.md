# Design Spec — Placed vs Skipped Signals on Per-Wallet Paper Performance (#1389)

Follow-up to #1314 (criterion 4) and #1390. Addendum to
`docs/design/copy-trading-live-views.md` (Positions & P&L). Design direction
only; no code. Status: **proposed**, pending Tech Lead PM feasibility sign-off
(see Open questions).

## User goal

An operator comparing paper wallets sees a P&L figure per wallet but not its
denominator: how many of that wallet's signals the engine actually copied vs
dropped, and why. A wallet with +$40 on 3 settled trades looks very different
if 60 signals were skipped as structurally unrepresentable. Goal: see
"placed vs skipped (24h)" at a glance, and reach the reason breakdown in one
click, without leaving the table.

## Scope

- **Paper per-wallet table only** (`_copyPerWalletTableHtml`, "Per-wallet P&L
  breakdown"). The Live per-wallet table is unchanged: live skips have their
  own vocabulary (`live_*` reasons) and the "never blend live and paper"
  rule applies. A paper-only column is labelled as such by its panel.
- Independent of the closed-positions history list and its polling (#1473).
  This spec touches only the per-wallet breakdown table and the
  `/api/copy-trading/positions` `per_wallet` rows.

## Data contract (UX-relevant; shape is the PM's call)

[Tech constraint: no skip vocabulary in JS — agreed approach: the API sends
display-ready strings; the frontend renders them verbatim, escaped, and never
maps raw reason keys itself.]

Each paper `per_wallet` row gains one optional object, `signals_24h`
(null/absent = "could not be computed"):

| Field | Meaning |
|---|---|
| `placed` | int, signals copied in the last 24h (`WINDOW_HOURS`) |
| `skipped` | int, signals skipped in the last 24h (= sum of `reasons[].count`) |
| `reasons[]` | sorted by `count` desc, then reason key |
| `reasons[].reason` | raw key, e.g. `non_binary_outcome_index` |
| `reasons[].meaning` | the `SKIP_REASONS` meaning string; for an unknown key, `"Unrecognized skip reason"` |
| `reasons[].class` | `structural` / `routine` / `lifecycle` / `breaker` / `unknown` (`classify()`) |
| `reasons[].class_label` | display word: "Structural", "Routine", "Lifecycle", "Circuit breaker", "Unrecognized" |
| `reasons[].count` | int |
| `reasons[].trend` | `STEADY` / `SPIKE` / `NEW` (`trend()`) |
| `reasons[].accepted_ref` | `accepted_ref()` result, e.g. `#1390`, else null |

Display strings (`class_label`, the meaning text) live in Python next to
`SKIP_REASONS`; the JS only chooses a CSS modifier from `class`. An
unexpected `class` value gets the `unknown` styling (so new classes cannot
hide).

## User flow

1. Operator opens Copy · Positions & P&L (Paper column).
2. In the per-wallet table, each wallet row shows "12 placed · 5 skipped".
3. Operator clicks "Why skipped?" on a row with skips; a detail row opens
   directly beneath it listing each reason with count, class, and meaning.
4. Click again (or Esc while focus is inside) to collapse. Multiple rows may
   be open at once. Open state survives the periodic data refresh (keyed by
   wallet address) so a poll never snaps the panel shut under the reader.

## Screen layout

### Per-wallet table (desktop / tablet)

New column **"Signals (24h)"** inserted directly after Wallet (before
Settled), so it is seen first when the table scrolls.

```
Wallet           Signals (24h)                    Settled  Realized P&L
0xbca0…8f1 [cp]  12 placed · 5 skipped            3        +$40.10
                 [v Why skipped?]
0x9243…a02 [cp]  8 placed · 0 skipped             7        -$12.00
0x51ee…77c [cp]  No signals in 24h                0        $0.00
```

- Summary text: `{placed} placed · {skipped} skipped`, `.copy-td` body size.
  Numbers tabular; "placed" in default text color, "skipped" in
  `var(--muted)` when 0, default color when > 0. No red/green: skipped is
  not a loss, and color here must not read as P&L.
- Disclosure button below the summary, only when `skipped > 0`: plain text
  button (same family as `.btn-followed-action`, no color modifier), label
  **"Why skipped?"** with a `chevron-down` icon that rotates to `chevron-up`
  when open (label becomes "Hide reasons").
- Header cell: `Signals (24h)`; `scope="col"`.

### Expanded detail row

A `<tr class="copy-row copy-signals-detail">` with one `<td colspan>` spanning
all columns (colspan computed, including the backtest columns when toggled).
Contents, a compact list (not a nested table):

```
Skipped in last 24h (5)
 3  Structural   Trade is on a multi-outcome market (outcomeIndex 2+); the engine only models binary markets   non_binary_outcome_index   Accepted (#1390)
 2  Routine      Wallet already holds an open position in this market                                          duplicate_market_exposure  SPIKE
```

Per item: count (bold, right-aligned in a fixed 2ch+ gutter), class chip,
meaning text (wraps), raw key in `var(--muted)` mono small (operators grep
logs with it), then optional markers:

- `SPIKE` / `NEW` (from `trend`) as a small amber chip; `STEADY` shows nothing.
- `Accepted (#N)` muted chip when `accepted_ref` is set (reviewed structural
  reason, #1390). A SPIKE/NEW marker still shows alongside it.

Class chip colors (reuse tokens, no new ones; always paired with the word):

| Class | Style |
|---|---|
| Structural | `var(--warn-bg)` / `var(--warn)` |
| Circuit breaker | `var(--no-bg)` / `var(--no)` |
| Unrecognized | `var(--warn-bg)` / `var(--warn)` plus `alert-triangle` icon (aria-hidden), so it cannot be overlooked |
| Routine, Lifecycle | neutral: `var(--muted)` text on subtle background |

### 375px behavior

- Page must not scroll horizontally (816-responsive-layer rule). The table
  already lives in `.copy-table-wrap`; its overflow-x scroll stays the only
  scroll container. Signals is the second column and the summary is
  ~16 chars, so Wallet + Signals are visible without scrolling; Settled and
  Realized P&L are reached by the wrap's existing horizontal scroll.
  [Tech constraint: reusing the existing table instead of building a card
  layout — agreed approach: keep the wrap scroll; a dedicated mobile card
  layout for this table is out of scope.]
- Summary shortens at `max-width:480px` to `12 placed · 5 skipped` on two
  lines if needed (allow wrap; do not truncate numbers).
- Detail row at 375px: each reason becomes a stacked block — line 1: count,
  class chip, markers (wrapping); line 2: meaning text; line 3: raw key
  (`overflow-wrap:anywhere`). The detail `<td>` is `position:sticky;left:0`
  with `max-width:calc(100vw - 32px)` so the text is readable at viewport
  width regardless of horizontal scroll position of the wrap.
- Tap targets: "Why skipped?" button min 44px tall.

## States

| State | Rendering |
|---|---|
| Loaded, skipped > 0 | Summary + "Why skipped?" button |
| Loaded, skipped = 0, placed > 0 | `8 placed · 0 skipped`, no button |
| Loaded, no signals | `No signals in 24h` muted; no button |
| Loading | Part of the same response as the table, so the table's existing loading state covers it; no separate spinner |
| `signals_24h` null (query failed) | Cell shows `Unavailable` muted with `title="Signal counts could not be loaded"`; rest of row and table render normally. Never show `0 placed · 0 skipped` for a failure |
| Wallet unfollowed / paused | Still shown if row is shown; counts are factual for 24h |
| Many reasons (>6) | List shows first 6 by count, then `+N more reasons` text button that reveals the rest in place |
| Long wallet row count | No change; table already handles many rows |

## Interactions

- Disclosure: native `<button type="button" aria-expanded aria-controls>`
  toggling the detail row's `hidden`. Enter/Space work natively.
- No tooltips are required for comprehension (meaning is inline); the class
  word is always visible, never color-only.

## Design tokens / references

- Tokens only: `--warn`, `--warn-bg`, `--no`, `--no-bg`, `--muted`. New
  classes follow the `.mode-badge-*` pattern, e.g. `.skip-class-structural`,
  `.skip-class-breaker`, `.skip-class-unknown`, `.skip-class-neutral`.
- Wording: the vocabulary is "placed" and "skipped" (matches the Activity
  Feed's "Order placed (paper)" / "Order skipped (paper)"). Do not say
  "executed", "rejected" or "filtered".

## Accessibility notes

- Summary cell has no extra aria; the text is complete. The button's
  `aria-label`: `Show why {skipped} signals were skipped for wallet {address}`
  (and `Hide reasons for wallet {address}` when open).
- Detail row is a `role="region"` with `aria-label="Skip reasons for
  {address}"`, id referenced by `aria-controls`.
- Class chips carry text; Unrecognized icon is `aria-hidden`.
- Chip text contrast must meet 4.5:1 on both themes (verify the existing
  tokens in dark and light).
- Focus stays on the toggle button after expand/collapse and after a refresh
  re-render (the re-render must restore focus by wallet address).

## Test notes for developers (JS-logic tests, per acceptance criteria)

- Renders `placed`/`skipped` from payload; no JS lookup table of reasons
  (a test should render an invented reason key + meaning and see it verbatim).
- Null `signals_24h` renders `Unavailable`, never zeros.
- Unknown `class` value renders unknown styling.
- Open state preserved across re-render; colspan correct with backtest toggle on/off.
- Everything escaped via `_copyEscapeHtml`.

## Open questions

1. **For PM (feasibility):** is a per-wallet 24h placed count cheap on the
   positions endpoint, or should `signals_24h` be fetched lazily on first
   expand? Preferred: eager (counts visible at a glance); lazy is acceptable
   only for `reasons[]`, with the summary still eager.
2. **For PM:** confirm definition of "placed": signals with no `skip_reason`
   in the last 24h of `copy_signals` (not positions opened, not settled).
3. Should unfollowed wallets that still have positions keep the column? Spec
   assumes yes (shows factual counts, typically 0).
