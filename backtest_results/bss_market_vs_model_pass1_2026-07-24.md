# Market-vs-Model Skill Test -- Pass 1 status (issue #822)

**Date:** 2026-07-24
**Status:** Tool built and unit-tested. **Pass 1 has NOT been executed against
real data in this environment** -- see "Blocker" below. This file is a status
note, not a findings report; it must not be read as, or cited as, a Pass-1
result.

---

## What this is

Per `docs/REMEDIATION_PLAN.md` ("M1 -- Fast verdict") and issue #822, Pass 1
runs the Brier Skill Score test retrospectively on the 37 days of archived
`logs/candidates.*.csv.gz` (gate-selected, pre-#820-fix data) that already
exist, without waiting for the clean forward data #826/#820 will produce.

The tool that runs this test is implemented and unit-tested in this PR:

- `src/scripts/bss_market_vs_model_report.py`
- `src/tests/test_bss_market_vs_model_report.py` (33 tests, synthetic
  fixtures only)

It implements, exactly per the issue and the decision-gate spec in
`docs/REMEDIATION_PLAN.md`:

- `BS_model = mean((p_yes_raw - outcome)^2)`,
  `BS_market = mean((p_market_yes - outcome)^2)`, `BSS = 1 - BS_model/BS_market`.
- Exclusions: `p_yes_raw` missing, `p_yes_raw == 0.0` (the #820 certainty-shortcut
  artifact), missing market price, and 1c/99c rail rows on either side of
  the book.
- De-duplication to one row per (station, ticker, settlement date) -- the
  final (lowest `minutes_to_settlement`) poll, mirroring
  `src/scripts/calibration_report.py::pick_samples`.
- Outcome join against the `settlements` table (`meteoedge.db`), keyed by
  ticker.
- Segmentation by same-day vs. next-day (derived from station-local `ts`
  date vs. `end_date`, since the CSV carries no `is_next_day` column) and by
  UTC-offset bucket (derived from `STATION_TZ` at `ts`).
- Reliability tables and sharpness histograms for both the model and the
  market-implied probability.
- Self-gating: it never writes a report unless it has both real candidate
  rows AND real settlement outcomes to join them against -- confirmed in
  this exact sandbox (see below).

## Blocker: no real archived data reachable in this environment

This development/agent environment has **no `logs/` directory at all** (it
is correctly gitignored; the 37 days of `candidates.*.csv.gz` referenced in
the issue live on the production bot host) and **no `data/meteoedge.db`**
(only `data/.gitkeep` is checked in). `scripts/fetch_remote_data.sh` /
`Fetch-RemoteData.ps1` (added 2026-07-24) can pull both from the host over
SSH, but require `REMOTE_HOST` / `REMOTE_USER` / `REMOTE_KEY_PATH`
credentials in `.env` that are not present in this session.

Running the tool here today confirms the self-gate does exactly what it is
supposed to -- no fabricated result, just an honest log line:

```
$ python -m src.scripts.bss_market_vs_model_report
INFO __main__: [bss] no rows found under logs/candidates.csv (rotated sources)
-- nothing to score. This is expected in a fresh checkout / dev sandbox;
logs/ is gitignored and lives on the bot host. Not writing a report.
```

**This PR deliberately does not fabricate or simulate Pass-1 numbers.**
Unlike some existing backtest scripts in this repo that synthesize a proxy
when a genuinely new data source has no logged history yet (e.g.
`ecmwf_icon_backtest.py`), there is nothing legitimate to proxy for a
Brier-skill decision-gate input that already has 37 real days sitting on the
host -- synthesizing a number here would risk it being mistaken for the real
read on a question the plan explicitly says is "fixed in advance so it
cannot be rationalised after the number is seen."

### What is needed to unblock

One of:

1. Grant this session (or a follow-up session) access to the production
   host's `logs/candidates.*.csv.gz` and `data/meteoedge.db`, e.g. via
   `scripts/fetch_remote_data.sh` with real `REMOTE_*` credentials, then
   re-run:
   ```
   python -m src.scripts.bss_market_vs_model_report \
       --candidates-csv logs/candidates.csv --db data/meteoedge.db \
       --out backtest_results
   ```
2. Run the same command directly on/from the bot host and hand back the
   generated `backtest_results/bss_market_vs_model_pass1_<date>.md` for
   review as a follow-up PR.

Either path produces the actual Pass-1 report this file is a placeholder
for. **Escalated to the Tech Lead PM via the issue comment on #822.**
