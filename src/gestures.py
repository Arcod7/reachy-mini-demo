"""Head and antenna gestures shared by the demos (all built on face_centering.Rig).

Every head move goes through the Rig's smooth spring, so gestures are as smooth as
tracking; quick ones just use a stiffer spring.
"""

import math
import random
import threading
import time

from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS

from face_centering import Rig

FAST_SPEED = math.radians(150)  # for quick gestures

# The original companion's emotion choreography: (yaw, up, roll, seconds), angles in
# radians. +yaw = robot's left, +up = looks up.
EMOTIONS = {
    "joy": [(0.0, 0.3, 0.1, 0.5), (0.2, 0.1, 0.2, 0.3), (-0.2, 0.1, -0.2, 0.3), (0.0, 0.0, 0.0, 0.5)],
    "curiosity": [(-0.2, 0.2, 0.0, 0.8), (0.2, 0.2, 0.0, 0.8), (0.0, 0.0, 0.1, 0.5)],
    "excitement": [(0.0, 0.4, 0.2, 0.2), (0.3, -0.1, 0.0, 0.2), (-0.3, -0.1, 0.0, 0.2), (0.0, 0.0, 0.0, 0.3)],
    "thinking": [(-0.1, 0.1, -0.1, 1.0), (0.1, 0.1, -0.1, 1.0), (0.0, 0.0, 0.0, 0.5)],
}


class Gestures:
    def __init__(self, rig: Rig, stop: threading.Event | None = None) -> None:
        self.rig = rig
        self.stop = stop or threading.Event()

    def wait(self, seconds: float) -> bool:
        """Sleep; True if we should stop."""
        return self.stop.wait(seconds)

    def move(self, yaw=None, pitch=None, roll=None, seconds: float = 1.0, wait: bool = True) -> None:
        """Smooth scripted head move (degrees); quick moves get a stiffer spring."""
        omega = min(max(4.5 / seconds, 2.0), 14.0)
        self.rig.look_deg(yaw, pitch, roll, omega=omega, tau=min(0.25, seconds / 4), vmax=FAST_SPEED)
        if wait:
            self.rig.wait_settled(timeout=seconds * 2 + 1.0, stop=self.stop)

    def look_forward(self, seconds: float = 1.0) -> None:
        self.move(0, 0, 0, seconds)

    def flap(self, seconds: float, amount: float = 0.45) -> None:
        """Quick antenna flap (both open, then settle) in the background."""
        def run() -> None:
            left, right = INIT_ANTENNAS_JOINT_POSITIONS
            self.rig.antennas(left=left - amount, right=right + amount)
            time.sleep(max(0.15, seconds * 0.5))
            self.rig.antennas()
        threading.Thread(target=run, daemon=True).start()

    def emotion_animation(self, name: str) -> None:
        """The original's choreographed emotions, with the antennas joining in."""
        for yaw, up, roll, seconds in EMOTIONS[name]:
            if self.stop.is_set():
                return
            self.flap(seconds)
            self.move(math.degrees(yaw), -math.degrees(up), math.degrees(roll), seconds)
            self.wait(0.1)
        self.rig.antennas()

    def animate_excitement(self) -> None:
        """Three quick head wiggles with an antenna flap each."""
        for _ in range(3):
            for yaw, pitch in ((0, 0), (20, -8), (-20, -8)):
                if self.stop.is_set():
                    return
                self.flap(0.3, 0.4)
                self.move(yaw, pitch, 0, 0.3)
                self.wait(0.05)
        self.look_forward(0.6)
        self.rig.antennas()

    def twitch(self) -> None:
        """A curious flick of one antenna."""
        left, right = INIT_ANTENNAS_JOINT_POSITIONS
        if random.random() < 0.5:
            self.rig.antennas(left=left - 0.4)
        else:
            self.rig.antennas(right=right + 0.4)
        self.wait(0.3)
        self.rig.antennas()

    def droop(self, seconds: float = 1.5) -> None:
        """Sad: antennas fall outward, head dips. Returns early if stopping."""
        left, right = INIT_ANTENNAS_JOINT_POSITIONS
        self.rig.antennas(left=left - 0.8, right=right + 0.8)
        self.move(None, 9, 0, 1.0, wait=False)
        self.wait(seconds)

    def search(self, seconds: float = 9.0) -> bool:
        """Slow look-around (like a patrol). True as soon as a face shows up."""
        end, side = time.monotonic() + seconds, 1
        next_sweep = 0.0
        while time.monotonic() < end and not self.stop.is_set():
            if self.rig.face() is not None:
                return True
            if time.monotonic() >= next_sweep:
                self.rig.look_deg(30 * side, -3, 0, omega=1.6, tau=0.4)
                side, next_sweep = -side, time.monotonic() + 3.5
            self.wait(0.1)
        return False
