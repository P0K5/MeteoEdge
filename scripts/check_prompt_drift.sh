#!/usr/bin/env bash
# =============================================================================
# check_prompt_drift.sh
#
# Drift guard for issue #748: within the agent prompt surface (CLAUDE.md,
# agents/, .claude/), the governance protocol and graphify rules must each
# exist in exactly one place. Fails if an agent file, skill, or CLAUDE.md
# re-inlines content that belongs in .claude/instructions/ or the
# board-status skill.
#
# Operational scripts, workflows, and docs/ are NOT prompt surface and are
# free to mention these strings.
#
# Usage: bash scripts/check_prompt_drift.sh   (from the repo root; CI runs it
# in the lint job)
# =============================================================================

set -euo pipefail

FAIL=0

prompt_surface() {
  ls CLAUDE.md 2>/dev/null
  find agents .claude -name '*.md' -type f 2>/dev/null
}

check_only_in() {
  local pattern="$1" desc="$2"
  shift 2
  local allowed=("$@")

  local bad=""
  while IFS= read -r f; do
    grep -q -e "$pattern" "$f" || continue
    local ok=0
    for a in "${allowed[@]}"; do
      [ "$f" = "$a" ] && ok=1 && break
    done
    [ "$ok" -eq 1 ] || bad="$bad $f"
  done < <(prompt_surface)

  if [ -n "$bad" ]; then
    echo "DRIFT: $desc re-inlined in:$bad"
    echo "       Single source of truth: ${allowed[*]}"
    FAIL=1
  fi
}

# The GraphQL status mutation lives only in the board-status skill (usage
# reference) and governance.md (rule reference) — no agent file or CLAUDE.md
# may re-inline it.
check_only_in "updateProjectV2ItemFieldValue" \
  "GraphQL status mutation" \
  ".claude/skills/board-status/SKILL.md" \
  ".claude/instructions/governance.md"

# The mandatory status-transition table lives only in governance.md;
# board-status carries the per-event comment templates.
check_only_in "Issue created and added to board" \
  "governance status-transition table" \
  ".claude/instructions/governance.md"

# Graphify setup/rules live only in instructions/graphify.md.
check_only_in "pip install graphifyy" \
  "graphify setup rules" \
  ".claude/instructions/graphify.md"

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "check_prompt_drift: FAILED — move duplicated protocol text into the"
  echo "single-source file and reference it instead (see issue #748)."
  exit 1
fi

echo "check_prompt_drift: OK — no duplicated protocol content in the prompt surface."
