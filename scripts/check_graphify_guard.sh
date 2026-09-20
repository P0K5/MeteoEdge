#!/usr/bin/env bash
# =============================================================================
# check_graphify_guard.sh (issue #1153)
#
# CI guard that prevents feature PRs from accidentally bundling regenerated
# graphify-out/ diffs. graphify-out/ is regenerated exclusively by the
# graphify-update workflow (.github/workflows/graphify-update.yml) -- and
# that workflow's own `on:` block (as of this writing) is
# `push: {branches: [master]}` + `schedule` + `workflow_dispatch` only.
# There is no `pull_request` trigger and no step in that file that opens a
# PR; every one of its jobs ends with `git push` straight to master. This
# is a direct read of that file, not an assumption -- if it ever changes
# to open a PR of its own, whoever makes that change needs to revisit this
# guard too (an AI review on this PR asked for that link to be explicit,
# hence this paragraph). Until then, no PR should ever touch graphify-out/,
# and this guard fails unconditionally when one does.
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

  # GitHub caps per_page at 100 -- a PR with more changed files than that
  # needs every page walked, or a graphify-out/ path on page 2+ would
  # silently pass the guard (the exact large-file-count scenario this
  # guard exists to catch). Loop until a page returns fewer than 100
  # entries. MAX_PAGES is a defensive cap (5000 files, far beyond any real
  # PR) so a pagination/API bug can never hang this job indefinitely --
  # it fails loudly instead.
  local -r MAX_PAGES=50
  local page=1 changed_files="" page_files page_count
  while :; do
    if [ "$page" -gt "$MAX_PAGES" ]; then
      echo "ERROR: PR #${PR_NUMBER} has more than ${MAX_PAGES} pages of" \
        "changed files (>$((MAX_PAGES * 100)))-- refusing to keep paginating." >&2
      exit 1
    fi

    local response
    response=$(curl -sS -H "Authorization: token ${GITHUB_TOKEN}" \
      -H "Accept: application/vnd.github.v3+json" \
      "${GITHUB_API}/repos/${REPO_OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}/files?per_page=100&page=${page}")

    if ! echo "$response" | jq -e 'type == "array"' >/dev/null 2>&1; then
      echo "ERROR: Failed to fetch PR #${PR_NUMBER}'s changed files (page ${page}):" >&2
      echo "$response" | head -c 2000 >&2
      exit 1
    fi

    page_files=$(echo "$response" | jq -r '.[].filename')
    page_count=$(echo "$response" | jq -e 'length')
    if [ -n "$page_files" ]; then
      changed_files="${changed_files}${changed_files:+$'\n'}${page_files}"
    fi

    if [ "$page_count" -lt 100 ]; then
      break
    fi
    page=$((page + 1))
  done

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
