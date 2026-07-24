#!/bin/bash

# Fetch and sync remote server data (logs/ and data/) via delta sync with rsync.
# Uses SSH key authentication from .env file (REMOTE_HOST, REMOTE_USER, REMOTE_KEY_PATH, REMOTE_PROJECT_ROOT).
# Only transfers new/modified files; exits with code 1 on failure (agent run fails).

set -euo pipefail

# Get the project root (script is in scripts/ subdirectory)
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Load .env
if [[ ! -f "$ENV_FILE" ]]; then
    echo -e "${RED}ERROR: .env file not found at $ENV_FILE${NC}"
    echo "Create one by copying .env.example and filling in REMOTE_* values."
    exit 1
fi

# Source .env (suppress shellcheck warning about dynamic sourcing)
# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

# Validate required vars
required_vars=("REMOTE_HOST" "REMOTE_USER" "REMOTE_KEY_PATH" "REMOTE_PROJECT_ROOT")
for var in "${required_vars[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        echo -e "${RED}ERROR: $var is not set in .env${NC}"
        exit 1
    fi
done

# Expand ~ to home directory
REMOTE_KEY_PATH="${REMOTE_KEY_PATH/#\~/$HOME}"

# Validate SSH key exists
if [[ ! -f "$REMOTE_KEY_PATH" ]]; then
    echo -e "${RED}ERROR: SSH key not found at $REMOTE_KEY_PATH${NC}"
    exit 1
fi

# Build rsync command with SSH key auth
# -a: archive mode (preserves permissions, timestamps, etc.)
# -v: verbose
# -z: compress during transfer
# --delete: delete local files that don't exist on remote (keeps in sync)
# --info=progress2: show overall progress
REMOTE_LOGS="${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PROJECT_ROOT}/logs/"
REMOTE_DATA="${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PROJECT_ROOT}/data/"
LOCAL_LOGS="$PROJECT_ROOT/logs"
LOCAL_DATA="$PROJECT_ROOT/data"

# Ensure local directories exist
mkdir -p "$LOCAL_LOGS" "$LOCAL_DATA"

echo -e "${YELLOW}Syncing remote data...${NC}"
echo "  Remote host: $REMOTE_HOST"
echo "  User: $REMOTE_USER"
echo "  SSH key: $REMOTE_KEY_PATH"
echo "  Local project root: $PROJECT_ROOT"
echo ""

# Sync logs/
echo -e "${YELLOW}Syncing logs/ directory...${NC}"
if ! rsync -avz --delete \
    -e "ssh -i '$REMOTE_KEY_PATH' -o StrictHostKeyChecking=no" \
    --info=progress2 \
    "$REMOTE_LOGS" "$LOCAL_LOGS"; then
    echo -e "${RED}ERROR: Failed to sync logs/ directory${NC}"
    exit 1
fi

# Sync data/
echo -e "${YELLOW}Syncing data/ directory...${NC}"
if ! rsync -avz --delete \
    -e "ssh -i '$REMOTE_KEY_PATH' -o StrictHostKeyChecking=no" \
    --info=progress2 \
    "$REMOTE_DATA" "$LOCAL_DATA"; then
    echo -e "${RED}ERROR: Failed to sync data/ directory${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Data sync complete${NC}"
echo "  logs/: $(find "$LOCAL_LOGS" -type f | wc -l) files"
echo "  data/: $(find "$LOCAL_DATA" -type f | wc -l) files"
