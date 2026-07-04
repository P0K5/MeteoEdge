## Linked issue
Closes #

## What changed
<!-- Brief summary of what this PR does -->

## How to test
<!-- Steps to verify the change works -->

## How to deploy
<!--
State exactly what an operator must do to ship this change to the running host.
Check every line that applies and fill in the <angle-bracket> details; delete lines that don't.
If more than "code only" applies, spell out the ORDER of steps.
-->
- [ ] **Code only** — `git pull` + restart these services: `<e.g. meteoedge-run, meteoedge-dashboard, meteoedge-settle.timer>`
- [ ] **Dependencies changed** — run `uv pip install -r requirements.txt --system` (or the venv equivalent) before restart
- [ ] **DB schema change** — migration applies automatically on service start / needs a manual step: `<describe>`
- [ ] **One-off script(s)** to run after deploy: `<.venv/bin/python -m src.scripts.NAME — dry-run first if supported>`
- [ ] **Config / env change** — set or update: `<VAR=value, which config, default>`
- [ ] **Post-deploy validation** — how to confirm it took: `<log line to watch, sanity query, dashboard panel>`
- [ ] **No deploy needed** (docs / tests / CI only)

## Checklist
- [ ] Tests pass locally (`pytest src/tests/ -v`)
- [ ] No secrets or credentials committed
- [ ] PR body includes `Closes #N` linking the issue
- [ ] Project board status updated (In review)
- [ ] Docs updated if this PR adds/changes env vars, DB schema, API endpoints, or run modes
- [ ] **How to deploy** section filled in (or "No deploy needed" checked)
