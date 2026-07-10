# Prob-cap shadow report -- 2026-07-10 (issue #551 stage 2)

## STATUS: BLOCKED -- no usable production data in this environment

**This report cannot state a data-backed recommendation today.** The
generator (`scripts/prob_cap_shadow_report.py`, built for this issue --
see #570) is complete, tested, and runs cleanly, but this Claude Code
session's container has **no production log data at all**:

```
$ ls logs/
ls: cannot access 'logs/': No such file or directory
```

`logs/candidates.csv`, `logs/settlements.csv`, `logs/snapshots.jsonl`, and
any `analytics.db` the script's docstring references are all host-local
files that live next to the deployed bot process. This dev/CI checkout has
never run the bot, so none of them exist here -- not truncated, not empty,
genuinely absent. There is no `--fixture` mode and no bundled sample/demo
log data anywhere in the repo (checked `find . -iname '*sample*log*'
-o -iname '*fixture*'`) to substitute.

Running the script's own self-gate against this checkout confirms it finds
zero distinct dates of `p_yes_raw` history:

```
$ python3 scripts/prob_cap_shadow_report.py --dry-run --min-days 1
INFO __main__: [prob_cap_shadow] only 0 distinct date(s) with p_yes_raw data
(< --min-days=1) -- skipping report, safe to re-run any day.
```

**I am not fabricating win-rate/PnL/count numbers to fill this report.**
Per the task instructions, everything below is methodology, verification
that the tooling works, and a ready-to-run scaffold -- not a decision.

### What this means for the #551 timeline

Stage 1 (PR #564, merged ~2026-07-01) has been logging `p_yes_raw` /
`ev_yes_raw` / `ev_no_raw` in production for >=9 days per the Tech Lead's
brief, so the *data almost certainly exists on the production bot host*.
It simply is not present in this ephemeral dev container. **Someone with
access to the production host's `logs/` directory (or a copy of
`logs/candidates.csv`, `logs/settlements.csv`, `logs/snapshots.jsonl`
since 2026-07-01) needs to either run the command below there, or ship
those files into a session that has this repo checked out.**

```bash
# On the production host, or any checkout with the real logs/ populated:
python3 scripts/prob_cap_shadow_report.py --min-days 7
# writes backtest_results/prob_cap_shadow_<today>.md

# To inspect without writing:
python3 scripts/prob_cap_shadow_report.py --dry-run --min-days 7
```

No code changes are required to unblock this -- the script is finished
and its test suite (`src/tests/test_prob_cap_shadow_report.py`, 26 tests)
plus the related `src/tests/test_scanner_shadow.py` (6 tests) all pass in
this environment:

```
$ python3 -m pytest src/tests/test_prob_cap_shadow_report.py src/tests/test_scanner_shadow.py -q
................................                                         [100%]
32 passed in 0.38s
```

---

## Methodology (what the script computes, once real data is available)

This mirrors the script's module docstring and the #551/#570 spec so a
reviewer can validate the plan without needing the numbers yet.

**Constraint respected throughout:** the live NO entry gate keeps
consuming the *capped* `p` exactly as deployed today. Nothing here changes
`MODEL_PROB_CAP`, `MAX_CONFIDENCE_YES_FOR_NO`, or `RANK_ON_RAW_PROB`'s
default. All cap values other than the deployed 0.95 are **simulated only**
-- computed from logged `p_yes_raw` / snapshot data, never fed back into
live trading.

1. **Self-gate.** Refuses to write a report below `--min-days` (default 7)
   distinct dates with non-null `p_yes_raw` in `candidates.csv`. Safe to
   schedule daily from day one of stage 1.

2. **Clamp saturation.** Of all settled candidates with `p_yes_raw`
   populated, what fraction were actually clamped (`p_yes_raw != p_yes`)?
   Broken out **overall, per side (YES/NO), and per station** -- this is
   the direct evidence for the #551 premise ("~99% of candidates saturate
   the cap").

3. **Distribution of `p_yes_raw`** within the clamped population (min /
   p25 / median / p75 / max / mean) -- shows *how far* below the floor (or
   above the ceiling) the raw model probability actually sits, i.e. how
   much signal the cap is discarding.

4. **Cap simulation at 0.95 (deployed) / 0.97 / 0.98, NO side only** (NO is
   the protected, profitable side per the issue's Constraint section).
   Two populations are combined per cap:
   - **Already-admitted** rows from `settlements.csv` -- membership never
     changes with cap (a real trade that happened stays counted once at
     every simulated cap); only `ev_no` is recomputed for transparency.
   - **Newly-admitted** candidates discovered from `snapshots.jsonl` --
     brackets evaluated every poll but that never cleared `MIN_EDGE_CENTS`
     under the deployed cap, which a laxer simulated floor pulls over the
     edge bar. Resolved against `actual_high` where a matching settlement
     exists for that `(station, date)`; otherwise reported separately as
     **unresolved** and excluded from win-rate/PnL (never guessed).
   - Every newly-admitted NO candidate is attributed to exactly one of two
     **channels**, per the binding PM spec addition (2026-07-02,
     `MAX_CONFIDENCE_YES_FOR_NO` held fixed while `MODEL_PROB_CAP` is
     varied):
     - `edge` -- gained purely because a lower clamp floor raised the
       computed NO edge past `MIN_EDGE_CENTS` for a candidate that already
       passed the (unchanged) confidence gate. This is the expected,
       intended channel.
     - `gate_headroom` -- would mean the fixed confidence gate itself
       newly passed. **Mathematically impossible for any cap in [0.95, 1)
       held against the current 0.05 gate** (the simulated floor
       `1 - cap` never exceeds 0.05 in that range), but the classifier
       still evaluates every row rather than hardcoding the assumption,
       so the report will surface it immediately if a future cap proposal
       ever drops below 0.95 or the gate value changes.
     - **Any non-zero `gate_headroom` count in the real report is an
       explicit flag: it means a simulated cap change would loosen the NO
       gate, and the recommendation logic (below) will refuse to recommend
       raising the cap under that condition regardless of PnL.**

5. **`RANK_ON_RAW_PROB=true` simulated ordering effect.** For polls that
   produced >=2 simultaneous NO candidates, compares the scan-order pick
   (today's default execution order) against the raw-edge-ranked pick, and
   sums the realized PnL delta over every poll where they diverge. This is
   evidence for the *ranking* flag (already shippable, default off,
   doesn't touch gates) independent of any cap decision.

6. **Recommendation heuristic** (`recommend()` in the script): for each
   simulated cap vs. the 0.95 baseline,
   - fewer than 5 newly-simulated resolved candidates -> `EXTEND WINDOW`
     (sample too thin to trust);
   - otherwise, `CANDIDATE TO RAISE CAP` only if PnL improves, win-rate
     doesn't regress by more than 5 points, **and** the gate-headroom
     channel count is exactly 0;
   - otherwise `HOLD`.

## Per-station / per-side breakdown (required by this issue, stage 2)

The clamp-saturation table (methodology item 2 above) already reports
clamped/total/rate **per side** and **per station** -- this is the
per-station, per-side saturation breakdown the issue asks for. The cap
simulation table (item 4) additionally reports trade-count / win-rate /
PnL **per cap value**, all on the NO side per the Constraint section (YES
is not the protected side and was out of scope for #551's shadow
question). No extension to the script was needed: it already computes
everything the issue's Deliverable section requires -- see
`clamp_saturation_stats()` and `simulate_cap_values()` in
`scripts/prob_cap_shadow_report.py`. What's missing is exclusively input
data, not report logic.

## Structural template (NOT real data -- do not cite these numbers)

For reviewers who want to see the exact section layout / table shape the
real report will have, here is the script's own output against this
environment's empty logs, with `--min-days 0` forced only to bypass the
self-gate for this preview. **Every number below is a structural zero
because zero rows of input exist here, not a finding.** Re-running against
production logs will replace every row of every table with real counts.

```
# Prob-cap shadow report -- 2026-07-10

Window: 0 distinct date(s) with p_yes_raw data since PR #564 deploy.
Deployed cap: 0.95 | Fixed NO-entry gate (MAX_CONFIDENCE_YES_FOR_NO, held constant across all simulations): 0.05

## Clamp saturation

- Overall: 0/0 (0.0%) candidates were clamped (p_yes_raw != p_yes).

| Side | Clamped | Total | Rate |
|---|---|---|---|

| Station | Clamped | Total | Rate |
|---|---|---|---|

## Distribution of p_yes_raw within the clamped population

No clamped candidates in this window.

## Cap simulation (NO side only -- protected side)

| Cap | Trades | Already-admitted | Newly-admitted | Unresolved-new | Win rate | Total PnL (c) | Edge-channel | Gate-headroom-channel |
|---|---|---|---|---|---|---|---|---|
| 0.95 | 0 | 0 | 0 | 0 | n/a | 0.0 | 0 | 0 |
| 0.97 | 0 | 0 | 0 | 0 | n/a | 0.0 | 0 | 0 |
| 0.98 | 0 | 0 | 0 | 0 | n/a | 0.0 | 0 | 0 |

## RANK_ON_RAW_PROB=true simulated ordering effect

- Polls with >=2 simultaneous NO candidates: 0
- Polls where raw-edge ranking would have preferred a different candidate than scan order: 0
- Realized PnL delta if raw-edge pick had been taken instead (sum over divergent polls): 0.0c
- Wins: raw-pick=0 capped-pick=0

## Recommendation

HOLD -- insufficient settled baseline data to compare cap values.
```

## Recommendation (this report, today)

**BLOCKED / HOLD pending data.** No cap change can be justified from this
environment. Concretely:

- Do **not** raise `MODEL_PROB_CAP`, change `MAX_CONFIDENCE_YES_FOR_NO`, or
  flip `RANK_ON_RAW_PROB`'s default off the back of this report -- there is
  no evidence in it, by design.
- Next action is operational, not code: run
  `python3 scripts/prob_cap_shadow_report.py --min-days 7` against the
  production host's real `logs/` directory (or copy those three files --
  `candidates.csv`, `settlements.csv`, `snapshots.jsonl`, since PR #564's
  ~2026-07-01 deploy -- into a session that has this repo checked out) and
  commit the resulting `backtest_results/prob_cap_shadow_<date>.md` over
  this one.
- Once real numbers exist, apply the same heuristic documented above
  (methodology item 6): treat any non-zero `gate_headroom_channel_count`
  as an automatic disqualifier for raising the cap, regardless of PnL,
  since that channel is specifically the "loosens the NO gate" case this
  issue's Constraint section prohibits.
- Issue #551 should stay **open** after this PR -- this is the evidence
  stage, not the decision stage (that's a separate follow-up per the
  issue's Implementation plan step 4).
