# reachy-mini-demo

Reachy Mini (Wireless) looks the nearest person straight in the eyes: the midpoint
between their eyes is kept at the centre of the camera image, with smooth head
motion. The script runs **on the robot** and starts at every boot.

```
camera -> YuNet face detector -> eye midpoint -> face direction in the world
       (head pose at the frame's timestamp + pixel offset / measured px-per-degree)
       -> One-Euro filter -> goal smoothing + critically damped spring -> set_target @ 50 Hz
```

Inverse kinematics is the SDK's (analytical, in the robot's daemon); this code only
sends head poses.

## Why not the daemon's built-in tracking

`start_head_tracking()` low-passes the face position (it reports where you *were*, so
the head overshoots), ignores moves under 0.02 and aims at the nose. Here the detection
is raw, aimed at the eyes, and aligned in time with the head pose of its frame. The
spring acts on the *measured* pose, so at rest the eyes are centred by construction.
Measured: error settles to ~0.005 (target ±0.01) when the person is still.

## Setup

Requirements: a Reachy Mini Wireless with daemon **>= 1.11.0**, on the same network as
your computer (the robot cannot join WPA-Enterprise networks; a phone hotspot works).

1. Put the robot on WiFi (it exposes a `reachy-mini-ap` hotspot until configured):
   `scripts/wifi_setup.sh <ssid>`
2. Update the robot if needed: `curl -X POST http://reachy-mini.local:8000/update/start`
3. Install and enable the service: `scripts/install.sh`
   (SSH user `pollen`, default password `root`; set `ROBOT_PASSWORD` to skip prompts)
4. Optional, after moving the robot or changing the camera: calibrate. Sit still facing
   it, then on the robot `/venvs/apps_venv/bin/python ~/reachy-mini-demo/look_straight.py --calibrate`
   (stop the service first). Results go to `~/camera_calibration.json`.

Live view and tracking state: <http://reachy-mini.local:8080> (camera with the centre
cross and a ±0.01 target box, face detected, error, head command, CENTRED badge).

## Start, stop, shutdown

| | |
|---|---|
| Autostart | `reachy-look.service`, enabled by `install.sh`; starts after the robot's daemon |
| Stop | `sudo systemctl stop reachy-look` — folds into the sleep pose, releases the motors (~6 s) |
| Shut the robot down | `scripts/shutdown.sh` (stops the demo cleanly, then `shutdown -h now`) |
| Reboot | `scripts/shutdown.sh --reboot` |
| Back button | released = the robot's gpio service runs `shutdown -h now`; the demo parks first |
| Logs | `journalctl -u reachy-look -f` |

Motors are disabled after every boot; the script enables them itself at start.

## Tuning (top of `src/look_straight.py`)

- `OMEGA` spring stiffness: lower = slower, softer approach (default 3.0).
- `GOAL_TAU` smoothing of the goal before the spring (default 0.25 s).
- `MAX_SPEED`, `YAW_LIMIT`, `PITCH_LIMIT`: speed cap and range.
- `FILTER_MIN_CUTOFF` / `FILTER_BETA`: how steady it is when you sit still vs how fast it
  follows when you move.
- `HOLD_S`, `LOST_RECENTRE_S`: what it does when the face disappears.

## Troubleshooting

- *Robot not found*: it is off (button released), or not on your network. Check the
  hotspot's device list, or look for the `reachy-mini-ap` WiFi network.
- *Head limp after a manual restart*: motors are off after boot; the service enables them.
- *`wake_up` timeouts*: motors disabled, `POST /api/motors/set_mode/enabled` on the robot.
