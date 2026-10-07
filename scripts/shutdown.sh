#!/usr/bin/env bash
# Clean shutdown (or reboot) of the robot.
#
# Stops the demo first, which folds the head into the sleep pose and releases the
# motors, then powers the Raspberry Pi off. Releasing the button on the robot's
# back does the same power-off (the robot's gpio-shutdown service runs
# `shutdown -h now`; systemd stops the demo first, so it parks cleanly too).
#
# Usage: scripts/shutdown.sh [--reboot]     (ROBOT=pollen@reachy-mini.local)
set -euo pipefail
[ -f "$(dirname "$0")/../.env" ] && { set -a; . "$(dirname "$0")/../.env"; set +a; }  # ROBOT_PASSWORD, REACHY_AP_PASSWORD

ROBOT="${ROBOT:-pollen@reachy-mini.local}"
action="-h now"
[ "${1:-}" = "--reboot" ] && action="-r now"

ssh_() { if [ -n "${ROBOT_PASSWORD:-}" ]; then sshpass -p "$ROBOT_PASSWORD" ssh "$@"; else ssh "$@"; fi; }

echo "==> Stopping the demo (sleep pose, motors off)..."
ssh_ "$ROBOT" "sudo systemctl stop reachy-look.service"   # blocks until it has parked
echo "==> shutdown $action"
ssh_ "$ROBOT" "sudo shutdown $action" || true             # connection drops as it goes down
echo "Done."
