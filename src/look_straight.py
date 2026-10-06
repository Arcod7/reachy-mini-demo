"""Reachy Mini looks the nearest person straight in the eyes, smoothly.

Pipeline (all on the robot, see face_centering.py):

  camera frame -> YuNet face detector (the SDK's) -> midpoint between the eyes
      -> where was the head when the frame was taken? (pose history, minus the
         camera latency) + pixel offset / measured px-per-degree -> world-frame
         direction of the face
      -> One-Euro filter (steady when you are still, quick when you move)
      -> two-stage smoothing + critically damped spring (on the measured pose)
      -> set_target @ 50 Hz

Live view + state: http://<robot>:8080

  python look_straight.py              run
  python look_straight.py --calibrate  fit camera latency + px/deg (sit still,
                                       facing the robot; the head swings ~12 s)
"""

import argparse
import signal
import time

from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS

from face_centering import PORT, Rig, calibrate, park


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt  # SIGTERM (systemctl stop, shutdown) takes the Ctrl+C path


def main(do_calibrate: bool) -> None:
    signal.signal(signal.SIGTERM, _raise_interrupt)
    with ReachyMini() as mini:
        mini.stop_head_tracking()  # make sure the daemon's own tracker isn't steering
        mini.enable_motors()  # motors are off after every boot
        mini.wake_up()
        mini.goto_target(antennas=INIT_ANTENNAS_JOINT_POSITIONS, body_yaw=None, duration=0.5)
        rig = Rig(mini)
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
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            park(mini, rig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calibrate", action="store_true", help="fit camera latency + px/deg, then exit")
    main(parser.parse_args().calibrate)
