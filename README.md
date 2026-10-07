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
   (SSH user `pollen`; copy `.env.example` to `.env` and set `ROBOT_PASSWORD` and `REACHY_AP_PASSWORD` so the scripts do not prompt)
4. Optional, after moving the robot or changing the camera: calibrate. Sit still facing
   it, then on the robot `/venvs/apps_venv/bin/python ~/reachy-mini-demo/look_straight.py --calibrate`
   (stop the service first). Results go to `~/camera_calibration.json`.

Personality on top of the tracking (disable all of it with `--no-reactions`):
- mostly looks straight at the nearest person; every ~8-18 s it tilts its head left or right
  (9-14 deg) for 2-4 s, then straightens up;
- random small ear movements (twitch, flutter, perk, double flap) every ~5-12 s;
- if you hold up an open hand: a short "yeaay!" (head up and bobbing, ears wide open, ~1.2 s) while the
  waist turns onto the head at 3.5x speed, so it has arrived before the gesture ends;
  A real hand detector runs on the robot (MediaPipe palm + 21 hand landmarks, OpenCV Zoo ONNX models
  in `models/`, fetched by `scripts/get_models.sh`): the hand must have index, middle, ring and pinky
  extended for ~0.5 s, 6 s cooldown, and only when a face is within about 3 m (otherwise the detector does no work at all). The live view outlines the hand and its landmarks. It costs about
  0.2 s of one core per check on the Pi; constants at the top of `src/face_centering.py`;
- when the person leaves (2 s) the ears droop (ears only), then it looks around slowly for ~11 s
  (+/-45 deg, the waist follows the head); with nobody around it twitches an antenna now and then.
Timings are constants at the top of `src/look_straight.py`; the gestures live in `src/gestures.py`.

Debug live view (off by default: no port opened, no extra CPU): set `REACHY_VIEW=1` for the service or run, then open <http://reachy-mini.local:8080> (camera with the centre
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

## AI companion (port of sankarlabs/reachy-ai-companion-demo)

`scripts/run_companion.sh [--loop] [--only MODE] [--profile professional]` runs the
companion on the robot (the face-centering service is paused meanwhile, then restarted;
Ctrl+C stops it cleanly). Modes: greeting, face_tracking (Follow the Leader), simon_says,
conversation, security, farewell. The original did not work on SDK >= 1.11 (it used
`reachy_mini.head.look_at`, which no longer exists, and `cv2.VideoCapture(0)`); this port
drives the head and camera through the same detector and smooth controller as the
face-centering demo, so every move is smooth, and the antennas move with the gestures
and while talking. Two things are kept as in the original: the "emotion detector" is a
face-size heuristic, and "security mode" reacts to any face, not to motion.

**Voice.** The robot has no speech engine, so each line is rendered on the laptop to
`speech/*.wav` and played by the robot (head wobbles with the audio):

```
uv venv tts-venv && uv pip install --python tts-venv/bin/python kokoro-onnx soundfile
# model files (~120 MB): kokoro-v1.0.int8.onnx + voices-v1.0.bin from
# https://github.com/thewh1teagle/kokoro-onnx/releases (model-files-v1.0) into ~/reachy/kokoro
tts-venv/bin/python scripts/make_speech.py --sample af_heart af_bella bf_emma   # compare
tts-venv/bin/python scripts/make_speech.py --voice af_heart --force
scripts/install.sh                                                              # copy to robot
```

`--engine espeak` is a no-model fallback (robotic). Speaker volume:
`POST /api/volume/set {"volume": 100}` on the robot (it was 67).

## Waist

The waist moves only when it has to: during the search, on the open-hand reaction (`rig.body_boost()`: it
turns onto the head quickly and tightly for a moment), or when the head can really not follow the person
any more (more than 30 deg from the waist; the head itself can go 35). Otherwise it stays put and never
drifts back to the front by itself; the head does the following. When nobody is around the head goes back
to the front and the waist follows only once it is more than 30 deg away. Constants: `BODY_FOLLOW_DELTA`,
`BODY_FAST`.

## Safety envelope

Everything sent to the head (tracking, tilts, gestures, the search, the companion) goes through
`limit_pose()` in `src/face_centering.py` so the head cannot hit its own body: head yaw at most 35 deg
from the waist and 75 deg overall; pitch within +/-24 deg and roll within +/-12 deg *together* (an
ellipse: a big tilt leaves less room for a big nod), shrinking by up to 50% when the head is turned
more than 15 deg from the waist. The waist (body yaw, +/-100 deg) moves slowly. Tune the constants at the top of
that file if you want it tighter.

## Troubleshooting

- *Robot not found*: it is off (button released), or not on your network. Check the
  hotspot's device list, or look for the `reachy-mini-ap` WiFi network.
- *Head limp after a manual restart*: motors are off after boot; the service enables them.
- *`wake_up` timeouts*: motors disabled, `POST /api/motors/set_mode/enabled` on the robot.
