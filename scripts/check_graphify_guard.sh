#!/usr/bin/env bash
# =============================================================================
# check_graphify_guard.sh (issue #1153)
#
# CI guard that prevents feature PRs from accidentally bundling regenerated
# graphify-out/ diffs. The graphify-out/ directory should only be modified by
# the automated graphify-update workflow.
#
# Workflow env vars required (provided by GitHub Actions):
#   GITHUB_TOKEN          — GitHub token for API calls
#   PR_NUMBER             — PR number being reviewed
#   REPO_OWNER            — repo owner
#   REPO_NAME             — repo name
#
# Behavior:
#   - Detects any changes to graphify-out/ in the PR
#   - If graphify-out/ is modified:
#     - Checks if the PR is a graphify-update PR (branch/title contains "graphify")
#     - If NOT a graphify-update PR, fails with a clear error message
#   - Otherwise, succeeds silently
#
# Usage:
#   bash scripts/check_graphify_guard.sh
#   (CI calls this; intended to run on pull_request events only)
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

REQUIRED_ENV=(
  "GITHUB_TOKEN"
  "PR_NUMBER"
  "REPO_OWNER"
  "REPO_NAME"
)

GITHUB_API="https://api.github.com"

# =============================================================================
# Verify environment
# =============================================================================

for var in "${REQUIRED_ENV[@]}"; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: Required environment variable $var is not set"
    exit 1
  fi
done

# =============================================================================
# Fetch PR metadata and changed files
# =============================================================================

PR_URL="${GITHUB_API}/repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}"

# Fetch PR metadata (title, head branch)
pr_meta=$(curl -s -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github.v3+json" \
  "${PR_URL}")

if echo "$pr_meta" | grep -q '"message"'; then
  echo "ERROR: Failed to fetch PR metadata"
  echo "$pr_meta" | head -10
  exit 1
fi

pr_title=$(echo "$pr_meta" | grep -o '"title":"[^"]*"' | head -1 | cut -d'"' -f4)
head_branch=$(echo "$pr_meta" | grep -o '"ref":"[^"]*"' | head -1 | cut -d'"' -f4)

echo "PR #${PR_NUMBER}: ${pr_title}"
echo "Branch: ${head_branch}"

# Fetch list of changed files in the PR
files_url="${PR_URL}/files?per_page=100"
changed_files=$(curl -s -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github.v3+json" \
  "${files_url}" | grep -o '"filename":"[^"]*"' | cut -d'"' -f4)

# Check if any changed files are in graphify-out/
graphify_changes=$(echo "$changed_files" | grep '^graphify-out/' || true)

if [ -z "$graphify_changes" ]; then
  echo "✓ No changes to graphify-out/ — guard passes"
  exit 0
fi

# =============================================================================
# graphify-out/ was modified — verify this is a graphify-update PR
# =============================================================================

echo ""
echo "⚠ Changes detected in graphify-out/:"
echo "$graphify_changes" | sed 's/^/  /'

# Check if this is a graphify-update PR (branch or title contains "graphify")
is_graphify_update=0

if echo "$head_branch" | grep -qi "graphify"; then
  is_graphify_update=1
  echo ""
  echo "✓ Branch name contains 'graphify' — this is a graphify-update PR"
fi

if echo "$pr_title" | grep -qi "graphify"; then
  is_graphify_update=1
  echo ""
  echo "✓ PR title contains 'graphify' — this is a graphify-update PR"
fi

if [ "$is_graphify_update" -eq 1 ]; then
  echo "✓ Guard passes — graphify-update PR is allowed to modify graphify-out/"
  exit 0
fi

# =============================================================================
# graphify-out/ modified but NOT a graphify-update PR — FAIL
# =============================================================================

echo ""
echo "ERROR: This PR modifies graphify-out/ but does not appear to be a"
echo "graphify-update PR (branch name and title should contain 'graphify')."
echo ""
echo "The graphify-out/ directory is managed by the automated graphify-update"
echo "workflow (.github/workflows/graphify-update.yml) and should never be"
echo "modified in feature or bugfix PRs."
echo ""
echo "If this is intended to be a graphify-update PR, please:"
echo "  1. Rename the branch to include 'graphify', OR"
echo "  2. Update the PR title to include 'graphify'"
echo ""
echo "If this is a feature PR that accidentally bundled graphify-out/ changes,"
echo "please undo those changes (e.g., git checkout origin/master -- graphify-out/)"
echo "and force-push to your branch."
echo ""
exit 1
