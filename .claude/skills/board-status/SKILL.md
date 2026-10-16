---
name: board-status
description: Update a GitHub Project board status via GraphQL and post the required checkpoint comment. Use when starting work on an issue, opening a PR, responding to review changes, re-requesting review, or when blocked.
---

# Board Status Transitions

Status lives EXCLUSIVELY on the GitHub Project board `Status` field, updated
via GraphQL — never via labels. Every transition needs a matching issue
comment. Full rules: `.claude/instructions/governance.md`.

> **Cloud sessions (Claude Code on the web):** GraphQL returns HTTP 403 "GraphQL
> is not available from Claude Code sessions". Try the mutation **once**; on
> that error skip it, say so in the transition comment ("Board not moved:
> GraphQL unavailable in cloud sessions"), and let the PM move the card. Don't
> retry, and never fall back to labels.

## 1. Get the pre-resolved IDs

Read `.claude/session-context.env`. It contains everything you need:

- `GITHUB_PROJECT_ID`
- `STATUS_FIELD_ID`
- `STATUS_OPT_BACKLOG` / `STATUS_OPT_READY` / `STATUS_OPT_IN_PROGRESS` /
  `STATUS_OPT_IN_REVIEW` / `STATUS_OPT_DONE`
- `ITEM_ID_ISSUE_<number>` — the board item ID for each issue

**Do NOT run GraphQL queries to look these up.** Only if the file is missing,
empty, or lacks your issue's item ID: run `bash scripts/bootstrap_session.sh`
(needs a token with `project` scope), then re-read the file.

**On Windows, this fails with "Python was not found" before it does anything
useful** — the script calls `python3`, which resolves to a broken Windows
Store alias stub, not the real interpreter (that's `python` on this machine).
Shim it once per session rather than debugging the same failure again:
```bash
mkdir -p /tmp/pyshim
printf '#!/bin/bash\nexec python "$@"\n' > /tmp/pyshim/python3
chmod +x /tmp/pyshim/python3
PATH="/tmp/pyshim:$PATH" bash scripts/bootstrap_session.sh
```
Keep `/tmp/pyshim` on `PATH` for any later re-run in the same session.

## 2. Run the mutation

Use your role's token. **CRITICAL: Always export the correct token BEFORE
running any `gh` command** — `GITHUB_TOKEN_SUPERVISOR` for PM,
`GITHUB_TOKEN_OPERATIONAL` for everyone else:

```bash
# For developers (use this):
export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL"

# For Tech Lead PM (use this):
export GH_TOKEN="$GITHUB_TOKEN_SUPERVISOR"

# Then run the mutation:
gh api graphql -f query='
  mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: { singleSelectOptionId: $optionId }
    }) {
      projectV2Item { id }
    }
  }' -f projectId="$GITHUB_PROJECT_ID" -f itemId="$ITEM_ID_ISSUE_N" \
     -f fieldId="$STATUS_FIELD_ID" -f optionId="$STATUS_OPT_..."
```

## 3. Post the matching issue comment

**The `export GH_TOKEN=...` above does not carry over to this step if it's a
separate tool call** — the Bash tool only persists the working directory
across calls, never shell state like exported env vars. Chain the export
with the comment command itself, every time:

```bash
export GH_TOKEN="$GITHUB_TOKEN_OPERATIONAL" && gh issue comment <N> --body "..."
```

(PM: `GITHUB_TOKEN_SUPERVISOR` instead.) Treating "I exported it a step ago"
as sufficient is exactly how #998/#1080/#1085/#1130/#1269/#1008/#1367 kept
recurring — the token-export instruction existed and was followed, just not
in the same shell invocation as the command it was supposed to authenticate.

| Event | New status option | Comment template |
|---|---|---|
| Starting work | `STATUS_OPT_IN_PROGRESS` | "Starting work — plan: [2–4 steps]" |
| PR opened | `STATUS_OPT_IN_REVIEW` | "PR #N submitted for review — [brief summary]" |
| Changes requested (reviewer moves it) | `STATUS_OPT_IN_PROGRESS` | "Addressing review feedback: [summary]" |
| Fixes pushed, review re-requested | `STATUS_OPT_IN_REVIEW` | "Feedback addressed, re-requesting review" |
| Blocked | _(no change)_ | "Blocked: [what you tried, where you're stuck, options]" |
| Issue created & added to board (PM) | `STATUS_OPT_READY` | — |

No silent status changes. If a board status doesn't match reality, fix it immediately.
