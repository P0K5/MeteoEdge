#!/usr/bin/env bash
# =============================================================================
# test_graphify_guard.sh
#
# Unit tests for check_graphify_guard.sh's pure decision function
# (files_touch_graphify_out). Sources the real script (BASH_SOURCE guard
# stops it from running main()'s network calls) and calls that function
# directly against fixture file lists -- no curl mocking, no network,
# exercises the actual code path rather than grepping the script's text.
#
# Usage:
#   bash scripts/test_graphify_guard.sh
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=check_graphify_guard.sh
source "$REPO_ROOT/scripts/check_graphify_guard.sh"
# The sourced script's own `set -euo pipefail` now applies to this shell too
# -- turn off -e so a failed assertion below doesn't abort the whole suite
# before it can tally the rest.
set +e

TESTS_RUN=0
TESTS_FAILED=0

# assert_touches <expected 0|1> <file-list...>
assert_touches() {
  local expected="$1"; shift
  TESTS_RUN=$((TESTS_RUN + 1))
  local files
  files=$(printf '%s\n' "$@")
  if echo "$files" | files_touch_graphify_out; then
    actual=0
  else
    actual=1
  fi
  if [ "$actual" -eq "$expected" ]; then
    echo "  ✓ ($*)"
  else
    echo "  ✗ ($*) — expected exit $expected, got $actual"
    TESTS_FAILED=$((TESTS_FAILED + 1))
  fi
}

echo "TEST: unrelated files only -> does not touch graphify-out/ (exit 1)"
assert_touches 1 "src/handlers.py" "src/config.py" "tests/test_handlers.py"

echo "TEST: one graphify-out/ file among others -> touches (exit 0)"
assert_touches 0 "src/handlers.py" "graphify-out/graph.json"

echo "TEST: only graphify-out/ files -> touches (exit 0)"
assert_touches 0 "graphify-out/graph.json" "graphify-out/manifest.json"

echo "TEST: empty file list -> does not touch (exit 1)"
assert_touches 1 ""

echo "TEST: a path that merely starts with 'graphify-out' as a substring but"
echo "      isn't the directory (e.g. 'graphify-outline.py') must NOT match"
assert_touches 1 "graphify-outline.py"

echo "TEST: nested graphify-out/ path still matches (exit 0)"
assert_touches 0 "graphify-out/wiki/index.md"

echo ""
echo "============================================"
echo "Tests run:    $TESTS_RUN"
echo "Tests failed: $TESTS_FAILED"
echo "============================================"

if [ "$TESTS_FAILED" -eq 0 ]; then
  echo "✓ All tests passed"
  exit 0
else
  echo "✗ $TESTS_FAILED test(s) failed"
  exit 1
fi
