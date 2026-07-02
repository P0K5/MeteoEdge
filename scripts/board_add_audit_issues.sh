#!/usr/bin/env bash
# =============================================================================
# board_add_audit_issues.sh
#
# Adds the 2026-07-01 audit issues (#548-#561) to the GitHub Project board and
# sets their Status (Ready for P0/P1, Backlog for P2).
#
# Needed because the session that created the issues could not reach the
# GraphQL API (proxy restriction) — Projects v2 mutations are GraphQL-only.
#
# Usage:  GH_TOKEN=<pat with project scope> bash scripts/board_add_audit_issues.sh
# Requires: gh CLI, .claude/session-context.env (for pre-resolved IDs).
#
# Idempotent: addProjectV2ItemById returns the existing item if already added.
# After running, trigger the "Refresh Session Context" workflow so
# session-context.env picks up the new item IDs.
# =============================================================================

set -euo pipefail

OWNER="P0K5"
REPO="MeteoEdge"
CTX=".claude/session-context.env"

[ -f "$CTX" ] || { echo "ERROR: $CTX not found — run scripts/bootstrap_session.sh first"; exit 1; }
# shellcheck disable=SC1090
source <(grep -E '^(GITHUB_PROJECT_ID|STATUS_FIELD_ID|STATUS_OPT_READY|STATUS_OPT_BACKLOG|STATUS_OPT_IN_PROGRESS|STATUS_OPT_DONE)=' "$CTX")

# Statuses reflect state as of 2026-07-02 (P0 batch executed):
#   Done:        548 (PR #563), 549 (PR #562), 550 (PR #569), 565 (PR #566)
#   In progress: 551 (stage 1 merged via PR #564; shadow window open)
#   Ready:       552-557 (P1 batch), 570 (prob-cap shadow report)
#   Backlog:     558-561 (P2), 567-568 (review follow-ups)
DONE_ISSUES=(548 549 550 565)
IN_PROGRESS_ISSUES=(551)
READY_ISSUES=(552 553 554 555 556 557 570)
BACKLOG_ISSUES=(558 559 560 561 567 568)

add_and_set_status() {
  local issue=$1 option_id=$2 status_name=$3

  local content_id
  content_id=$(gh api "repos/$OWNER/$REPO/issues/$issue" --jq .node_id)

  local item_id
  item_id=$(gh api graphql -f query='
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }' -f projectId="$GITHUB_PROJECT_ID" -f contentId="$content_id" \
    --jq .data.addProjectV2ItemById.item.id)

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
    }' -f projectId="$GITHUB_PROJECT_ID" -f itemId="$item_id" \
       -f fieldId="$STATUS_FIELD_ID" -f optionId="$option_id" > /dev/null

  echo "#$issue -> $status_name (item: $item_id)"
}

for n in "${DONE_ISSUES[@]}";        do add_and_set_status "$n" "$STATUS_OPT_DONE"        "Done";        done
for n in "${IN_PROGRESS_ISSUES[@]}"; do add_and_set_status "$n" "$STATUS_OPT_IN_PROGRESS" "In progress"; done
for n in "${READY_ISSUES[@]}";       do add_and_set_status "$n" "$STATUS_OPT_READY"       "Ready";       done
for n in "${BACKLOG_ISSUES[@]}";     do add_and_set_status "$n" "$STATUS_OPT_BACKLOG"     "Backlog";     done

echo "Done. Now run the 'Refresh Session Context' workflow to update $CTX with the new item IDs."
