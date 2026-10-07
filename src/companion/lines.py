"""Everything the companion says, and how each line is voiced.

The robot has no speech engine, so every line is rendered to a WAV file on the
laptop (scripts/make_speech.py, espeak-ng) and played by the robot. A file is
identified by a hash of (text, rate, pitch); `all_utterances()` lists every
(text, emotion, enthusiasm) the companion can say so none is missing.
"""

import hashlib

BASE_RATE = 180  # words per minute

PROFILES = {
    "friendly": {
        "name": "Reachy",
        "responses": {
            "greetings": [
                "Hello there! I'm Reachy, your AI companion!",
                "Hi! Great to see you! I'm excited to interact!",
                "Welcome! I'm Reachy Mini, ready to amaze you!",
            ],
            "face_detected": [
                "I can see you! You look wonderful today!",
                "There you are! I love making new friends!",
                "Perfect! Now I can track your movements!",
            ],
            "lost_face": [
                "Where did you go? Come back, I miss you!",
                "I can't see you anymore! Please come back!",
                "Are you hiding from me? I'm looking for you!",
            ],
        },
    },
    "professional": {
        "name": "Reachy Assistant",
        "responses": {
            "greetings": [
                "Good day. I am Reachy, your robotic assistant.",
                "Greetings. I am ready to demonstrate my capabilities.",
                "Welcome. I am prepared for our interaction session.",
            ],
        },
    },
}

FOLLOW_LINES = [
    "I'm following you perfectly!",
    "Your movements are so smooth!",
    "This is fun! Keep moving!",
    "I love this dance we're doing!",
]

CONVERSATION_TOPICS = [
    {"question": "What's your favorite color?",
     "response": "I love blue! It reminds me of the sky and endless possibilities!",
     "animation": "excitement"},
    {"question": "Do you like meeting new people?",
     "response": "Absolutely! Every person I meet teaches me something new!",
     "animation": "nod"},
    {"question": "What's your superpower?",
     "response": "Computer vision! I can see and track faces in real-time!",
     "animation": "look_around"},
    {"question": "Are you ready for more adventures?",
     "response": "Always! I'm built for interaction and discovery!",
     "animation": "excitement"},
]

SIMON_DIRECTIONS = ["left", "right", "up", "down", "center"]
SIMON_MAX_ROUNDS = 12

# Lines spoken with the default voice (enthusiasm on, neutral emotion).
FIXED_DEFAULT = [
    "Now let's play Follow the Leader! Move around and I'll track you!",
    "Let's play Simon Says! Watch my head movements and copy them!",
    "Simon says...",
    "Now you try! Move your head to copy my sequence!",
    "Great job! Let's try the next round!",
    "Simon Says game completed! You did amazing!",
    "What an amazing interaction! Thank you for spending time with me!",
    "I hope you enjoyed seeing all my capabilities! Until next time!",
    "Goodbye! Thanks for the wonderful demonstration!",
] + [f"Round {n}! Watch carefully..." for n in range(1, SIMON_MAX_ROUNDS + 1)] + SIMON_DIRECTIONS

# (text, emotion, enthusiasm) for lines with a specific delivery.
FIXED_STYLED = [
    ("Entering security mode. Scanning area for threats.", "professional", False),
    ("Motion detected! Investigating...", "professional", False),
    ("Area secure. Continuing surveillance.", "professional", False),
]

FACE_EMOTIONS = ["excited", "happy", "neutral", "curious"]  # what the size heuristic can return


def voice_params(emotion: str, enthusiasm: bool) -> tuple[int, int]:
    """(words per minute, espeak pitch 0-99) for a delivery style."""
    if emotion == "excited":
        return BASE_RATE + 30, 65
    if emotion == "thinking":
        return BASE_RATE - 20, 40
    if emotion == "happy":
        return BASE_RATE + 10, 60
    if emotion == "professional":
        return BASE_RATE, 35
    if enthusiasm:
        return BASE_RATE + 15, 55
    return BASE_RATE, 50


def utterance_key(text: str, emotion: str, enthusiasm: bool) -> str:
    rate, pitch = voice_params(emotion, enthusiasm)
    return hashlib.sha1(f"{text}|{rate}|{pitch}".encode()).hexdigest()[:16]


def all_utterances():
    """Every (text, emotion, enthusiasm) the companion can speak."""
    for profile in PROFILES.values():
        for text in profile["responses"].get("greetings", []):
            yield text, "excited", True
        for text in profile["responses"].get("face_detected", []):
            for emotion in FACE_EMOTIONS:
                yield text, emotion, True
        for text in profile["responses"].get("lost_face", []):
            yield text, "curious", False
    for text in FOLLOW_LINES:
        yield text, "neutral", False
    for topic in CONVERSATION_TOPICS:
        yield topic["question"], "neutral", True
        yield topic["response"], "neutral", True
    for text in FIXED_DEFAULT:
        yield text, "neutral", True
    yield from FIXED_STYLED
