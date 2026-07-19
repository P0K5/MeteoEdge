# Prob-cap shadow report -- 2026-07-19

Window: 17 distinct date(s) with p_yes_raw data since PR #564 deploy.
Deployed cap: 0.95 | Fixed NO-entry gate (MAX_CONFIDENCE_YES_FOR_NO, held constant across all simulations): 0.05

## Clamp saturation

- Overall: 277/290 (95.5%) candidates were clamped (p_yes_raw != p_yes).

| Side | Clamped | Total | Rate |
|---|---|---|---|
| NO | 188 | 192 | 97.9% |
| YES | 89 | 98 | 90.8% |

| Station | Clamped | Total | Rate |
|---|---|---|---|
| EFHK | 9 | 9 | 100.0% |
| EGLC | 1 | 1 | 100.0% |
| EPWA | 8 | 8 | 100.0% |
| KATL | 8 | 8 | 100.0% |
| KHOU | 5 | 5 | 100.0% |
| KLAX | 7 | 7 | 100.0% |
| KMIA | 6 | 8 | 75.0% |
| KORD | 8 | 9 | 88.9% |
| LFPB | 7 | 9 | 77.8% |
| LIMC | 5 | 5 | 100.0% |
| LLBG | 7 | 7 | 100.0% |
| LTAC | 6 | 6 | 100.0% |
| LTFM | 5 | 5 | 100.0% |
| MPMG | 6 | 6 | 100.0% |
| NZWN | 7 | 7 | 100.0% |
| OEJN | 13 | 20 | 65.0% |
| RCSS | 18 | 18 | 100.0% |
| RJTT | 21 | 21 | 100.0% |
| RKPK | 16 | 16 | 100.0% |
| RKSI | 13 | 14 | 92.9% |
| RPLL | 12 | 12 | 100.0% |
| SBGR | 10 | 10 | 100.0% |
| WMKK | 13 | 13 | 100.0% |
| WSSS | 14 | 14 | 100.0% |
| ZGGG | 15 | 15 | 100.0% |
| ZGSZ | 16 | 16 | 100.0% |
| ZHHH | 13 | 13 | 100.0% |
| ZSPD | 8 | 8 | 100.0% |

## Population-level clamp saturation (all evaluated brackets)

Source: `analytics.db::snapshot_archive`, gap-filled with the live (not-yet-archived) `logs/snapshots.<date>.jsonl` for the most recent ~24h the archiver hasn't ingested. Every evaluated bracket, gated or not -- much larger than, and complementary to, the settled-trades table above (no per-row trade side is recorded at evaluation time).

- Overall: 412668/585152 (70.5%) evaluated brackets clamped.
- Cross-check: `guardrail_events` recorded 412805 `cap_applied` events over the same window (population count above should be close).

| Station | Clamped | Total | Rate |
|---|---|---|---|
| EFHK | 12603 | 22171 | 56.8% |
| EGLC | 11606 | 20502 | 56.6% |
| EPWA | 11990 | 21301 | 56.3% |
| KATL | 14672 | 18929 | 77.5% |
| KHOU | 14884 | 18883 | 78.8% |
| KLAX | 16977 | 20084 | 84.5% |
| KMIA | 15184 | 18913 | 80.3% |
| KORD | 15436 | 18880 | 81.8% |
| LFPB | 13156 | 21271 | 61.8% |
| LIMC | 12273 | 21331 | 57.5% |
| LLBG | 14374 | 22155 | 64.9% |
| LTAC | 13415 | 22113 | 60.7% |
| LTFM | 13362 | 22152 | 60.3% |
| MPMG | 12421 | 18774 | 66.2% |
| NZWN | 20272 | 24919 | 81.4% |
| OEJN | 14648 | 22138 | 66.2% |
| RCSS | 18291 | 24811 | 73.7% |
| RJTT | 19273 | 25000 | 77.1% |
| RKPK | 15107 | 18628 | 81.1% |
| RKSI | 15342 | 18660 | 82.2% |
| RPLL | 17033 | 24905 | 68.4% |
| SBGR | 13080 | 18871 | 69.3% |
| WMKK | 16989 | 24776 | 68.6% |
| WSSS | 18265 | 24908 | 73.3% |
| ZGGG | 12921 | 17488 | 73.9% |
| ZGSZ | 12194 | 17500 | 69.7% |
| ZHHH | 12879 | 17548 | 73.4% |
| ZSPD | 14021 | 17541 | 79.9% |

## Distribution of p_yes_raw within the clamped population

count=277 min=0.0000 p25=0.0000 median=0.0190 p75=1.0000 max=1.0000 mean=0.3287

## Cap simulation (NO side only -- protected side)

| Cap | Trades | Already-admitted | Newly-admitted | Unresolved-new | Win rate | PnL/$1 notional (c) | Real PnL (acct, ref) | Edge-channel | Gate-headroom-channel |
|---|---|---|---|---|---|---|---|---|---|
| 0.95 | 192 | 192 | 0 | 0 | 80.3% | 718.0 | 2.55 | 0 | 0 |
| 0.97 | 216 | 192 | 24 | 0 | 80.2% | 701.0 | 2.55 | 24 | 0 |
| 0.98 | 220 | 192 | 28 | 0 | 80.6% | 777.0 | 2.55 | 28 | 0 |

Per the binding PM spec addition (2026-07-02): the confidence gate (MAX_CONFIDENCE_YES_FOR_NO=0.05) is held fixed across all three simulated caps above. "Edge-channel" counts NO admissions gained purely because a lower clamp floor raised the computed edge past MIN_EDGE_CENTS for candidates that already satisfied the (unchanged) confidence gate. "Gate-headroom-channel" counts admissions gained because the fixed gate itself newly passed -- mathematically 0 for any cap in [0.95, 1) held against a 0.05 gate, confirmed empirically above.

**PnL basis (issue #723):** "PnL/$1 notional (c)" is the single basis used for *both* populations -- `(100 - price) if the NO side won else -price`, in cents per $1 of notional, computed from each row's own entry price and realized outcome (already-admitted: `trades.actual_price` -- the bought-side cost, i.e. the NO ask for NO rows since issue #737 -- plus the `trades.pnl` sign; newly-admitted: the snapshot's `no_ask` + the observations-resolved bracket outcome). Because every row uses the same unit, a cap's PnL total and the cross-cap PnL delta are now apples-to-apples -- this column, not just win rate, is a trustworthy signal. "Real PnL (acct, ref)" is the real account-currency PnL of the actual positions (`trades.pnl`, scaled by each trade's `size_eur`); it covers the already-admitted population only (newly-admitted rows never held a real position) and is invariant to cap by construction, so it is reported for reference, not for cross-cap comparison. It reads `n/a` if any resolved already-admitted row is missing its stored PnL.

## RANK_ON_RAW_PROB=true simulated ordering effect

- Polls with >=2 simultaneous NO candidates: 15
- Polls where raw-edge ranking would have preferred a different candidate than scan order: 7
- Realized PnL delta if raw-edge pick had been taken instead (sum over divergent polls): -3.0c
- Wins: raw-pick=4 capped-pick=7

## Recommendation

cap=0.97: PnL delta -17.0c, win-rate delta -0.1%, gate-headroom channel=0 -- HOLD at current cap.
cap=0.98: PnL +59.0c, win-rate delta +0.2%, gate-headroom channel=0 (safe) -- CANDIDATE TO RAISE CAP.
