#!/usr/bin/env bash
# =============================================================================
# board_add_audit_issues.sh
#
# Adds a curated set of issues to the MeteoEdge GitHub Projects v2 board and
# sets each one's Status column. Idempotent: adding an issue that is already
# on the board is a no-op that returns its existing item id, and the Status
# is then (re)set to the value configured in the lists below.
#
# WHY THIS EXISTS: Claude Code agents in the execution environment cannot run
# Projects-v2 GraphQL mutations (the GraphQL endpoint is proxy-blocked and the
# agents have no `project`-scoped token). Agents therefore record status
# transitions as issue comments, and the board is reconciled out-of-band by an
# operator running THIS script with a project PAT.
#
# Usage:   GH_TOKEN=<pat-with-project-scope> bash scripts/board_add_audit_issues.sh
#          # or:  GH_PROJECT_PAT=<pat> GH_TOKEN=$GH_PROJECT_PAT bash scripts/...
#
# Requires: gh CLI authenticated with a token carrying `project` (read+write)
#           scope, same as scripts/bootstrap_session.sh.
#
# HOW TO KEEP CURRENT: edit the STATUS lists below as issues move. Each array
# holds issue NUMBERS; the array name maps to a Status option by name (resolved
# at runtime, so it tolerates option-id churn). Leave an issue out entirely to
# not touch its board state.
# =============================================================================

set -euo pipefail

OWNER="P0K5"
REPO="MeteoEdge"

# ---------------------------------------------------------------------------
# Issue → Status lists.  EDIT THESE as the board changes.
# Status names must match the project's Status single-select options exactly
# (Backlog / Ready / In progress / In review / Done).
# ---------------------------------------------------------------------------
# Wave 3 (2026-07-03): all six core issues + the gfs-fallback filler + the
# ECMWF hotfix were merged.
DONE_ISSUES=(583 582 586 559 592 584 568 560 599)

# Backlog: follow-ups filed during Wave 3 review + the deferred JMA filler +
# the still-open promotion-script decision.
BACKLOG_ISSUES=(601 602 80)

# Wave 4 (2026-07-03 audit): live settlement broken (#609, P0), low-side
# direction mislabel + wrong-truth settle (#610, P0), duplicate live entry
# stacking (#611, P1).
READY_ISSUES=(609 610 611)
IN_PROGRESS_ISSUES=()
IN_REVIEW_ISSUES=()
# ---------------------------------------------------------------------------

echo "[board] Resolving project + Status field for $OWNER/$REPO"

PROJECT_ID=$(gh api graphql -f query='
  query($owner: String!, $repo: String!) {
    repository(owner: $owner, name: $repo) {
      projectsV2(first: 5) { nodes { id title number } }
    }
  }' -f owner="$OWNER" -f repo="$REPO" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)['data']['repository']['projectsV2']['nodes']
if not nodes:
    raise SystemExit('[board] ERROR: no Projects v2 found for this repo.')
print(nodes[0]['id'])
")
echo "[board] PROJECT_ID=$PROJECT_ID"

# Resolve Status field id + a "Option Name" -> option id map (JSON on one line).
read -r STATUS_FIELD_ID STATUS_OPTIONS_JSON < <(gh api graphql -f query='
  query($projectId: ID!) {
    node(id: $projectId) {
      ... on ProjectV2 {
        fields(first: 30) {
          nodes {
            ... on ProjectV2SingleSelectField { id name options { id name } }
          }
        }
      }
    }
  }' -f projectId="$PROJECT_ID" | python3 -c "
import sys, json
fields = json.load(sys.stdin)['data']['node']['fields']['nodes']
for f in fields:
    if f.get('name') == 'Status':
        opts = {o['name']: o['id'] for o in f['options']}
        print(f['id'], json.dumps(opts))
        break
else:
    raise SystemExit('[board] ERROR: Status field not found in project.')
")
echo "[board] STATUS_FIELD_ID=$STATUS_FIELD_ID"

# Look up a Status option id by its display name from the resolved map.
option_id_for() {
  local name="$1"
  STATUS_OPTIONS_JSON="$STATUS_OPTIONS_JSON" python3 -c "
import os, sys, json
opts = json.loads(os.environ['STATUS_OPTIONS_JSON'])
name = sys.argv[1]
if name not in opts:
    raise SystemExit(f'[board] ERROR: Status option {name!r} not in project. Have: {list(opts)}')
print(opts[name])
" "$name"
}

# Add one issue to the board (idempotent) and set its Status.
sync_issue() {
  local num="$1" status_name="$2" option_id="$3"

  local content_id
  content_id=$(gh api graphql -f query='
    query($owner: String!, $repo: String!, $num: Int!) {
      repository(owner: $owner, name: $repo) { issue(number: $num) { id } }
    }' -f owner="$OWNER" -f repo="$REPO" -F num="$num" | python3 -c "
import sys, json
issue = json.load(sys.stdin)['data']['repository']['issue']
if not issue:
    raise SystemExit('[board] ERROR: issue not found')
print(issue['id'])
") || { echo "[board] #$num: SKIP (issue lookup failed)"; return 0; }

  local item_id
  item_id=$(gh api graphql -f query='
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }' -f projectId="$PROJECT_ID" -f contentId="$content_id" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['data']['addProjectV2ItemById']['item']['id'])")

  gh api graphql -f query='
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
        value: { singleSelectOptionId: $optionId }
      }) { projectV2Item { id } }
    }' -f projectId="$PROJECT_ID" -f itemId="$item_id" \
       -f fieldId="$STATUS_FIELD_ID" -f optionId="$option_id" >/dev/null

  echo "[board] #$num -> $status_name (item $item_id)"
}

process_list() {
  local status_name="$1"; shift
  local nums=("$@")
  [ ${#nums[@]} -eq 0 ] && return 0
  local option_id
  option_id=$(option_id_for "$status_name")
  for num in "${nums[@]}"; do
    sync_issue "$num" "$status_name" "$option_id"
  done
}

process_list "Done"        "${DONE_ISSUES[@]}"
process_list "Backlog"     "${BACKLOG_ISSUES[@]}"
process_list "Ready"       "${READY_ISSUES[@]}"
process_list "In progress" "${IN_PROGRESS_ISSUES[@]}"
process_list "In review"   "${IN_REVIEW_ISSUES[@]}"

echo "[board] Done."
