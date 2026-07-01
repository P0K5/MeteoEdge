# Audit 2026-07-01 — Implementation Plan

Response to the external audit (data/PnL/gaps review, 2026-07-01). All findings were
verified against the codebase before issue creation; every issue cites the exact
file:line evidence. Issues carry the `2026-07-01` title prefix.

## The meta-pattern the plan optimizes for

The audit's core diagnosis: the last two weeks built excellent *inputs* (GRIB channels,
Edge tab, capture infra), but the chain from inputs to orders is severed at three
joints — the probability clamp, the duplicate gfs channel, and the label gaps.
This plan spends the week on those joints before any new data source.

## Issues created

### P0 — must land before DEB activation (~July 2-3)

| # | Title (short) | Complexity | Key evidence |
|---|---|---|---|
| #548 | gfs channel is a byte-identical duplicate of open_meteo | Mid | `src/data/open_meteo.py:162-181`; 1,140 identical DB rows; no group_id (`deb_weighting.py:100-101`) |
| #549 | envelope reads DEB_ENABLED from env, not live config | Simple | `src/model/envelope.py:91` |
| #550 | Honest group_id assignments for correlated channels | Mid | open_meteo is a 4-model blend incl. ECMWF+GFS, ungrouped |
| #551 | MODEL_PROB_CAP saturation — p_yes clamps at 0.05/0.95 | Complex | 158/159 candidates saturated; `config.py:346`; cap_applied ~169k/wk |

### P1 — this week

| # | Title (short) | Complexity |
|---|---|---|
| #552 | deb_weight_log stale since June 26 (Edge tab shows old weights) | Mid |
| #553 | refresh_weights hardcodes rmse=0.0 — log real RMSE | Simple |
| #554 | Low-side scanner has never recorded a shadow trade | Mid |
| #555 | EMOS sigma inputs: GEFS floor at 1.00°F + NULL sigma_f | Mid |
| #556 | EMOS min_samples configurable; shadow-only fit at 30-40 days | Simple |
| #557 | Promote ZGGG + EGLC shadow → live NO-only | Simple |

### P2 — Backlog

| # | Title (short) | Complexity |
|---|---|---|
| #558 | Per-station verification-source policy; WSSS→MSS; ZSJN dead feed | Complex |
| #559 | Statistical promotion bar (Wilson bound, ≥30 trades) — extends #80 | Mid |
| #560 | JMA AMeDAS collector future-timestamp 404s | Simple |
| #561 | Short-lead (3-6h) errors for intraday DEB weighting | Complex |

## Dependency graph

```
#548 (gfs duplicate)  ──►  #550 (group_ids)  ──►  DEB activation safe
#549 (DEB_ENABLED)    ──►  DEB activation real
#553 (real RMSE)      ──►  observable DEB activation
#552 (weight log)     ──►  Edge tab trustworthy during activation
#551 (prob cap)       ──►  shadow window (≥7d) ──► cap decision (separate sign-off)
#555 (sigma capture)  ──►  #556 (EMOS shadow fit, ~late July)
#557 (promotions)     ──►  volume recovery now; #559 prevents the next ad-hoc one
#554 (low-side bug)   ──►  unblocks epic #452 sub-issues (#458/#459/#460)
```

## Sequencing (day-by-day intent)

- **Day 1-2 (Jul 1-2):** #548 + #549 (the two DEB-activation blockers), #553 in parallel.
  #551 step 1 (dual p_yes logging) starts immediately — its shadow window is the long pole.
- **Day 3-4:** #550 on top of #548; #552; #557 (config-only promotion).
- **Day 5-7:** #554 diagnosis; #555; #556.
- **Backlog, pull when capacity allows:** #558 (land its training-exclusion flag early if
  cheap), #559, #560 (junior-friendly), #561 (design note first; only valuable after #551).

## Cross-references to existing issues

- #420 (post-EMOS cap cleanup) stays open — #551 is the *now* fix, #420 the eventual removal.
- #80 (promotion criteria) is extended/superseded by #559 — decision recorded in that PR.
- #452/#458/#459/#460 (low-side epic) are blocked by bug #554.
- #226/#449 (EMOS) benefit from #555/#556.

## Guardrails carried through every issue

- NO is the protected side: nothing loosens a NO gate without a shadow-validated report
  (#551 keeps the live entry gate on capped p; #557 is NO-only).
- New strategy params go through `CONFIG_DEFAULTS` + `_CONFIG_META` + dashboard live-read,
  never env-only.
- Reduced-sample EMOS fits can never set `ready_for_promotion=1` (#556 hard guardrail).

## Board registration

The session that created these issues could not execute Projects-v2 GraphQL mutations
(proxy restriction). Run once with a project-scoped PAT:

```bash
GH_TOKEN=<pat> bash scripts/board_add_audit_issues.sh
```

This adds #548-#557 with Status **Ready** and #558-#561 with Status **Backlog**, then
trigger the "Refresh Session Context" workflow to pick up the new item IDs.
