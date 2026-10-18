---
name: dev-workflow
description: The mandatory issue-to-PR workflow for developers (Mid and Junior). Use when starting work on an assigned GitHub issue, before opening a PR, or when responding to review feedback.
---

# Developer Workflow — Issue to PR

Follow this exact sequence for every assigned issue. Steps marked (G) are
governance steps — mandatory, never skipped. Status transitions and comment
templates: invoke the **board-status** skill. Full protocol:
`.claude/instructions/governance.md`.

## Cloud sessions (Claude Code on the web) — read before step 4

GitHub **GraphQL is unavailable** in these sessions (HTTP 403 "GraphQL is not
available from Claude Code sessions"). That breaks the board mutation, `gh pr
create`, `gh issue view`, `gh repo view` and reviewer requests. Do not retry or
hand-roll workarounds; use this instead:

- **Board moves (steps 4, 9):** try the board-status mutation **once**. On the
  403 above, skip it and say so in the same issue comment you were going to post
  ("Board not moved: GraphQL unavailable in cloud sessions"). The PM moves the
  card. Never use labels as a substitute.
- **Open the PR (step 8) via REST**, with the repo spelled out (`gh repo view`
  is blocked): `gh api repos/P0K5/MeteoEdge/pulls -X POST -F title=… -F
  head=<branch> -F base=master -F body=@<body-file>`; read issues with
  `gh api repos/P0K5/MeteoEdge/issues/N`.
- **Reviewer request (step 11)** returns 422 when the PR author is the PM's
  account. Don't retry; state in your "PR #N submitted" comment that review is
  requested from the PM (and the Designer for frontend PRs).
- **PR author:** report who the PR shows as author in your hand-back. If it is
  the PM account, don't investigate the token (environment issue, tracked in
  #1367) — one line is enough.

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
   If the spawn prompt says another agent shares your issue/branch, pass your
   agent name as the third arg (`ensure_worktree.sh <branch> "" <agent>`); see
   governance.md "Same-issue multi-agent spawns".
6. **Implement** in small, coherent steps, following existing patterns.
7. **Test — run exactly what CI runs, before every push** (CI failures this
   session were all avoidable: PR #1372 pinned-schedule test, #1373 E741, #1375
   lint):

   ```bash
   python -m flake8 src/                                       # whole tree, not just your files
   python -m pytest src/tests -q -n auto --dist loadfile       # the full suite
   ```

   - CI runs only `src/tests` and `tests/integration`. A test placed in
     `tests/` **never runs** — put new tests in `src/tests`.
   - Before changing a constant, schedule, threshold or default, `grep -rn
     "<old value>" src/tests tests docs` — an existing test usually pins it.
   - Write tests following existing patterns; show a new test fails without your
     change when practical.
8. **(G) Open a PR** following `.github/pull_request_template.md` — since
   `gh pr create --body …` bypasses the template, you must include its sections
   yourself. **CRITICAL: `export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL"` must be
   in the SAME shell command as `gh pr create` (joined with `&&`, or the same
   heredoc), never a separate one.** The Bash tool does not persist shell
   state (env vars, `export`, `cd`) across separate tool calls — only the
   working directory carries over. An export run as its own call is gone by
   the time the next call runs `gh pr create`, and `gh` silently falls back
   to the default authenticated account (the PM's), which is exactly how
   #998/#1080/#1085/#1130/#1269/#1008/#1367 kept recurring despite this step
   already being documented. Always write it as one command:

   ```bash
   export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL" && gh pr create \
     --title "..." --body-file ./.pr-body-<issue>.md
   ```

   This ensures PRs are authored under the operational account (not the PM's
   account), and allows the PM to formally review and approve your PR via the
   GitHub API. Without this, `gh pr review --approve` will fail with "Can not
   approve your own pull request" (see governance.md for token rules). The
   same rule applies to EVERY later `gh` call in this workflow (step 9's
   board move, step 10's comment, step 11's reviewer request, step 12's
   fixes) — each is its own tool call, so each one re-chains its own
   `export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL" && gh ...`. Never rely on an
   export from an earlier step still being in effect.

   PR sections:
   - `Closes #N` for every related issue (mandatory — unlinked PRs are rejected)
   - What changed, why, and how to test
   - **How to deploy** (mandatory) — tick the applicable template line(s)
     (code-only + which services to restart, DB migration step, one-off script,
     config/env change, post-deploy validation) or check **"No deploy needed"**
     for docs/tests/CI-only PRs. A PR missing this section is a review blocker.
   - Any trade-offs or uncertainties

   **PR body hygiene (parallel devs have overwritten each other's bodies — PRs
   #1375/#1376 both got #1373's text and would have closed the wrong issue):**
   write the body to a file named for your issue **inside your own worktree**
   (`./.pr-body-<issue>.md`, never `/tmp` or a shared scratch path; stage files
   explicitly, don't `git add -A`). After creating or editing the PR, **re-read
   the body via the API and confirm its first line is exactly `Closes #<your
   issue>`** and the text is yours. The `PR body check` CI job (#791) also fails a
   body with no non-empty **How to deploy** section or a `Closes #N` whose number
   is not in the branch name, PR title or a commit message. Closing keywords
   count even in prose: write examples without the literal keyword, or inside a
   `code span`.
9. **(G) Move the issue to In review** on the board.
10. **(G) Comment on the issue:** "PR #N submitted for review — [brief summary]".
11. **(G) Request review** from the Tech Lead PM — and the Designer too if the
    PR touches UI components, layouts, styles, or user-facing text (both
    approvals required). Ensure `GH_TOKEN` is still exported (from step 8)
    before running `gh pr edit --add-reviewer` or equivalent commands:

    ```bash
    export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL"
    gh pr edit <PR-number> --add-reviewer @user
    ```
12. **Respond to review** — if changes are requested: move the issue back to
    In progress with an "Addressing review feedback: [summary]" comment, fix,
    then back to In review with "Feedback addressed, re-requesting review".
    When fixing blocking items, read the `AI / DeepSeek review` PR comment
    first and use its checklist as your task list.

## Handling authorship collisions — fix forward, never comment-approve

**This section changed 2026-10-17 (issue #1444).** Master now has real branch
protection: `required_pull_request_reviews.required_approving_review_count: 1`
is a GitHub-**enforced** gate, and `enforce_admins: true` means there is no
bypass for anyone, including the Tech Lead PM. A PM comment reading "Approved
(via comment — GitHub API collision)" used to be treated as formally
equivalent to a real review approval — **it no longer does anything.** The
merge button stays hard-blocked regardless of what anyone writes in a
comment. Do not rely on that fallback; it is dead.

**If a PR shows the PM (`P0K5`) as author** — which happens when `gh`'s
keyring default login is used instead of the operational token, usually
because `export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL"` wasn't in the *same*
shell invocation as `gh pr create` — the only correct recovery is to fix
forward, not to work around the review gate:

1. Close the misauthored PR (either token can do this).
2. Re-open a new PR from the **same branch**, same commit(s) — do not rewrite
   history or force-push. Re-run `gh pr create`, chaining the export and the
   create call together in one invocation:
   ```bash
   export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL" && gh pr create \
     --title "..." --body "..."
   ```
3. Verify the new PR's author before handing back:
   `gh pr view <N> --json author --jq .author.login` must print
   `ClaudioSenior`, not `P0K5`.
4. If it still doesn't work after two tries, **stop and escalate to the Tech
   Lead PM** rather than attempting a comment-approval or any other
   workaround of the review gate — with `enforce_admins: true`, there is no
   sanctioned bypass left, and repeated failures point to an environment or
   token setup issue that needs root-cause investigation, not a workaround.

## Pre-review checklist

- [ ] All work was done in an isolated worktree (`git rev-parse --show-toplevel`
      points inside `.claude/worktrees/agent-*`, not the shared checkout)
- [ ] Issue status is "In review" on the board (via GraphQL, not labels) — or your issue comment says the board was not moved because GraphQL is unavailable (cloud sessions)
- [ ] PR references issues with `Closes #N`
- [ ] PR description includes: what changed, why, how to test
- [ ] PR description includes a filled **How to deploy** section (or
      "No deploy needed" checked)
- [ ] Comment posted on issue: "PR #N submitted for review"
- [ ] Review requested from Tech Lead PM (and Designer if frontend)
- [ ] `python -m flake8 src/` clean and the full `src/tests` suite passes (commands in step 7)
- [ ] PR body re-read via the API: first line is `Closes #<your issue>` and the body is yours
- [ ] `GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL"` was exported before `gh pr create`
      (check the PR author; in cloud sessions it may still show the PM account — report it, see above)
