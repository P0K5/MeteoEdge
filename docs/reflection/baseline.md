# Prompt-Surface Baseline — before/after #748 restructure

Static measurement recorded 2026-07-20 (issue #748 acceptance criterion;
feeds issue #747's convergence metric). Bytes ≈ tokens × ~4.

## Always-loaded surface

| Surface | Before | After | Δ |
|---|---|---|---|
| CLAUDE.md (loaded every orchestrator turn) | 18,731 B (~4.7k tok) | 4,906 B (~1.2k tok) | **-74%** |
| PM role definition | 18,072 B (project-manager.md) | 4,031 B (agents/pm.md) | **-78%** |
| junior-dev spawn: agent def + spawn boilerplate | 9,415 + ~2,500 B | 2,932 + ~300 B (task block) | **-73%** |
| mid-dev spawn: agent def + spawn boilerplate | 9,010 + ~2,500 B | 2,894 + ~300 B | **-72%** |
| designer spawn: agent def + spawn boilerplate | 10,741 + ~2,000 B | 3,467 + ~300 B | **-70%** |

## Pay-per-use surface (loads only when invoked)

| File | Size |
|---|---|
| .claude/instructions/governance.md | 3,966 B |
| .claude/instructions/graphify.md | 1,543 B |
| skills: board-status / dev-workflow / pr-review / design-spec / create-issues / reflect | 2,365 / 2,503 / 4,247 / 2,251 / 2,146 / 4,898 B |

Until invoked, each skill costs only its name + description stub (~50 tokens).
The PR-review protocol (4.2 KB) now loads only when the PM actually reviews a
PR; the GraphQL reference (2.4 KB) only on a board transition.

## Dynamic metric — tokens per completed issue

The static numbers above are the fixed-overhead floor. The real convergence
metric is **tokens per completed issue**, measured from transcript `usage`
fields by `scripts/reflection_metrics.py` and appended to
`docs/reflection/metrics.jsonl`.

**Action:** run the metric on the first full epic executed on the new layout
and compare with a pre-restructure epic transcript. Every reflect-skill
proposal PR must state its expected impact against this number.
