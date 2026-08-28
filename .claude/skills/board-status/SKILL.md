---
name: board-status
description: Update a GitHub Project board status via GraphQL and post the required checkpoint comment. Use when starting work on an issue, opening a PR, responding to review changes, re-requesting review, or when blocked.
---

# Board Status Transitions

Status lives EXCLUSIVELY on the GitHub Project board `Status` field, updated
via GraphQL — never via labels. Every transition needs a matching issue
comment. Full rules: `.claude/instructions/governance.md`.

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

| Event | New status option | Comment template |
|---|---|---|
| Starting work | `STATUS_OPT_IN_PROGRESS` | "Starting work — plan: [2–4 steps]" |
| PR opened | `STATUS_OPT_IN_REVIEW` | "PR #N submitted for review — [brief summary]" |
| Changes requested (reviewer moves it) | `STATUS_OPT_IN_PROGRESS` | "Addressing review feedback: [summary]" |
| Fixes pushed, review re-requested | `STATUS_OPT_IN_REVIEW` | "Feedback addressed, re-requesting review" |
| Blocked | _(no change)_ | "Blocked: [what you tried, where you're stuck, options]" |
| Issue created & added to board (PM) | `STATUS_OPT_READY` | — |

No silent status changes. If a board status doesn't match reality, fix it immediately.
