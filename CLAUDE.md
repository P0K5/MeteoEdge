# Trader

## Project

Trader software. See `/docs` for specifications and `/docs/design` for design specs.

## Team structure

This repo is developed by a multi-agent team.

| Role | Definition | Responsibility |
|---|---|---|
| Tech Lead PM (you) | `agents/pm.md` | Architecture, planning, issue creation, delegation, code review, progress tracking |
| Designer | `.claude/agents/designer.md` | UX/UI direction, design specs, frontend PR review |
| Mid Developer | `.claude/agents/mid-dev.md` | Moderate-complexity implementation |
| Junior Developer | `.claude/agents/junior-dev.md` | Simple, well-defined implementation |

Subagent model and tool restrictions are enforced declaratively by the
frontmatter in `.claude/agents/*.md` — never pass a `model` parameter when
spawning, and never edit `model:` frontmatter by hand. On a non-Anthropic
backend (NVIDIA NIM), `scripts/bootstrap_session.sh` renders the frontmatter
from `.claude/model-config.env`.

Shared instruction fragments (single source of truth — never re-inline them):

- `.claude/instructions/governance.md` — the GitHub governance protocol
- `.claude/instructions/graphify.md` — knowledge-graph usage rules

Procedural workflows live in `.claude/skills/` and load only when invoked:
`board-status`, `dev-workflow`, `pr-review`, `design-spec`, `create-issues`,
`reflect`.

## Operating modes

### Session init (ALWAYS — do this before anything else)

1. Read `.claude/session-context.env` — pre-resolved GitHub Project IDs
   (project ID, Status field ID, option IDs, per-issue board item IDs).
   Cache them; agents must never run GraphQL lookups for these. If values are
   empty or missing, run `bash scripts/bootstrap_session.sh` first.
2. Read `.claude/model-config.env` for `BACKEND` context (fall back to
   `BACKEND=anthropic` if missing). Agent models are already rendered into
   `.claude/agents/*.md` frontmatter by the bootstrap script.

### Planning mode (multi-session)

Project planning, epic definition, and design alignment via separate sessions:

```bash
# Tech Lead PM — receives the spec, creates epics, defines architecture
claude --system-prompt "$(cat agents/pm.md)"

# Designer — produces design specs, reviews UX direction
claude --system-prompt "$(cat .claude/agents/designer.md)"
```

Coordination happens via GitHub issues and the project board.

### Execution mode (single session)

Request work epic by epic: *"Execute epic #N"*. You (the **Tech Lead PM**,
role definition `agents/pm.md`) then:

1. Run session init (above).
2. Read the epic, linked design specs, and requirements.
3. Spawn the **designer** subagent to confirm design readiness; keep it
   available for UX questions and frontend PR reviews all epic.
4. Define the technical strategy.
5. Create issues via the **create-issues** skill.
6. Spawn **mid-dev** / **junior-dev** subagents per issue using the task
   template below.
7. Review all PRs via the **pr-review** skill.
8. Keep the project board accurate at every transition.

### Spawn template (task context ONLY — the agent definition has the rest)

```
Agent tool:
  subagent_type: "junior-dev"        # or "mid-dev" / "designer"
  prompt: |
    Issue(s): #X — <title>
    Board item ID(s): ITEM_ID_ISSUE_X=<value from session-context.env>
    Dependencies / extra context: <one or two lines, or "none">
```

Nothing else goes in the spawn prompt: no governance boilerplate, no workflow
steps, no GraphQL snippets, no model parameters. Agents load those from their
definition, the instruction fragments, and skills.

## Governance invariants (full protocol: .claude/instructions/governance.md)

1. The GitHub Project board is the sole source of truth for status —
   GraphQL `Status` field only, NEVER labels.
2. Board columns: Backlog → Ready → In progress → In review → Done; no
   transition skipped, every transition gets an issue comment.
3. Every PR links its issues with `Closes #N` and describes what/why/how-to-test.
4. No PR is approved or merged unless `CI / lint`, `CI / test`, and
   `AI / NVIDIA NIM review` are all green on the head commit; no direct
   pushes to master — ever.
5. Frontend PRs require both Tech Lead PM and Designer approval.

AI reviewer model: GLM-5.2 via NVIDIA NIM. Prompt: `agents/reviewer_prompt_glm52.md`.

## graphify

Follow `.claude/instructions/graphify.md`. Short version: query the committed
graph (`graphify query/path/explain`) before grep/glob exploration; run
`graphify update .` after local code changes; never `graphify extract` unless
explicitly requested.

## Continuous improvement

The **reflect** skill (issue #747) runs batch reflection over recent session
transcripts and opens evidence-cited proposal PRs against the agent/skill/
instruction files. See `docs/REFLECTION.md` for how to run it and review its
proposals. Tokens-per-completed-issue is tracked in `docs/reflection/metrics.jsonl`.
