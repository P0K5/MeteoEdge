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

# ---------------------------------------------------------------------------
# Integration tests: main()'s pagination loop, via a stub `curl` on PATH.
# GitHub caps per_page at 100 -- these prove a graphify-out/ file that only
# shows up on page 2 is still caught, not just that it *should* be in theory.
# ---------------------------------------------------------------------------

_run_main_with_stub_curl() {
  local stub_dir="$1"  # directory containing a fake `curl` executable
  # Hard timeout: a broken stub or a real bug in main()'s pagination loop
  # must fail the test suite fast, never hang CI indefinitely.
  timeout 10 env PATH="${stub_dir}:${PATH}" GITHUB_TOKEN=x PR_NUMBER=1 REPO_OWNER=o REPO_NAME=r \
    bash "$REPO_ROOT/scripts/check_graphify_guard.sh"
}

_make_stub_dir() {
  mktemp -d
}

echo "TEST: pagination — a graphify-out/ file that only appears on page 2 of"
echo "      100+ changed files is still caught (integration, stub curl)"
TESTS_RUN=$((TESTS_RUN + 1))
stub_dir=$(_make_stub_dir)
cat > "$stub_dir/curl" << 'EOF'
#!/usr/bin/env bash
# Match "&page=N" (with the leading &), never a bare "page=N" substring --
# "per_page=100" itself contains the literal substring "page=1", so a naive
# *"page=1"* match spuriously fires on every request regardless of the
# actual page number (caught the hard way: this stub first-drafted with
# the naive check and it infinite-looped every request as page=1's 100-item
# response, since per_page=100&page=2/3/4/... all still contain "page=1").
url="${*: -1}"
if [[ "$url" == *"&page=1"* ]]; then
  python3 -c 'import json; print(json.dumps([{"filename": f"src/file_{i}.py"} for i in range(100)]))'
elif [[ "$url" == *"&page=2"* ]]; then
  echo '[{"filename": "graphify-out/graph.json"}]'
else
  echo '[]'
fi
EOF
chmod +x "$stub_dir/curl"
if _run_main_with_stub_curl "$stub_dir" >/dev/null 2>&1; then
  echo "  ✗ expected exit 1 (graphify-out/ on page 2 must fail the guard), got 0"
  TESTS_FAILED=$((TESTS_FAILED + 1))
else
  echo "  ✓ page-2 graphify-out/ file correctly fails the guard"
fi
rm -rf "$stub_dir"

echo "TEST: pagination — a single page under 100 files with no graphify-out/"
echo "      change passes cleanly (no phantom extra page fetched)"
TESTS_RUN=$((TESTS_RUN + 1))
stub_dir=$(_make_stub_dir)
cat > "$stub_dir/curl" << 'EOF'
#!/usr/bin/env bash
url="${*: -1}"
if [[ "$url" == *"&page=1"* ]]; then
  echo '[{"filename": "src/a.py"}, {"filename": "src/b.py"}]'
elif [[ "$url" == *"&page=2"* ]]; then
  echo "unexpected second page fetch" >&2
  exit 1
fi
EOF
chmod +x "$stub_dir/curl"
if _run_main_with_stub_curl "$stub_dir" >/dev/null 2>&1; then
  echo "  ✓ single under-100-file page passes, loop terminates after page 1"
else
  echo "  ✗ expected exit 0, got non-zero"
  TESTS_FAILED=$((TESTS_FAILED + 1))
fi
rm -rf "$stub_dir"

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
