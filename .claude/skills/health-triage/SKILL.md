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

It is a full `scp`, not a delta sync, and the files are large (~250 MB each),
so a successful run takes **~4 minutes** (measured 249 s on 2026-07-31). Run it
**once**, at the very start of a triage session, before you begin analysis —
never per query, and never again later in the same session.

Then, **whether or not the sync ran**, measure staleness from the data itself:

```python
import sqlite3, os, datetime
c = sqlite3.connect('file:data/meteoedge.db?mode=ro', uri=True)
print('max poll_ts :', c.execute('SELECT max(poll_ts) FROM scan_decisions').fetchone()[0])
print('bot.log mtime:', datetime.datetime.fromtimestamp(
    os.path.getmtime('logs/bot.log'), datetime.UTC).strftime('%Y-%m-%d %H:%M'))
print('now (UTC)   :', datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M'))
```

Healthy cadence is ~256 polls/day, so a gap beyond ~15 minutes is real.

### A stale copy and a dead bot look identical — do not confuse them

A gap between `now` and `max(poll_ts)` has two completely different causes, and
guessing wrong is expensive in both directions:

| Sync succeeded this session? | Data still old means | Action |
|---|---|---|
| Yes | The **bot genuinely stopped polling** — a real, probably P0 finding | Investigate and file |
| No / not attempted | **Unknown.** Could be a stale local copy, could be an outage | Do **not** file an outage issue |

Never open a "bot is down" or "data collection stopped" issue from a snapshot
you did not just sync — a false P0 costs the user a cleanup. Say instead:
"the local copy ends at *T*; I could not sync, so I cannot distinguish a stale
copy from an outage — please run `Fetch-RemoteData.ps1` if you want that
confirmed."

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
