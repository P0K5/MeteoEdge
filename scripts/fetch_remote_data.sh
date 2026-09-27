#!/bin/bash

# DEPRECATED: This script is no longer maintained.
#
# Reason for retirement
# ---------------------
# This script attempted to sync live SQLite databases using rsync, which copies
# files byte-for-byte while they are being written. This produces torn/unreadable
# databases paired with inconsistent -wal sidecars, a fundamental flaw that
# cannot be fixed within the rsync approach.
#
# Migration
# ---------
# Use scripts/Fetch-RemoteData.ps1 (or scripts/remote_sync.py directly) instead.
# For details on what each platform supports and how to migrate, see:
# docs/OPERATIONS.md (section on remote data sync).
#
# Quick start
# -----------
# PowerShell:
#   powershell -File scripts/Fetch-RemoteData.ps1
#
# Unix/Linux with Python:
#   python scripts/remote_sync.py

echo "ERROR: fetch_remote_data.sh is deprecated as of issue #1239." >&2
echo "" >&2
echo "It copied live SQLite databases byte-for-byte while the bot was writing" >&2
echo "them, producing torn/unreadable databases. This is a fundamental flaw" >&2
echo "in the rsync approach that cannot be fixed." >&2
echo "" >&2
echo "Use scripts/Fetch-RemoteData.ps1 or scripts/remote_sync.py instead." >&2
echo "See docs/OPERATIONS.md for details." >&2
exit 1
