#!/usr/bin/env bash
# Install / reinstall the MeteoEdge systemd units.
# Run as root (sudo) from anywhere — paths are absolute.
set -euo pipefail

UNIT_DIR="/etc/systemd/system"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (sudo)."
    exit 1
fi

echo "Stopping and disabling the old midnight-noon timer (if present)..."
systemctl disable --now meteoedge.timer 2>/dev/null || true

# Retired units. Left enabled on a host they loop forever, because
# Restart=always retries a process that exits 0 -- meteoedge-dashboard.service
# accumulated ~78,000 restarts over nine days before anyone looked.
#
#   meteoedge-dashboard.service — redundant. run.py:930 already calls
#     start_dashboard(), so meteoedge.service serves :8000 from an embedded
#     thread. The standalone launcher could never work anyway: start_dashboard()
#     spawns a daemon thread and returns, so as an entrypoint the process exits
#     immediately and takes the thread with it.
#   meteoedge-shadow.service — never shipped from this repo. Pointed at a
#     ~/MeteoEdge-Shadow/ tree that no longer exists and failed at step STDOUT,
#     so it never started an interpreter. ~348,000 restarts.
echo "Removing retired units (if present)..."
for retired in meteoedge-dashboard.service meteoedge-shadow.service; do
    systemctl disable --now "$retired" 2>/dev/null || true
    rm -f "$UNIT_DIR/$retired"
done
systemctl reset-failed 2>/dev/null || true

echo "Copying unit files from $SRC_DIR to $UNIT_DIR..."
install -m 0644 "$SRC_DIR/meteoedge.service"            "$UNIT_DIR/meteoedge.service"
install -m 0644 "$SRC_DIR/meteoedge-settle.service"     "$UNIT_DIR/meteoedge-settle.service"
install -m 0644 "$SRC_DIR/meteoedge-settle.timer"       "$UNIT_DIR/meteoedge-settle.timer"
install -m 0644 "$SRC_DIR/meteoedge-archive.service"               "$UNIT_DIR/meteoedge-archive.service"
install -m 0644 "$SRC_DIR/meteoedge-archive.timer"                "$UNIT_DIR/meteoedge-archive.timer"
install -m 0644 "$SRC_DIR/meteoedge-capture-forecasts.service"    "$UNIT_DIR/meteoedge-capture-forecasts.service"
install -m 0644 "$SRC_DIR/meteoedge-capture-forecasts.timer"      "$UNIT_DIR/meteoedge-capture-forecasts.timer"
install -m 0644 "$SRC_DIR/meteoedge-prob-cap-report.service"      "$UNIT_DIR/meteoedge-prob-cap-report.service"
install -m 0644 "$SRC_DIR/meteoedge-prob-cap-report.timer"        "$UNIT_DIR/meteoedge-prob-cap-report.timer"
install -m 0644 "$SRC_DIR/meteoedge-purge-retention.service"      "$UNIT_DIR/meteoedge-purge-retention.service"
install -m 0644 "$SRC_DIR/meteoedge-purge-retention.timer"        "$UNIT_DIR/meteoedge-purge-retention.timer"
install -m 0644 "$SRC_DIR/meteoedge-copy-screening.service"       "$UNIT_DIR/meteoedge-copy-screening.service"
install -m 0644 "$SRC_DIR/meteoedge-copy-screening.timer"         "$UNIT_DIR/meteoedge-copy-screening.timer"
install -m 0644 "$SRC_DIR/meteoedge-resolve-outcomes.service"     "$UNIT_DIR/meteoedge-resolve-outcomes.service"
install -m 0644 "$SRC_DIR/meteoedge-resolve-outcomes.timer"       "$UNIT_DIR/meteoedge-resolve-outcomes.timer"
install -m 0644 "$SRC_DIR/meteoedge-health-report.service"        "$UNIT_DIR/meteoedge-health-report.service"
install -m 0644 "$SRC_DIR/meteoedge-health-report.timer"          "$UNIT_DIR/meteoedge-health-report.timer"
install -m 0644 "$SRC_DIR/meteoedge-copy-signals.service"          "$UNIT_DIR/meteoedge-copy-signals.service"
install -m 0644 "$SRC_DIR/meteoedge-copy-settle.service"          "$UNIT_DIR/meteoedge-copy-settle.service"
install -m 0644 "$SRC_DIR/meteoedge-copy-settle.timer"            "$UNIT_DIR/meteoedge-copy-settle.timer"

echo "Reloading systemd..."
systemctl daemon-reload

echo "Enabling and starting services..."
systemctl enable --now meteoedge.service
systemctl enable --now meteoedge-copy-signals.service
systemctl enable --now meteoedge-settle.timer
systemctl enable --now meteoedge-archive.timer
systemctl enable --now meteoedge-capture-forecasts.timer
systemctl enable --now meteoedge-prob-cap-report.timer
systemctl enable --now meteoedge-purge-retention.timer
systemctl enable --now meteoedge-copy-screening.timer
systemctl enable --now meteoedge-copy-settle.timer
systemctl enable --now meteoedge-resolve-outcomes.timer
systemctl enable --now meteoedge-health-report.timer

echo
echo "Done. Current status:"
systemctl --no-pager status meteoedge.service meteoedge-copy-signals.service meteoedge-settle.timer meteoedge-archive.timer meteoedge-capture-forecasts.timer meteoedge-prob-cap-report.timer meteoedge-purge-retention.timer meteoedge-copy-screening.timer meteoedge-copy-settle.timer meteoedge-resolve-outcomes.timer meteoedge-health-report.timer || true
echo
echo "Tail the bot log with:  journalctl -u meteoedge.service -f"
echo "Tail the copy-signal log with:  journalctl -u meteoedge-copy-signals.service -f"
echo "Next settle run:  systemctl list-timers meteoedge-settle.timer"
echo "Next archive run:  systemctl list-timers meteoedge-archive.timer"
echo "Next capture run:  systemctl list-timers meteoedge-capture-forecasts.timer"
echo "Next prob-cap report run:  systemctl list-timers meteoedge-prob-cap-report.timer"
echo "Next purge-retention run:  systemctl list-timers meteoedge-purge-retention.timer"
echo "Next copy-screening run:  systemctl list-timers meteoedge-copy-screening.timer"
echo "Next copy-settle run:  systemctl list-timers meteoedge-copy-settle.timer"
echo "Next health report run:  systemctl list-timers meteoedge-health-report.timer"
