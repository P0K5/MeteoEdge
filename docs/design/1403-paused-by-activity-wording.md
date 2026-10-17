# Design Spec — Pause Origin Wording in the Activity Feed (#1403)

Follow-up to #1368 / PR #1399. Addendum to the Activity Feed section of
`docs/design/copy-trading-live-views.md`. Design direction only; no code.
Optional issue: do it only if operators ask for the distinction; this spec
is ready if it is picked up.

## User goal

Tell at a glance whether a wallet stopped because the health job tripped
(needs investigation) or because a person paused it (already known). Since
#1368 both read "Wallet paused — {reason}".

## Wording rules (the whole change)

`paused_by` is exposed on the `wallet_paused` event. Text:

| `paused_by` | Description text |
|---|---|
| `health_job` | `Wallet auto-paused — {reason}` |
| `cli` | `Wallet paused — {reason}` |
| `dashboard` | `Wallet paused — {reason}` |
| null / absent / any unrecognized value | `Wallet paused — {reason}` (neutral; never claim "auto") |

- Reason fallback unchanged: `No reason given`.
- Live opt-in revoked variant keeps its sentence:
  `Wallet auto-paused — {reason}. Live trading opt-in was revoked.` /
  `Wallet paused — {reason}. Live trading opt-in was revoked.`
  The warning icon behavior is unchanged.
- Rationale for not labelling manual pauses "manually paused": a human
  pause already carries a human-written reason; plain "paused" is the
  restored pre-#1368 wording and keeps `cli` and `dashboard` identical. Only
  the automatic case needs a distinguishing word.
- "auto-paused" is hyphenated, lowercase after "Wallet", matching the
  pre-#1368 feed wording and the existing tests' vocabulary.

## What does NOT change

- Badge text stays `Wallet paused` and the filter option `Wallet paused`:
  one event type, so filtering finds both origins. No new badge, icon,
  color or filter option.
- Followed Wallets status badge stays `Paused` (+ reason caption). Origin
  is not surfaced there in this issue.
- Mode stays `paper`; event ordering and click-through to wallet detail unchanged.

## 375px behavior

Text-only change of a few characters ("auto-"); the description already
wraps in `.copy-activity-desc`. No layout change; verify the longest realistic
line (auto-paused + long reason + revoked sentence) wraps without overflow.

## States

- Legacy rows (paused before the column existed, `paused_by` null): neutral
  "Wallet paused". [Tech constraint: origin cannot be reconstructed
  reliably — agreed approach: no backfill and no guessing from
  `paused_reason` text; legacy pauses stay neutral permanently.]
- Loading / empty / error: unchanged (feed owns them).
- Resume then pause again by a different origin: wording follows the
  current pause. Because the event is derived from the wallet's current row,
  `paused_by` must be written and cleared together with `paused_at` at the
  `update_followed_wallet_status` chokepoint, or a stale value would mislabel
  a later pause (feasibility point for the PM).

## Accessibility notes

The row's `aria-label` is built from the same description, so it must use
the same word: `…Wallet auto-paused — {reason}, …click to view wallet
detail`. Do not rely on any visual-only cue.

## Test notes

Update the #1368 assertions: `paused_by: 'health_job'` yields
`Wallet auto-paused — roi` (plain and revoked variants, and the aria-label);
`cli`, `dashboard`, null, and an unknown value yield `Wallet paused — roi`
and never contain `auto-paused`. Keep the neutral-null assertion.

## Open questions

1. Whether to show origin on the Followed Wallets Paused badge too is
   deliberately out of scope; raise a separate issue if wanted.
2. Operator demand (the issue's own gate): PM/user decides whether to schedule.
