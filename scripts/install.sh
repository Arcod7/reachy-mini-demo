#!/usr/bin/env bash
# Install the demo on the robot (a git checkout of this repo) and make it start at boot.
#
# Usage: scripts/install.sh            (ROBOT=pollen@reachy-mini.local by default; BRANCH=<branch> to override)
# The robot clones/pulls the PUBLIC repo over HTTPS, so push your commits first. It deploys the branch
# you are on. Not in git, copied from here: models/ (scripts/get_models.sh) and speech/ (scripts/make_speech.py).
# ROBOT_PASSWORD (SSH password, see .env.example) avoids the prompts (needs sshpass).
set -euo pipefail
[ -f "$(dirname "$0")/../.env" ] && { set -a; . "$(dirname "$0")/../.env"; set +a; }  # ROBOT_PASSWORD, REACHY_AP_PASSWORD

ROBOT="${ROBOT:-pollen@reachy-mini.local}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/home/pollen/reachy-mini-demo
BRANCH="${BRANCH:-$(git -C "$HERE" rev-parse --abbrev-ref HEAD)}"
REPO="$(git -C "$HERE" remote get-url origin | sed -E 's|^git@github.com:|https://github.com/|')"

ssh_() { if [ -n "${ROBOT_PASSWORD:-}" ]; then sshpass -p "$ROBOT_PASSWORD" ssh -o StrictHostKeyChecking=accept-new "$@"; else ssh -o StrictHostKeyChecking=accept-new "$@"; fi; }
scp_() { if [ -n "${ROBOT_PASSWORD:-}" ]; then sshpass -p "$ROBOT_PASSWORD" scp -o StrictHostKeyChecking=accept-new "$@"; else scp -o StrictHostKeyChecking=accept-new "$@"; fi; }

if [ -n "$(git -C "$HERE" log --oneline "origin/$BRANCH..HEAD" 2>/dev/null)" ]; then
  echo "WARNING: unpushed commits on $BRANCH: the robot only gets what is on $REPO (git push first)." >&2
fi

echo "==> Stopping the demo, getting $BRANCH of $REPO on $ROBOT"
ssh_ "$ROBOT" "sudo systemctl stop reachy-look.service 2>/dev/null || true
  pkill -f '[l]ook_straight.py' || true   # leftover manual runs
  if [ -d $DEST/.git ]; then
    git -C $DEST fetch -q origin && git -C $DEST checkout -q $BRANCH && git -C $DEST pull -q --ff-only origin $BRANCH
  else
    [ -e $DEST ] && mv $DEST $DEST.old.\$(date +%s)   # the former flat copy
    git clone -q --branch $BRANCH $REPO $DEST
  fi
  git -C $DEST log --oneline -1"

echo "==> Copying models/ and speech/ (not in git)"
for d in models speech; do
  [ -d "$HERE/$d" ] && { ssh_ "$ROBOT" "mkdir -p $DEST/$d"; scp_ "$HERE/$d"/* "$ROBOT:$DEST/$d/"; }
done

echo "==> Installing and enabling the services"
ssh_ "$ROBOT" "sudo install -m 644 $DEST/systemd/reachy-look.service /etc/systemd/system/reachy-look.service
  sudo install -m 755 $DEST/scripts/wifi_window.sh /usr/local/sbin/reachy-wifi-window
  sudo install -m 644 $DEST/systemd/reachy-wifi-window.service /etc/systemd/system/reachy-wifi-window.service
  sudo systemctl daemon-reload
  sudo systemctl enable reachy-look.service reachy-wifi-window.service
  grep -q '^export WIFI_WINDOW_S=' ~/.zshrc 2>/dev/null || printf '\n# Seconds after boot before the robot switches its Wi-Fi off (0 disables, Wi-Fi then stays on)\nexport WIFI_WINDOW_S=900\n' >> ~/.zshrc
  sudo systemctl restart reachy-wifi-window.service
  sudo systemctl restart reachy-look.service
  sleep 5; systemctl is-active reachy-look.service"

echo "Done. It now starts at every boot. In hotspot mode, Wi-Fi and Bluetooth switch off 15 min after boot / the last connection"
echo "(power-cycle the robot to open a new window; change WIFI_WINDOW_S in ~/.zshrc on the robot, 0 disables)."
