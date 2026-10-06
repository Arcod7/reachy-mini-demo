"""Open-hand detection in its own low-priority process, so its numpy/OpenCV work can
never stall the 50 Hz head controller (the Pi's cores and Python's GIL are shared).

Protocol on stdin/stdout, started by face_centering.Rig:
  parent -> child : 8 bytes (height, width as uint32), then height*width*3 BGR bytes
  child  -> parent: one JSON line: null (no hand) or
                    {"open": bool, "bbox": [x1,y1,x2,y2], "landmarks": [[x,y],...] | null}
  child -> parent first line: "ready"
Coordinates are in the pixels of the image that was sent.
"""

import json
import os
import struct
import sys


def read_exact(stream, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def main(model_dir: str) -> None:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stray prints from libraries must not corrupt the protocol
    os.nice(10)  # lowest priority: the controller always wins
    try:
        os.sched_setaffinity(0, {os.cpu_count() - 1})  # one core only, so it can never take more
    except (AttributeError, OSError):
        pass
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hand_detector

    det = hand_detector.OpenHandDetector(model_dir)
    out.write(b"ready\n")
    out.flush()
    inp = sys.stdin.buffer
    while True:
        header = read_exact(inp, 8)
        if header is None:
            return
        h, w = struct.unpack("<II", header)
        data = read_exact(inp, h * w * 3)
        if data is None:
            return
        res = det.detect(np.frombuffer(data, np.uint8).reshape(h, w, 3))
        if res is None:
            reply = None
        else:
            lm = res["landmarks"]
            reply = {"open": bool(res["open"]), "bbox": [float(v) for v in res["bbox"]],
                     "landmarks": None if lm is None else [[float(x), float(y)] for x, y in lm]}
        out.write((json.dumps(reply) + "\n").encode())
        out.flush()


if __name__ == "__main__":
    main(sys.argv[1])
