"""Reachy Mini AI Companion: a port of sankarlabs/reachy-ai-companion-demo.

Same demo as the original (greeting, Follow the Leader, Simon Says, a small
conversation, a "security" patrol, farewell), rebuilt so it works on a real robot
with SDK >= 1.11 and runs on the robot itself:

  * the original calls `reachy_mini.head.look_at(...)`, which no longer exists, and
    opens the camera with cv2.VideoCapture(0), which the daemon owns. Here the camera
    and head go through face_centering.Rig: the YuNet face detector and the smooth,
    time-aligned head controller from the face-centering demo. Scripted moves (games,
    gestures) use the same spring, so every move is smooth;
  * speech: the robot has no TTS, so the lines are pre-rendered on the laptop
    (scripts/make_speech.py) and played with the SDK; the head wobbles with the audio
    and the antennas flutter;
  * the original defined emotion animations and an "antenna wiggle" it never ran;
    here they move the head and antennas for real.

Not changed on purpose: the "emotion detector" is still the original's face-size
heuristic (closer face = more excited), not facial-expression recognition, and
"security mode" reacts to any face, not to motion.

  python companion.py            one pass through all modes
  python companion.py --loop     repeat until stopped
  python companion.py --only simon_says   one mode (greeting, face_tracking,
                                 simon_says, conversation, security, farewell)
"""

import argparse
import math
import random
import signal
import threading
import time

from reachy_mini import ReachyMini

import lines
from face_centering import Rig, park
from gestures import Gestures
from speech import Speaker

R = math.radians

# Simon Says moves, in the *user's* point of view: "left" is the user's left, i.e. the
# robot turns to its right. (yaw deg, pitch deg; +pitch = down)
SIMON_MOVES = {"left": (-35, 0), "right": (35, 0), "up": (0, -22), "down": (0, 18), "center": (0, 0)}


class Companion(Gestures):
    def __init__(self, mini: ReachyMini, rig: Rig, stop: threading.Event, profile: str) -> None:
        super().__init__(rig, stop)
        self.mini = mini
        self.speaker = Speaker(mini, rig, stop)
        self.profile = lines.PROFILES.get(profile, lines.PROFILES["friendly"])
        self.metrics = {"face_detections": 0, "modes_run": 0}
        self.emotion = "neutral"
        self._last_seq = 0
        self.started = time.monotonic()

    # ------------------------------------------------------------------ helpers

    def say(self, text: str, enthusiasm: bool = True, emotion: str = "neutral") -> None:
        self.speaker.say(text, enthusiasm, emotion)

    def pick(self, category: str) -> str:
        return random.choice(self.profile["responses"].get(category, ["Hello!"]))

    def face(self) -> dict | None:
        """Newest face, updating the 'emotion' (the original's face-size heuristic)."""
        f = self.rig.face()
        if f is not None:
            if f["seq"] != self._last_seq:
                self._last_seq = f["seq"]
                self.metrics["face_detections"] += 1
            # The original's thresholds (15000 / 8000 / 3000 px on a 640x480 frame).
            a = f["area_frac"]
            self.emotion = ("excited" if a > 0.049 else "happy" if a > 0.026
                            else "neutral" if a > 0.0098 else "curious")
        return f

    def track(self) -> None:
        self.rig.set_tracking(True)

    # ------------------------------------------------------------------ modes

    def intro(self) -> None:
        self.look_forward(1.5)
        self.say(self.pick("greetings"), True, "excited")
        self.animate_excitement()

    def greeting(self, duration: float = 10) -> None:
        self.rig.set_status(mode="greeting")
        self.track()
        end, last = time.monotonic() + duration, 0.0
        while time.monotonic() < end and not self.stop.is_set():
            if self.face() is not None and time.monotonic() - last > 5:
                self.say(self.pick("face_detected"), True, self.emotion)
                last = time.monotonic()
            self.wait(0.1)

    def face_tracking(self, duration: float = 20) -> None:
        self.rig.set_status(mode="face_tracking (Follow the Leader)")
        self.track()
        self.say("Now let's play Follow the Leader! Move around and I'll track you!")
        end = time.monotonic() + duration
        last_follow, last_seen = 0.0, time.monotonic()
        while time.monotonic() < end and not self.stop.is_set():
            now = time.monotonic()
            if self.face() is not None:
                last_seen = now
                if now - last_follow > 5:
                    self.say(random.choice(lines.FOLLOW_LINES), False)
                    last_follow = time.monotonic()
            elif now - last_seen > 8:
                self.say(self.pick("lost_face"), False, "curious")
                last_seen = time.monotonic()
            self.wait(0.1)

    def simon_says(self, duration: float = 25) -> None:
        self.rig.set_status(mode="simon_says")
        self.say("Let's play Simon Says! Watch my head movements and copy them!")
        start, round_num = time.monotonic(), 1
        names = list(SIMON_MOVES)
        while time.monotonic() - start < duration and not self.stop.is_set():
            self.say(f"Round {min(round_num, lines.SIMON_MAX_ROUNDS)}! Watch carefully...")
            sequence = random.choices(names, k=min(3 + round_num // 2, 6))
            self.say("Simon says...")
            if self.wait(1):
                break
            for name in sequence:
                if self.stop.is_set():
                    break
                self.say(name)
                yaw, pitch = SIMON_MOVES[name]
                self.move(yaw, pitch, 0, 1.0)
                if self.wait(0.5):
                    break
            if self.stop.is_set():
                break
            self.look_forward(1.0)
            self.say("Now you try! Move your head to copy my sequence!")
            if self.wait(3):  # time for the user to copy
                break
            self.say("Great job! Let's try the next round!")
            round_num += 1
            if self.wait(1):
                break
        self.say("Simon Says game completed! You did amazing!")
        self.animate_excitement()

    def conversation(self) -> None:
        self.rig.set_status(mode="conversation")
        self.track()
        topic = random.choice(lines.CONVERSATION_TOPICS)
        self.say(topic["question"])
        self.wait(2)
        self.emotion_animation("thinking")
        self.say(topic["response"])
        kind = topic["animation"]
        if kind == "excitement":
            self.animate_excitement()
        elif kind == "nod":
            for _ in range(3):
                self.move(None, 12, 0, 0.5)
                self.move(None, -6, 0, 0.5)
            self.look_forward(0.6)
        elif kind == "look_around":
            self.emotion_animation("curiosity")
            self.look_forward(1.0)

    def security(self, duration: float = 20) -> None:
        self.rig.set_status(mode="security (patrol)")
        self.say("Entering security mode. Scanning area for threats.", False, "professional")
        end = time.monotonic() + duration
        state, patrol_side, next_patrol = "patrol", 1, 0.0
        last_face, last_alert = 0.0, 0.0
        while time.monotonic() < end and not self.stop.is_set():
            now = time.monotonic()
            f = self.face()
            if f is not None:
                last_face = now
                if state == "patrol" and now - last_alert > 6:
                    self.track()  # look at whatever moved
                    self.say("Motion detected! Investigating...", False, "professional")
                    state, last_alert = "investigate", time.monotonic()
            elif state == "investigate" and now - last_face > 4:
                self.say("Area secure. Continuing surveillance.", False, "professional")
                state, next_patrol = "patrol", 0.0
            if state == "patrol" and now >= next_patrol:
                self.rig.look_deg(30 * patrol_side, -4, 0, omega=1.6, tau=0.4)  # slow sweep
                patrol_side, next_patrol = -patrol_side, now + 3.5
            self.wait(0.1)
        self.look_forward(1.5)

    def farewell(self) -> None:
        self.rig.set_status(mode="farewell")
        self.track()
        self.say("What an amazing interaction! Thank you for spending time with me!")
        self.animate_excitement()
        self.track()
        self.say("I hope you enjoyed seeing all my capabilities! Until next time!")

    MODES = ("greeting", "face_tracking", "simon_says", "conversation", "security", "farewell")

    def run(self, only: str | None = None) -> None:
        print("🎭 Reachy Mini AI Companion Starting!")
        print("=" * 60)
        if only in (None, "greeting"):
            self.intro()
        for name in self.MODES:
            if only not in (None, name) or self.stop.is_set():
                continue
            print(f"🔄 Mode: {name}", flush=True)
            getattr(self, name)()
            self.metrics["modes_run"] += 1
        self.look_forward(1.5)
        self.say("Goodbye! Thanks for the wonderful demonstration!")
        self.rig.set_status(mode="", said="")

    def summary(self) -> None:
        total = time.monotonic() - self.started
        print(f"\n🎉 AI COMPANION DEMO COMPLETED! {total:.0f} s, {self.metrics['modes_run']} modes, "
              f"{self.speaker.said} lines spoken, {self.metrics['face_detections']} face detections", flush=True)
        if self.speaker.missing:
            print(f"⚠️  {len(self.speaker.missing)} lines had no audio file", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loop", action="store_true", help="repeat until stopped")
    parser.add_argument("--only", choices=Companion.MODES, help="run a single mode")
    parser.add_argument("--profile", choices=list(lines.PROFILES), default="friendly")
    args = parser.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with ReachyMini() as mini:
        mini.stop_head_tracking()  # the daemon's own tracker must not steer
        mini.enable_motors()  # motors are off after every boot
        mini.wake_up()
        rig = Rig(mini)
        rig.start()
        companion = None
        try:
            time.sleep(1.0)
            companion = Companion(mini, rig, stop, args.profile)
            while not stop.is_set():
                companion.run(args.only)
                if not args.loop:
                    break
                companion.wait(3)
        finally:
            if companion is not None:
                companion.summary()
            try:
                mini.disable_wobbling()
            except Exception:
                pass
            park(mini, rig)


if __name__ == "__main__":
    main()
