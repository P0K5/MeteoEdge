#!/usr/bin/env bash
# =============================================================================
# check_graphify_guard.sh (issue #1153)
#
# CI guard that prevents feature PRs from accidentally bundling regenerated
# graphify-out/ diffs. graphify-out/ is regenerated exclusively by the
# scheduled/dispatch graphify-update workflow (.github/workflows/graphify-update.yml),
# which pushes its own commits directly to master -- it never opens a pull
# request. There is therefore no legitimate case where a pull request should
# touch graphify-out/, and this guard fails unconditionally when one does.
# (An earlier draft of this guard let a PR bypass it by putting "graphify" in
# its branch name or title -- removed: that never matches how the real
# automation works, and just gives every other PR a trivial way around the
# guard it exists to be.)
#
# The GitHub-API-calling logic (`main`) is kept separate from the pure
# decision logic (`files_touch_graphify_out`) so scripts/test_graphify_guard.sh
# can exercise the latter directly, with no network access and no mocking.
#
# Workflow env vars required (provided by GitHub Actions):
#   GITHUB_TOKEN  — token for the PR-files API call
#   PR_NUMBER     — PR number being checked
#   REPO_OWNER    — repo owner
#   REPO_NAME     — repo name
#
# Usage:
#   bash scripts/check_graphify_guard.sh
#   (CI calls this on pull_request events only)
# =============================================================================

set -euo pipefail

GITHUB_API="${GITHUB_API:-https://api.github.com}"

# Pure decision function: does the newline-separated file list on stdin
# contain any path under graphify-out/? Exit 0 (touches it) / 1 (doesn't) --
# testable with a plain heredoc, no curl, no GitHub API.
files_touch_graphify_out() {
  grep -q '^graphify-out/' || return 1
}

main() {
  local required_env=(GITHUB_TOKEN PR_NUMBER REPO_OWNER REPO_NAME)
  for var in "${required_env[@]}"; do
    if [ -z "${!var:-}" ]; then
      echo "ERROR: Required environment variable $var is not set" >&2
      exit 1
    fi
  done

  local files_url="${GITHUB_API}/repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}/files?per_page=100"
  local response
  response=$(curl -sS -H "Authorization: token ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github.v3+json" \
    "${files_url}")

  if ! echo "$response" | jq -e 'type == "array"' >/dev/null 2>&1; then
    echo "ERROR: Failed to fetch PR #${PR_NUMBER}'s changed files:" >&2
    echo "$response" | head -c 2000 >&2
    exit 1
  fi

  local changed_files
  changed_files=$(echo "$response" | jq -r '.[].filename')

  if ! echo "$changed_files" | files_touch_graphify_out; then
    echo "✓ No changes to graphify-out/ — guard passes"
    exit 0
  fi

  echo "⚠ PR #${PR_NUMBER} modifies graphify-out/:" >&2
  echo "$changed_files" | grep '^graphify-out/' | sed 's/^/  /' >&2
  echo "" >&2
  echo "ERROR: graphify-out/ is regenerated exclusively by the scheduled" >&2
  echo "graphify-update workflow (.github/workflows/graphify-update.yml)," >&2
  echo "which pushes its own commits directly to master -- it never opens a" >&2
  echo "pull request. No PR should ever touch graphify-out/." >&2
  echo "" >&2
  echo "Fix: revert these files to match master and push again, e.g.:" >&2
  echo "  git checkout origin/master -- graphify-out/" >&2
  echo "  git commit -m 'Revert accidental graphify-out/ changes'" >&2
  echo "  git push" >&2
  exit 1
}

# Allow scripts/test_graphify_guard.sh to source this file and call
# files_touch_graphify_out() directly without running main()'s API calls.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main
fi
