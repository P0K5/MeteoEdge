#!/usr/bin/env bash
# =============================================================================
# bootstrap_session.sh
#
# Resolves all static GitHub Project GraphQL IDs needed by Claude Code agents
# and writes them to .claude/session-context.env.
#
# Usage (manual):  GH_TOKEN=<pat> bash scripts/bootstrap_session.sh
# Usage (CI):      called by .github/workflows/refresh-session-context.yml
#
# Requires: gh CLI + a token with `project` read scope (GH_PROJECT_PAT secret).
# =============================================================================

set -euo pipefail

OWNER="P0K5"
REPO="MeteoEdge"
OUT=".claude/session-context.env"

mkdir -p .claude

echo "[bootstrap] Resolving GitHub Project GraphQL IDs for $OWNER/$REPO"

# ---------------------------------------------------------------------------
# 1. Project ID
# ---------------------------------------------------------------------------
PROJECT_JSON=$(gh api graphql -f query='
  query($owner: String!, $repo: String!) {
    repository(owner: $owner, name: $repo) {
      projectsV2(first: 5) {
        nodes { id title number }
      }
    }
  }' -f owner="$OWNER" -f repo="$REPO")

PROJECT_ID=$(echo "$PROJECT_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)['data']['repository']['projectsV2']['nodes']
if not nodes:
    raise SystemExit('[bootstrap] ERROR: No GitHub Projects v2 found for this repo. Create one first.')
print(nodes[0]['id'])
")

echo "[bootstrap] PROJECT_ID=$PROJECT_ID"

# ---------------------------------------------------------------------------
# 2. Status field ID + option IDs
# ---------------------------------------------------------------------------
FIELD_JSON=$(gh api graphql -f query='
  query($projectId: ID!) {
    node(id: $projectId) {
      ... on ProjectV2 {
        fields(first: 30) {
          nodes {
            ... on ProjectV2SingleSelectField {
              id name
              options { id name }
            }
          }
        }
      }
    }
  }' -f projectId="$PROJECT_ID")

FIELD_BLOCK=$(echo "$FIELD_JSON" | python3 -c "
import sys, json
fields = json.load(sys.stdin)['data']['node']['fields']['nodes']
for f in fields:
    if f.get('name') == 'Status':
        print(f\"STATUS_FIELD_ID={f['id']}\")
        for opt in f['options']:
            key = opt['name'].upper().replace(' ', '_')
            print(f\"STATUS_OPT_{key}={opt['id']}\")
        sys.exit(0)
raise SystemExit('[bootstrap] ERROR: Status field not found in project. Ensure a Status single-select field exists.')
")

echo "[bootstrap] Status field + options resolved."

# ---------------------------------------------------------------------------
# 3. Issue item IDs (ALL board items, via cursor pagination)
#
#     items(first: 100) returns only the first page. Boards larger than one
#     page (this one is 4x over) MUST be walked with a pageInfo/endCursor loop,
#     otherwise higher-numbered issues silently never get an ITEM_ID_ISSUE_
#     entry no matter how many times the script is re-run (see issue #793).
#
#     The same pattern applies to any *(first: N) query that can exceed N —
#     e.g. fields(first: 30) above — if the project ever grows past that bound.
# ---------------------------------------------------------------------------
ITEM_BLOCK=$(GH_BIN="${GH_BIN:-gh}" python3 - "$PROJECT_ID" <<'PYEOF'
import json, os, subprocess, sys

project_id = sys.argv[1]
gh_bin = os.environ.get("GH_BIN", "gh")

QUERY = """
  query($projectId: ID!, $cursor: String) {
    node(id: $projectId) {
      ... on ProjectV2 {
        items(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            content {
              ... on Issue { number title }
            }
          }
        }
      }
    }
  }
"""

def fetch(cursor):
    cmd = [gh_bin, "api", "graphql",
           "-f", "query=" + QUERY,
           "-f", "projectId=" + project_id]
    if cursor:
        cmd += ["-f", "cursor=" + cursor]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)["data"]["node"]["items"]

# number -> item id (a de-dupe map: each issue maps to a single board item)
entries = {}
cursor = None
pages = 0
while True:
    items = fetch(cursor)
    pages += 1
    for item in items["nodes"]:
        content = item.get("content") or {}
        if "number" in content:
            entries[content["number"]] = item["id"]
    page_info = items["pageInfo"]
    if page_info["hasNextPage"]:
        cursor = page_info["endCursor"]
    else:
        break

sys.stderr.write(
    f"[bootstrap] items query walked {pages} page(s), "
    f"{len(entries)} issue-backed board item(s) resolved.\n"
)

if not entries:
    print("# (no issues currently on the board)")
else:
    print("\n".join(f"ITEM_ID_ISSUE_{n}={entries[n]}" for n in sorted(entries)))
PYEOF
)

COUNT=$(echo "$ITEM_BLOCK" | grep -c 'ITEM_ID_ISSUE_' || true)
echo "[bootstrap] $COUNT board item(s) resolved."

# ---------------------------------------------------------------------------
# 4. Write .claude/session-context.env
# ---------------------------------------------------------------------------
GENERATED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

cat > "$OUT" <<EOF
# =============================================================================
# MeteoEdge — GitHub Project Session Context
# Auto-generated by scripts/bootstrap_session.sh
# Last refreshed: $GENERATED_AT
#
# HOW TO USE (Tech Lead PM at session start):
#   1. Read this file at the start of every Execution Mode session.
#   2. Inject the values below into EVERY developer spawn prompt under
#      a '## Pre-resolved GitHub Context' section.
#   3. Tell agents: 'Use these directly — DO NOT run GraphQL lookups.'
#
# To manually refresh:  GH_TOKEN=<pat> bash scripts/bootstrap_session.sh
# To trigger via CI:    Actions → 'Refresh Session Context' → Run workflow
#
# DO NOT edit manually — overwritten on every CI run.
# =============================================================================

GITHUB_PROJECT_ID=$PROJECT_ID

$FIELD_BLOCK

# Issue → Project board item ID mapping
# Usage in mutations: use ITEM_ID_ISSUE_<number> as the itemId parameter
$ITEM_BLOCK
EOF

echo "[bootstrap] Written to $OUT"

# ---------------------------------------------------------------------------
# 5. Render agent `model:` frontmatter from .claude/model-config.env
#    designer + mid-dev use AGENT_MODEL_STRONG, junior-dev uses AGENT_MODEL_LIGHT.
#    Falls back to the Anthropic preset (sonnet/haiku) if no config is present.
# ---------------------------------------------------------------------------
MODEL_CONFIG=".claude/model-config.env"

AGENT_MODEL_STRONG="sonnet"
AGENT_MODEL_LIGHT="haiku"
if [ -f "$MODEL_CONFIG" ]; then
  # shellcheck disable=SC1090
  source "$MODEL_CONFIG"
  echo "[bootstrap] Model config loaded: strong=$AGENT_MODEL_STRONG light=$AGENT_MODEL_LIGHT"
else
  echo "[bootstrap] $MODEL_CONFIG not found — using Anthropic defaults (sonnet/haiku)"
fi

render_agent_model() {
  local file="$1" model="$2"
  [ -f "$file" ] || { echo "[bootstrap] WARN: $file missing, skipping"; return; }
  python3 - "$file" "$model" <<'PYEOF'
import re, sys
path, model = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    text = f.read()
new = re.sub(r"^model:.*$", f"model: {model}", text, count=1, flags=re.M)
if new != text:
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"[bootstrap] {path}: model -> {model}")
else:
    print(f"[bootstrap] {path}: model already {model}")
PYEOF
}

render_agent_model ".claude/agents/designer.md"   "$AGENT_MODEL_STRONG"
render_agent_model ".claude/agents/mid-dev.md"    "$AGENT_MODEL_STRONG"
render_agent_model ".claude/agents/junior-dev.md" "$AGENT_MODEL_LIGHT"

echo ""
echo "===== Session Context ====="
cat "$OUT"
echo "==========================="
