#!/usr/bin/env bash
# Run the AI companion demo on the robot (live output here, Ctrl+C stops it cleanly).
#
# The face-centering service and the companion both drive the head, so the service
# is stopped for the run and started again afterwards.
#
# Usage: scripts/run_companion.sh [--loop] [--only MODE] [--profile friendly|professional]
#        (ROBOT=pollen@reachy-mini.local; ROBOT_PASSWORD for sshpass)
set -euo pipefail
[ -f "$(dirname "$0")/../.env" ] && { set -a; . "$(dirname "$0")/../.env"; set +a; }  # ROBOT_PASSWORD, REACHY_AP_PASSWORD

ROBOT="${ROBOT:-pollen@reachy-mini.local}"
DEST=/home/pollen/reachy-mini-demo
ssh_() { if [ -n "${ROBOT_PASSWORD:-}" ]; then sshpass -p "$ROBOT_PASSWORD" ssh "$@"; else ssh "$@"; fi; }

restore() { echo "==> Restarting the face-centering service"; ssh_ "$ROBOT" "sudo systemctl start reachy-look.service" || true; }
trap restore EXIT

echo "==> Stopping the face-centering service (it parks the head)"
ssh_ "$ROBOT" "sudo systemctl stop reachy-look.service"
echo "==> Companion (Ctrl+C to stop)  "
ssh_ -t "$ROBOT" "cd $DEST/src && /venvs/apps_venv/bin/python -u -m companion $*" 2>&1 | grep --line-buffered -v -i "onnxruntime"
