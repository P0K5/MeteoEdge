# Prob-cap shadow report -- 2026-07-11

Window: 9 distinct date(s) with p_yes_raw data since PR #564 deploy.
Deployed cap: 0.95 | Fixed NO-entry gate (MAX_CONFIDENCE_YES_FOR_NO, held constant across all simulations): 0.05

## Clamp saturation

- Overall: 173/181 (95.6%) candidates were clamped (p_yes_raw != p_yes).

| Side | Clamped | Total | Rate |
|---|---|---|---|
| NO | 116 | 120 | 96.7% |
| YES | 57 | 61 | 93.4% |

| Station | Clamped | Total | Rate |
|---|---|---|---|
| EFHK | 7 | 7 | 100.0% |
| EPWA | 3 | 3 | 100.0% |
| KATL | 4 | 4 | 100.0% |
| KHOU | 1 | 1 | 100.0% |
| KMIA | 4 | 6 | 66.7% |
| KORD | 5 | 5 | 100.0% |
| LFPB | 5 | 7 | 71.4% |
| LIMC | 3 | 3 | 100.0% |
| LLBG | 2 | 2 | 100.0% |
| LTAC | 3 | 3 | 100.0% |
| LTFM | 5 | 5 | 100.0% |
| MPMG | 4 | 4 | 100.0% |
| NZWN | 5 | 5 | 100.0% |
| OEJN | 10 | 14 | 71.4% |
| RCSS | 11 | 11 | 100.0% |
| RJTT | 12 | 12 | 100.0% |
| RKPK | 8 | 8 | 100.0% |
| RKSI | 9 | 9 | 100.0% |
| RPLL | 8 | 8 | 100.0% |
| SBGR | 6 | 6 | 100.0% |
| WMKK | 12 | 12 | 100.0% |
| WSSS | 11 | 11 | 100.0% |
| ZGGG | 8 | 8 | 100.0% |
| ZGSZ | 10 | 10 | 100.0% |
| ZHHH | 11 | 11 | 100.0% |
| ZSPD | 6 | 6 | 100.0% |

## Population-level clamp saturation (all evaluated brackets)

Source: `analytics.db::snapshot_archive`, gap-filled with the live (not-yet-archived) `logs/snapshots.<date>.jsonl` for the most recent ~24h the archiver hasn't ingested. Every evaluated bracket, gated or not -- much larger than, and complementary to, the settled-trades table above (no per-row trade side is recorded at evaluation time).

- Overall: 163066/203954 (80.0%) evaluated brackets clamped.
- Cross-check: `guardrail_events` recorded 162811 `cap_applied` events over the same window (population count above should be close).

| Station | Clamped | Total | Rate |
|---|---|---|---|
| EFHK | 4943 | 7562 | 65.4% |
| EGLC | 4053 | 5944 | 68.2% |
| EPWA | 4002 | 6697 | 59.8% |
| KATL | 4136 | 4433 | 93.3% |
| KHOU | 4105 | 4378 | 93.8% |
| KLAX | 4773 | 5313 | 89.8% |
| KMIA | 3909 | 4438 | 88.1% |
| KORD | 3870 | 4366 | 88.6% |
| LFPB | 4668 | 6703 | 69.6% |
| LIMC | 4477 | 6726 | 66.6% |
| LLBG | 5393 | 7544 | 71.5% |
| LTAC | 4999 | 7539 | 66.3% |
| LTFM | 5217 | 7550 | 69.1% |
| MPMG | 3941 | 4310 | 91.4% |
| NZWN | 8568 | 9798 | 87.4% |
| OEJN | 5813 | 7516 | 77.3% |
| RCSS | 8853 | 10453 | 84.7% |
| RJTT | 8666 | 10443 | 83.0% |
| RKPK | 7243 | 8352 | 86.7% |
| RKSI | 7934 | 8384 | 94.6% |
| RPLL | 8034 | 10446 | 76.9% |
| SBGR | 3791 | 4422 | 85.7% |
| WMKK | 7984 | 10408 | 76.7% |
| WSSS | 8732 | 10456 | 83.5% |
| ZGGG | 6366 | 7430 | 85.7% |
| ZGSZ | 6030 | 7432 | 81.1% |
| ZHHH | 5956 | 7454 | 79.9% |
| ZSPD | 6610 | 7457 | 88.6% |

## Distribution of p_yes_raw within the clamped population

count=173 min=0.0000 p25=0.0000 median=0.0146 p75=1.0000 max=1.0000 mean=0.3355

## Cap simulation (NO side only -- protected side)

| Cap | Trades | Already-admitted | Newly-admitted | Unresolved-new | Win rate | Total PnL (c) | Edge-channel | Gate-headroom-channel |
|---|---|---|---|---|---|---|---|---|
| 0.95 | 120 | 120 | 0 | 0 | 78.4% | 48.2 | 0 | 0 |
| 0.97 | 138 | 120 | 18 | 0 | 78.4% | 10.2 | 18 | 0 |
| 0.98 | 141 | 120 | 21 | 0 | 78.8% | 67.2 | 21 | 0 |

Per the binding PM spec addition (2026-07-02): the confidence gate (MAX_CONFIDENCE_YES_FOR_NO=0.05) is held fixed across all three simulated caps above. "Edge-channel" counts NO admissions gained purely because a lower clamp floor raised the computed edge past MIN_EDGE_CENTS for candidates that already satisfied the (unchanged) confidence gate. "Gate-headroom-channel" counts admissions gained because the fixed gate itself newly passed -- mathematically 0 for any cap in [0.95, 1) held against a 0.05 gate, confirmed empirically above.

**Unit caveat (DB path, issue #682):** "Total PnL" mixes two different scales here. Already-admitted rows carry `trades.pnl`, the real account-currency PnL of the actual position (scaled by that trade's `size_eur`); newly-admitted rows (discovered from snapshots, resolved via observations daily highs) use the per-$1-notional synthetic PnL `(100 - price) if won else -price` that this simulation has always used. A cap's "Total PnL" is therefore not an apples-to-apples number once it has any newly-admitted rows -- read the win-rate delta and the gate-headroom-channel count as the primary signals, and treat PnL deltas as directional, not literal, until this is reconciled.

## RANK_ON_RAW_PROB=true simulated ordering effect

- Polls with >=2 simultaneous NO candidates: 10
- Polls where raw-edge ranking would have preferred a different candidate than scan order: 6
- Realized PnL delta if raw-edge pick had been taken instead (sum over divergent polls): 0.1c
- Wins: raw-pick=5 capped-pick=5

## Recommendation

cap=0.97: PnL delta -38.0c, win-rate delta -0.1%, gate-headroom channel=0 -- HOLD at current cap.
cap=0.98: PnL +19.0c, win-rate delta +0.4%, gate-headroom channel=0 (safe) -- CANDIDATE TO RAISE CAP.
