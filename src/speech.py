"""Plays the pre-rendered speech lines (see lines.py / scripts/make_speech.py).

While a line plays the head wobbles with the audio (the SDK's audio-reactive
wobbler) and the antennas flutter, more or less depending on the emotion.
"""

import math
import os
import threading
import time
import wave

import lines
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS

SPEECH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speech")

# (amplitude rad, frequency Hz) of the antenna flutter while talking.
FLUTTER = {"excited": (0.35, 3.0), "happy": (0.25, 2.2), "curious": (0.2, 1.6),
           "thinking": (0.12, 0.9), "professional": (0.04, 0.6), "neutral": (0.14, 1.3)}
PREFIX = {"excited": "😊", "happy": "😊", "thinking": "🤔", "curious": "🤔", "professional": "💼"}


class Speaker:
    def __init__(self, mini, rig, stop: threading.Event, speech_dir: str = SPEECH_DIR) -> None:
        self.mini, self.rig, self.stop, self.dir = mini, rig, stop, speech_dir
        self.missing: set[str] = set()
        self.said = 0
        mini.enable_wobbling()  # head moves with the audio

    def say(self, text: str, enthusiasm: bool = True, emotion: str = "neutral") -> None:
        """Speak `text` and return when it has finished (or the app is stopping)."""
        if self.stop.is_set():
            return
        prefix = PREFIX.get(emotion, "🎭" if enthusiasm else "🤖")
        print(f"{prefix} Reachy says: '{text}'", flush=True)
        self.said += 1
        self.rig.set_status(said=text)

        path = os.path.join(self.dir, f"{lines.utterance_key(text, emotion, enthusiasm)}.wav")
        if os.path.exists(path):
            with wave.open(path) as w:
                duration = w.getnframes() / w.getframerate()
            self.mini.media.play_sound(path)
        else:  # not rendered: say it on screen only
            if text not in self.missing:
                self.missing.add(text)
                print("⚠️  no audio for this line (run scripts/make_speech.py)", flush=True)
            duration = len(text) / 14.0

        amp, freq = FLUTTER.get(emotion, FLUTTER["neutral"])
        t0 = time.monotonic()
        while not self.stop.is_set():
            t = time.monotonic() - t0
            if t >= duration + 0.15:
                break
            s = amp * math.sin(2 * math.pi * freq * t)
            left, right = INIT_ANTENNAS_JOINT_POSITIONS
            self.rig.antennas(left=left - s, right=right + s)  # symmetric spread
            time.sleep(0.04)
        self.rig.antennas()  # back to the rest pose
        self.rig.set_status(said="")
        self.stop.wait(0.2)  # small gap between sentences
