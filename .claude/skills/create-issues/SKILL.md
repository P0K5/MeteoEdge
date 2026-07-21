---
name: create-issues
description: Tech Lead PM structure for creating GitHub issues from an epic and registering them on the project board. Use when decomposing an epic into issues or adding issues to the board.
---

# Issue Creation & Board Registration (Tech Lead PM)

## Issue structure

- **Title:** imperative, specific, ≤72 characters.
  Example: "Add rate-limiting middleware to auth endpoints".
- **Context:** one paragraph — why this work is needed and how it fits the
  broader implementation.
- **Acceptance criteria:** bulleted, verifiable conditions ("Given/When/Then"
  or plain boolean statements).
- **Technical notes:** pointers to relevant files, interfaces, patterns. For
  Junior issues: specific file paths, function names, and example snippets.
- **Test requirements:** explicit test cases (unit, integration, e2e as appropriate).
- **Dependencies:** issues that must be completed first.
- **Complexity label** (determines assignment):
  - **Simple** → junior-dev agent: isolated, well-defined, clear patterns, no
    ambiguity, no architectural decisions.
  - **Mid** → mid-dev agent: moderate complexity, judgment within established patterns.
  - **Complex** → implement yourself, or assign to mid-dev with detailed guidance.
- **Other labels:** `backend`, `frontend`, `bug`, `epic` — categorization
  only, NEVER status (see `.claude/instructions/governance.md`).

Do not create issues too large for a single PR — break them down further.

## After creating issues (MANDATORY — do not skip)

1. Add ALL issues to the GitHub Project board.
2. Set each issue's status to **Ready** via GraphQL (board-status skill).
   If the new issues aren't in `.claude/session-context.env` yet, run
   `bash scripts/bootstrap_session.sh` to refresh the item-ID map before
   spawning developers.
3. Assign each issue to the appropriate developer based on complexity.
4. Post a summary to the user: all created issues, assignments, and the
   dependency/execution order.

When multiple issues have dependencies, spawn developers in dependency order —
never assign a dependent issue before its prerequisites are done.
