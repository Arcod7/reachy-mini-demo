"""Open-hand detection: MediaPipe palm detector + 21-point hand landmarks (OpenCV Zoo
ONNX models, run with OpenCV's dnn module; see third_party/opencv_zoo).

`OpenHandDetector.detect(crop_bgr)` finds the most confident palm in the image, gets the
hand landmarks and says whether the hand is open: index, middle, ring and pinky fingers
all extended (a fist, a pointing hand or a peace sign is not open).
"""

import os

import numpy as np

from mp_handpose import MPHandPose
from mp_palmdet import MPPalmDet

PALM_MODEL = "palm_detection_mediapipe_2023feb.onnx"
POSE_MODEL = "handpose_estimation_mediapipe_2023feb.onnx"

# MediaPipe landmark ids: 0 wrist; thumb 1-4; then (mcp, pip, dip, tip) for each finger.
FINGERS = {"index": (5, 6, 8), "middle": (9, 10, 12), "ring": (13, 14, 16), "pinky": (17, 18, 20)}
EXTEND_RATIO = 1.15  # tip must be this much farther from the wrist than the middle joint


def available(model_dir: str) -> bool:
    return all(os.path.exists(os.path.join(model_dir, m)) for m in (PALM_MODEL, POSE_MODEL))


def finger_states(lm: np.ndarray) -> dict[str, bool]:
    """Which fingers are extended, from the 21 (x, y) landmarks."""
    wrist = lm[0]
    states = {}
    for name, (_, pip, tip) in FINGERS.items():
        states[name] = bool(np.linalg.norm(lm[tip] - wrist) > EXTEND_RATIO * np.linalg.norm(lm[pip] - wrist))
    # thumb: the tip is farther from the base of the little finger than the joint below it
    states["thumb"] = bool(np.linalg.norm(lm[4] - lm[17]) > 1.1 * np.linalg.norm(lm[3] - lm[17]))
    return states


class OpenHandDetector:
    def __init__(self, model_dir: str, palm_score: float = 0.6, pose_conf: float = 0.7) -> None:
        self.palm = MPPalmDet(os.path.join(model_dir, PALM_MODEL), nmsThreshold=0.3,
                              scoreThreshold=palm_score, topK=100)
        self.pose = MPHandPose(os.path.join(model_dir, POSE_MODEL), confThreshold=pose_conf)

    def detect(self, crop_bgr: np.ndarray) -> dict | None:
        """None if no hand; else {"open", "fingers", "bbox" (x1,y1,x2,y2), "landmarks" or None}."""
        palms = self.palm.infer(crop_bgr)
        if len(palms) == 0:
            return None
        palm = palms[int(np.argmax(palms[:, -1]))]
        result = self.pose.infer(crop_bgr, palm)
        if result is None:  # a palm-like blob but the landmarks are not confident
            return {"open": False, "fingers": {}, "bbox": palm[:4].copy(), "landmarks": None}
        lm = result[4:67].reshape(21, 3)[:, :2]
        fingers = finger_states(lm)
        is_open = all(fingers[f] for f in FINGERS)
        return {"open": is_open, "fingers": fingers, "bbox": result[:4].copy(), "landmarks": lm}
