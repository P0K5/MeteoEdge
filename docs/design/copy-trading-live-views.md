# Design Spec — Copy-Trading Live/Paper Dashboard Views (Epic J)

**Updated 2026-10-05** for issues #1264 and #1266 (epic #1304, live-safety
follow-ups from the 2026-10-04 incident on wallet `0x924379a7` — live
trading ran 4/4 losing trades and is now halted). Both updates are to
sections that already existed in this document; see "Pausing an opted-in
wallet" (now superseding its own prior text, #1264) and the fifth badge
state under "Followed Wallets" (#1266). This revision is design direction
only — implementation for both issues is still open.

Addendum to `docs/design/copy-trading-dashboard.md` (Epic F, shipped) and
`docs/design/copy-trading-architecture.md` (Epic J, #1161). Read the Epic F
spec first — this document only covers the delta: what changes in
**Positions & P&L**, **Followed Wallets**, and **Activity Feed** now that
Epic H (live order execution, `copy_live_positions`) and Epic I (live
settlement/reconciliation, live circuit breaker) exist and are merged.
Candidates and the rest of Epic F's layout, interactions, and states are
unchanged and not repeated here.

## Why this exists (read before designing anything new here)

Per the epic's own acceptance criteria: live and paper numbers must **never
be blended into one total**, and mistaking a paper figure for a live one is
a real trust/safety failure, not a cosmetic issue. Every design decision
below is evaluated against one question: *if an operator glances at this for
two seconds, could they mistake a paper number for a live one, or vice
versa?* If the answer isn't a confident "no," the design isn't done.

**Precedent to reuse, not reinvent** (already in
`src/dashboard/static/index.html`, verified before writing this spec):

- `.side-mode-badge` + `.side-mode-live` (green, `var(--yes-bg)`/`var(--yes)`)
  / `.side-mode-shadow` (amber, `var(--warn-bg)`/`var(--warn)`) — uppercase
  pill badges, `display:inline-flex;gap:4px`, used for the weather
  strategy's per-side live/shadow state (lines ~613-621).
- Every such badge carries an explicit `aria-label` stating the state in
  words (`"YES side: live"` / `"YES side: shadow"`) — never color-only, per
  that code's own comment (lines ~2337-2338).
- Trade-history text convention: `execution_mode === 'live'` → **"Traded
  live"**, anything else (including missing/unrecognized) → **"Traded
  (paper)"** — the conservative reading is the default; "live" is never
  claimed without an explicit, confirmed signal (lines ~3205-3247,
  `gateTooltip()` / `gateChipHTML()`).

This spec reuses the **visual pattern** (colors, pill shape, uppercase
weight, explicit aria-label, "assume paper unless proven live" default) but
**not the literal word "SHADOW"** — copy-trading's own domain vocabulary,
established consistently across Epics A–I (`copy_positions` vs.
`copy_live_positions`, "paper mode," the Epic F spec's own "Traded (paper)"
convention, the issue title itself), already says **"paper," never
"shadow."** Carrying "SHADOW" over here would introduce a second word for
the same concept inside one product. New class names, to avoid coupling to
the weather strategy's per-*side* (YES/NO) semantics, which isn't the axis
copy-trading badges vary on:

```css
.mode-badge{ /* structurally identical to .side-mode-badge */
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 8px;border-radius:var(--radius-full);
  font-size:var(--text-xs);font-weight:700;letter-spacing:.05em;
  flex-shrink:0;
}
.mode-badge-live{background:var(--yes-bg);color:var(--yes);}
.mode-badge-paper{background:var(--warn-bg);color:var(--warn);}
```

Badge text is always the word **LIVE** or **PAPER**, uppercase, never an
abbreviation or icon alone.

## Global: a tab-wide live/paper posture banner

Because `COPY_LIVE_TRADING_ENABLED` (Epic G) is a single **global** kill
switch — not per-wallet — read once per cycle in
`src/scripts/copy_signal_loop.py` and applied uniformly to every active
followed wallet, the Copy-Trading tab needs one persistent, unmissable
indicator of the platform-wide posture, visible across all four views (not
just Positions & P&L), so an operator on Followed Wallets or the Activity
Feed never loses track of whether live is even switched on:

- A banner/pill pinned near the Copy-Trading tab header (same sticky region
  as the existing dashboard's `.live-pill`, but a distinct element — that
  existing pill denotes data-connection freshness, not trading mode, and
  must not be repurposed): **"LIVE TRADING ON"** (green, `mode-badge-live`
  styling) or **"LIVE TRADING OFF — paper only"** (amber,
  `mode-badge-paper` styling, plus the literal words "paper only" so the OFF
  state reads as a statement about current behavior, not just an inert
  switch position).
- This banner is a landmark region for screen readers (`role="status"` or
  equivalent, `aria-live="polite"` if it can change without a page reload —
  it can, via the config API / `halt_live_copy_trading.py`).

## Positions & P&L

**Decision: twin panels, not a toggle.** A single view with a toggle risks
an operator misreading whichever regime happens to be selected as "the"
number, especially after a page refresh where the toggle's prior state
isn't obvious at a glance. Twin panels — both always visible, always
labeled — make blending structurally impossible rather than just
discouraged. `[Tech constraint: twin panels means two independent sets of
API calls/renders instead of one toggled dataset, and needs a call on
narrow-viewport stacking order — flagging for Tech Lead PM feasibility
confirmation before implementation; if two live API round-trips per poll is
a real cost concern, the fallback is a toggle with a hard-coded persistent
banner announcing which regime is showing, but twin panels is the design
default given the "never blended" requirement.]`

- **Layout:** two side-by-side columns on wide viewports — **Live always
  left, Paper always right**, a fixed order repeated everywhere in this
  epic so operators build one stable mental model. Stacked vertically on
  narrow viewports, Live column first. Each column has its own heading
  (`<h3>LIVE</h3>` / `<h3>PAPER</h3>`) using the `.mode-badge` styling,
  its own open-positions table, its own realized-P&L chart, and its own
  per-wallet breakdown table — the exact shape Epic F already specified,
  just not sharing a single aggregate number across the two.
- **Summary strip:** the existing "# active / # paused / aggregate P&L"
  strip splits into two qualified numbers — **"Paper aggregate P&L"** and
  **"Live aggregate P&L"** — never a single "aggregate P&L" anywhere in
  this view, including any future CSV/API export: no server-side sum across
  `copy_positions` and `copy_live_positions` should ever be computed or
  returned as one figure. `[Tech constraint: confirm with Tech Lead PM that
  the API layer never introduces a combined-total field even as a
  convenience — agreed approach: two independent response keys, always.]`
- **Backtest-comparison toggle** (Epic F's phase-7 go/no-go feature) stays
  in the **Paper** column only — the backtest was a flat-stake paper
  projection; it has no live counterpart yet and must not appear to.
- **Click-through / date range:** unchanged from Epic F, applied
  independently per column.

### States (both columns, independently)

- **Default:** populated, as above.
- **Loading:** skeleton per column — a slow live fetch must not block or
  blank the paper column, and vice versa (isolation applies to rendering,
  not just data).
- **Empty, live trading OFF (global switch off):** the Live column shows a
  **dedicated off-state**, not "$0.00" and not a plain empty table — a
  live P&L of literal zero and "live isn't running" must never look the
  same. Render it dimmed/neutral (not green, not the live accent) with the
  text **"Live trading is off"** plus a link to wherever the switch lives
  (config panel or docs), and no numeric figures at all in that column
  (not even a zero).
- **Empty, live trading ON but no live positions yet:** Live column keeps
  its live accent (green) but body text reads **"No live positions yet."**
  This is deliberately visually distinct from the OFF state above — same
  accent color as populated live data, different text, no numbers pretending
  to be a real zero-P&L reading versus an absent one.
- **Paper empty / unresolved-only:** unchanged from Epic F ("No
  copy-trading positions yet" / realized-P&L-chart-has-nothing-to-plot
  distinct empty state).
- **Error:** each column gets its own stale-data banner — a live-fetch
  failure shows the Live column's last-known-good data with its own banner;
  it must never blank the page or, worse, silently fall back to showing
  paper data in the Live column's slot.
- **Reconciliation required (Epic I balance-drift):** if the backend
  surfaces a live wallet-balance drift beyond
  `COPY_LIVE_BALANCE_DRIFT_TOLERANCE_USD` (`check_wallet_balance_drift` in
  `src/scripts/copy_live_settle.py`), the Live column shows a high-priority
  warning banner above its content: **"Live balance mismatch detected —
  manual reconciliation required"** (do not soften this language; it is a
  real-money discrepancy). `[Open question — needs Tech Lead PM
  confirmation: is a drift-status read endpoint in scope for Epic J, or is
  this a fast-follow? If out of scope for this epic's PRs, this state ships
  as a documented but not-yet-wired UI affordance, and the open question
  below tracks it explicitly rather than silently dropping it.]`

## Followed Wallets

Add a **second, independent** status badge per wallet row — paper
follow-status (existing: active/paused) and live-eligibility status (new)
are two separate axes, exactly as the issue specifies: a wallet can be
paper-followed without being live-enabled.

`[Tech constraint — RESOLVED by #1253: the previous version of this block
flagged that no per-wallet `live_enabled` column existed and specified two
fallback derivations. #1253 adds `copy_wallets_followed.live_enabled`
(default 0, no wallet becomes live as a side effect) and wires it into
`_derive_live_eligibility` — already shipped in `src/dashboard/api.py` per
derivation (a) below, now gated on the new flag as well. Kept here for
history; do not re-derive from (b) — the real column exists now.]`

`_derive_live_eligibility` (`src/dashboard/api.py`) returns
`(is_live, reason)` checked in this deterministic order — the same order
this spec's copy below assumes: `paused` → not `live_enabled` → live config
unreadable → **live stake exceeds the per-wallet cap (new, issue #1266 —
see the fifth badge state below)** → global switch off → per-wallet
exposure cap reached (transient) → eligible. The new #1266 check is placed
ahead of "global switch off" and the transient exposure check deliberately:
it is the one permanent, human-actionable blocker in this list, and it
would still block every order even once the switch is on and exposure is
clear, so it should win the reported reason regardless of what else happens
to be true at the same moment — surfacing "switch off" or "cap reached"
first would hide the actual, permanent problem behind a transient-looking
one. `[Tech constraint: confirm this ordering with Tech Lead PM before
implementation — feasibility isn't in question, this is purely about which
reason wins when more than one is simultaneously true.]`

A `paused` wallet is always PAPER regardless of every other flag, including
`live_enabled` — this is unchanged. What pausing itself *does* to the
`live_enabled` flag changed as of issue #1264: see "Pausing an opted-in
wallet" below, which supersedes this document's prior text on that point.

- **Badge — five label states, three visual treatments** (Tech Lead PM
  approved, 2026-09-30, for issue #1258's original four; **extended
  2026-10-05 with a fifth state for issue #1266**, below):
  - **`LIVE`** — `.mode-badge-live` (green) — `live_enabled` **and**
    `live_eligible` are both true (every gate passes right now).
  - **`LIVE (switch off)`** — a new variant, same badge shape, muted green
    (see Design tokens below) — `live_enabled` is true but
    `live_status_reason === "live trading is currently off"`. Reads as "this
    wallet is opted in and will go live the moment the global switch flips
    on," distinct from a wallet that was never opted in at all.
  - **`LIVE (cap reached)`** — same muted-green variant as above, required
    by the acceptance criteria specifically because `_derive_live_eligibility`
    already returns this as its own distinct reason
    (`"this wallet's live exposure limit is currently reached"`) once
    `live_enabled` exists as a true operator toggle — an opted-in wallet
    temporarily throttled by its own cap must not collapse to plain `PAPER`,
    which would look identical to a wallet the operator never opted in. This
    state is **transient**: it clears on its own the moment an open live
    position settles and frees up exposure, with no operator action
    required.
  - **`LIVE (stake exceeds cap)`** (new, issue #1266) — a **third, distinct
    visual treatment**, `.mode-badge-live-blocked` (muted red — see Design
    tokens below), for `live_status_reason === "this wallet's live stake
    exceeds its live exposure cap"`: the wallet's *resolved live stake*
    (not its current exposure) is itself larger than
    `COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD`, so **no order can ever be
    placed, at any exposure level, until the operator lowers the live
    stake or raises the cap.** This is deliberately a different color from
    `LIVE (switch off)` / `LIVE (cap reached)`, not a third muted-green
    variant, because it is a categorically different kind of "not eligible
    right now": the two muted-green states are self-resolving (wait for the
    switch, wait for a position to settle) and need no operator action;
    this one is permanent until a human changes a number — exactly the
    distinction #1266's issue text draws between this state and the
    existing cap-reached reason. Reusing the red (`--no`) family this same
    view already uses for `.btn-followed-unfollow`, and that the dashboard
    already uses for blocked-gate styling elsewhere (`.gate-entry_guard`),
    keeps "red means stop, this needs you" consistent with vocabulary this
    operator already reads, rather than inventing a new color meaning for
    the same concept.
  - **`PAPER`** — `.mode-badge-paper` (amber) — every other case:
    `live_enabled` is false, **or** the wallet is `paused` (paused always
    wins and shows plain `PAPER`, never a `LIVE (...)` variant, even if
    `live_enabled` is true underneath — same rule as before), **or** the
    live-status derivation itself couldn't be computed (conservative
    fallback, unchanged from the existing rule below).
  - Label text is driven directly off `live_status_reason` on both the
    muted-green branch and the new muted-red branch — never a
    hand-maintained copy of the backend's reason strings — so a future new
    reason string safely falls through to `PAPER` (the conservative
    direction) instead of silently mis-rendering. Do not invent further
    reason-specific labels beyond the two muted-green variants and the one
    muted-red variant specified here.
  - Placed next to the existing paper active/paused status in the row (not
    replacing it — e.g. "Active" (paper) + "LIVE" (live-badge), or "Active"
    (paper) + "PAPER" (live-badge)).
- **aria-label** spells out the reason, not just the state, e.g. `"Live
  status: paper only — live trading is currently off"`, `"Live status: live,
  opted in but the global switch is off"`, `"Live status: live, opted in but
  this wallet's live exposure limit is currently reached"`, `"Live status:
  live opt-in blocked — this wallet's live stake exceeds its live exposure
  cap. No live orders can be placed until the stake is lowered or the cap is
  raised."` (new, #1266), or `"Live status: live — eligible for live
  execution"`. Never rely on the badge color alone (same accessibility rule
  as the Epic F instability badge).
- **Table-level banner vs. per-row noise:** when the global switch is off,
  every row would otherwise show an identical PAPER badge with an identical
  tooltip — noisy. Add one banner above the table: **"Live trading is
  currently off — all wallets are paper-only."** The per-row badges stay
  present regardless (a screenshot of a single row must still be
  unambiguous on its own — never rely on surrounding page context for a
  safety-relevant signal). This banner text is unaffected by the new
  `LIVE (switch off)` badge state below it — the banner describes current
  execution ("nothing is trading live right now"), the badge describes
  standing opt-in intent; the two are complementary, not contradictory, and
  both are always literally true at once when the switch is off.
- **Summary strip:** extend the existing "# active / # paused / aggregate
  P&L" strip with **# opted into live** / **# live-eligible** /
  **# paper-only**, in that order (opted-in is the coarser, operator-set
  count; live-eligible is the narrower, currently-executing subset of it),
  and split "aggregate P&L" the same way as Positions & P&L (paper aggregate
  / live aggregate, never combined). The **# opted into live** pill is
  server-computed (Tech Lead PM decision, 2026-09-30): #1259 adds
  `live_opted_in_count` to `CopyFollowedWalletsOut` alongside the existing
  `live_eligible_count`/`paper_only_count`, so every pill on this strip stays
  consistently server-sourced — no client-computed count.

### States

Unchanged from Epic F (default/loading/empty/row-level action error) with
one addition: the live badge and table-level banner degrade to "PAPER" /
"off" language whenever the live-status derivation can't be computed (e.g.
API error fetching live config) — never guess LIVE when the source of truth
is unavailable, same conservative-default rule as the `execution_mode`
precedent.

### Per-wallet live opt-in control (issues #1258, #1259)

This turns the badge above from a pure derived indicator into the visible
result of an operator action: opt a paper-proven wallet into real-money
execution while every other followed wallet keeps paper-trading the study,
per #1253's independent `live_enabled` flag (AND-ed with the global
`COPY_LIVE_TRADING_ENABLED` switch and every existing live gate — see the
deterministic order above). This is a real-money action living one click
away from Pause / Edit stake / Unfollow, so placement, states, and copy all
carry the safety burden the issue calls out. #1259 adds a second dimension
to the same action — a live stake independent of the wallet's paper stake —
because `stake_per_trade` currently sizes both paths, and without a separate
live stake, promoting a wallet forces its real-money size to equal its paper
size, with no way to size down live without also reshaping the ongoing
paper study's comparability. The enable-live flow below folds both concerns
into one operator-facing sequence.

**Placement.** A new control joins the row's action group, in this order:
**Pause/Resume → Edit stake → Go live / Revert to paper → Unfollow.**
Rationale: Pause/Resume and Edit stake are frequent, low-consequence,
reversible-in-place actions and stay first, together, as today. Go live and
Unfollow are the row's two consequential actions — one adds real-money risk,
the other removes the wallet — and are grouped last, in that order, so an
operator reaching for the frequent controls doesn't land next to either by
accident. No new divider or layout primitive is introduced between them;
color alone already separates them (see Design tokens) and a divider would
be a new primitive this spec doesn't need.

- **Mobile width:** `.followed-actions-cell` already wraps via
  `flex-wrap:wrap;gap:6px` with no dedicated breakpoint for 3 buttons today;
  the same rule extends to 4 with no new CSS — the control simply wraps to
  its own line at narrow widths like any other action button. DOM order (and
  therefore wrap order and screen-reader order) is unchanged from the
  placement above at every width; no icon-only collapse is introduced (every
  other action button in this row keeps its icon **and** text label at all
  widths, and this control does too).

**Control states.**

| State | Trigger | Rendering |
|---|---|---|
| Opt-in available | `live_enabled=false`, `status='active'` | `.btn-followed-action.btn-followed-golive`, icon `zap`, label **"Go live"**, `aria-label="Go live for {address}"` |
| Opted-in (offer to demote) | `live_enabled=true`, `status='active'` | `.btn-followed-action` (neutral, no color modifier — see rationale below), icon `zap-off`, label **"Revert to paper"**, `aria-label="Revert {address} to paper-only"` |
| Disabled — paused | `status='paused'` (regardless of `live_enabled`) | Same button as whichever state above `live_enabled` implies, native `disabled` attribute (existing `.btn-followed-action:disabled{opacity:.5}` styling, no new CSS), `title="Live opt-in is unavailable while this wallet is paused — resume it first."`, and the same sentence duplicated into `aria-label` (tooltips are not reliably exposed to screen readers, so the reason must live in the accessible name too, not just `title`) |
| In-flight / pending | Request in progress | Existing `.loading` + spinner + `aria-busy="true"` convention (`_followedSubmitAction`), identical to Pause/Resume/Unfollow — no new pending-state pattern needed |

Button color rationale: **"Go live"** gets a green modifier
(`.btn-followed-golive`, new — see Design tokens) because it is the
risk-*increasing* direction, mirroring why `.btn-followed-unfollow` alone
among the original three action buttons carries a color (red) — a
consequential action should look different from the row's routine controls.
**"Revert to paper"** stays visually neutral, like `Resume`, because the
risk-*reducing* direction never needs a visual warning, only the
risk-increasing one does.

**Disambiguating "Edit stake" now that two stakes exist.** The existing
`.btn-followed-edit-stake` button's visible label ("Edit stake") and icon
stay unchanged — it sits directly above the row's now-two-line stake display
(below), so it reads unambiguously as "edit the paper stake" from
proximity/context alone, per Epic F's own layout. Its `aria-label` changes
from `"Edit stake per trade for {address}"` to `"Edit paper stake per trade
for {address}"`, since a screen-reader user tabbing directly to the button
loses that visual proximity cue and two same-named "stake" controls would
otherwise be genuinely ambiguous. This is the only change to the existing
paper-stake edit flow — editing paper stake never writes to, reads from, or
resets a wallet's live stake override, and vice versa; the two edit paths
are fully independent, consistent with the live/paper axis-independence
rule this whole document is built on.

### Enabling live: stake + confirmation (issues #1258, #1259)

Enabling live now does two things in one operator-facing flow: choosing the
live stake, then confirming the real-money action. `[Tech constraint: this
dashboard has no modal framework — `src/dashboard/static/index.html` is a
single file whose only dialog primitives are `window.confirm` (2 uses,
including Unfollow) and `window.prompt` (the pause-reason flow, ~L4751).
This spec deliberately does **not** introduce a modal for this. It composes
the two existing primitives — one `window.prompt` to capture the live stake,
followed by one `window.confirm` to commit — because (a) capturing a
free-text number requires a text input, which only `prompt` offers among the
two primitives `confirm` and `prompt`; (b) two sequential native dialogs for
a rare, high-stakes action is not excessive friction — it is the same shape
of friction `Pause` already imposes today (prompt for a mandatory reason,
before the action completes) for a lower-stakes action; and (c) reusing
existing primitives means #1254 ships with zero new UI infrastructure. If
this reads as too many dialogs in practice once built, the fallback is
collapsing to a single `prompt` whose message asks for the stake with the
cap/wallet/reason context folded into the prompt text itself, and treating
that single OK as both the stake choice and the commit — but that removes
the deliberate two-step reconsideration point before a real-money action,
so this spec's default is the two-dialog sequence above. Flagging for
Tech Lead PM sign-off on this trade-off, not feasibility — the two-primitive
approach is confirmed feasible.]`

**Step 1 — live stake, via `window.prompt` (verbatim):**

```js
// w.live_stake_per_trade is the API's *resolved* value (#1259): the
// existing override if this wallet has one, otherwise its paper stake.
// Pre-filling from this, not from w.stake_per_trade directly, is what
// makes a disable/re-enable cycle preserve the operator's prior sizing.
const rawStake = window.prompt(
  `Set a live stake per trade for ${address}.\n\n` +
  `Leave this blank and press OK to keep the live stake linked to this ` +
  `wallet's paper stake (currently $${w.stake_per_trade.toFixed(2)} per ` +
  `trade) — it will automatically track future paper-stake edits.\n\n` +
  `Enter a dollar amount to set a live stake that stays fixed at that ` +
  `amount even if the paper stake later changes.\n\n` +
  `Live per-wallet exposure cap: $${liveCapUsd.toFixed(2)}.`,
  w.live_stake_per_trade.toFixed(2)
);
if (rawStake === null) return; // operator cancelled the whole action, identical to Unfollow's short-circuit
```

**Pre-filled with the wallet's current *resolved* live stake — its existing
override if it has one, otherwise its paper stake (corrected 2026-09-30,
per #1258's corrected acceptance criteria; the original wording said
"pre-filled with the paper stake," which was wrong).** For a wallet that has
never had an override, the two values are identical, so first-time
promotion is unaffected — this only changes behavior on a *second* enable.
The correction exists because pre-filling from the paper stake makes a
disable/re-enable cycle hazardous: set live = $2 → Revert to paper → Go live
again → the prompt would have offered $5 (the paper stake) → accepting the
default, the natural action, silently discards the $2 override and raises
real-money size 2.5×. The confirm dialog in Step 2 does name the resolved
figure, so the old behavior was disclosed rather than silent — but the
pre-fill itself was steering toward the larger number, which is the wrong
default for the one control in this row that spends real money. Accepting
the pre-filled value as-is is treated as an **explicit override** equal to
whatever value was shown (`live_stake_is_override = true`) — the prompt's
instructional text is explicit that *clearing the field* (not accepting the
default) is how an operator chooses "inherit." This is the one unambiguous
way to express two distinct states (explicit-value-equal-to-paper vs.
inherit-from-paper) through a single native text field that has no separate
"reset to default" affordance. Cancelling (`null`) aborts the entire
enable-live action with no API call — same convention as every other
cancellable action in this row.

**Client-side validation (before any dialog or API call proceeds):**

```js
const trimmed = rawStake.trim();
let liveStake = null;            // null → inherit paper stake
let liveStakeOverride = false;
if (trimmed !== '') {
  const parsed = parseFloat(trimmed);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    _followedShowBannerError(`Could not set a live stake for ${_copyTruncateAddress(address)} — enter a live stake greater than $0, or clear the field entirely to inherit the paper stake.`);
    return;
  }
  if (parsed > liveCapUsd) {
    _followedShowBannerError(`Could not set a live stake for ${_copyTruncateAddress(address)} — $${parsed.toFixed(2)} exceeds this wallet's live exposure cap of $${liveCapUsd.toFixed(2)}. Enter an amount at or under the cap, or clear the field entirely to inherit the paper stake.`);
    return;
  }
  liveStake = parsed;
  liveStakeOverride = true;
}
```

Covers all three required validation states — non-finite, non-positive, and
above-cap — each aborting the whole flow with no confirmation dialog and no
API call, via the same shared error banner and `"Could not {verb} {address}
— {detail}"` format used everywhere else in this view. There is no inline
retry for a native `prompt` — the operator re-clicks "Go live" to try again,
same recovery pattern as every other validation failure in this row (e.g.
Edit stake's `<= 0` check).

**Step 2 — final confirmation, via `window.confirm` (verbatim):**

```js
const resolvedStakeText = liveStakeOverride
  ? `$${liveStake.toFixed(2)} per trade`
  : `$${w.stake_per_trade.toFixed(2)} per trade (this inherits this wallet's paper stake and will change automatically if the paper stake is edited later)`;
const confirmed = window.confirm(
  `Go live for ${address}?\n\n` +
  `Every future signal from this wallet will place REAL trades of ${resolvedStakeText}, ` +
  `up to a live exposure cap of $${liveCapUsd.toFixed(2)} for this wallet.\n\n` +
  `Paper trading for this wallet continues unaffected, at its own stake of ` +
  `$${w.stake_per_trade.toFixed(2)} per trade.`
);
if (!confirmed) return;
```

Names the wallet (full address, matching Unfollow's own precedent of using
the untruncated address in this one dialog), the **resolved live stake**
(never the paper stake, even when they happen to be numerically equal — the
wording always distinguishes "$X per trade" from "$X per trade (inherits
...)"), and the live per-wallet exposure cap, per the acceptance criteria.
The closing sentence restates the paper stake by its own label and value so
the two numbers are never presented as one — this is the same
"paper-vs-live must be unmistakable" requirement the rest of this document
applies to every dollar figure, now applied to a dialog instead of a table
cell. Cancelling makes no API call, identical to Unfollow's
`if (!confirmed) return;` short-circuit.

`live_cap_usd` is confirmed shipping on `CopyFollowedWalletsOut` via #1259
(Tech Lead PM decision, 2026-09-30) — `copy_trading_followed_wallets`
already reads it from `get_live_config` into a local variable, so this spec
treats it as a settled dependency, not provisional copy.

**API call sequencing.** Two separate endpoints exist (#1253's
`POST .../live`, #1259's `PATCH .../live-stake`) — there is no single
atomic call. Sequence:
1. If the resolved stake differs from the wallet's current stored state
   (`liveStakeOverride !== w.live_stake_is_override`, or the override value
   itself changed), call `PATCH .../live-stake` first with
   `{"stake": liveStakeOverride ? liveStake : null}`. Skip this call
   entirely when nothing changed (e.g. re-enabling a wallet whose stake
   override was already set correctly from a prior enable/disable cycle) —
   avoids a redundant write.
2. Only if step 1 succeeds (or was skipped), call `POST .../live` with
   `{"enabled": true}`.
3. If step 1 fails, stop — show its error (below) and **do not** call the
   enable endpoint. The wallet is left exactly as it was before (paper-only,
   whatever its previous stake override was) — a safe partial state, since
   `live_enabled` was never touched.
4. If step 1 succeeds but step 2 fails, the wallet now has a live-stake
   override saved but `live_enabled` is still `false` — also safe (no real
   trading is possible while `live_enabled=false`), but show the error
   specifically as an enable failure, not a stake failure (below), since
   from the operator's perspective they asked to "go live" and that part is
   what failed; the stake is simply already correctly set for next time.

**Disabling live requires no confirmation.** Clicking "Revert to paper"
fires the `POST .../live` request with `{"enabled": false}` immediately, no
`window.prompt` or `window.confirm()` — mirroring why `Resume` (also
risk-reducing) never prompts today, only `Pause` and `Unfollow`
(risk/consequence-bearing in their own ways) do. Reverting to paper never
touches the wallet's live-stake override — combined with Step 1's pre-fill
now sourcing from the resolved live stake (corrected above), a disable/
re-enable cycle genuinely preserves the operator's prior sizing: the prompt
on re-enable shows exactly the override that was in effect before, not the
(potentially larger) paper stake. Adjusting the size of an already-live
wallet without a full disable/re-enable round-trip is still not possible
from this row alone — see #1262 (Open Questions, below) for the dedicated
in-place control that covers that case.

**Optimistic UI — deliberately asymmetric, unlike every other action in this
row.** Pause, Resume, and Unfollow all apply an optimistic DOM change before
the request resolves (see `_followedSubmitAction`'s `optimisticApply`). This
control does **not** follow that pattern symmetrically:

- **Enabling:** no optimistic badge change. The button shows its normal
  `.loading` spinner state; the badge stays exactly as it was (`PAPER`) until
  the request succeeds and the row re-fetches. This is the one deliberate
  exception to the shared runner's optimistic-apply convention, required by
  the acceptance criteria's explicit rule: **the badge must never show LIVE
  optimistically.** Showing `PAPER` a moment too long is the safe-direction
  error (understates status); showing `LIVE` a moment too early, for a
  request that might still fail, is the unsafe-direction error this control
  exists specifically to prevent.
- **Disabling:** optimistic apply *is* used, flipping the badge to `PAPER`
  immediately, rolled back to its prior state on failure — safe direction,
  consistent with Pause's existing optimistic badge flip.

**Error-state copy.** Uses the existing shared `_followedShowBannerError`
banner (`#copy-trading-error-banner`), matching the exact
`"Could not {verb} {truncated address} — {detail}"` format already used by
Pause/Resume/Unfollow/Edit stake:
- `Could not set a live stake for {truncated address} — {detail}` (step 1
  of API sequencing above failing; also covers the three client-side
  validation failures shown before any request is made, same wording
  pattern)
- `Could not enable live trading for {truncated address} — {detail}` (step 2
  failing, whether or not step 1 ran)
- `Could not disable live trading for {truncated address} — {detail}`

On failure the control's `rollback` restores its prior label/icon/color and
`disabled` state exactly as `_followedSubmitAction` already does for the
other three actions — no new rollback mechanism. The badge and stake display
(below) both re-derive from the row's last-known-good data on any failure —
never from anything typed into the now-dismissed `prompt`/`confirm` dialogs.

**Success announcement.** A new per-row status region,
`<p class="followed-live-msg" id="followed-live-msg-${safeId}"
role="status" aria-live="polite"></p>`, structurally identical to the
existing `copy-follow-msg` element used by the Follow flow (the issue's own
cited precedent). Unlike Follow's element (which today carries error text
only), this one also carries a success confirmation on enable/disable — a
deliberate, small excess over the letter of "existing conventions," because
this is the one action in the row with real-money consequences and warrants
an explicit spoken confirmation, not just a silent badge repaint a
screen-reader user would otherwise never hear:
- `Live trading enabled for {truncated address}.`
- `Live trading reverted to paper for {truncated address}.`

**Pausing an opted-in wallet (issue #1264 — supersedes this document's
prior text on this point).** `live_enabled` **is** cleared on any
transition to `paused` — manual or automatic, identically — per the Tech
Lead PM's decision (option (a) of #1264's acceptance criteria, 2026-10-05).
Resuming a previously-live wallet now leaves it paper-only until it is
deliberately re-promoted through the full enable-live confirmation above;
there is no fast path back to live via Resume alone. This document's prior
text specified the opposite (keep the flag, no re-consent on resume) — that
choice is retired, not merely amended, following the live incident on
wallet `0x924379a7` (2026-10-04, 4/4 live trades lost, live trading halted)
that motivated #1264. This decision itself is settled and not open for
re-litigation here; what follows is the UX around it.

Rationale for the UX below: clearing the flag silently is exactly the kind
of "discover it later" trust failure this whole document exists to prevent
(see "Why this exists," top of file). An operator must learn *that* their
live opt-in was revoked and *why* — at the moment it happens where
possible, and durably afterward if they weren't looking at the time — never
by inferring it from a button that's quietly relabeled itself. Three
surfaces carry this, layered the same way the rest of this document layers
redundant signals (badge + text + border, elsewhere) rather than relying on
one:

1. **In the moment (ships without any schema change).** The pause flow
   already has the wallet's current `live_enabled` value client-side — it's
   on the row being acted on — before the pause request is even sent, so
   this needs no new API field:
   - If `live_enabled` is true at the moment "Pause" is clicked, the
     existing mandatory-reason `window.prompt` gains a prepended warning
     sentence, disclosing the consequence *before* the operator commits,
     not just after:
     ```js
     const liveWarning = w.live_enabled
       ? `\n\nThis wallet is currently opted into live trading. Pausing it ` +
         `will revoke that opt-in — resuming will restore paper-following ` +
         `only, and live trading will need to be re-enabled separately via ` +
         `"Go live."\n`
       : '';
     const reason = window.prompt(
       `Reason for pausing ${_copyTruncateAddress(address)} (required):${liveWarning}`,
       ''
     );
     ```
     This stays a single dialog, not a second `window.confirm` — Pause
     keeps the same one-dialog friction level it has today (it is still
     the row's frequent, routine control per the "Placement" rationale
     above), with the live consequence folded into the reason prompt it
     already shows rather than new dialog machinery for a
     risk-*reducing* action. (Only "Go live," the risk-*increasing*
     direction, gets the two-dialog treatment — see "Button color
     rationale" above; this stays consistent with that asymmetry.)
   - On success, extend the per-row `followed-live-msg` status region (the
     same element #1254 introduced for Go-live/Revert success
     announcements, `role="status" aria-live="polite"`) to also announce
     pause outcomes when a live opt-in was just revoked:
     `"{truncated address} paused. Live trading opt-in was revoked — resume
     will restore paper-following only."` A pause that didn't touch a live
     opt-in (the common case — most wallets are paper-only) announces
     nothing new, matching today's silent-on-success behavior; this is
     additive, not a change to the existing common path.
   - **Auto-pause has no equivalent moment** — `copy_wallet_health.py` runs
     with no operator present to warn beforehand or hand a success toast
     to. This is an unavoidable asymmetry in *which* surface carries the
     notice (manual pause gets a pre-action warning + a toast; auto-pause
     gets only the two durable surfaces below), not an asymmetry in
     *outcome* — the acceptance criteria's "auto-pause and manual pause
     must behave identically" is about the end state (`live_enabled`
     cleared either way), which this preserves exactly.

2. **Durable record — the Activity Feed.** The existing synthetic
   `wallet_paused` event (sourced from `copy_wallets_followed.paused_at` —
   the issue's own cited precedent) is the natural home for this, rather
   than inventing a new event type: when the pause that produced a given
   `wallet_paused` event also cleared `live_enabled`, append a sentence to
   its existing text. Mode stays `"paper"`, unchanged — see the existing,
   deliberate rationale in `CopyActivityEventOut`'s docstring for why a
   pause event is always `mode="paper"`; this spec isn't asking to
   relitigate that.
   - Unchanged (no live opt-in involved): `"Wallet auto-paused —
     {paused_reason}"`.
   - New: `"Wallet auto-paused — {paused_reason}. Live trading opt-in was
     revoked."`
   - Because the event stays `mode="paper"`, an operator filtered to
     Mode=Live would miss it entirely — given the real-money relevance,
     this one `wallet_paused` row additionally gets the small warning icon
     the Activity Feed spec already uses for "live rejections/skips" (see
     Activity Feed, below), the same "real capital implications a paper
     [event] doesn't" rationale applying here. It is not reclassified to
     `mode="live"` — it is still fundamentally a pause, which is correctly
     paper — just flagged, exactly as a live-relevant paper-mode row is
     flagged elsewhere in this same view.
   `[Tech constraint: the synthetic `wallet_paused` event is derived purely
   from the wallet's CURRENT `paused_at`/`status` row at query time — there
   is no stored history of what `live_enabled` was a moment *before* that
   particular pause, and by the time this renders, the flag has already
   been cleared. Rendering this correctly needs one new persisted fact set
   at the same `update_followed_wallet_status` chokepoint that clears the
   flag (e.g. a boolean recorded alongside `paused_at`/`paused_reason`) —
   flagging for Tech Lead PM feasibility confirmation; this is a schema
   question, not a UX one, and this spec is not prescribing the column.]`

3. **The row itself, after the fact.** No new badge state is needed here
   (the badge already correctly shows plain `PAPER` for any paused wallet,
   per the unchanged first-check rule above) and no new persistent
   indicator is added to the row either — the existing "Go live" / "Revert
   to paper" control (above) already re-derives purely from `live_enabled`,
   so the moment this wallet's row re-fetches after pausing, the control
   itself reads **"Go live"** again (not "Revert to paper"), which is
   itself the correct, truthful signal: there is nothing live left to
   revert. An operator who previously saw "Revert to paper" on this row and
   now sees "Go live" has, without any extra UI, the same information a
   dedicated indicator would have given them — consistent with this
   control already being specified as "a pure reflection of `live_enabled`"
   and avoiding a sixth badge-like element for one fact three other signals
   already carry.

**The Resume affordance itself.** Per #1264's explicit ask — wording so an
operator does not resume expecting live to come back — two existing strings
change, both unconditionally (no new per-wallet field needed: under
decision (a), *every* paused wallet now has `live_enabled=false`, so these
are static corrections, not conditional logic):

- **Resume button** (`_followedPauseResumeBtnHtml`) — visible label stays
  **"Resume"** (no change — the row's other action buttons keep icon+label
  at every width per the existing mobile-width rule, and Resume isn't
  becoming a two-state button). Its `title` and `aria-label` gain the
  clarification, duplicated into both per this document's existing
  tooltip-isn't-enough-for-screen-readers rule:
  ```
  title="Resuming restores paper-following only. Live trading opt-in is cleared whenever a wallet is paused and must be re-enabled separately via Go live."
  aria-label="Resume copying {address}. Resuming restores paper-following only — live trading opt-in is cleared whenever a wallet is paused and must be re-enabled separately."
  ```
- **The Go-live/Revert control's disabled-while-paused state**
  (`_followedGoLiveButtonHtml`) currently reads `"Live opt-in is
  unavailable while this wallet is paused — resume it first."` — this is
  now **actively wrong**: it implies resuming is sufficient to restore live
  eligibility, which is no longer true. Corrected text, same `title` +
  `aria-label` duplication:
  ```
  "Live opt-in is unavailable while this wallet is paused. Pausing clears any prior live opt-in — resume the wallet, then use \"Go live\" again to re-enable live trading."
  ```

### Stake display — two numbers per wallet, never blended (issue #1259)

Once a wallet can have a live stake independent of its paper stake, the
existing single-value stake cell (`_followedStakeDisplayHtml`) is no longer
sufficient — this document's own standing rule ("live and paper figures are
never blended") now applies to per-trade stake sizing, not just P&L.

- **Wallet never opted into live (`live_enabled=false`):** the stake cell is
  **unchanged** from today — one value, no "Paper:" qualifier, no new
  markup. Adding a second line here for the common case (most wallets stay
  paper-only) would be noise with nothing to disambiguate.
- **Wallet opted into live (`live_enabled=true`), any badge state:** the
  cell shows two explicitly labeled lines, using the same "the word LIVE or
  PAPER is always adjacent to any dollar figure" rule this document already
  applies everywhere else:
  ```html
  <div class="followed-stake-value">Paper: $5.00</div>
  <div class="followed-stake-value followed-stake-live">Live: $2.00</div>
  ```
  When the live stake is inherited rather than overridden
  (`live_stake_is_override === false`), append a muted inline note reusing
  the existing `.followed-paused-reason` text treatment (small, `--muted`
  color) rather than inventing new typography:
  ```html
  <div class="followed-stake-value followed-stake-live">Live: $5.00 <span class="followed-paused-reason" style="display:inline">(inherits paper)</span></div>
  ```
  This stays visible **regardless of current badge state** — including when
  the badge shows plain `PAPER` because the wallet is paused — because the
  live-stake override is an independent, standing fact about the wallet
  (#1259's own axis-independence framing) that shouldn't disappear just
  because execution is temporarily inactive; hiding it on pause would force
  the operator to remember it or resume the wallet just to check it.
- `.followed-stake-live` (new): `color:var(--yes);` — same green used
  everywhere else for the live figure, no new token.
- The "Edit stake" button only ever edits the paper line (see
  disambiguation above); there is currently no standalone control to edit an
  already-opted-in wallet's live stake without a full disable/re-enable
  cycle — see the new Open Questions entry on this below.

**Issue #1266 addition — stake exceeds cap.** When a wallet's resolved live
stake is greater than `live_cap_usd` (the `LIVE (stake exceeds cap)` badge
state above), the existing `Live: $X.XX` line itself carries the
explanation, rather than adding a new line to an already-dense cell:

```html
<div class="followed-stake-value followed-stake-live followed-stake-live-blocked">
  Live: $12.00 <span class="followed-paused-reason" style="display:inline">(exceeds the $10.00 cap — no live orders will place until the stake is lowered or the cap is raised)</span>
</div>
```

- `.followed-stake-live-blocked` overrides the figure's color from `--yes`
  to `--no` (Design tokens below) — the dollar figure itself turns red, not
  just the badge up in the status cell, so the one number that's actually
  the problem is flagged at its own location in the row.
- The parenthetical note reuses `.followed-paused-reason`'s existing
  small/muted treatment — the same component already used for "(inherits
  paper)" two lines above it and for the paper pause-reason text — no new
  typography.
- This note is **persistent**, not a hover-only tooltip: given this is the
  exact silent-failure trap #1266 exists to close, the explanation needs to
  survive a screenshot or a glance, not require the operator to find and
  hover the right element.

**Header pill.** Covered above (Summary strip bullet in the Followed Wallets
badge section) — **# opted into live**, positioned before **# live-eligible**
in the strip, server-computed via `live_opted_in_count` (#1259).

## Activity Feed

Every event row gains a required **mode indicator** — LIVE or PAPER — using
the same `.mode-badge` component, plus explicit event text (never rely on
the badge alone; the badge and the text must independently make the mode
clear). Default to PAPER when a mode can't be determined for an event,
mirroring the `execution_mode` fallback rule verbatim.

**Paper events (Epic F, relabeled for clarity now that live exists
alongside them):**
- "Signal detected"
- "Order placed (paper)"
- "Order skipped (paper) — `{skip_reason}`"
- "Wallet auto-paused — `{paused_reason}`" — gains a conditional appended
  sentence, `"Live trading opt-in was revoked."`, plus the same small
  warning icon the live-rejection/skip rows use below (issue #1264) when
  this particular pause also cleared the wallet's `live_enabled` flag. Full
  rationale, the exact two text variants, and why this stays `mode="paper"`
  rather than becoming a live event are in "Pausing an opted-in wallet"
  under Followed Wallets, above — not repeated here.

**Live events (new, from `copy_live_positions` / live gate reasons in
`copy_signal_loop.py` / `copy_risk_manager.py`):**
- "Live order pending"
- "Live order filled"
- "Live order partially filled"
- "Live order rejected — `{rejected_reason}`"
- "Live order skipped — `{live_gate_reason}`" (covers both the Epic G
  startup sanity-check and the Epic I live circuit breaker —
  `live_circuit_breaker_daily_loss` / `live_circuit_breaker_drawdown` —
  surfaced as plain-language reason text, not the raw constant)
- "Live position settled — `{settled_pnl_usd}`"
- "Live circuit breaker tripped — `{reason}`"
- "Live trading halted" / "Live trading resumed" (global switch flips, via
  the config API or `halt_live_copy_trading.py`)
- "Live balance mismatch detected — reconciliation required" (Epic I
  drift check) — a distinct, high-visibility event type, never folded into
  a generic "error" row, matching the language `copy_live_settle.py` itself
  uses internally. `[Open question — same one as Positions & P&L above:
  needs Tech Lead PM confirmation that a drift event is actually emitted
  somewhere queryable for this epic's scope.]`

**Visual treatment:** a colored left-border accent (green/live, amber/paper)
on each row, in addition to the badge and text — three redundant signals
(border, badge, text), because this is exactly the kind of risk-relevant
information the Epic F accessibility notes already insist can't rely on
color alone. Live rejections/skips additionally get a small warning icon
(not a separate panel — keep "why didn't this get copied" in one place per
Epic F's own purpose statement) since a live rejection has real capital
implications a paper skip doesn't — the same icon, and the same rationale,
now also applies to a `wallet_paused` row that revoked a live opt-in (issue
#1264), even though that row stays `mode="paper"` overall.

**Filters:** add a **Mode** filter (Live / Paper / All) alongside the
existing wallet and event-type filters.

### States

Unchanged from Epic F (default/loading/empty/auto-refresh-paused) plus:
- **Empty with Mode=Live selected:** distinguish "live trading has never
  been switched on" ("Live trading hasn't been turned on yet") from "live
  is on, but nothing happened in this window" ("No live activity in this
  range") — the same off-vs-on-but-empty distinction used throughout this
  spec, for the same reason: "No live activity" must never read as
  reassurance if live isn't even running.

## Design tokens / references

- Reuses `var(--yes)` / `var(--yes-bg)` (live/green) and `var(--warn)` /
  `var(--warn-bg)` (paper/amber) exactly as the weather strategy's
  `.side-mode-badge` already does — no new color tokens.
- New class names (`.mode-badge`, `.mode-badge-live`, `.mode-badge-paper`)
  are structurally identical to `.side-mode-badge`/`.side-mode-live`/
  `.side-mode-shadow` but kept distinct because they vary on a different
  axis (live/paper execution regime) than the weather strategy's per-side
  (YES/NO) live/shadow axis — do not literally alias the two, to avoid a
  future weather-strategy CSS change silently altering copy-trading's
  badges or vice versa.
- No new typography, spacing, or layout primitives beyond what Epic F
  already established.
- **Issue #1258 addition — muted-green "opted-in but not currently live"
  badge variant:** reuses `var(--yes-dim)` (already defined alongside
  `--yes`/`--yes-bg` in the existing palette, used today for `.btn-toggle
  .enable:hover` and `.btn-emos.btn-promote`) as the background, paired with
  `var(--yes)` text — no new color token. New class, structurally identical
  to `.mode-badge-live`/`.mode-badge-paper`:
  ```css
  .mode-badge-live-pending{background:var(--yes-dim);color:var(--yes);}
  ```
  Used for both the `LIVE (switch off)` and `LIVE (cap reached)` label
  states — one visual treatment, two possible texts, per the "two visual
  treatments" rule above.
- **Issue #1258 addition — Go-live button color:** reuses `var(--yes-bg)` /
  `var(--yes)` / `var(--yes-dim)` exactly as `.btn-followed-unfollow`
  already reuses `var(--no-bg)`/`var(--no)`/`var(--no-dim)` for the row's
  other consequential action — no new color token, same structural pattern,
  opposite polarity:
  ```css
  .btn-followed-golive{border-color:var(--yes-dim);background:var(--yes-bg);color:var(--yes);}
  .btn-followed-golive:hover:not(:disabled){background:var(--yes-dim);}
  ```
  "Revert to paper" uses the existing plain `.btn-followed-action` with no
  color modifier (same as `Resume`) — no new class needed for it.
- **Issue #1266 addition — "stake exceeds cap" badge + stake-figure
  color:** reuses `var(--no-bg)`/`var(--no)` exactly as
  `.btn-followed-unfollow` already does for this row's other
  consequential/blocking signal — no new color token:
  ```css
  .mode-badge-live-blocked{background:var(--no-bg);color:var(--no);}
  .followed-stake-live-blocked{color:var(--no);}
  ```
  `.followed-stake-live-blocked` is applied alongside (not instead of)
  `.followed-stake-live` on the same element — the later rule wins on
  `color`, the same modifier-class composition already used elsewhere in
  this file's CSS (e.g. `.btn-cfg.btn-save`).

## Accessibility notes

- Every LIVE/PAPER badge carries a full-sentence `aria-label`, not just the
  two-word visible text — never color-only, per Epic F's own precedent.
- Live is always ordered/positioned first (left column, first in stacking,
  first in reading order) everywhere in this epic, consistently, so the
  mental model ("live always comes first") stays stable across views.
- The global live/paper posture banner is a landmark/status region,
  announced on change (`aria-live="polite"`), not a purely visual cue.
- Numbers are never shown with color as the only mode indicator — the word
  LIVE or PAPER (or the qualified label, e.g. "Live aggregate P&L") is
  always adjacent to any dollar figure, so the information survives being
  read aloud, printed in grayscale, or viewed by someone colorblind.
- Table-level "all wallets are paper-only" banners do not replace per-row
  badges (redundancy is intentional here, not noise, given the trust/safety
  stakes) — see Followed Wallets above.

## Open questions

1. ~~**Per-wallet live-eligibility derivation** (Followed Wallets)~~ —
   **RESOLVED.** #1253 ships the real `live_enabled` column and the
   preferred (a) derivation is already live in `_derive_live_eligibility`,
   now additionally gated on the new flag. See the `[Tech constraint —
   RESOLVED by #1253 ...]` block above.
2. **Reconciliation/balance-drift surface** (Positions & P&L, Activity
   Feed) — Epic I's `check_wallet_balance_drift` exists in
   `copy_live_settle.py`, but is its result exposed anywhere the dashboard
   API can read from today? If not, is adding a minimal read endpoint in
   scope for this epic, or a fast-follow? Both states above are specified
   either way, but this needs a Tech Lead PM call before implementation.
3. **Twin panels vs. toggle** (Positions & P&L) — this spec defaults to
   twin panels (always both visible) as the safer choice against the
   "never blended" requirement; flagging for Tech Lead PM sign-off given
   the added API/render cost of two simultaneous live queries per poll.
4. **Live aggregate P&L when live trading has never been turned on for any
   wallet** — should "Live aggregate P&L" render as the off-state (no
   number) or as a genuine "$0.00" once at least one wallet has ever gone
   live, even if currently off? Leaning toward: once live has ever run
   (any `copy_live_positions` row exists), show the real historical
   aggregate even while currently off, with the off-banner layered on top
   to make clear no *new* live activity is occurring — needs confirmation
   this reads unambiguously in practice, not just on paper.
5. ~~**`live_cap_usd` exposure on the followed-wallets response**~~ —
   **RESOLVED.** #1259 adds `live_cap_usd: float` to `CopyFollowedWalletsOut`
   (Tech Lead PM decision, 2026-09-30). The enable-live confirmation copy in
   "Enabling live: stake + confirmation" above is written against it as a
   settled dependency.
6. ~~**`# opted into live` pill as the one client-computed count**~~ —
   **RESOLVED.** #1259 adds `live_opted_in_count` to
   `CopyFollowedWalletsOut` alongside the existing `live_eligible_count`/
   `paper_only_count` (Tech Lead PM decision, 2026-09-30) — server-computed,
   no client-side counting.
7. ~~**No standalone control to edit an already-opted-in wallet's live
   stake**~~ — **RESOLVED by #1262** (Tech Lead PM, 2026-09-30). Now that
   Step 1's pre-fill sources from the resolved live stake rather than the
   paper stake (corrected above), the disable/re-enable round-trip no longer
   silently discards an existing override — but adjusting size in place,
   without leaving the live state at all, is still not possible from this
   row. #1262 (Backlog, depends on #1254) adds a dedicated "Edit live stake"
   control — same shape as the proposal originally flagged here (visible
   only when `live_enabled=true` and not paused, `window.prompt` pre-filled
   with the current resolved live stake, `PATCH .../live-stake` directly,
   no `window.confirm` for a *decrease* since the wallet is already live and
   consented) — plus one decision this entry left implicit: #1262 requires a
   confirmation specifically when *raising* an already-live wallet's stake,
   since a size increase is the one direction on an already-live wallet that
   adds real-money exposure, unlike a decrease or an unchanged value.
8. **Reverse direction: lowering the live cap below an existing wallet's
   resolved live stake** (#1266 acceptance criteria) — refuse the config
   change outright (naming the affected wallets) vs. let it through and let
   the new `LIVE (stake exceeds cap)` badge/stake-line above silently catch
   it on the next render. This is a config-validation *behavior* decision,
   not a badge-*design* one, so it isn't answered by this spec — Tech Lead
   PM call. Either choice is compatible with everything specified above:
   the badge and stake-figure copy are correct regardless of which path put
   a wallet into that state.
9. **Persisted "was this wallet live immediately before this pause" fact**
   (#1264, Activity Feed durable record) — see the `[Tech constraint ...]`
   block in "Pausing an opted-in wallet" above. Needs a Tech Lead PM
   feasibility call on the smallest way to persist this (e.g. a new boolean
   column alongside `paused_at`/`paused_reason`) before the Activity Feed
   half of that design can ship. Not a blocker for the rest of #1264's UX:
   the pre-pause prompt warning, the pause-success toast, and the corrected
   Resume/Go-live copy all use only data already available client-side and
   can ship independently of this one.
