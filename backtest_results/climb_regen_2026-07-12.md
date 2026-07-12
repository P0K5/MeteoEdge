# Climb-table regeneration report — 2026-07-12 (#669 host step)

Regenerated on the production host with `python scripts/build_climb_lookup.py --from-db`
after PR #715 (issue #669) taught the builder to consume the 24/7 city feeds
(Seoul AMOS, Busan, Tokyo AMeDAS, Singapore MSS) and decoupled METAR persistence
from `STATION_ACTIVE_HOURS`.

## Before / after: DB-derived cell coverage per station

288 cells = 12 months × 24 hours. With ~2 months of collected history the
theoretical maximum is 48 cells (2 months × 24 h). Cells not covered by DB
history use the synthetic climatological fallback, as before.

| Station | Cells before (#585-era tables) | Cells after this regen | Note |
|---|---|---|---|
| RKSI  | 12 | 48 | city feed (Seoul AMOS) — hit theoretical max |
| RKPK  | 12 | 48 | city feed (Busan) — hit theoretical max |
| RJTT  | 17 | 48 | city feed (Tokyo AMeDAS) — hit theoretical max |
| WSSS  | 17 | 48 | city feed (Singapore MSS) — hit theoretical max |
| KORD  | 18 | 36 | METAR, accumulated history |
| KMIA  | 18 | 35 | METAR |
| KLAX  | 18 | 35 | METAR |
| KATL  | 18 | 36 | METAR |
| KHOU  | 18 | 34 | METAR |
| WMKK  | 17 | 34 | METAR |
| ZGSZ  | 12 | 24 | 2-hourly METAR (training-excluded per #558; climb cells still usable) |
| MPMG  | 17 | 34 | METAR |
| EGLC  | 18 | 35 | METAR |
| LFPB  | 17 | 34 | METAR |
| LIMC  | 18 | 35 | METAR |
| EFHK  | 18 | 36 | METAR |
| EPWA  | 17 | 34 | METAR |
| LTFM  | 18 | 35 | METAR |
| LTAC  | 18 | 35 | METAR |
| RCSS  | 17 | 34 | METAR |
| ZSPD  | 12 | 24 | 2-hourly METAR |
| ZGGG  | 12 | 24 | 2-hourly METAR |
| ZHHH  | 12 | 24 | 2-hourly METAR |
| ZSJN  | 0  | 0  | dead feed (#558/#694) — fully synthetic, as expected |
| ZHCC  | 12 | 24 | 2-hourly METAR |
| RPLL  | 17 | 34 | METAR |
| LLBG  | 18 | 36 | METAR |
| OEJN  | 17 | 34 | METAR |
| SBGR  | 17 | 34 | METAR |
| NZWN  | 17 | 34 | METAR |

Headline: the four city-feed stations jumped to the theoretical coverage maximum
(48/48 possible cells) — the intended effect of #669. All other stations roughly
doubled via accumulated history. Every station still warns `< 10 days` for most
month×hour cells (expected at ~2 months of history); those cells retain the
synthetic fallback.

## Per-station hour-6 p95 climb (°F) by month, post-regen

```
Station  M01 M02 M03 M04 M05 M06 M07 M08 M09 M10 M11 M12
EFHK      5.3  7.1  8.8 12.4 15.9 20.8 19.5 15.9 12.4  8.8  7.1  5.3
EGLC      7.2  9.0 10.8 12.6 14.4 22.6 23.1 16.2 14.4 10.8  9.0  7.2
EPWA      7.1  8.8 12.4 15.9 17.7 27.0 16.2 19.5 15.9 12.4  8.8  7.1
KATL     20.0 21.0 23.0 24.0 23.0 18.1 19.5 21.0 22.0 23.0 21.0 19.0
KHOU     19.0 20.0 21.0 22.0 22.0 15.8 20.0 20.0 20.0 21.0 20.0 19.0
KLAX     18.0 19.0 18.0 20.0 18.0 10.6 10.9 16.0 18.0 20.0 19.0 18.0
KMIA     16.0 17.0 18.0 18.0 17.0 14.9 13.8 14.0 14.0 16.0 17.0 16.0
KORD     17.7 18.7 21.6 23.6 24.6 20.1 18.2 23.6 22.6 21.6 18.7 16.7
LFPB      9.0 10.8 14.4 18.0 19.8 32.6 32.1 21.6 18.0 14.4 10.8  9.0
LIMC     10.8 12.6 16.2 18.0 19.8 24.4 30.4 23.4 19.8 16.2 12.6 10.8
LLBG     10.8 10.8 12.6 14.4 14.4 15.4 15.9 12.6 14.4 14.4 12.6 10.8
LTAC     12.6 14.4 18.0 19.8 21.6 32.4 32.4 27.0 25.2 19.8 16.2 12.6
LTFM      9.0  9.0 10.8 12.6 14.4 12.6 19.3 16.2 14.4 12.6 10.8  9.0
MPMG     10.0 11.0 11.0 10.0  9.0 14.5 13.5  9.0  9.0  9.0  9.0 10.0
NZWN     10.8 10.8 10.8  9.9  9.0 12.6 10.6  9.0  9.9 10.8 10.8 10.8
OEJN     18.0 18.0 18.0 16.2 16.2 21.6 16.2 14.4 14.4 16.2 16.2 18.0
RCSS     10.8 10.8 10.8 10.8 10.8 18.0 16.9 12.6 12.6 12.6 10.8 10.8
RJTT     14.4 14.4 14.4 14.4 12.6 14.9 13.1 12.6 12.6 12.6 14.4 14.4
RKPK     12.8 13.8 15.7 17.7 17.7 19.3 17.6  9.8 12.8 15.7 13.8 11.8
RKSI     13.8 14.7 17.7 19.7 19.7 17.6 16.6 10.8 14.7 17.7 14.7 12.8
RPLL     10.8 10.8 12.6 12.6 10.8 14.9 11.7  9.0  9.0  9.0  9.0 10.8
SBGR     16.2 16.2 16.2 16.2 16.2 25.6 30.6 18.0 18.0 16.2 16.2 16.2
WMKK     12.0 13.0 13.0 12.0 12.0 14.4 15.9 12.0 12.0 12.0 11.0 11.0
WSSS      9.0  9.0  9.0  9.0  9.0  9.7  7.0  9.0  9.0  9.0  9.0  9.0
ZGGG     14.0 13.0 12.0 11.0 11.0 10.0 10.0 10.0 11.0 12.0 13.0 14.0
ZGSZ     14.0 13.0 12.0 11.0 11.0 10.0 10.0 10.0 11.0 12.0 13.0 14.0
ZHCC     16.2 18.0 19.8 21.6 21.6 19.8 16.2 16.2 18.0 19.8 18.0 16.2
ZHHH     14.4 14.4 16.2 16.2 16.2 14.4 14.4 14.4 16.2 16.2 14.4 14.4
ZSJN     16.2 18.0 19.8 21.6 21.6 19.8 16.2 16.2 19.8 19.8 18.0 16.2
ZSPD     12.6 12.6 12.6 12.6 12.6 10.8 10.8 10.8 12.6 14.4 14.4 12.6
```

## Values worth a reviewer's glance

- **LFPB Jun/Jul (32.6 / 32.1)** and **LTAC Jun/Jul (32.4 / 32.4)** are well above
  their synthetic neighbours (~18–22). These are DB-derived p95s from the current
  European summer — plausibly genuine heat-wave-era morning-climb extremes, but
  they widen `max_env` for those stations in summer mornings. Flagged for review
  rather than silently merged.
- **RKPK/RKSI August (9.8 / 10.8)** sit *below* their synthetic neighbours —
  monsoon-season cloud damping is plausible; same rationale for a glance.
- ZGSZ/ZGGG identical rows: both fall back to the same synthetic profile for most
  cells (2-hourly METAR gives thin coverage), so similarity is expected, not a bug.

## Downstream consumer

The September blanketing re-analysis (#591) compares candidate behaviour on
old-vs-new tables for RKSI/RKPK/ZGSZ/ZGGG; the old tables remain available in git
history at `src/data/climb_lookup.py` prior to this commit.
