"""Reachy Mini looks the nearest person straight in the eyes, smoothly.

Pipeline (all in this script, on the robot):

  camera frame -> YuNet face detector (the SDK's) -> midpoint between the eyes
      -> where was the head when the frame was taken? (pose history, minus the
         camera latency) + pixel offset / measured px-per-degree -> world-frame
         direction of the face
      -> One-Euro filter (steady when you are still, quick when you move)
      -> two-stage smoothing + critically damped spring (on the measured pose)
      -> set_target @ 50 Hz

Why not the daemon's own tracker: it low-passes the face position (so it reports
where you *were*, which makes the head overshoot), ignores moves < 0.02, and
aims at the nose. Here the detection is raw and time-aligned to the head pose
of its frame, so a delay can't make the head chase a stale position.

The face direction is a fixed world-frame target while you sit still. The spring
acts on the *measured* head pose (the command only integrates its velocity), so
at rest measured pose == face direction, i.e. the eye midpoint sits at the image
centre, whatever constant gap exists between commanded and real pose.

Inverse kinematics is the SDK's (analytical, in the daemon); we only send
head poses with set_target.

  python look_straight.py              run (live view + state on :8080)
  python look_straight.py --calibrate  measure the camera latency (sit still,
                                       facing the robot; the head swings ~10 s)
"""

import argparse
import bisect
import collections
import json
import math
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.rotation import Rotation as R
from reachy_mini.vision.face_detector import FaceDetector

PORT = 8080
VIEW_EVERY = 3  # annotate + encode one frame in N for the web view
DETECT_WIDTH = 384  # detector input width (px); eyes are averaged to cut noise
CONTROL_HZ = 50.0
CAL_FILE = os.path.expanduser("~/camera_calibration.json")
# Camera delay and pixels-per-degree of head motion (+yaw moves the image right,
# +pitch moves it up). Defaults measured on this robot; --calibrate refits them.
DEFAULT_CAL = {"latency_s": 0.06, "px_per_deg_yaw": 11.9, "px_per_deg_pitch": -13.9}
DEFAULT_LATENCY_S = DEFAULT_CAL["latency_s"]

# Feel. Lower OMEGA / larger GOAL_TAU = slower, softer approach.
OMEGA = 3.0  # spring stiffness (rad/s): ~1.3 s to settle
GOAL_TAU = 0.25  # first-order smoothing of the goal before the spring (s)
MAX_SPEED = math.radians(40)
YAW_LIMIT = math.radians(55)
PITCH_LIMIT = math.radians(28)
FILTER_MIN_CUTOFF = 0.6  # Hz, when the face is still (kills detector jitter)
FILTER_BETA = 12.0  # cutoff rise per rad/s of face motion (keeps up when you move)
STILL_SPEED = math.radians(2.0)  # head counts as still below this (rad/s)
HOLD_S = 0.8  # keep the last goal this long when the face vanishes
LOST_RECENTRE_S = 6.0  # then, after this long, look straight ahead
TOLERANCE = 0.01  # target accuracy (normalised image units; 1 = half the image)

lock = threading.Lock()
stop_detector = threading.Event()  # set on shutdown so the camera thread exits first
latest_jpeg: bytes | None = None
detection: dict = {"seq": 0}  # newest detection (t_arr, u, v, ...)
det_log: list | None = None  # filled during --calibrate
state: dict = {
    "detected": False, "fps": 0.0, "latency_s": DEFAULT_LATENCY_S,
    "err_x": 0.0, "err_y": 0.0, "err_x_avg": 0.0, "err_y_avg": 0.0,
    "err_rms": 0.0, "within_tol": False, "tolerance": TOLERANCE,
    "goal_err_yaw_deg": 0.0, "goal_err_pitch_deg": 0.0,
    "cmd_yaw_deg": 0.0, "cmd_pitch_deg": 0.0, "speed_deg_s": 0.0,
    "still": False,
}

PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;background:#111;color:#eee;font:15px/1.5 monospace">
<img src="/stream" style="width:100%;max-width:1000px;display:block">
<pre id=s style="margin:8px"></pre>
<script>
const f=(v,d=1)=>(v>=0?"+":"")+v.toFixed(d);
async function tick(){try{const s=await (await fetch("/state")).json();
const ok=s.within_tol;
document.getElementById("s").innerHTML=
(s.detected?"<b style='color:#4f4'>FACE DETECTED</b>":"<b style='color:#f84'>no face</b>")+
"   detector "+s.fps.toFixed(1)+" fps   camera latency "+(s.latency_s*1000).toFixed(0)+" ms\\n"+
"eye-midpoint error  x "+f(s.err_x_avg,3)+"  y "+f(s.err_y_avg,3)+"   rms "+s.err_rms.toFixed(3)+
"   (0 = centred, goal +/-"+s.tolerance+")  "+(ok?"<b style='color:#4f4'>CENTRED</b>":"<b style='color:#fc4'>correcting</b>")+"\\n"+
"it still wants to turn  yaw "+f(s.goal_err_yaw_deg,2)+"deg  pitch "+f(s.goal_err_pitch_deg,2)+"deg\\n"+
"head command            yaw "+f(s.cmd_yaw_deg)+"deg  pitch "+f(s.cmd_pitch_deg)+"deg   speed "+s.speed_deg_s.toFixed(1)+" deg/s"+(s.still?"  (still)":"")
}catch(e){}
setTimeout(tick,150)}tick();
</script>"""


class Viewer(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    def do_GET(self) -> None:
        if self.path == "/state":
            with lock:
                body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with lock:
                        jpeg = latest_jpeg
                    if jpeg is not None:
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                        )
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)


class OneEuro:
    """One-Euro filter: heavy smoothing when still, light when moving fast."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0) -> None:
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.t: float | None = None
        self.x = self.dx = 0.0

    @staticmethod
    def _alpha(dt: float, cutoff: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self) -> None:
        self.t = None

    def __call__(self, x: float, t: float) -> float:
        if self.t is None:
            self.t, self.x, self.dx = t, x, 0.0
            return x
        dt = max(t - self.t, 1e-3)
        self.dx += self._alpha(dt, self.d_cutoff) * ((x - self.x) / dt - self.dx)
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        self.x += self._alpha(dt, cutoff) * (x - self.x)
        self.t = t
        return self.x


class PoseHistory:
    """Measured head poses with timestamps, queried at the time a frame was taken."""

    def __init__(self, seconds: float = 15.0) -> None:
        self.t: collections.deque = collections.deque(maxlen=int(seconds * CONTROL_HZ))
        self.T: collections.deque = collections.deque(maxlen=int(seconds * CONTROL_HZ))

    def add(self, t: float, T: np.ndarray) -> None:
        self.t.append(t)
        self.T.append(T)

    def at(self, t: float) -> np.ndarray:
        times = list(self.t)
        i = bisect.bisect_left(times, t)
        if i <= 0:
            return self.T[0]
        if i >= len(times):
            return self.T[-1]
        return self.T[i] if times[i] - t < t - times[i - 1] else self.T[i - 1]


def euler(T: np.ndarray) -> tuple[float, float, float]:
    roll, pitch, yaw = R.from_matrix(T[:3, :3]).as_euler("xyz")
    return float(roll), float(pitch), float(yaw)


def eye_mid(face) -> tuple[float, float]:
    return (face.left_eye[0] + face.right_eye[0]) / 2, (face.left_eye[1] + face.right_eye[1]) / 2


def pick_face(faces: list, prev: tuple[float, float] | None, w: int):
    """Nearest person = largest face; stay on the previous one while it is still there."""
    if not faces:
        return None
    if prev is not None:
        best = min(faces, key=lambda f: math.hypot(*(a - b for a, b in zip(eye_mid(f), prev))))
        if math.hypot(*(a - b for a, b in zip(eye_mid(best), prev))) < 0.25 * w:
            return best
    return max(faces, key=lambda f: f.bbox[2] * f.bbox[3])


def detector_loop(mini: ReachyMini, width: int, height: int) -> None:
    """Grab frames, find the eyes, publish raw detections and the annotated view."""
    global latest_jpeg
    det = FaceDetector()
    scale = DETECT_WIDTH / width
    size = (DETECT_WIDTH, int(round(height * scale)))
    prev: tuple[float, float] | None = None
    seq, n, fps_t, fps_n = 0, 0, time.monotonic(), 0
    while not stop_detector.is_set():
        frame = mini.media.get_frame()
        t_arr = time.monotonic()
        if frame is None:
            time.sleep(0.02)
            continue
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        faces = det.detect(small)
        face = pick_face(faces, prev, size[0])
        n += 1
        fps_n += 1
        if t_arr - fps_t > 1.0:
            with lock:
                state["fps"] = fps_n / (t_arr - fps_t)
            fps_t, fps_n = t_arr, 0

        eye = None
        if face is not None:
            prev = eye_mid(face)
            eye = (prev[0] / scale, prev[1] / scale)  # full-resolution pixels
            seq += 1
            rec = {"seq": seq, "t_arr": t_arr, "u": eye[0], "v": eye[1]}
            with lock:
                detection.clear()
                detection.update(rec)
                if det_log is not None:
                    det_log.append(rec)
        else:
            prev = None

        if n % VIEW_EVERY == 0:
            view = frame.copy()  # SDK frames are read-only
            cx, cy = (width - 1) / 2, (height - 1) / 2
            tw, th = TOLERANCE * cx, TOLERANCE * cy
            cv2.rectangle(view, (int(cx - tw), int(cy - th)), (int(cx + tw), int(cy + th)),
                          (255, 255, 255), 1)  # the +/-0.01 target zone
            cv2.drawMarker(view, (int(cx), int(cy)), (255, 255, 255), cv2.MARKER_CROSS, 40, 1)
            for f in faces:
                x, y, bw, bh = (v / scale for v in f.bbox)
                cv2.rectangle(view, (int(x), int(y)), (int(x + bw), int(y + bh)), (120, 120, 120), 1)
            if eye is not None:
                cv2.line(view, (int(cx), int(cy)), (int(eye[0]), int(eye[1])), (0, 200, 255), 2)
                cv2.circle(view, (int(eye[0]), int(eye[1])), 14, (0, 255, 0), 2)
            ok, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                with lock:
                    latest_jpeg = buf.tobytes()


def load_cal() -> dict:
    try:
        with open(CAL_FILE) as fh:
            return {**DEFAULT_CAL, **json.load(fh)}
    except (OSError, ValueError):
        return dict(DEFAULT_CAL)


def calibrate(mini: ReachyMini, hist: PoseHistory) -> None:
    """Swing the head while you sit still, then fit (a) the camera latency and
    (b) how many pixels the image shifts per degree of head motion."""
    global det_log
    print("Calibrating: sit still, face the robot (head swings for ~12 s)...")
    with lock:
        det_log = []
    t0 = time.monotonic()
    while (t := time.monotonic() - t0) < 12.0:
        c_yaw = math.radians(10) * math.sin(2 * math.pi * 0.35 * t)
        c_pitch = math.radians(6) * math.sin(2 * math.pi * 0.23 * t + 1.0)
        mini.set_target(head=create_head_pose(pitch=c_pitch, yaw=c_yaw, degrees=False, mm=False))
        hist.add(time.monotonic(), mini.get_current_head_pose())
        time.sleep(1.0 / CONTROL_HZ)
    with lock:
        log, det_log = list(det_log), None
    if len(log) < 30:
        print(f"Only {len(log)} detections: is a face in view? Aborting.")
        return
    with open(os.path.expanduser("~/cal_dump.json"), "w") as fh:  # raw data, for debugging
        json.dump({"det": log, "pose": [[t, *euler(T)] for t, T in zip(hist.t, hist.T)]}, fh)

    pt = np.array(list(hist.t))
    pose = np.array([euler(T) for T in hist.T])  # roll, pitch, yaw
    t_arr = np.array([d["t_arr"] for d in log])
    u = np.array([d["u"] for d in log])
    v = np.array([d["v"] for d in log])
    results = []
    for latency in np.arange(0.0, 0.42, 0.02):
        yaw = np.degrees(np.interp(t_arr - latency, pt, pose[:, 2]))
        pitch = np.degrees(np.interp(t_arr - latency, pt, pose[:, 1]))
        A = np.c_[yaw, pitch, np.ones_like(yaw)]
        cu, *_ = np.linalg.lstsq(A, u, rcond=None)
        cv_, *_ = np.linalg.lstsq(A, v, rcond=None)
        # residual of the fit, in degrees of head motion (what you did + detector noise)
        score = float((u - A @ cu).std() / abs(cu[0]) + (v - A @ cv_).std() / abs(cv_[1]))
        results.append((score, float(latency), float(cu[0]), float(cv_[1])))
    for score, latency, gu, gv in results[::2]:
        print(f"  latency {latency * 1000:4.0f} ms  fit residual {score:5.2f} deg   {gu:+5.1f} px/deg yaw  {gv:+6.1f} px/deg pitch")
    score, latency, gu, gv = min(results)
    print(f"Best: latency {latency * 1000:.0f} ms, {gu:+.1f} px/deg yaw, {gv:+.1f} px/deg pitch "
          f"(residual {score:.2f} deg, {len(log)} detections)")
    with open(CAL_FILE, "w") as fh:
        json.dump({"latency_s": latency, "px_per_deg_yaw": gu, "px_per_deg_pitch": gv,
                   "residual_deg": score, "detections": len(log)}, fh)
    mini.goto_target(create_head_pose(), body_yaw=None, duration=1.5)


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt  # SIGTERM (systemctl stop, shutdown) takes the Ctrl+C path


def park(mini: ReachyMini) -> None:
    """Clean stop: fold into the sleep pose, then release the motors."""
    # A second signal while parking must not cut the move short.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    stop_detector.set()
    time.sleep(0.3)  # let the camera thread finish its current frame
    print("Stopping: going to the sleep pose...", flush=True)
    try:
        mini.goto_sleep()
    except Exception as exc:  # daemon already gone, etc.: still release the motors
        print(f"Could not reach the sleep pose: {exc}", flush=True)
    try:
        mini.disable_motors()
    except Exception as exc:
        print(f"Could not release the motors: {exc}", flush=True)
    print("Stopped.", flush=True)


def main(do_calibrate: bool) -> None:
    signal.signal(signal.SIGTERM, _raise_interrupt)
    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", PORT), Viewer).serve_forever, daemon=True
    ).start()

    with ReachyMini() as mini:
        mini.stop_head_tracking()  # make sure the daemon's own tracker isn't steering
        mini.enable_motors()  # motors are off after every boot
        mini.wake_up()
        mini.goto_target(antennas=INIT_ANTENNAS_JOINT_POSITIONS, body_yaw=None, duration=0.5)
        width, height = mini.media.camera.resolution
        cx, cy = (width - 1) / 2, (height - 1) / 2
        hist = PoseHistory()
        threading.Thread(target=detector_loop, args=(mini, width, height), daemon=True).start()
        time.sleep(1.0)

        if do_calibrate:
            calibrate(mini, hist)
            return

        cal = load_cal()
        latency = cal["latency_s"]
        px_yaw = math.degrees(1.0) * cal["px_per_deg_yaw"]  # px per rad
        px_pitch = math.degrees(1.0) * cal["px_per_deg_pitch"]
        with lock:
            state["latency_s"] = latency
        print(f"Looking people in the eyes (latency {latency * 1000:.0f} ms, "
              f"{cal['px_per_deg_yaw']:+.1f}/{cal['px_per_deg_pitch']:+.1f} px/deg). "
              f"View + state: http://reachy-mini.local:{PORT}")

        _, m0_pitch, m0_yaw = euler(mini.get_current_head_pose())
        yaw, pitch = m0_yaw, m0_pitch  # commanded pose
        vyaw = vpitch = 0.0
        goal_yaw, goal_pitch = m0_yaw, m0_pitch  # filtered face direction (world frame)
        gs_yaw, gs_pitch = m0_yaw, m0_pitch  # smoothed goal the spring chases
        f_yaw = OneEuro(FILTER_MIN_CUTOFF, FILTER_BETA)
        f_pitch = OneEuro(FILTER_MIN_CUTOFF, FILTER_BETA)
        avg_x = avg_y = 0.0
        recent: collections.deque = collections.deque(maxlen=20)
        last_seq, last_seen = 0, 0.0
        dt = 1.0 / CONTROL_HZ
        next_tick = time.monotonic()
        a_goal = 1.0 - math.exp(-dt / GOAL_TAU)

        try:
            while True:
                now = time.monotonic()
                T_now = mini.get_current_head_pose()
                hist.add(now, T_now)
                _, m_pitch, m_yaw = euler(T_now)

                # Measured head speed over the last 0.25 s.
                _, o_pitch, o_yaw = euler(hist.at(now - 0.25))
                speed = math.hypot(m_yaw - o_yaw, m_pitch - o_pitch) / 0.25

                with lock:
                    det = dict(detection)
                if det.get("seq", 0) != last_seq and now - det["t_arr"] < 1.0:
                    last_seq = det["seq"]
                    if now - last_seen > 1.0:  # face (re)appeared: drop stale filter state
                        f_yaw.reset()
                        f_pitch.reset()
                    last_seen = now
                    # Face direction = head pose when the frame was taken + the angle
                    # that brings the eye midpoint to the image centre.
                    _, f_p, f_y = euler(hist.at(det["t_arr"] - latency))
                    g_yaw = f_y + (cx - det["u"]) / px_yaw
                    g_pitch = f_p + (cy - det["v"]) / px_pitch
                    goal_yaw = float(np.clip(f_yaw(g_yaw, det["t_arr"]), -YAW_LIMIT, YAW_LIMIT))
                    goal_pitch = float(np.clip(f_pitch(g_pitch, det["t_arr"]), -PITCH_LIMIT, PITCH_LIMIT))
                    nx, ny = (det["u"] - cx) / cx, (det["v"] - cy) / cy
                    avg_x += 0.35 * (nx - avg_x)
                    avg_y += 0.35 * (ny - avg_y)
                    if speed < STILL_SPEED:
                        recent.append(nx * nx + ny * ny)
                    with lock:
                        state.update(err_x=nx, err_y=ny, err_x_avg=avg_x, err_y_avg=avg_y)

                face_here = now - last_seen < HOLD_S
                still = speed < STILL_SPEED

                if face_here:
                    target_yaw, target_pitch = goal_yaw, goal_pitch
                elif now - last_seen > LOST_RECENTRE_S:
                    target_yaw = target_pitch = 0.0
                else:
                    target_yaw, target_pitch = gs_yaw, gs_pitch  # hold
                target_yaw = float(np.clip(target_yaw, -YAW_LIMIT, YAW_LIMIT))
                target_pitch = float(np.clip(target_pitch, -PITCH_LIMIT, PITCH_LIMIT))

                # Stage 1: first-order smoothing of the goal (removes steps -> continuous
                # acceleration). Stage 2: critically damped spring, speed-limited.
                gs_yaw += a_goal * (target_yaw - gs_yaw)
                gs_pitch += a_goal * (target_pitch - gs_pitch)
                # The spring acts on the *measured* pose; the command just integrates its
                # velocity. At rest measured == goal, i.e. the eye midpoint is at the image
                # centre, whatever constant gap exists between commanded and real pose.
                for meas, vel, goal, axis in ((m_yaw, vyaw, gs_yaw, 0), (m_pitch, vpitch, gs_pitch, 1)):
                    acc = OMEGA * OMEGA * (goal - meas) - 2.0 * OMEGA * vel
                    vel = float(np.clip(vel + acc * dt, -MAX_SPEED, MAX_SPEED))
                    if axis == 0:
                        vyaw = vel
                        yaw = float(np.clip(yaw + vel * dt, -YAW_LIMIT, YAW_LIMIT))
                    else:
                        vpitch = vel
                        pitch = float(np.clip(pitch + vel * dt, -PITCH_LIMIT, PITCH_LIMIT))
                mini.set_target(head=create_head_pose(pitch=pitch, yaw=yaw, degrees=False, mm=False))

                rms = math.sqrt(sum(recent) / len(recent)) if recent else 0.0
                with lock:
                    state.update(
                        detected=face_here,
                        err_rms=rms,
                        within_tol=bool(face_here and abs(avg_x) < TOLERANCE and abs(avg_y) < TOLERANCE),
                        goal_err_yaw_deg=math.degrees(gs_yaw - m_yaw),
                        goal_err_pitch_deg=math.degrees(gs_pitch - m_pitch),
                        cmd_yaw_deg=math.degrees(yaw), cmd_pitch_deg=math.degrees(pitch),
                        speed_deg_s=math.degrees(math.hypot(vyaw, vpitch)), still=bool(still),
                    )

                next_tick += dt
                time.sleep(max(0.0, next_tick - time.monotonic()))
        except KeyboardInterrupt:
            pass
        finally:
            park(mini)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calibrate", action="store_true", help="fit camera latency + px/deg, then exit")
    main(parser.parse_args().calibrate)
