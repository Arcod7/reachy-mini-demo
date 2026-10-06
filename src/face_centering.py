"""Shared machinery for the Reachy Mini demos: face detection, smooth head control.

`Rig` owns everything that runs on the robot:

  * a detector thread: camera frame -> YuNet face detector (the SDK's) ->
    midpoint between the eyes (+ face size), published with the frame timestamp;
  * a 50 Hz controller thread that is the *only* thing sending head poses:
      - tracking on : face direction (head pose when the frame was taken + the
        pixel offset / measured px-per-degree) -> One-Euro filter -> goal;
      - tracking off: the goal set by `look()` (scripted moves, games, gestures);
    then goal smoothing + a critically damped spring acting on the *measured*
    pose, so at rest measured pose == goal (eyes centred, or the scripted angle
    reached) whatever constant gap exists between commanded and real pose;
  * a small web view (live camera, state) on :8080.

Why not the daemon's own tracker: it low-passes the face position (so it reports
where you *were*, which makes the head overshoot), ignores moves < 0.02, and aims
at the nose. Here the detection is raw and time-aligned to the head pose of its
frame, so a delay can't make the head chase a stale position.

Conventions (robot frame): +yaw = head turns to the robot's left, +pitch = looks
down, +roll = tilts to the robot's left (as in the SDK). Inverse kinematics is the
SDK's (analytical, in the daemon); we only send head poses with set_target.
"""

import bisect
import collections
import json
import math
import os
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

# Feel. Lower OMEGA / larger GOAL_TAU = slower, softer approach.
OMEGA = 3.0  # spring stiffness (rad/s): ~1.3 s to settle
GOAL_TAU = 0.25  # first-order smoothing of the goal before the spring (s)
MAX_SPEED = math.radians(40)
YAW_LIMIT = math.radians(55)
PITCH_LIMIT = math.radians(28)
ROLL_LIMIT = math.radians(25)
ANTENNA_TAU = 0.06  # antenna smoothing (s)
FILTER_MIN_CUTOFF = 0.6  # Hz, when the face is still (kills detector jitter)
FILTER_BETA = 12.0  # cutoff rise per rad/s of face motion (keeps up when you move)
STILL_SPEED = math.radians(2.0)  # head counts as still below this (rad/s)
HOLD_S = 0.8  # keep the last goal this long when the face vanishes
LOST_RECENTRE_S = 6.0  # then, after this long, look straight ahead
TOLERANCE = 0.01  # target accuracy (normalised image units; 1 = half the image)

PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;background:#111;color:#eee;font:15px/1.5 monospace">
<img src="/stream" style="width:100%;max-width:1000px;display:block">
<pre id=s style="margin:8px"></pre>
<script>
const f=(v,d=1)=>(v>=0?"+":"")+v.toFixed(d);
async function tick(){try{const s=await (await fetch("/state")).json();
let h="";
if(s.mode)h+="<b style='color:#8cf'>mode: "+s.mode+"</b>"+(s.said?"   <span style='color:#fc4'>Reachy: \\""+s.said+"\\"</span>":"")+"\\n";
h+=(s.detected?"<b style='color:#4f4'>FACE DETECTED</b>":"<b style='color:#f84'>no face</b>")+
"   "+(s.tracking?"tracking":"scripted head")+"   detector "+s.fps.toFixed(1)+" fps   camera latency "+(s.latency_s*1000).toFixed(0)+" ms\\n";
if(s.tracking)h+="eye-midpoint error  x "+f(s.err_x_avg,3)+"  y "+f(s.err_y_avg,3)+"   rms "+s.err_rms.toFixed(3)+
"   (0 = centred, goal +/-"+s.tolerance+")  "+(s.within_tol?"<b style='color:#4f4'>CENTRED</b>":"<b style='color:#fc4'>correcting</b>")+"\\n";
h+="it still wants to turn  yaw "+f(s.goal_err_yaw_deg,2)+"deg  pitch "+f(s.goal_err_pitch_deg,2)+"deg\\n"+
"head command            yaw "+f(s.cmd_yaw_deg)+"deg  pitch "+f(s.cmd_pitch_deg)+"deg  roll "+f(s.cmd_roll_deg)+"deg   speed "+s.speed_deg_s.toFixed(1)+" deg/s"+(s.still?"  (still)":"");
document.getElementById("s").innerHTML=h;
}catch(e){}
setTimeout(tick,150)}tick();
</script>"""


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


def load_cal() -> dict:
    try:
        with open(CAL_FILE) as fh:
            return {**DEFAULT_CAL, **json.load(fh)}
    except (OSError, ValueError):
        return dict(DEFAULT_CAL)


class Rig:
    """Camera + face detector + smooth head/antenna controller + web view."""

    def __init__(self, mini: ReachyMini, port: int = PORT) -> None:
        self.mini = mini
        self.port = port
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.hist = PoseHistory()
        self.width, self.height = mini.media.camera.resolution
        self.cal = load_cal()

        self._jpeg: bytes | None = None
        self._det: dict = {"seq": 0}  # newest detection
        self.det_log: list | None = None  # filled during calibration
        self._tracking = False
        self._track_edge = False
        self._goal = {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "omega": OMEGA,
                      "tau": GOAL_TAU, "vmax": MAX_SPEED}
        self._ant = list(INIT_ANTENNAS_JOINT_POSITIONS)
        self._cur = (0.0, 0.0, 0.0)  # smoothed goal now (yaw, pitch, roll); written by the controller
        self._settled = False
        self._threads: list[threading.Thread] = []
        self.state: dict = {
            "mode": "", "said": "", "tracking": False,
            "detected": False, "fps": 0.0, "latency_s": self.cal["latency_s"],
            "err_x": 0.0, "err_y": 0.0, "err_x_avg": 0.0, "err_y_avg": 0.0,
            "err_rms": 0.0, "within_tol": False, "tolerance": TOLERANCE,
            "goal_err_yaw_deg": 0.0, "goal_err_pitch_deg": 0.0,
            "cmd_yaw_deg": 0.0, "cmd_pitch_deg": 0.0, "cmd_roll_deg": 0.0,
            "speed_deg_s": 0.0, "still": False,
        }

    # ------------------------------------------------------------------ public API

    def start(self, controller: bool = True) -> None:
        """Start the web view and detector; `controller=False` for calibration."""
        handler = self._make_handler()
        server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        server.daemon_threads = True
        self._server = server
        self._spawn(server.serve_forever)
        self._spawn(self._detector_loop)
        if controller:
            self._spawn(self._control_loop)

    def stop(self) -> None:
        """Stop the detector and controller threads (before parking the robot)."""
        self.stop_event.set()
        for t in self._threads:
            if t.name != "serve_forever":
                t.join(timeout=2.0)
        time.sleep(0.1)

    def set_tracking(self, on: bool) -> None:
        """Tracking on: the head follows the nearest face. Off: it follows look()."""
        with self.lock:
            if on and not self._tracking:
                self._track_edge = True
            self._tracking = on
            self.state["tracking"] = on

    def look(self, yaw: float | None = None, pitch: float | None = None,
             roll: float | None = None, omega: float = OMEGA, tau: float = GOAL_TAU,
             vmax: float = MAX_SPEED) -> None:
        """Scripted head goal in radians (robot frame). Turns tracking off.

        Omitted axes keep their current goal. Same smooth spring as tracking;
        a larger `omega` (and smaller `tau`) gives a snappier, more excited move.
        """
        with self.lock:
            self._tracking = False
            self.state["tracking"] = False
            g = self._goal
            # Axes left out start from where the smoothed goal is *now* (not from an
            # old scripted goal), so handing over from tracking never snaps the head.
            base = dict(zip(("yaw", "pitch", "roll"), self._cur))
            for key, val in (("yaw", yaw), ("pitch", pitch), ("roll", roll)):
                g[key] = base[key] if val is None else val
            g["omega"], g["tau"], g["vmax"] = omega, tau, vmax
            self._settled = False

    def look_deg(self, yaw: float | None = None, pitch: float | None = None,
                 roll: float | None = None, **kw) -> None:
        r = math.radians
        self.look(None if yaw is None else r(yaw), None if pitch is None else r(pitch),
                  None if roll is None else r(roll), **kw)

    def antennas(self, left: float | None = None, right: float | None = None) -> None:
        """Antenna goals in radians; None = rest pose for that antenna."""
        with self.lock:
            self._ant[0] = INIT_ANTENNAS_JOINT_POSITIONS[0] if left is None else left
            self._ant[1] = INIT_ANTENNAS_JOINT_POSITIONS[1] if right is None else right

    def wait_settled(self, timeout: float = 4.0, stop: threading.Event | None = None) -> bool:
        """Block until the scripted goal is reached (or timeout / stop)."""
        t0 = time.monotonic()
        time.sleep(0.15)
        while time.monotonic() - t0 < timeout:
            if (stop is not None and stop.is_set()) or self.stop_event.is_set():
                return False
            with self.lock:
                if self._settled:
                    return True
            time.sleep(0.03)
        return False

    def current_goal_deg(self) -> tuple[float, float, float]:
        """Where the (smoothed) head goal is now, in degrees: (yaw, pitch, roll)."""
        with self.lock:
            return tuple(math.degrees(v) for v in self._cur)

    def wait_on_target(self, timeout: float = 1.5) -> bool:
        """While tracking: block until the head has settled on the face (or timeout)."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout and not self.stop_event.is_set():
            with self.lock:
                if self.state["detected"] and self.state["still"] and \
                        abs(self.state["goal_err_yaw_deg"]) < 2.0 and abs(self.state["goal_err_pitch_deg"]) < 2.0:
                    return True
            time.sleep(0.05)
        return False

    def face(self) -> dict | None:
        """Newest face (< HOLD_S old): {"u","v" px, "area_frac", "age"} or None."""
        with self.lock:
            det = dict(self._det)
        if det.get("seq", 0) and time.monotonic() - det["t_arr"] < HOLD_S:
            det["age"] = time.monotonic() - det["t_arr"]
            return det
        return None

    def set_status(self, mode: str | None = None, said: str | None = None) -> None:
        with self.lock:
            if mode is not None:
                self.state["mode"] = mode
            if said is not None:
                self.state["said"] = said

    # ------------------------------------------------------------------ internals

    def _spawn(self, fn) -> None:
        t = threading.Thread(target=fn, daemon=True, name=getattr(fn, "__name__", "t"))
        t.start()
        self._threads.append(t)

    def _make_handler(self):
        rig = self

        class Viewer(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the console quiet
                pass

            def do_GET(self) -> None:
                if self.path == "/state":
                    with rig.lock:
                        body = json.dumps(rig.state).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    try:
                        while not rig.stop_event.is_set():
                            with rig.lock:
                                jpeg = rig._jpeg
                            if jpeg is not None:
                                self.wfile.write(
                                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                            time.sleep(0.1)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(PAGE)

        return Viewer

    def _detector_loop(self) -> None:
        """Grab frames, find the eyes, publish raw detections and the annotated view."""
        width, height = self.width, self.height
        det = FaceDetector()
        scale = DETECT_WIDTH / width
        size = (DETECT_WIDTH, int(round(height * scale)))
        prev: tuple[float, float] | None = None
        seq, n, fps_t, fps_n = 0, 0, time.monotonic(), 0
        while not self.stop_event.is_set():
            frame = self.mini.media.get_frame()
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
                with self.lock:
                    self.state["fps"] = fps_n / (t_arr - fps_t)
                fps_t, fps_n = t_arr, 0

            eye = None
            if face is not None:
                prev = eye_mid(face)
                eye = (prev[0] / scale, prev[1] / scale)  # full-resolution pixels
                seq += 1
                area = face.bbox[2] * face.bbox[3] / (size[0] * size[1])
                rec = {"seq": seq, "t_arr": t_arr, "u": eye[0], "v": eye[1], "area_frac": area}
                with self.lock:
                    self._det = rec
                    if self.det_log is not None:
                        self.det_log.append(rec)
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
                    cv2.rectangle(view, (int(x), int(y)), (int(x + bw), int(y + bh)),
                                  (120, 120, 120), 1)
                if eye is not None:
                    cv2.line(view, (int(cx), int(cy)), (int(eye[0]), int(eye[1])), (0, 200, 255), 2)
                    cv2.circle(view, (int(eye[0]), int(eye[1])), 14, (0, 255, 0), 2)
                ok, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    with self.lock:
                        self._jpeg = buf.tobytes()

    def _control_loop(self) -> None:
        mini, hist, cal = self.mini, self.hist, self.cal
        width, height = self.width, self.height
        cx, cy = (width - 1) / 2, (height - 1) / 2
        latency = cal["latency_s"]
        px_yaw = math.degrees(1.0) * cal["px_per_deg_yaw"]  # px per rad
        px_pitch = math.degrees(1.0) * cal["px_per_deg_pitch"]

        m_roll, m_pitch, m_yaw = euler(mini.get_current_head_pose())
        yaw, pitch, roll = m_yaw, m_pitch, m_roll  # commanded pose
        vyaw = vpitch = vroll = 0.0
        goal_yaw, goal_pitch = m_yaw, m_pitch  # filtered face direction (world frame)
        gs_yaw, gs_pitch, gs_roll = m_yaw, m_pitch, m_roll  # smoothed goal the spring chases
        with self.lock:  # scripted goal starts where the head is
            self._goal.update(yaw=m_yaw, pitch=m_pitch, roll=m_roll)
        ant = list(INIT_ANTENNAS_JOINT_POSITIONS)
        f_yaw = OneEuro(FILTER_MIN_CUTOFF, FILTER_BETA)
        f_pitch = OneEuro(FILTER_MIN_CUTOFF, FILTER_BETA)
        avg_x = avg_y = 0.0
        recent: collections.deque = collections.deque(maxlen=20)
        last_seq, last_seen = 0, 0.0
        dt = 1.0 / CONTROL_HZ
        next_tick = time.monotonic()

        while not self.stop_event.is_set():
            now = time.monotonic()
            T_now = mini.get_current_head_pose()
            hist.add(now, T_now)
            m_roll, m_pitch, m_yaw = euler(T_now)

            # Measured head speed over the last 0.25 s.
            _, o_pitch, o_yaw = euler(hist.at(now - 0.25))
            speed = math.hypot(m_yaw - o_yaw, m_pitch - o_pitch) / 0.25

            with self.lock:
                det = dict(self._det)
                tracking = self._tracking
                edge, self._track_edge = self._track_edge, False
                sg = dict(self._goal)
                ant_goal = list(self._ant)

            if edge:  # tracking just switched on: start from where we are, with a grace period
                goal_yaw, goal_pitch = gs_yaw, gs_pitch
                f_yaw.reset()
                f_pitch.reset()
                last_seen = now

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
                with self.lock:
                    self.state.update(err_x=nx, err_y=ny, err_x_avg=avg_x, err_y_avg=avg_y)

            face_here = now - last_seen < HOLD_S

            if tracking:
                if face_here:
                    target_yaw, target_pitch = goal_yaw, goal_pitch
                elif now - last_seen > LOST_RECENTRE_S:
                    target_yaw = target_pitch = 0.0
                else:
                    target_yaw, target_pitch = gs_yaw, gs_pitch  # hold
                target_roll = 0.0
                omega, tau, vmax = OMEGA, GOAL_TAU, MAX_SPEED
            else:
                target_yaw, target_pitch, target_roll = sg["yaw"], sg["pitch"], sg["roll"]
                omega, tau, vmax = sg["omega"], sg["tau"], sg["vmax"]
            target_yaw = float(np.clip(target_yaw, -YAW_LIMIT, YAW_LIMIT))
            target_pitch = float(np.clip(target_pitch, -PITCH_LIMIT, PITCH_LIMIT))
            target_roll = float(np.clip(target_roll, -ROLL_LIMIT, ROLL_LIMIT))

            # Stage 1: first-order smoothing of the goal (removes steps -> continuous
            # acceleration). Stage 2: critically damped spring on the *measured* pose;
            # the command just integrates its velocity.
            a_goal = 1.0 - math.exp(-dt / tau)
            gs_yaw += a_goal * (target_yaw - gs_yaw)
            gs_pitch += a_goal * (target_pitch - gs_pitch)
            gs_roll += a_goal * (target_roll - gs_roll)
            limits = (YAW_LIMIT, PITCH_LIMIT, ROLL_LIMIT)
            axes = [[yaw, vyaw, m_yaw, gs_yaw], [pitch, vpitch, m_pitch, gs_pitch],
                    [roll, vroll, m_roll, gs_roll]]
            for ax, lim in zip(axes, limits):
                cmd, vel, meas, goal = ax
                acc = omega * omega * (goal - meas) - 2.0 * omega * vel
                vel = float(np.clip(vel + acc * dt, -vmax, vmax))
                ax[0] = float(np.clip(cmd + vel * dt, -lim, lim))
                ax[1] = vel
            (yaw, vyaw, _, _), (pitch, vpitch, _, _), (roll, vroll, _, _) = axes

            a_ant = 1.0 - math.exp(-dt / ANTENNA_TAU)
            ant = [a + a_ant * (g - a) for a, g in zip(ant, ant_goal)]

            mini.set_target(
                head=create_head_pose(roll=roll, pitch=pitch, yaw=yaw, degrees=False, mm=False),
                antennas=ant,
            )

            err_yaw, err_pitch = gs_yaw - m_yaw, gs_pitch - m_pitch
            spd = math.hypot(vyaw, vpitch)
            rms = math.sqrt(sum(recent) / len(recent)) if recent else 0.0
            with self.lock:
                self._cur = (gs_yaw, gs_pitch, gs_roll)
                self._settled = (not tracking and abs(err_yaw) < math.radians(1.5)
                                 and abs(err_pitch) < math.radians(1.5)
                                 and abs(gs_roll - m_roll) < math.radians(2.0)
                                 and math.degrees(spd) < 4.0)
                self.state.update(
                    detected=face_here,
                    err_rms=rms,
                    within_tol=bool(tracking and face_here and abs(avg_x) < TOLERANCE
                                    and abs(avg_y) < TOLERANCE),
                    goal_err_yaw_deg=math.degrees(err_yaw),
                    goal_err_pitch_deg=math.degrees(err_pitch),
                    cmd_yaw_deg=math.degrees(yaw), cmd_pitch_deg=math.degrees(pitch),
                    cmd_roll_deg=math.degrees(roll),
                    speed_deg_s=math.degrees(spd), still=bool(speed < STILL_SPEED),
                )

            next_tick += dt
            time.sleep(max(0.0, next_tick - time.monotonic()))


def calibrate(mini: ReachyMini, rig: Rig) -> None:
    """Swing the head while you sit still, then fit (a) the camera latency and
    (b) how many pixels the image shifts per degree of head motion."""
    hist = rig.hist
    print("Calibrating: sit still, face the robot (head swings for ~12 s)...")
    with rig.lock:
        rig.det_log = []
    t0 = time.monotonic()
    while (t := time.monotonic() - t0) < 12.0:
        c_yaw = math.radians(10) * math.sin(2 * math.pi * 0.35 * t)
        c_pitch = math.radians(6) * math.sin(2 * math.pi * 0.23 * t + 1.0)
        mini.set_target(head=create_head_pose(pitch=c_pitch, yaw=c_yaw, degrees=False, mm=False))
        hist.add(time.monotonic(), mini.get_current_head_pose())
        time.sleep(1.0 / CONTROL_HZ)
    with rig.lock:
        log, rig.det_log = list(rig.det_log), None
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
        print(f"  latency {latency * 1000:4.0f} ms  fit residual {score:5.2f} deg   "
              f"{gu:+5.1f} px/deg yaw  {gv:+6.1f} px/deg pitch")
    score, latency, gu, gv = min(results)
    print(f"Best: latency {latency * 1000:.0f} ms, {gu:+.1f} px/deg yaw, {gv:+.1f} px/deg pitch "
          f"(residual {score:.2f} deg, {len(log)} detections)")
    with open(CAL_FILE, "w") as fh:
        json.dump({"latency_s": latency, "px_per_deg_yaw": gu, "px_per_deg_pitch": gv,
                   "residual_deg": score, "detections": len(log)}, fh)
    mini.goto_target(create_head_pose(), body_yaw=None, duration=1.5)


def park(mini: ReachyMini, rig: Rig | None = None) -> None:
    """Clean stop: fold into the sleep pose, then release the motors."""
    import signal
    # A second signal while parking must not cut the move short.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if rig is not None:
        rig.stop()
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
