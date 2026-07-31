---
name: health-triage
description: Tech Lead PM procedure for triaging a daily health report, bot log errors, or a "look at the data and tell me what's broken" request into verified, correctly-scoped issues. Use when the user pastes a health report, points at logs/ and data/, or asks whether an observed metric or alert is a real bug.
---

# Health Report & Production Bug Triage (Tech Lead PM)

This is the most common non-epic session type. The failure mode is **not**
missing the bug — it is filing a confidently-worded issue with the wrong root
cause, then having to post a correction and re-scope it. Verify against
production data *before* `gh issue create`, not after.

## 1. Orient before probing

1. `graphify query "<the metric or subsystem the report names>"` — find which
   module computes the number before reading any source file.
2. `git log --oneline -25` — a metric that changed sign or magnitude overnight
   usually tracks a merge, not a production fault.
3. Check what the report *itself* claims vs what it *measures*. Several past
   P0s were bugs in `src/scripts/daily_health_report.py`, not in the bot.

## 2. Query production data read-only

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

## 3. Confirm the root cause before filing

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

## 4. File and register

Group findings by root cause, not by symptom — one issue per defect, even when
it surfaced as three report lines. Then follow **create-issues** for structure
and board registration, and label severity honestly (a silent data-loss bug
outranks a cosmetic metric bug).

Report back to the user with: what is genuinely broken, what is
known-noise, what is now tracked (issue numbers), and what needs no action.
