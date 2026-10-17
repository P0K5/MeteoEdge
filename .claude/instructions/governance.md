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
   - **Exception: deliberately-sequenced PRs.** When one issue is delivered by
     N PRs, only the final PR uses a closing keyword. Earlier PRs use a
     non-closing reference: `Part of #N`, `Refs #N`, `Related to #N`, or
     `See #N` (comma/"and" lists allowed, e.g. `Refs #1264, #1266`). Non-closing
     references satisfy the linking requirement and do not auto-close the
     issue on merge, so no close/reopen step is needed. The AI reviewer treats
     either form as valid linkage. A PR with no issue reference of either kind
     is still a violation.
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

1. **All CI checks pass** — `CI / lint`, `CI / test`, and `AI / DeepSeek review`
   must show green on the PR's **head commit**. Verify via
   `mcp__github__actions_list` (or equivalent) before approving.
2. **No direct pushes to master** — every change, including one-line hotfixes,
   goes through a PR. **Branch protection is now enabled on `master`**
   (configured 2026-10-17, issue #1444) with `enforce_admins: true`, so
   `git push origin master` is rejected for everyone, including the Tech Lead
   PM's token — there is no admin bypass. `required_status_checks.strict`
   is also enabled ("require branches to be up to date before merging"),
   which closes the staleness gap #1444 was filed for: GitHub now refuses to
   merge a PR whose last CI run predates the current `master` tip, so a base
   branch that moved after the PR's last push can no longer slip through on
   a stale green check (the #1357/#1441/#1443 incident this issue
   documents). Required status checks: `Guard against graphify-out/ changes`,
   `Lint (flake8)`, `Test (pytest)`, `PR body (How to deploy + issue link)`,
   `gitleaks`.

   **`AI / DeepSeek review` is deliberately NOT a GitHub-enforced required
   check**, even though it's still mandatory in practice via rule 1 above and
   must be verified by a human before every approval. Reason: the review
   workflow has repeatedly posted a second, zero-step duplicate check run
   under the same name that fails instantly (`started_at == completed_at`,
   no steps) independent of the real review's verdict — see the pr-review
   skill's "known flake" note. Making it a hard-required GitHub check would
   let that duplicate block a merge with no recourse other than an admin
   override, which `enforce_admins: true` above deliberately forecloses.
   Keeping it a human-verified-not-GitHub-blocking gate preserves the
   ability to read the *real* review's verdict and act on it instead of on
   infra flakiness. Revisit if that duplicate-check bug gets fixed upstream.

   Before the *first* `git commit` of any change, still run
   `git rev-parse --abbrev-ref HEAD`. If it prints `master`, stop and
   `git checkout -b <type>/<issue>-<slug>` first — branch protection rejects
   the push regardless, but catching it before committing avoids the
   `git reset --soft HEAD~1` cleanup dance. Never force-push master.
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
