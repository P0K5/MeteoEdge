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
5. **Create a feature branch.**
   - Junior: `junior/<issue-number>-<short-description>`
   - Mid: any descriptive name, linked to ALL related issues
6. **Implement** in small, coherent steps, following existing patterns.
7. **Test** — write tests following existing test patterns; run the full suite
   before opening the PR.
8. **(G) Open a PR** containing:
   - `Closes #N` for every related issue (mandatory — unlinked PRs are rejected)
   - What changed, why, and how to test
   - Any trade-offs or uncertainties
9. **(G) Move the issue to In review** on the board.
10. **(G) Comment on the issue:** "PR #N submitted for review — [brief summary]".
11. **(G) Request review** from the Tech Lead PM — and the Designer too if the
    PR touches UI components, layouts, styles, or user-facing text (both
    approvals required).
12. **Respond to review** — if changes are requested: move the issue back to
    In progress with an "Addressing review feedback: [summary]" comment, fix,
    then back to In review with "Feedback addressed, re-requesting review".
    When fixing blocking items, read the `AI / NVIDIA NIM review` PR comment
    first and use its checklist as your task list.

## Pre-review checklist

- [ ] Issue status is "In review" on the board (via GraphQL, not labels)
- [ ] PR references issues with `Closes #N`
- [ ] PR description includes: what changed, why, how to test
- [ ] Comment posted on issue: "PR #N submitted for review"
- [ ] Review requested from Tech Lead PM (and Designer if frontend)
- [ ] All tests pass
