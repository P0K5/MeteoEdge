# GitHub Governance Protocol — SINGLE SOURCE OF TRUTH

This is the only copy of the governance protocol. Agent definitions, skills, and
CLAUDE.md reference this file — do not re-inline its contents anywhere else
(CI enforces this via `scripts/check_prompt_drift.sh`).

Applies to ALL agents. Violations are treated as bugs that must be fixed immediately.

## Tokens

- **Tech Lead PM:** use `GITHUB_TOKEN_SUPERVISOR` for all GitHub API calls.
- **Everyone else (Designer, Mid Dev, Junior Dev):** use `GITHUB_TOKEN_OPERATIONAL`.
- Never swap tokens between roles. No exceptions.

## Status management rules

1. The **GitHub Project board** is the **sole** source of truth for workflow status.
2. Board columns: **Backlog → Ready → In progress → In review → Done**
3. Status is managed via the `Status` field (GraphQL `updateProjectV2ItemFieldValue`),
   **NEVER via labels**.
4. Labels are for **categorization only**: `epic`, `backend`, `frontend`, `bug`,
   `Simple`, `Mid`, `Complex`.
5. **No status transition may be skipped.** If a status is wrong, fix it immediately.

## Mandatory status transitions

| Event | Who updates | New status |
|---|---|---|
| Issue created and added to board | Tech Lead PM | **Ready** |
| Developer starts work | Developer | **In progress** |
| Developer opens PR | Developer | **In review** |
| Reviewer requests changes | Reviewer (Tech Lead PM) | **In progress** |
| Developer pushes fixes, re-requests review | Developer | **In review** |
| PR merged (auto-closes issue) | _(automatic)_ | **Done** |

Every status transition must be accompanied by a comment on the issue.
No silent status changes. For the exact GraphQL mutation and comment
templates, invoke the **board-status** skill.

## Issue and PR linking rules

1. **Every PR** must reference its issues with closing keywords: `Closes #N`,
   `Fixes #N`, or `Resolves #N`. PRs without issue links will be rejected.
2. **Every PR description** must include:
   - What issue(s) it addresses (with `#N` references)
   - What changed (brief summary)
   - How to test the changes
3. **Issue comments** are mandatory at these checkpoints:
   - Starting work: `"Starting work — plan: [steps]"`
   - PR opened: `"PR #N submitted for review — [brief summary]"`
   - Blocked: `"Blocked: [clear description]"`
   - Addressing feedback: `"Addressing review feedback: [summary]"`
4. **Non-blocking improvements** found during PR review must be logged as new
   issues in **Backlog** — never left as PR comments only.

## PR merge gate — CI must be green (NON-NEGOTIABLE)

A PR may NEVER be approved or merged unless ALL of the following are true:

1. **All CI checks pass** — `CI / lint`, `CI / test`, and `AI / NVIDIA NIM review`
   must show green on the PR's **head commit**. Verify via
   `mcp__github__actions_list` (or equivalent) before approving.
2. **No direct pushes to master** — every change, including one-line hotfixes,
   goes through a PR. Branch protection enforces this; do not try to bypass it.
3. **Approval comes after CI is green** — if CI is still running, wait. If CI is
   red, the author must fix it first; never approve in anticipation of a fix.

## Frontend PRs

Any PR touching UI components, layouts, styles, or user-facing text requires
**both** Tech Lead PM (technical) and Designer (UX) approval before merge.
Designer change requests are blockers.

## Governance checklist (every agent, every task)

Before considering any task complete, verify:

- [ ] Project board status reflects the current state of every touched issue (via GraphQL)
- [ ] All PRs reference issues with closing keywords (`Closes #N`)
- [ ] PR description includes: what changed, why, how to test
- [ ] Issue comments posted at: work start, PR submission, any blockers
- [ ] Frontend PRs have both Tech Lead PM and Designer as reviewers
- [ ] No labels were used for status tracking
- [ ] Non-blocking review suggestions logged as new issues in Backlog
