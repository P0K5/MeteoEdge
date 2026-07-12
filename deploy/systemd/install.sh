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

echo "Copying unit files from $SRC_DIR to $UNIT_DIR..."
install -m 0644 "$SRC_DIR/meteoedge.service"            "$UNIT_DIR/meteoedge.service"
install -m 0644 "$SRC_DIR/meteoedge-dashboard.service"  "$UNIT_DIR/meteoedge-dashboard.service"
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

echo "Reloading systemd..."
systemctl daemon-reload

echo "Enabling and starting services..."
systemctl enable --now meteoedge.service
systemctl enable --now meteoedge-dashboard.service
systemctl enable --now meteoedge-settle.timer
systemctl enable --now meteoedge-archive.timer
systemctl enable --now meteoedge-capture-forecasts.timer
systemctl enable --now meteoedge-prob-cap-report.timer
systemctl enable --now meteoedge-purge-retention.timer

echo
echo "Done. Current status:"
systemctl --no-pager status meteoedge.service meteoedge-dashboard.service meteoedge-settle.timer meteoedge-archive.timer meteoedge-capture-forecasts.timer meteoedge-prob-cap-report.timer meteoedge-purge-retention.timer || true
echo
echo "Tail the bot log with:  journalctl -u meteoedge.service -f"
echo "Tail dashboard log with:  journalctl -u meteoedge-dashboard.service -f"
echo "Next settle run:  systemctl list-timers meteoedge-settle.timer"
echo "Next archive run:  systemctl list-timers meteoedge-archive.timer"
echo "Next capture run:  systemctl list-timers meteoedge-capture-forecasts.timer"
echo "Next prob-cap report run:  systemctl list-timers meteoedge-prob-cap-report.timer"
echo "Next purge-retention run:  systemctl list-timers meteoedge-purge-retention.timer"
