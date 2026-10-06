"""Reachy Mini looks the nearest person straight in the eyes, smoothly.

Pipeline (all on the robot, see face_centering.py):

  camera frame -> YuNet face detector (the SDK's) -> midpoint between the eyes
      -> where was the head when the frame was taken? (pose history, minus the
         camera latency) + pixel offset / measured px-per-degree -> world-frame
         direction of the face
      -> One-Euro filter (steady when you are still, quick when you move)
      -> two-stage smoothing + critically damped spring (on the measured pose)
      -> set_target @ 50 Hz

On top of the tracking, small reactions (disable with --no-reactions):
  * they leave (2 s without a face)    -> ears droop (ears only), then it looks around
                                          slowly with its waist for ~9 s, then idles
  * nobody around                      -> an occasional curious antenna twitch
While it is tracking the antennas stay still, and the head copies the person's head
tilt (roll): the eye line is kept level in the camera image (--no-tilt to disable).

Live view + state: http://<robot>:8080

  python look_straight.py              run
  python look_straight.py --calibrate  fit camera latency + px/deg (sit still,
                                       facing the robot; the head swings ~12 s)
"""

import argparse
import random
import signal
import time

from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS

from face_centering import PORT, Rig, calibrate, park
from gestures import Gestures

PRESENT_S = 0.5  # a face must stay this long before we react
LOST_AFTER_S = 2.0  # no face for this long -> the person left
IDLE_TWITCH_S = (20.0, 40.0)  # nobody around: twitch an antenna every so often


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt  # SIGTERM (systemctl stop, shutdown) takes the Ctrl+C path


def presence_loop(rig: Rig) -> None:
    """React to people leaving; the head itself is driven by the Rig."""
    g = Gestures(rig)
    state = "idle"  # idle (nobody) | tracking (someone here)
    last_seen, first_seen = -1e9, None
    next_twitch = time.monotonic() + random.uniform(*IDLE_TWITCH_S)
    rig.set_tracking(True)
    while True:
        now = time.monotonic()
        if rig.face() is not None:
            if first_seen is None:
                first_seen = now
            last_seen = now
            if state != "tracking" and now - first_seen >= PRESENT_S:
                rig.set_tracking(True)  # calm eye contact, antennas still
                state = "tracking"
        else:
            first_seen = None
            if state == "tracking" and now - last_seen > LOST_AFTER_S:
                print("Person left: looking for them...", flush=True)
                g.droop()
                found = g.search()
                rig.body_center()  # waist back to centre (gently)
                if not found:
                    g.look_forward(2.0)
                rig.set_tracking(True)
                g.rig.antennas()
                state = "idle"
                next_twitch = time.monotonic() + random.uniform(*IDLE_TWITCH_S)
            elif state == "idle" and now >= next_twitch:
                g.twitch()
                next_twitch = time.monotonic() + random.uniform(*IDLE_TWITCH_S)
        time.sleep(0.1)


def main(do_calibrate: bool, reactions: bool, tilt: bool) -> None:
    signal.signal(signal.SIGTERM, _raise_interrupt)
    with ReachyMini() as mini:
        mini.stop_head_tracking()  # make sure the daemon's own tracker isn't steering
        mini.enable_motors()  # motors are off after every boot
        mini.wake_up()
        mini.goto_target(antennas=INIT_ANTENNAS_JOINT_POSITIONS, body_yaw=None, duration=0.5)
        rig = Rig(mini)
        rig.follow_tilt = tilt
        rig.start(controller=not do_calibrate)
        try:
            time.sleep(1.0)
            if do_calibrate:
                calibrate(mini, rig)
                return
            rig.set_tracking(True)
            cal = rig.cal
            print(f"Looking people in the eyes (latency {cal['latency_s'] * 1000:.0f} ms, "
                  f"{cal['px_per_deg_yaw']:+.1f}/{cal['px_per_deg_pitch']:+.1f} px/deg). "
                  f"View + state: http://reachy-mini.local:{PORT}", flush=True)
            if reactions:
                presence_loop(rig)
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            park(mini, rig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calibrate", action="store_true", help="fit camera latency + px/deg, then exit")
    parser.add_argument("--no-reactions", action="store_true", help="plain eye contact, no gestures")
    parser.add_argument("--no-tilt", action="store_true", help="do not copy the person's head tilt")
    args = parser.parse_args()
    main(args.calibrate, not args.no_reactions, not args.no_tilt)
