# PR body check (issue #791)

Workflow `.github/workflows/pr-body-check.yml` runs `scripts/check_pr_body.py`
on every PR to master (including docs-only PRs). Two rules:

1. The body needs a non-empty `## How to deploy` section (a ticked box, or
   text such as "No deploy needed"). See `.github/pull_request_template.md`.
2. Every issue in a closing keyword (Closes/Fixes/Resolves #N) must also appear
   in the head branch name, PR title, or a commit message. `Part of`/`Refs` are
   exempt.

**Closing keywords in prose count.** GitHub closes the issue on merge wherever
the keyword appears, so the check treats it the same. When writing an example,
do not use the literal keyword followed by a hash and number: write "a body
whose first line closes issue 944", or put it in a `code span` or fenced
block (both are ignored).

Exempt: `*[bot]` authors, `chore(graph)` / `chore(ci)` titles, `[skip ci]`.

Repo owner note: the check is advisory until added as a required status check
in branch protection (needs repo-admin; not decided yet).
