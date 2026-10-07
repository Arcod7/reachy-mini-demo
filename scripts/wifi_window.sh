#!/usr/bin/env bash
# Runs ON the robot (systemd: reachy-wifi-window.service). Wi-Fi is on at boot (the robot's hotspot if
# it finds no known network); WIFI_WINDOW_S seconds later it is switched off, once nobody is connected.
# Power-cycle the robot to open a new window. Bluetooth/USB recovery still work with the radio off.
set -u

# systemd never reads ~/.zshrc, so read the `export WIFI_WINDOW_S=...` line from it (0 = never switch off)
WINDOW="$(sed -n 's/^export WIFI_WINDOW_S=\([0-9]*\).*/\1/p' /home/pollen/.zshrc 2>/dev/null | tail -1)"
WINDOW="${WINDOW:-${WIFI_WINDOW_S:-900}}"
[ "$WINDOW" = 0 ] && { echo "WIFI_WINDOW_S=0: Wi-Fi stays on"; exit 0; }
IFACE="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')"

busy() {
  # an SSH session, or a client attached to our hotspot
  [ -n "$(ss -Htn state established '( sport = :22 )' 2>/dev/null)" ] && return 0
  command -v iw >/dev/null && [ -n "$IFACE" ] && iw dev "$IFACE" station dump 2>/dev/null | grep -q '^Station' && return 0
  return 1
}

sleep "$WINDOW"
while busy; do sleep 30; done
echo "Wi-Fi window over: switching the radio off"
nmcli radio wifi off
