#!/usr/bin/env bash
# Download the hand models (about 8 MB, Apache 2.0, OpenCV Zoo) into models/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
base=https://huggingface.co/opencv
[ -s models/palm_detection_mediapipe_2023feb.onnx ] || curl -fL -o models/palm_detection_mediapipe_2023feb.onnx \
  $base/palm_detection_mediapipe/resolve/main/palm_detection_mediapipe_2023feb.onnx
[ -s models/handpose_estimation_mediapipe_2023feb.onnx ] || curl -fL -o models/handpose_estimation_mediapipe_2023feb.onnx \
  $base/handpose_estimation_mediapipe/resolve/main/handpose_estimation_mediapipe_2023feb.onnx
ls -la models
