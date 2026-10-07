#!/usr/bin/env bash
# Install the demo on the robot and make it start at boot.
#
# Usage: scripts/install.sh            (ROBOT=pollen@reachy-mini.local by default)
# The robot's SSH password is "root" unless you changed it; export ROBOT_PASSWORD
# to avoid the prompts (needs sshpass).
set -euo pipefail
[ -f "$(dirname "$0")/../.env" ] && { set -a; . "$(dirname "$0")/../.env"; set +a; }  # ROBOT_PASSWORD, REACHY_AP_PASSWORD

ROBOT="${ROBOT:-pollen@reachy-mini.local}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/home/pollen/reachy-mini-demo

ssh_() { if [ -n "${ROBOT_PASSWORD:-}" ]; then sshpass -p "$ROBOT_PASSWORD" ssh -o StrictHostKeyChecking=accept-new "$@"; else ssh -o StrictHostKeyChecking=accept-new "$@"; fi; }
scp_() { if [ -n "${ROBOT_PASSWORD:-}" ]; then sshpass -p "$ROBOT_PASSWORD" scp -o StrictHostKeyChecking=accept-new "$@"; else scp -o StrictHostKeyChecking=accept-new "$@"; fi; }

echo "==> Copying files to $ROBOT"
ssh_ "$ROBOT" "mkdir -p $DEST"
scp_ "$HERE"/src/*.py "$ROBOT:$DEST/"
[ -d "$HERE/models" ] && { ssh_ "$ROBOT" "mkdir -p $DEST/models"; scp_ "$HERE"/models/*.onnx "$ROBOT:$DEST/models/"; }
[ -d "$HERE/speech" ] && { ssh_ "$ROBOT" "mkdir -p $DEST/speech"; scp_ "$HERE"/speech/* "$ROBOT:$DEST/speech/"; }
scp_ "$HERE/systemd/reachy-look.service" "$HERE/systemd/reachy-wifi-window.service" "$HERE/scripts/wifi_window.sh" "$ROBOT:/tmp/"

echo "==> Installing and enabling the service"
ssh_ "$ROBOT" "sudo systemctl stop reachy-look.service 2>/dev/null || true
  pkill -f '[l]ook_straight.py' || true   # leftover manual runs
  sudo install -m 644 /tmp/reachy-look.service /etc/systemd/system/reachy-look.service
  sudo install -m 755 /tmp/wifi_window.sh /usr/local/sbin/reachy-wifi-window
  sudo install -m 644 /tmp/reachy-wifi-window.service /etc/systemd/system/reachy-wifi-window.service
  sudo systemctl daemon-reload
  sudo systemctl enable reachy-look.service reachy-wifi-window.service
  grep -q '^export WIFI_WINDOW_S=' ~/.zshrc 2>/dev/null || printf '\n# Seconds after boot before the robot switches its Wi-Fi off (0 disables, Wi-Fi then stays on)\nexport WIFI_WINDOW_S=900\n' >> ~/.zshrc
  sudo systemctl restart reachy-wifi-window.service
  sudo systemctl restart reachy-look.service
  sleep 5; systemctl is-active reachy-look.service"

echo "Done. It now starts at every boot. Wi-Fi switches itself off 15 min after boot once nobody is connected"
echo "(power-cycle the robot to open a new window; change WIFI_WINDOW_S in ~/.zshrc on the robot, 0 disables)."
