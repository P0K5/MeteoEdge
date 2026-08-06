#!/usr/bin/env bash
#
# Run the M3 window diagnostics on the bot host, publish the output to a branch,
# and leave the checkout exactly as it was found: on master, clean.
#
# Written for the case where the operator is on a phone and cannot copy files
# off the server -- the report is pushed to git so it can be read from GitHub,
# and the short form is echoed at the end so it can be relayed by hand even if
# the push fails.
#
# Usage (one line, from anywhere):
#
#   cd ~/MeteoEdge && bash scripts/run_m3_diagnostics.sh
#
# Defaults to the live clean-data window (the clock start, 2026-08-06). Pass an
# earlier date to re-read the contaminated window:
#
#   bash scripts/run_m3_diagnostics.sh --since 2026-07-24
#
# Safety properties, in order of importance:
#
#   1. It refuses to start on a dirty tree. On this host the working tree IS
#      production -- merged means deployed -- so stashing or overwriting
#      someone's in-flight edit is not an acceptable cost for a read-only report.
#   2. It always returns to master, including on any error path, via an EXIT
#      trap set before the first branch switch.
#   3. It fast-forwards master BEFORE running, so the report always describes
#      the code that is actually deployed rather than whatever the host last
#      happened to check out.
#   4. It reads. It opens no database connection and writes nothing under
#      logs/ or data/.

set -euo pipefail

REPORT_BRANCH="claude/m3-diagnostics-output"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_FULL="/tmp/m3diag_full_${STAMP}.txt"
TMP_BRIEF="/tmp/m3diag_brief_${STAMP}.txt"

cd "$(git rev-parse --show-toplevel)"

# --- 1. refuse to touch MODIFIED TRACKED files --------------------------------
# Deliberately --untracked-files=no. Untracked files survive a branch switch
# untouched, and this host legitimately accumulates them: report outputs under
# backtest_results/, .grib_cache/, SQLite -wal/-shm sidecars. Blocking on those
# would mean the script could never run on the machine it was written for.
# Modified *tracked* files are the real hazard -- the working tree here is
# production, so a switch that discarded an in-flight edit would be costly.
DIRTY_TRACKED="$(git status --porcelain --untracked-files=no)"
if [ -n "$DIRTY_TRACKED" ]; then
    echo "ABORT: tracked files are modified. Nothing has been changed."
    echo
    echo "$DIRTY_TRACKED"
    exit 1
fi

UNTRACKED_N="$(git ls-files --others --exclude-standard | wc -l | tr -d ' ')"
if [ "$UNTRACKED_N" -gt 0 ]; then
    echo "note: ${UNTRACKED_N} untracked file(s) present -- left untouched."
fi

# --- 2. guarantee we end on master, whatever happens below --------------------
restore_master() {
    local rc=$?
    git checkout -q master 2>/dev/null || true
    if [ $rc -ne 0 ]; then
        echo
        echo "--- run failed (exit $rc); checkout restored to master ---"
        git status --short || true
    fi
    return $rc
}
trap restore_master EXIT

# --- 3. run the DEPLOYED code, not a stale checkout ---------------------------
# The diagnostic ships on master. Fast-forward first so the report describes the
# code the bot is actually running; a host sitting on last week's master would
# otherwise report through last week's classifier.
echo "==> updating master"
git fetch -q origin master
git checkout -q master
git pull -q --ff-only origin master 2>/dev/null \
    || echo "note: could not fast-forward master (diverged?) -- running as-is"

# --- 4. run it (read-only) ----------------------------------------------------
echo "==> running against logs/bracket_evals.*.jsonl"
PYTHONPATH=. python3 -m src.scripts.m3_window_diagnostics "$@" \
    > "$TMP_FULL"  2>&1
PYTHONPATH=. python3 -m src.scripts.m3_window_diagnostics --brief "$@" \
    > "$TMP_BRIEF" 2>&1

# --- 5. publish to a report branch cut fresh from origin/master ---------------
# Branched from origin/master rather than the local HEAD so the report carries
# no unrelated local state, and so re-running never stacks onto a stale base.
echo "==> publishing to ${REPORT_BRANCH}"
git checkout -q -B "$REPORT_BRANCH" origin/master

mkdir -p backtest_results
cp "$TMP_FULL"  "backtest_results/m3_window_diagnostics_${STAMP}.txt"
cp "$TMP_BRIEF" "backtest_results/m3_window_diagnostics_brief_${STAMP}.txt"
git add "backtest_results/m3_window_diagnostics_${STAMP}.txt" \
        "backtest_results/m3_window_diagnostics_brief_${STAMP}.txt"
git commit -q -m "chore(m3): window diagnostics output ${STAMP}

Read-only diagnostic run on the bot host. Reports parser version by day, mass
conservation split by censoring and by which end the ladder was truncated, and
scoreable station-day accrual toward the 300 bar. #917 and #920 are both fixed
and deployed; this run is the standing regression check on that. See
docs/REMEDIATION_PLAN.md, M3 section."

# A push failure must not cost the run: the report is already on screen and in
# /tmp, and the operator can relay the short form by hand.
PUSH_LOG="/tmp/m3diag_push_${STAMP}.log"
if git push -u origin "$REPORT_BRANCH" > "$PUSH_LOG" 2>&1; then
    echo "==> pushed ${REPORT_BRANCH}"
    PUSHED=yes
else
    # Show the reason. Swallowing it (the previous `2>/dev/null`) turned an
    # actionable credential or branch-protection error into a dead end.
    echo "!!! push FAILED -- reason:"
    sed 's/^/    /' "$PUSH_LOG" | head -12
    echo
    echo "    The full report is at ${TMP_FULL} and the short form prints below."
    PUSHED=no
fi

# --- 6. back to master, up to date, clean ------------------------------------
git checkout -q master
git pull -q --ff-only origin master 2>/dev/null \
    || echo "note: could not fast-forward master (diverged?) -- left as-is"

echo
echo "=============================================================="
echo " server is on: $(git rev-parse --abbrev-ref HEAD)  @ $(git rev-parse --short HEAD)"
if [ -z "$(git status --porcelain --untracked-files=no)" ]; then
    echo " tracked files: clean (untracked files left as they were)"
else
    echo " tracked files: NOT CLEAN --"
    git status --short --untracked-files=no
fi
echo "=============================================================="
echo
cat "$TMP_BRIEF"

# When the push failed there is no way to read the full report remotely, so
# print it too rather than leaving it stranded in /tmp on a machine the
# operator may only reach from a phone.
if [ "${PUSHED:-no}" = "no" ]; then
    echo
    echo "----- full report (push failed, so it is printed in full) -----"
    cat "$TMP_FULL"
fi
