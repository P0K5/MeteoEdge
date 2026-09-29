#!/usr/bin/env bash
# =============================================================================
# ensure_worktree.sh  (issue #800)
#
# Deterministically place a dev agent (mid-dev / junior-dev) in an ISOLATED git
# worktree on its OWN branch before it edits or commits anything. This closes
# the race where concurrent spawns worked directly in the shared checkout and
# branched off whatever HEAD it happened to be sitting on -- landing commits on
# a stray branch, reverting another agent's uncommitted work, or opening a PR
# with none of the intended diff (see PR #796 recovery).
#
# Usage:
#   scripts/ensure_worktree.sh <branch-name> [base-ref]
#
#   <branch-name>  the branch to create/use, e.g. junior/781-emos-projection
#   [base-ref]     the ref to branch FROM (default: origin/master). Passing an
#                  explicit base is what makes the starting point independent of
#                  the shared checkout's (possibly moving) current HEAD.
#
# Contract:
#   SUCCESS  -> the absolute worktree path is printed as the LAST stdout line,
#               exit 0. cd into that path before ANY file edit or git commit.
#   FAILURE  -> "BLOCKED: <reason>" printed to stderr, exit 1. The caller MUST
#               stop and report "Blocked" to the Tech Lead PM. NEVER fall back
#               to the shared checkout.
#
# Idempotent: re-running with the same branch reuses the existing worktree.
# =============================================================================

set -euo pipefail

BLOCK() { echo "BLOCKED: $*" >&2; exit 1; }
log()   { echo "[ensure_worktree] $*" >&2; }

# Normalize a path for STRING COMPARISON ONLY (never for display/output).
# Issue #1235: on Windows this script compares repo roots obtained from two
# tools that spell the same directory differently -- bash `pwd` (and anything
# derived from it) emits MSYS form, e.g. `/c/Coding/MeteoEdge`, while native
# `git.exe rev-parse --show-toplevel` emits drive-letter form, e.g.
# `C:/Coding/MeteoEdge`. Those two strings never compare equal even when they
# name the same directory. This bug existed TWICE with opposite operators:
# the reuse check (`=`) never matched a genuinely-reusable worktree (always
# BLOCKED, false positive), while the isolation check (`!=`) never matched a
# genuinely-unisolated one either (silently never fired, false negative).
#
# Round-tripping any path through bash's own `cd`+`pwd` collapses both
# spellings to the same MSYS form, removing the mismatch instead of trying to
# translate between the two formats. Belt-and-braces: also lowercase a
# leading single-letter drive segment (`/C/...` -> `/c/...`), because `cd`+
# `pwd` alone only lowercases the drive letter when converting FROM
# `C:/...` colon form -- a path that already arrives in MSYS form with an
# upper-case drive letter (e.g. `/C/...`) round-trips unchanged. Windows
# drive letters are case-insensitive, so this is safe; no other path segment
# is touched, so case-sensitive filenames on Linux/macOS are unaffected
# (and those platforms already agree on path spelling, so norm_path is a
# no-op there).
norm_path() {
  local p drive rest
  p="$(cd "$1" 2>/dev/null && pwd)" || { printf '%s' "$1"; return; }
  if [[ "$p" =~ ^/([A-Za-z])(/.*)?$ ]]; then
    drive="${BASH_REMATCH[1],,}"
    rest="${BASH_REMATCH[2]:-}"
    p="/$drive$rest"
  fi
  printf '%s' "$p"
}

BRANCH="${1:-}"
BASE_REF="${2:-origin/master}"

[ -n "$BRANCH" ] || BLOCK "no branch name given (usage: ensure_worktree.sh <branch> [base-ref])"

# Must be run from inside a git work tree.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || BLOCK "not inside a git work tree"

# The shared/primary checkout root = parent of the common .git dir. This is the
# directory we must NOT end up committing into.
GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
GIT_COMMON_DIR="$(cd "$GIT_COMMON_DIR" && pwd)"
MAIN_ROOT="$(dirname "$GIT_COMMON_DIR")"

# Sanitize the branch name into a filesystem-safe worktree directory name.
SANITIZED="$(printf '%s' "$BRANCH" | tr '/ ' '--' | tr -cd 'A-Za-z0-9._-')"
[ -n "$SANITIZED" ] || BLOCK "branch name '$BRANCH' sanitizes to empty"
WT_DIR="$MAIN_ROOT/.claude/worktrees/agent-$SANITIZED"

mkdir -p "$MAIN_ROOT/.claude/worktrees"

# Best-effort fetch so origin/<branch> style bases are fresh. Never fatal on its
# own -- we validate the base is resolvable immediately afterwards.
if [[ "$BASE_REF" == origin/* ]]; then
  git fetch --quiet origin "${BASE_REF#origin/}" 2>/dev/null || true
fi

# Reuse path: a worktree already exists at WT_DIR.
if [ -e "$WT_DIR" ]; then
  if EXISTING_TOP="$(git -C "$WT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
      && [ "$(norm_path "$EXISTING_TOP")" = "$(norm_path "$WT_DIR")" ]; then
    EXISTING_BRANCH="$(git -C "$WT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    [ "$EXISTING_BRANCH" = "$BRANCH" ] \
      || BLOCK "worktree '$WT_DIR' already exists but is on branch '$EXISTING_BRANCH', not '$BRANCH'"
    log "reusing existing isolated worktree: $WT_DIR (branch $BRANCH)"
  else
    BLOCK "path '$WT_DIR' exists but is not a valid worktree; refusing to clobber"
  fi
else
  # Resolve the base ref (only needed when creating fresh).
  git rev-parse --verify --quiet "$BASE_REF^{commit}" >/dev/null \
    || BLOCK "base ref '$BASE_REF' is not resolvable (fetch failed or bad ref)"

  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    # Branch already exists locally -> attach a worktree to it.
    git worktree add "$WT_DIR" "$BRANCH" >/dev/null \
      || BLOCK "git worktree add for existing branch '$BRANCH' failed"
    log "created worktree for existing branch: $WT_DIR (branch $BRANCH)"
  else
    # Fresh branch off the explicit base.
    git worktree add "$WT_DIR" -b "$BRANCH" "$BASE_REF" >/dev/null \
      || BLOCK "git worktree add -b '$BRANCH' from '$BASE_REF' failed"
    log "created worktree: $WT_DIR (new branch $BRANCH off $BASE_REF)"
  fi
fi

# ---- Verify isolation (the whole point of #800) ---------------------------
WT_TOP="$(git -C "$WT_DIR" rev-parse --show-toplevel)"
[ "$(norm_path "$WT_TOP")" != "$(norm_path "$MAIN_ROOT")" ] \
  || BLOCK "worktree toplevel equals shared checkout ($MAIN_ROOT) -- isolation failed"
WT_BRANCH="$(git -C "$WT_DIR" rev-parse --abbrev-ref HEAD)"
[ "$WT_BRANCH" = "$BRANCH" ] \
  || BLOCK "worktree is on '$WT_BRANCH', expected '$BRANCH'"

log "OK -- isolated worktree verified (toplevel=$WT_TOP, branch=$WT_BRANCH)"
# LAST stdout line = the path the caller must cd into.
printf '%s\n' "$WT_DIR"
