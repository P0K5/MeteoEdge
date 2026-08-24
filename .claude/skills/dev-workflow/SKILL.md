---
name: dev-workflow
description: The mandatory issue-to-PR workflow for developers (Mid and Junior). Use when starting work on an assigned GitHub issue, before opening a PR, or when responding to review feedback.
---

# Developer Workflow — Issue to PR

Follow this exact sequence for every assigned issue. Steps marked (G) are
governance steps — mandatory, never skipped. Status transitions and comment
templates: invoke the **board-status** skill. Full protocol:
`.claude/instructions/governance.md`.

1. **Read the issue completely** — acceptance criteria, technical notes,
   test requirements, dependencies.
2. **Find examples** — look at 2–3 similar implementations in the codebase and
   note the patterns (use graphify per `.claude/instructions/graphify.md`).
3. **(G) Post your plan** — comment on the issue: "Starting work — plan: [2–4 steps]".
4. **(G) Move the issue to In progress** on the board (board-status skill).
5. **(G) Enter your isolated worktree — MANDATORY FIRST git action, before any
   file edit or commit.** Never work in the shared checkout: concurrent spawns
   there branch off each other's moving HEAD and corrupt commits (issue #800,
   PR #796). Run, from the repo root:

   ```bash
   WT=$(bash scripts/ensure_worktree.sh <branch-name>) || { echo "Blocked"; exit 1; }
   cd "$WT"
   ```

   Branch naming:
   - Junior: `junior/<issue-number>-<short-description>`
   - Mid: any descriptive name, linked to ALL related issues

   The helper creates `.claude/worktrees/agent-<branch>` off `origin/master`,
   verifies the worktree is isolated (its toplevel is **not** the shared
   checkout) and on your branch, and prints the path as its last line. **If it
   exits non-zero / prints `BLOCKED`, STOP** — post a "Blocked: worktree
   isolation failed" comment and escalate to the Tech Lead PM. Do **not** fall
   back to the shared checkout. Do every subsequent edit, test run, and commit
   from inside `$WT`; confirm with `git rev-parse --show-toplevel` if unsure.
6. **Implement** in small, coherent steps, following existing patterns.
7. **Test** — write tests following existing test patterns; run the full suite
   before opening the PR.
8. **(G) Open a PR** following `.github/pull_request_template.md` — since
   `gh pr create --body …` bypasses the template, you must include its sections
   yourself:
   - `Closes #N` for every related issue (mandatory — unlinked PRs are rejected)
   - What changed, why, and how to test
   - **How to deploy** (mandatory) — tick the applicable template line(s)
     (code-only + which services to restart, DB migration step, one-off script,
     config/env change, post-deploy validation) or check **"No deploy needed"**
     for docs/tests/CI-only PRs. A PR missing this section is a review blocker.
   - Any trade-offs or uncertainties
9. **(G) Move the issue to In review** on the board.
10. **(G) Comment on the issue:** "PR #N submitted for review — [brief summary]".
11. **(G) Request review** from the Tech Lead PM — and the Designer too if the
    PR touches UI components, layouts, styles, or user-facing text (both
    approvals required).
12. **Respond to review** — if changes are requested: move the issue back to
    In progress with an "Addressing review feedback: [summary]" comment, fix,
    then back to In review with "Feedback addressed, re-requesting review".
    When fixing blocking items, read the `AI / DeepSeek review` PR comment
    first and use its checklist as your task list.

## Pre-review checklist

- [ ] All work was done in an isolated worktree (`git rev-parse --show-toplevel`
      points inside `.claude/worktrees/agent-*`, not the shared checkout)
- [ ] Issue status is "In review" on the board (via GraphQL, not labels)
- [ ] PR references issues with `Closes #N`
- [ ] PR description includes: what changed, why, how to test
- [ ] PR description includes a filled **How to deploy** section (or
      "No deploy needed" checked)
- [ ] Comment posted on issue: "PR #N submitted for review"
- [ ] Review requested from Tech Lead PM (and Designer if frontend)
- [ ] All tests pass
