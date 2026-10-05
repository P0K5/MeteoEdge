# Post-mortem: the four live copy-trading losses on 2026-10-04

**Status as of 2026-10-05 (self-dated — read the "Current live state" section before acting on this document).**
**Author:** Mid Developer, for issue #1302 (epic #1304).
**Data sources:** `data/meteoedge.db` (opened `file:...?mode=ro&immutable=1`), `logs/copy_live_settle.log`, `logs/copy_signals.log`, source at the commit this branch forked from (`origin/master`).

## TL;DR for the operator

- Live copy-trading placed 4 real orders on 2026-10-04, lost all 4, and took the live wallet from **$20.00 → $0.88**. Live trading is currently halted — not by a decision, but because the drawdown breaker is mathematically stuck tripped and there is no capital left either way (see "Current live state").
- **The losses do not show a broken signal.** Four losses at coin-flip entry prices (0.37–0.54) is an ordinary ~1-in-16 outcome, and paper money made the *identical* four bets and lost the *identical* four ways — this was not an execution or signing problem.
- **The real defect is structural, not statistical:** the exposure cap that gates new live orders and the drawdown breaker that is supposed to stop the bleeding read from two different clocks. The exposure cap reacts to *committed* stake; the breaker reacts to *settled* P&L. Because settlement on these markets lags entry by 6–10 hours, 100% of live capital was committed before the breaker ever got a single data point.
- This document does not conclude the edge is broken, and does not conclude it's fine — it concludes the sample is too small to say either, and that the sizing relationship between capital, exposure cap, and the drawdown threshold must change before more capital is risked, regardless of which way the edge question eventually resolves.

---

## 1. Timeline (all times UTC, from the database — see the log-timestamp caveat in §6)

| Time | Event | Evidence |
|---|---|---|
| 2026-10-03T07:40:56 | `COPY_LIVE_TRADING_ENABLED` set to `true` | `bot_config.updated_at` for that key (query 6) |
| 2026-10-03 14:56–18:46 | 13 live order attempts (ids 1–13), all `rejected_reason='place_failed'` — pre-signing-fix failures (#1292/#1295), not part of this loss | `copy_live_positions` ids 1–13 (query 1) |
| 2026-10-04T11:15:49 | Live fill #1 (id 14, market `0x173338be…`, outcome 1, fill 0.54) | `copy_live_positions` id 14 |
| 2026-10-04T12:42:44 | Live fill #2 (id 15, market `0xed203f42…`, outcome 0, fill 0.52) | `copy_live_positions` id 15 |
| 2026-10-04T12:58:17 | Live fills #3 and #4 (id 16 market `0xe3748f2a…` outcome 1 fill 0.52; id 17 market `0x00175589…` outcome 1 fill 0.38) — same poll cycle, two different source markets | `copy_live_positions` ids 16, 17 |
| 2026-10-04T14:16:33 – 15:38:42 | 5 further signals rejected `live_wallet_exposure_limit` (ids 18–22) — exposure cap correctly refuses new stake once $20 of $20 is committed | `copy_live_positions` ids 18–22 |
| 2026-10-04T18:45:15 | First settlement: id 15 → **-$5.00** | `copy_live_positions.settled_at`/`settled_pnl_usd` |
| 2026-10-04T19:17:05 | First breaker rejection (id 23, `live_circuit_breaker_drawdown`) — drawdown = 5/20 = 25% ≥ 20% stop | `copy_live_positions` id 23; `src/risk/copy_risk_manager.py:140-152` |
| 2026-10-04T19:57:17 | Second breaker rejection (id 24, same reason) | `copy_live_positions` id 24 |
| 2026-10-04T21:00:15 | Second settlement: id 17 → **-$5.00** | `copy_live_positions` id 17 |
| 2026-10-04T22:30:02 | Third and fourth settlement: id 14 and id 16 → **-$5.00 each** | `copy_live_positions` ids 14, 16 |
| 2026-10-05T03:45:13 | The followed wallet (`0x9243…`) auto-paused, reason `stability_check_failed` — **`live_enabled` was left `1`** | `copy_wallets_followed` row for `0x9243…` |
| 2026-10-05T07:40:07 | Wallet balance drift check: expected $0.00, actual $0.88, within $2.00 tolerance | `bot_config.COPY_LIVE_WALLET_BALANCE_DRIFT_CHECK` |

Net: 4 real orders, 4 losses, $20.00 realized, in a little under 3 hours of entries (11:15→12:58) and roughly 11 hours to full settlement (11:15→22:30).

---

## 2. The paper/live twin table, re-derived independently

The issue's seed table is **verified correct**. I did not copy it — I joined `copy_live_positions` to its paper twin directly via the `signal_id` foreign key (both the live and paper rows are written from the exact same `copy_signal_loop._handle_buy_trade` call, off the same `copy_signals.id`), rather than matching on timestamp/price as the seed table's "idx" column implies:

```sql
-- live fills and their signal rows
SELECT id, signal_id, market, outcome_index, fill_price, stake_usd, settled_pnl_usd
FROM copy_live_positions WHERE id IN (14,15,16,17);

-- the same signal_id's paper position (copy_signals.position_id -> copy_positions.id)
SELECT cp.id AS paper_position_id, cp.entry_price, cp.settled_pnl_usd, cs.id AS signal_id
FROM copy_signals cs JOIN copy_positions cp ON cp.signal_id = cs.id
WHERE cs.id IN (109153,109245,109296,109297);
```

| live id | signal_id | market | outcome | source_price | live fill | paper position id | paper entry | live pnl | paper pnl |
|---|---|---|---|---|---|---|---|---|---|
| 14 | 109153 | `0x173338be…` | 1 | 0.53 | 0.54 | 1618 | 0.53795 | -5.00 | -5.00 |
| 15 | 109245 | `0xed203f42…` | 0 | 0.51 | 0.52 | 1620 | 0.51765 | -5.00 | -5.00 |
| 16 | 109296 | `0xe3748f2a…` | 1 | 0.51 | 0.52 | 1621 | 0.51765 | -5.00 | -5.00 |
| 17 | 109297 | `0x00175589…` | 1 | 0.37 | 0.38 | 1622 | 0.37555 | -5.00 | -5.00 |

This matches the issue's seed table exactly — no correction needed there. One thing the seed table doesn't explain, which I chased down: the live fill is consistently **exactly one cent above the paper fill**, not "one tick of extra slippage." Both prices come from the same `apply_slippage(source_price, "BUY", DEFAULT_SLIPPAGE_BPS)` call in `copy_signal_loop.py:576` — paper stores that float as-is (fractional cents, e.g. 0.53795); the live order has to be placed at a valid CLOB price (whole cents), and 0.53795 rounds to 0.54, 0.51765 rounds to 0.52, 0.37555 rounds to 0.38 — standard nearest-cent rounding, not an extra slippage charge. It happens to round *up* in all four cases here because `DEFAULT_SLIPPAGE_BPS` pushes the price into the upper half of its cent, not because of a directional rule. **Conclusion unchanged from the issue: this was not an execution-quality problem**, but the mechanism is "tick rounding of an identical price," not "worse slippage," which matters if anyone later tries to tune `DEFAULT_SLIPPAGE_BPS` to fix this — there's nothing to fix here.

---

## 3. Four losses is not evidence the signal is broken — and here's the bar that would be

All four entries were in the 0.37–0.54 band — effectively coin-flip territory. The probability of losing all 4 independent bets at ~50% true win probability is 1/16 ≈ 6.25%; that's an unremarkable outcome, not a rejection of the hypothesis that the edge is positive. (At 0.37, the "fair" implied win probability is lower still, which makes an all-four-loss outcome *less* surprising, not more.)

What sample size would support a real conclusion: this project already has a precedent for "how many resolved trades does it take to trust a win rate" — the wallet-quality screening gate requires **100 resolved trades** before a followed wallet is even considered track-record-qualified (`src/config.py:813`, `COPY_SCREEN_MIN_RESOLVED_TRADES`; enforced in `src/scripts/copy_wallet_screening.py:297-364`). Applying the same bar to our own live book: 4 samples is two orders of magnitude short of the threshold this codebase already uses elsewhere for exactly this kind of judgment. As a sanity check from first principles: a 95% binomial confidence interval on an observed win rate has half-width roughly `1/sqrt(n)`; at n=4 that's ±50 points (no information), at n=25 it's ±20 points (can distinguish a disaster from a coin flip), at n=100 it's ±10 points (can distinguish a modest edge from none). **Do not re-enable live capital on the theory that "the signal is bad," and do not re-enable it on the theory that "it's fine" either — neither is supported yet.**

---

## 4. Primary finding: the exposure cap and the drawdown breaker read from different clocks

This is the finding that should drive the go/no-go decision, independent of what you believe about the edge.

**The breaker only ever sees settled P&L.** `allow_live_copy_signal` (`src/risk/copy_risk_manager.py:103-154`) computes `drawdown = -total_pnl / COPY_LIVE_CAPITAL_USD` from `get_copy_live_realized_pnl_total()`, which sums `copy_live_positions.settled_pnl_usd` — rows with `status='settled'` only. An order that is live and at risk, but unresolved, contributes **nothing** to this number.

**The exposure cap only ever sees committed (not necessarily settled) stake.** `_handle_live_order` (`src/scripts/copy_signal_loop.py:302-326`) sums `stake_usd` over `get_open_copy_live_positions()` — any row not yet in a terminal rejected/settled state — and compares it to `COPY_LIVE_MAX_TOTAL_EXPOSURE_USD`.

With this incident's live config (`bot_config`, read-only query below):

```sql
SELECT key, value FROM bot_config
WHERE key IN ('COPY_LIVE_MAX_TOTAL_EXPOSURE_USD','COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD',
              'COPY_LIVE_DRAWDOWN_STOP_PCT','COPY_LIVE_DAILY_LOSS_LIMIT_USD');
```

| key | value |
|---|---|
| `COPY_LIVE_MAX_TOTAL_EXPOSURE_USD` | 20.00 |
| `COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD` | 20.00 |
| `COPY_LIVE_DRAWDOWN_STOP_PCT` | 0.20 |
| `COPY_LIVE_DAILY_LOSS_LIMIT_USD` | 25.00 |

`COPY_LIVE_CAPITAL_USD` is a deploy-time env var, not in `bot_config` (by design — see the comment at `src/config.py:280-290`), so it is not directly queryable from the database. I did not take the issue's "$20" on faith; I re-derived it independently from the wallet-balance reconciliation `copy_live_settle.py` already performs (`docs/DB_SCHEMA.md:728`, `check_wallet_balance_drift`): expected balance = `COPY_LIVE_CAPITAL_USD − committed_unsettled_stake + realized_pnl`. At 2026-10-05T07:40:07 there is no unsettled stake and realized P&L is -$20.00, and `bot_config.COPY_LIVE_WALLET_BALANCE_DRIFT_CHECK` records `expected_balance_usd: 0.0`. The only value of `COPY_LIVE_CAPITAL_USD` consistent with `0 = CAPITAL − 0 + (−20)` is **$20.00** — confirming the issue's figure from a second, independent source rather than restating it.

**Only one wallet had `live_enabled=1`** (`0x9243…` — confirmed by `SELECT address, live_enabled FROM copy_wallets_followed`; all 9 others are 0), so in this incident the per-wallet cap and the total cap were numerically identical and both ultimately bound the same $20. The code checks the per-wallet cap first (`copy_signal_loop.py:316` before `:319`), which is why all five rejections (ids 18–22) carry `live_wallet_exposure_limit` rather than `live_total_exposure_limit` — an artifact of check ordering with a single live wallet, not a second independent control. With more than one live-enabled wallet, the same drawdown-stop math below needs to be re-derived against the *total* cap specifically; it isn't automatically covered by per-wallet caps.

**The timeline this produces:** by 12:58:17 — under 2 hours after the first entry — the $20 total-exposure cap was fully committed across 4 unsettled positions. The breaker could not have objected at any point in that window: it had zero settled P&L to read. It took until 18:45 (the first settlement, 6h43m after that fill was placed) for the breaker to get its first data point at all — by which point 100% of capital was already at risk. It then fired correctly, five minutes after the config allowed it a reading (observed trip at 19:17, first eligible settlement at 18:45 — the ~30 minute gap is just the next poll cycle of `copy_signal_loop.py`, not a bug).

There's a second multiplier worth naming explicitly: at `COPY_LIVE_CAPITAL_USD=$20` and a $5 stake, **a single losing trade is already 25% of capital** — past the 20% drawdown-stop threshold on its own. The breaker tripping "at 25%" (not 20%) isn't a miscalibration of the threshold; it's that the position-sizing granularity (4 positions covering 100% of capital) can't land any value ≤20% other than 0%. The breaker's first possible non-zero reading was always going to overshoot.

**The rule that would have stopped this after one or two losses, not four** (per the issue's acceptance criteria, expressed as a relationship, not pasted-in numbers):

> `COPY_LIVE_MAX_TOTAL_EXPOSURE_USD` must be ≤ `COPY_LIVE_CAPITAL_USD × COPY_LIVE_DRAWDOWN_STOP_PCT`.

Reasoning: the breaker cannot see anything until a position settles, so the worst case it is defending against is "every currently-open, unsettled position turns out to be a total loss." Bounding simultaneous unsettled exposure to exactly `CAPITAL × DRAWDOWN_STOP_PCT` means that worst case lands *at* the stop threshold, never past it — which is the strongest guarantee obtainable without also watching live prices (§5). Today's actual relationship is `MAX_TOTAL_EXPOSURE_USD = CAPITAL_USD` — i.e., 5× the value this rule implies (at the current 20% threshold, the rule would cap total exposure at $4, not $20). This is why it took all four losses, not one, to produce a signal the breaker could act on. This also subsumes per-trade stake: a single trade's stake is always ≤ the total cap, so bounding the total automatically bounds any individual trade too.

This rule is a sizing constraint, not a new mechanism — it belongs with the breaker's clear-path work. **Filed as new issue #1310** (see Recommendations) rather than duplicating #1300, since #1300's own scope is specifically the clear-path/fail-closed mechanism, not the sizing relationship.

---

## 5. Does this need an unrealized-exposure-aware (mark-to-market) gate?

The acceptance criteria ask this directly. **Recommendation: no, not as a must-fix — the rule in §4 already solves the specific failure mode seen here, and a mark-to-market gate solves a different, additional problem at a real ongoing cost.**

What §4's rule fixes: the breaker being blind during the settlement-lag window. Bounding total unsettled exposure to `CAPITAL × DRAWDOWN_STOP_PCT` means the maximum loss reachable *before the breaker has ever seen a number* is capped at the stop threshold itself — exactly the guarantee a drawdown stop is supposed to provide. That is a static, computed-once-at-config-load bound; no new runtime mechanism, no new data source.

What a mark-to-market gate would add on top: protection against *adverse price movement before resolution* — e.g., if a position craters from 0.52 to 0.10 pre-settlement, §4's rule still only reacts once it formally settles. That's a real, separate risk, but it is not what happened in this incident (all four markets' resolutions were binary YES/NO settlements with no live quote tracked in between — `copy_live_positions` doesn't store intermediate price snapshots, so this isn't something I can even measure from current data). The cost of adding it: a per-open-position mid-price fetch every poll cycle (one more outbound call per unsettled position, every ~5 minutes, same rate-limit budget already shared with `fetch_market_resolution`), plus a second, independent threshold to tune and get wrong. Given §4's rule already closes the specific gap this incident exposed, I'd hold mark-to-market as a should-fix for later, not a blocker — logged as new issue #1311.

---

## 6. `COPY_ONE_POSITION_PER_MARKET` — confirmed intended, not a bug

The issue asks whether the engine holding opposing positions in one market (`0xed203f42…`) across different followed wallets is in-scope for this flag. Re-derived from the database (not restated):

```sql
SELECT id, address, outcome_index, entry_ts, settled_pnl_usd
FROM copy_positions
WHERE market = '0xed203f42ae15344ac944092c1e4574eeadb72f26a449d0a592516ac32c40ef26'
ORDER BY id;
```

| position | wallet | outcome | entered | settled pnl |
|---|---|---|---|---|
| 1359 | `0xd106…` | 0 | 2026-10-03 21:26 | -5.00 |
| 1472 | `0xd106…` | 1 | 2026-10-04 01:20 | +4.85 |
| 1620 | `0x9243…` (our live wallet, paper side) | 0 | 2026-10-04 12:42 | -5.00 |
| 1632 | `0xbca0…` | 0 | 2026-10-04 13:48 | -5.00 |
| 1642 | `0xbca0…` | 1 | 2026-10-04 13:54 | +6.20 |

This is a **more specific** finding than the issue's framing: it isn't only "different wallets take opposite sides of the same market" (expected — nothing should or does dedupe across wallets); it's that **the same wallet** (`0xd106…`, then independently `0xbca0…`) held **both outcomes of the same market open simultaneously** — `0xbca0…`'s two positions are 6 minutes apart. Our live-enabled wallet (`0x9243…`) only ever took one side (1620); it did not itself contribute to this pattern.

The code: `copy_signal_loop.py:547-558` scopes the dedupe to `p["market"] == market and p["outcome_index"] == outcome_index` over `db.get_open_copy_positions(address)` — i.e. **(wallet, market, outcome)**, not (wallet, market). This is not an oversight — it is exactly what issue #1207 (which introduced the flag) specified and tested: its acceptance criteria title is "Cap copy-trading at one open position per (wallet, market, outcome)", and its own test requirements explicitly list "Same market, different outcome_index → allowed through (not a duplicate)" and "Different wallet, same market/outcome → allowed through (the constraint is per wallet)". **Confirmed as intended — no new issue.** It does mean a followed wallet that self-hedges (trades both sides of its own market) gets mirrored on both sides, which is a legitimate thing for a copy-trading bot to do (it's copying the wallet's actual behavior) but worth knowing if wallet selection criteria ever start caring about this pattern specifically.

---

## 7. A caveat for whoever reads these logs next

While reconstructing the settlement timeline I initially cross-referenced `logs/copy_live_settle.log` timestamps against `copy_live_positions.settled_at` and got a real, reproducible 1-hour mismatch (e.g. the log's `settlement pass complete: settled=1` lines land at 19:45:15, 22:00:15, 23:30:02 — but the corresponding `settled_at` values the same pass wrote are 18:45:15, 21:00:15, 22:30:02 — each exactly 1 hour earlier). Root cause: `src/logging_config.py:31-35` calls `logging.basicConfig(..., datefmt=...)` without setting `logging.Formatter.converter = time.gmtime`, so `%(asctime)s` renders in the host's **local** time (Portugal, WEST/UTC+1 in October), while every DB timestamp in this codebase is written via `datetime.now(timezone.utc)`. The data itself is fine — I used `copy_live_positions`/`copy_signals` timestamps throughout this document, not log timestamps — but anyone correlating logs against the database by eye will misjudge ordering by an hour. Logged as new issue #1312 (should-fix, not blocking).

---

## 8. Current live state (as of 2026-10-05, verified against `bot_config`)

```sql
SELECT key, value, updated_at FROM bot_config WHERE key = 'COPY_LIVE_TRADING_ENABLED';
-- COPY_LIVE_TRADING_ENABLED | true | 2026-10-03T07:40:56.668064+00:00
```

- `COPY_LIVE_TRADING_ENABLED` is still `true` — **the kill switch was never turned off.** What is actually holding the line: (a) the drawdown breaker, which — per §4's math — is deterministically recomputed every call from `get_copy_live_realized_pnl_total()` and currently always returns "tripped" (realized P&L is -$20 against $20 capital, 100% drawdown, nowhere near able to self-clear), and (b) the live balance is ~$0.88, so even a config change wouldn't fund a new order today.
- The one `live_enabled=1` wallet (`0x9243…`) was auto-paused 2026-10-05T03:45:13 for `stability_check_failed` — **unrelated to this incident's cause** (a separate wallet-health check), but its `live_enabled` flag was left `1` by the pause. This is a live, dated instance of exactly the gap #1264 already identified and decided to close (option (a): any transition to paused clears `live_enabled` — see the merged design spec for #1264/#1266). If the breaker is ever cleared and this wallet resumed under current behavior, it would go live again with no fresh human confirmation.
- There is no manual clear mechanism for the breaker today (confirmed by reading `allow_live_copy_signal` end to end — it is pure recomputation, no stored "cleared" state, and no caller in `src/dashboard/api.py` or the CLI resets it). This is exactly #1300's scope.

---

## 9. Recommendations

### Must-fix before live resumes
1. **#1300** — give the breaker an auditable clear path and make it fail closed without capital. Today the breaker cannot be cleared at all, and the fact that it happens to still be tripped is the only thing standing between `COPY_LIVE_TRADING_ENABLED=true` and a new live order the moment settled P&L math changes (e.g. a future capital top-up without an explicit breaker reset).
2. **New issue #1310** (filed by this PR, Backlog) — enforce `COPY_LIVE_MAX_TOTAL_EXPOSURE_USD ≤ COPY_LIVE_CAPITAL_USD × COPY_LIVE_DRAWDOWN_STOP_PCT` at config-load / `live_startup_sanity_check` time (the pattern `live_startup_sanity_check` already uses for `MAX_TOTAL_EXPOSURE_USD vs CAPITAL_USD`, `copy_signal_loop.py:190-213`, just a tighter bound). This is the primary structural fix — see §4.
3. **#1264** — pause must clear `live_enabled` (already decided, option (a); design spec merged). §8 shows a live, dated example of the exact gap it closes. Should land before the breaker in #1300 is ever cleared, so a wallet auto-paused for unrelated reasons can't silently resume live.

### Should-fix
4. **#1266** — refuse a live stake above the per-wallet cap. Not causal in this incident (the $5 stake never exceeded the $20 cap), but it's the same class of config-integrity gap and matters once more than one wallet is live-enabled.
5. **#1171** — ghost-order reconciliation and log severity. Not triggered here (all 4 orders filled cleanly with a captured `order_id`; zero `cancel_failed_ghost` rows in this incident), but still open general-reliability debt ahead of scaling up live capital.
6. **New issue #1311** (filed by this PR, Backlog) — unrealized-exposure-aware (mark-to-market) gate for open live positions, as discussed in §5. Not blocking; the §4 rule already covers this incident's failure mode.
7. **New issue #1312** (filed by this PR, Backlog) — `src/logging_config.py` should log in UTC (`Formatter.converter = time.gmtime`), to match every DB timestamp in the codebase. Purely an observability fix (§6); no trading impact.

### Won't-fix (with reasons)
- **A second, independent "didn't we already decide this" dedupe for `COPY_ONE_POSITION_PER_MARKET` across wallets** — not a defect; §6 confirms the (wallet, market, outcome) scope is exactly what #1207 specified and tested. Changing it now would be a new design decision (should a self-hedging followed wallet be mirrored on both sides?), not a bug fix, and is out of scope for this document.
- **A mark-to-market gate as a must-fix** — see §5. Argued against as a blocker; kept as should-fix (#1311).

---

## Appendix: reproducibility

All queries above were run against `data/meteoedge.db` opened as `file:C:/Coding/MeteoEdge/data/meteoedge.db?mode=ro&immutable=1` (read-only, immutable — never opened writable, per #1238). Row counts at the time of this analysis: `copy_live_positions` = 24 rows (ids 1–24, matching the issue's technical notes), `copy_signals` ≥ 109297 (autoincrement id), `copy_positions` includes ids 1618, 1620, 1621, 1622 for the four paper twins and 1359/1472/1632/1642 for the §6 cross-wallet example. No row in this document's queries was written to; this PR adds only this file.
