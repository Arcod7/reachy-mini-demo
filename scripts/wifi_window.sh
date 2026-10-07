#!/usr/bin/env bash
# Runs ON the robot (systemd: reachy-wifi-window.service). Only applies to the robot's own hotspot: the
# Wi-Fi radio and Bluetooth (Pollen's reset/provisioning service) are switched off WIFI_WINDOW_S seconds
# after boot, or that long after the last SSH session / hotspot client, whichever is later.
# On a normal Wi-Fi network (client mode) nothing is switched off.
# Power-cycle the robot to open a new window (the power button and USB still work with both radios off).
set -u

# systemd never reads ~/.zshrc, so read the `export WIFI_WINDOW_S=...` line from it (0 = never switch off)
WINDOW="$(sed -n "s/^export WIFI_WINDOW_S=[\"']\{0,1\}\([0-9]*\).*/\1/p" /home/pollen/.zshrc 2>/dev/null | tail -1)"
WINDOW="${WINDOW:-${WIFI_WINDOW_S:-900}}"
[ "$WINDOW" = 0 ] && { echo "WIFI_WINDOW_S=0: Wi-Fi stays on"; exit 0; }
IFACE="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')"

now() { date +%s; }

hotspot_mode() {
  local name
  name="$(nmcli -t -f NAME,TYPE con show --active | awk -F: '$2=="802-11-wireless"{print $1; exit}')"
  [ -n "$name" ] && [ "$(nmcli -g 802-11-wireless.mode con show "$name" 2>/dev/null)" = "ap" ]
}

busy() {
  # an SSH session, or a client attached to our hotspot
  [ -n "$(ss -Htn state established '( sport = :22 )' 2>/dev/null)" ] && return 0
  command -v iw >/dev/null && [ -n "$IFACE" ] && iw dev "$IFACE" station dump 2>/dev/null | grep -q '^Station' && return 0
  return 1
}

deadline=$(( $(now) + WINDOW ))
while :; do
  sleep 30
  # not in hotspot mode, or in use: push the deadline back by a full window
  if ! hotspot_mode || busy; then
    t=$(( $(now) + WINDOW ))
    [ "$t" -gt "$deadline" ] && deadline=$t
  fi
  if [ "$(now)" -ge "$deadline" ] && hotspot_mode && ! busy; then
    break
  fi
done
echo "Hotspot window over: switching Wi-Fi and Bluetooth off"
nmcli radio wifi off
systemctl stop reachy-mini-bluetooth.service
/usr/sbin/rfkill block bluetooth  # the Bluetooth service unblocks it again at every boot
