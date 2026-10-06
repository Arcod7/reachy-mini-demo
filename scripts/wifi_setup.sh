#!/usr/bin/env bash
# Put a Reachy Mini (Wireless) on a WiFi network without the desktop app.
#
# 1. joins the robot's setup hotspot (reachy-mini-ap / reachy-mini)
# 2. asks the robot's daemon to join <ssid> (POST /wifi/connect)
# 3. moves this laptop onto <ssid> too, then looks for the robot there
#
# Usage: ./wifi_setup.sh <ssid>     (prompts for the password)
set -euo pipefail

ssid="${1:?usage: $0 <ssid>}"
read -rsp "Password for '$ssid': " password; echo

echo "==> Joining reachy-mini-ap..."
nmcli con up reachy-mini-ap >/dev/null 2>&1 || nmcli dev wifi connect reachy-mini-ap password reachy-mini >/dev/null
# Always try to get the laptop back on '$ssid', even if a step fails.
trap 'nmcli -t -f ACTIVE,SSID dev wifi list | grep -qx "yes:$ssid" || nmcli con up "$ssid" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 20); do curl -fs -m 2 http://10.42.0.1:8000/wifi/status >/dev/null && break; sleep 1; done

echo "==> Robot status: $(curl -fs -m 3 http://10.42.0.1:8000/wifi/status || echo unreachable)"
# Older robot daemons don't rescan after AP mode, so the SSID looks missing
# ("access point does not exist"). Refresh the robot's scan list first.
echo "==> Robot sees: $(curl -fs -m 20 -X POST http://10.42.0.1:8000/wifi/scan_and_list || echo '?')"
echo "==> Asking the robot to join '$ssid'..."
curl -fsS -m 10 -X POST -G http://10.42.0.1:8000/wifi/connect \
  --data-urlencode "ssid=$ssid" --data-urlencode "password=$password"
echo

echo "==> Moving this laptop to '$ssid'..."
sleep 5
if nmcli -t -f NAME con show | grep -qx "$ssid"; then
  nmcli con up "$ssid" >/dev/null
else
  nmcli dev wifi rescan 2>/dev/null || true; sleep 3
  nmcli dev wifi connect "$ssid" password "$password" >/dev/null
fi

echo "==> Looking for the robot on '$ssid' (up to 60 s)..."
for _ in $(seq 30); do
  if ip=$(getent hosts reachy-mini.local | awk '{print $1}') && [ -n "$ip" ]; then
    echo "Found it: reachy-mini.local = $ip"
    curl -fs -m 3 "http://$ip:8000/wifi/status" && echo
    exit 0
  fi
  sleep 2
done
echo "Robot not found via mDNS. Check your hotspot's connected-devices list for its IP."
exit 1
