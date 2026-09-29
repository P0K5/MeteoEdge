---
name: health-triage
description: Tech Lead PM procedure for triaging a daily health report, bot log errors, or a "look at the data and tell me what's broken" request into verified, correctly-scoped issues. Use when the user pastes a health report, points at logs/ and data/, or asks whether an observed metric or alert is a real bug.
---

# Health Report & Production Bug Triage (Tech Lead PM)

This is the most common non-epic session type. The failure mode is **not**
missing the bug — it is filing a confidently-worded issue with the wrong root
cause, then having to post a correction and re-scope it. Verify against
production data *before* `gh issue create`, not after.

## 1. Refresh the data — then establish how fresh it actually is

`logs/` and `data/` are **local copies of the production server's files**, not
the server itself. They are only as current as the last sync.

Try the sync first — it succeeds slowly but fails fast, so attempting it is
always the right opening move:

```powershell
powershell -File scripts\Fetch-RemoteData.ps1
```

(Use `powershell`, not `pwsh` — this machine has Windows PowerShell 5.1 only.)

**If it fails, do not retry it and do not debug SSH.** It needs `REMOTE_*` in
`.env` plus the key at `REMOTE_KEY_PATH`, which exist only when the user is
running locally on their desktop. From phone/web sessions there is no SSH
access to the server and the script exits 1 within a second — that is expected,
not a fault to fix. Note it and carry on with the snapshot you have. (Its
docstring says it "blocks agent runs without data"; treat a non-zero exit as
*proceed with stale data, clearly labelled*, not as *abort*.)

It is an incremental sync (since 2026-09-27): files already held identically are
not re-transferred, append-only logs resume from where the local copy ends, and
databases arrive as digest-verified SQLite snapshots with only their changed
4 MiB blocks on the wire. A steady-state run takes **~15-60 s** (measured 13.7 s
for `meteoedge.db` + logs on 2026-09-27; it was ~4 minutes before). Still run it
**once**, at the very start of a triage session, before you begin analysis —
never per query, and never again later in the same session.

Two consequences for triage:

- **`data/*.db` are now trustworthy.** They are `sqlite3` snapshots taken inside
  a read transaction, `integrity_check`-clean by construction and verified by
  digest on arrival, so a SQLite error reading one is a real finding, not the
  torn copy it used to be. `-wal`/`-shm` sidecars are never fetched, and stale
  local ones are deleted — do not go looking for them.
- **Narrow the sync when you only need part of it**, e.g.
  `powershell -File scripts\Fetch-RemoteData.ps1 -Only 'data/meteoedge.db,logs/bot.log'`
  or `-SkipDatabases` for a logs-only refresh. `-DryRun` reports what would move
  without transferring anything.

Then, **whether or not the sync ran**, measure staleness from the data itself —
across every table the bot must keep writing to, not just one. A single-table
probe is how #1237 happened: `scan_decisions` stopped receiving writes for a
month while `candidates` and `poll_runs` stayed current, and the old version of
this snippet checked only `scan_decisions`, so it read as a dead bot when the
bot was polling fine. This list mirrors `TABLE_LIVENESS_SPECS` in
`src/scripts/daily_health_report.py` — if that list changes, update this one
too:

```python
import sqlite3, os, datetime
c = sqlite3.connect('file:data/meteoedge.db?mode=ro', uri=True)
now = datetime.datetime.now(datetime.UTC)
for table, col in [
    ('poll_runs', 'poll_ts'),            # unconditional per-poll heartbeat
    ('candidates', 'ts'),
    ('scan_decisions', 'poll_ts'),       # the table that died in #1237
    ('observations', 'ts'),
    ('model_forecast_log', 'logged_at'),
]:
    newest = c.execute(f'SELECT max({col}) FROM {table}').fetchone()[0]
    print(f'{table:<20} max {col}: {newest}')
print('bot.log mtime:', datetime.datetime.fromtimestamp(
    os.path.getmtime('logs/bot.log'), datetime.UTC).strftime('%Y-%m-%d %H:%M'))
print('now (UTC)   :', now.strftime('%Y-%m-%d %H:%M'))
```

Healthy cadence is ~256 polls/day for `poll_runs`/`candidates`, hourly for
`observations`, daily for `model_forecast_log`, and `scan_decisions` should
track within a few hours of `now` whenever the bot is evaluating anything — a
gap beyond ~15 minutes on `poll_runs` is real; see `TABLE_LIVENESS_SPECS` for
the other tables' per-table thresholds. The daily health report now runs this
same check unattended (issue #1237's "Table Liveness" section), so a WARN
there is the same finding surfaced automatically — corroborate against it
before filing.

### A stale copy and a dead bot look identical — do not confuse them

A gap between `now` and one table's newest row has causes that must not be
guessed at, because each points somewhere different:

| Sync succeeded this session? | Which tables are stale | Data still old means | Action |
|---|---|---|---|
| Yes | **All** of them, together | The **bot genuinely stopped polling** — a real, probably P0 finding | Investigate and file |
| Yes | **One** table, others current (e.g. `scan_decisions` alone) | A **write-path bug in that one table** — not an outage, the bot is running fine (this is exactly #1237) | File against that table's write path, not as a bot-down incident |
| No / not attempted | Any | **Unknown.** Could be a stale local copy, could be an outage | Do **not** file an outage issue |

Never open a "bot is down" or "data collection stopped" issue from a snapshot
you did not just sync — a false P0 costs the user a cleanup. Say instead:
"the local copy ends at *T*; I could not sync, so I cannot distinguish a stale
copy from an outage — please run `Fetch-RemoteData.ps1` if you want that
confirmed." Likewise, never file "bot is down" when only one table in the list
above is stale and the rest are current with a fresh sync — that is a targeted
write-path defect in one table, the same shape as #1237, and scoping it as an
outage sends the fix to the wrong place.

### Stamp every conclusion with the as-of time

Lead the report with the snapshot time and the gap, e.g. *"as of the
2026-07-31 20:19 UTC snapshot (36 min old, synced this session)"*. A health
verdict without an as-of timestamp is how "Healthy" gets reported over two days
of silent data loss.

Working from a stale snapshot is still worthwhile — historical patterns,
calibration, model behaviour, backtest results, a metric's definition in the
source, and GitHub issues/PRs are all fully analysable. Only claims about
*current* liveness require a fresh sync. Scope the conclusions, don't refuse
the work.

## 2. Orient before probing

1. `graphify query "<the metric or subsystem the report names>"` — find which
   module computes the number before reading any source file.
2. `git log --oneline -25` — a metric that changed sign or magnitude overnight
   usually tracks a merge, not a production fault.
3. Check what the report *itself* claims vs what it *measures*. Several past
   P0s were bugs in `src/scripts/daily_health_report.py`, not in the bot.

## 3. Query production data read-only

Always open the DB read-only — an analysis session must never be able to write
to the live database:

```python
import sqlite3
c = sqlite3.connect('file:data/meteoedge.db?mode=ro', uri=True)
q = lambda s, *a: c.execute(s, a).fetchall()
```

`sqlite3.connect('data/meteoedge.db')` (read-write, and it will create the file
if the path is wrong) is a defect in an analysis script — do not use it.

`logs/bot.log` is large (100 MB+). Seek from the end rather than reading it
whole, and pick the window from the question up front instead of re-scanning
with a bigger window each time:

```python
import os
p = 'logs/bot.log'; sz = os.path.getsize(p)
f = open(p, 'rb'); f.seek(max(0, sz - 30_000_000))
data = f.read().decode('utf-8', 'replace')
```

Rule of thumb: ~2 MB ≈ last hour, ~30 MB ≈ last day, ~120 MB ≈ several days.
Per-bracket detail lives in `logs/bracket_evals.<date>.jsonl`, not in `bot.log`.

## 4. Confirm the root cause before filing

For every candidate bug, state the mechanism and then prove it holds in the
data. Minimum bar before `gh issue create`:

- [ ] The symptom is reproduced from the DB or logs, not just from the report.
- [ ] The suspect code path is read and confirmed to produce that symptom.
- [ ] A **counter-check** rules out the obvious alternative explanation
      (a clamp, a threshold that can never fire, a timezone, a metric that
      measures a different population than its label says).
- [ ] The blast radius is known: how many rows/stations/days, since when.

A metric reading `0.0%` or exactly at a bound is more often an unfireable
threshold or a saturated clamp than a fixed bug. Verify which.

If you file first and the diagnosis then changes, post the correction on the
issue and re-scope it explicitly — do not silently edit the description.

## 5. File and register

Group findings by root cause, not by symptom — one issue per defect, even when
it surfaced as three report lines. Then follow **create-issues** for structure
and board registration, and label severity honestly (a silent data-loss bug
outranks a cosmetic metric bug).

Report back to the user with: the snapshot's as-of time and whether it was
synced this session, what is genuinely broken, what is known-noise, what is now
tracked (issue numbers), and what needs no action.
