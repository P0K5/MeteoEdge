#!/usr/bin/env bash
# =============================================================================
# extract_data.sh — Database and Log Extraction Script
#
# Safely extracts consistent copies of MeteoEdge database files and logs
# using SQLite's `.backup` command (safe against live writers) instead of
# plain file copy, which can capture torn pages during concurrent writes.
#
# The `.backup` command uses SQLite's backup API, handles locking correctly,
# and retries on transient contention — producing database integrity
# guaranteed copies even while the bot is writing.
#
# Usage:
#   extract_data.sh /path/to/dest                    # Extract to /path/to/dest
#   DEST_DIR=/backup/today extract_data.sh           # Use env var for destination
#   extract_data.sh /mnt/backups DB_PATH=data/alt.db # Extract alternate DB
#
# Examples:
#   mkdir -p /backups/$(date +\%Y-\%m-\%d)
#   extract_data.sh /backups/$(date +\%Y-\%m-\%d)
#
# Exit code:
#   0  — Extraction succeeded; all DBs verified with PRAGMA integrity_check = ok
#   1  — sqlite3 CLI not found, destination dir not writable, or integrity check failed
#
# Output:
#   - $DEST_DIR/meteoedge.db       (consistent copy of live trading DB)
#   - $DEST_DIR/analytics.db       (consistent copy of analytics DB)
#   - $DEST_DIR/logs/              (copy of logs directory)
#   - stdout: progress messages and integrity check results
#   - stderr: error messages and warnings
#
# Dependencies:
#   - sqlite3 CLI (checked at startup)
#   - git (for repo root detection)
#   - bash 4.0+
# =============================================================================

set -euo pipefail

# Find the repo root
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [[ -z "$REPO_ROOT" ]]; then
    echo "ERROR: not in a git repository. Cannot determine repo root." >&2
    exit 1
fi

# Parse command line — destination dir is first positional arg
DEST_DIR="${1:-${DEST_DIR:-}}"

if [[ -z "$DEST_DIR" ]]; then
    echo "ERROR: destination directory required." >&2
    echo "Usage: $0 <dest-dir>" >&2
    echo "   or: DEST_DIR=<dir> $0" >&2
    exit 1
fi

# Check sqlite3 CLI is available
if ! command -v sqlite3 &>/dev/null; then
    echo "ERROR: sqlite3 CLI not found on PATH." >&2
    echo "       Install sqlite3 and ensure it is accessible." >&2
    exit 1
fi

# Resolve DB paths from environment, with defaults
DB_PATH_LIVE="${DB_PATH:-${REPO_ROOT}/data/meteoedge.db}"
DB_PATH_ANALYTICS="${REPO_ROOT}/data/analytics.db"

# Check source DBs exist (they may not in a fresh repo)
if [[ ! -f "$DB_PATH_LIVE" ]]; then
    echo "WARNING: meteoedge.db not found at $DB_PATH_LIVE" >&2
    echo "         Continuing with analytics.db only..." >&2
    DB_PATH_LIVE=""
fi

if [[ ! -f "$DB_PATH_ANALYTICS" ]]; then
    echo "WARNING: analytics.db not found at $DB_PATH_ANALYTICS" >&2
    echo "         Continuing with meteoedge.db only..." >&2
    DB_PATH_ANALYTICS=""
fi

# If neither DB exists, fail
if [[ -z "$DB_PATH_LIVE" ]] && [[ -z "$DB_PATH_ANALYTICS" ]]; then
    echo "ERROR: no database files found to extract." >&2
    echo "       Expected: $REPO_ROOT/data/meteoedge.db or $REPO_ROOT/data/analytics.db" >&2
    exit 1
fi

# Create destination directory if it does not exist
if ! mkdir -p "$DEST_DIR" 2>/dev/null; then
    echo "ERROR: Cannot create destination directory: $DEST_DIR" >&2
    exit 1
fi

# Verify destination is writable
if ! touch "$DEST_DIR/.extract_test" 2>/dev/null; then
    echo "ERROR: Destination directory not writable: $DEST_DIR" >&2
    exit 1
fi
rm -f "$DEST_DIR/.extract_test"

echo "[extract] Starting extraction to $DEST_DIR"

# Helper function to extract and verify a single database
extract_db() {
    local src_db="$1"
    local dest_db="$2"
    local db_name="$3"

    if [[ ! -f "$src_db" ]]; then
        echo "[extract] SKIP: $db_name not found at $src_db"
        return 0
    fi

    echo "[extract] Extracting $db_name via .backup..."

    # Use sqlite3 .backup to safely copy the DB while it may be live
    if ! sqlite3 "$src_db" ".backup '$dest_db'" 2>/dev/null; then
        echo "ERROR: .backup failed for $db_name" >&2
        return 1
    fi

    echo "[extract] Running PRAGMA integrity_check on $db_name..."

    # Verify the backup is consistent
    local integrity_result
    integrity_result=$(sqlite3 "$dest_db" "PRAGMA integrity_check;" 2>&1 || true)

    if [[ "$integrity_result" != "ok" ]]; then
        echo "ERROR: Integrity check FAILED for $db_name" >&2
        echo "       Result: $integrity_result" >&2
        echo "       Backup is INVALID and must not be used." >&2
        return 1
    fi

    echo "[extract] ✓ $db_name backup verified (integrity_check = ok)"
    return 0
}

# Extract both databases
FAILED=0

if [[ -n "$DB_PATH_LIVE" ]]; then
    if ! extract_db "$DB_PATH_LIVE" "$DEST_DIR/meteoedge.db" "meteoedge.db"; then
        FAILED=1
    fi
fi

if [[ -n "$DB_PATH_ANALYTICS" ]]; then
    if ! extract_db "$DB_PATH_ANALYTICS" "$DEST_DIR/analytics.db" "analytics.db"; then
        FAILED=1
    fi
fi

# Copy logs directory (plain recursive copy is safe; logs are append-only)
if [[ -d "$REPO_ROOT/logs" ]]; then
    echo "[extract] Copying logs directory..."
    if cp -r "$REPO_ROOT/logs" "$DEST_DIR/logs" 2>/dev/null; then
        echo "[extract] ✓ logs/ copied"
    else
        echo "WARNING: Could not copy logs directory" >&2
    fi
else
    echo "[extract] NOTE: logs directory not found; skipping"
fi

# Report results
if [[ $FAILED -eq 0 ]]; then
    echo "[extract] ✓ Extraction complete and verified"
    echo "[extract]   Output directory: $DEST_DIR"
    echo "[extract]   Contents:"
    ls -lh "$DEST_DIR" | tail -n +2 | sed 's/^/     /'
    exit 0
else
    echo "ERROR: Extraction failed. Check errors above." >&2
    exit 1
fi
