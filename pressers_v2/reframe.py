"""
Speaker-following crop for the vertical (9:16) and square (1:1) clips.

  1. Sample the downloaded segment about 6 times a second and find faces
     with OpenCV's YuNet detector (trained on WIDER FACE, which includes
     profile and partly turned faces).
  2. Pick the speaker: the biggest, most confident face, preferring the one
     nearest to where the speaker was a moment ago (two-shots, podcasts).
  3. Fill gaps with the last known position (the centre if no face was ever
     found), drop outliers with a median filter, then run a "lazy camera":
     it only pans when the face leaves a dead zone, at a capped speed, with
     eased starts and stops. A camera cut in the source (the face jumps
     across the picture and stays there) is followed with a cut, not a pan.
  4. Write one crop position per output frame as ffmpeg sendcmd commands.

Without OpenCV or the model file it returns a steady centred crop.
"""

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")     # keep OpenCV's backend notes off the screen

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 6.0
DETECT_WIDTH = 640
SCORE_THRESHOLD = 0.55
OUT_FPS = 30
DEAD_ZONE = 0.10          # of the crop width: face moves inside this -> camera stays
MAX_PAN_PER_SEC = 0.55    # of the crop width per second
CUT_JUMP = 0.30           # of the source width: a jump this big that persists is a camera cut
SMOOTH_SECS = 0.45        # gaussian easing of the camera path


def model_path() -> Path | None:
    """The YuNet model, shipped next to the app (models/)."""
    here = Path(__file__).resolve().parent
    for d in (here / "models", here):
        p = d / MODEL_NAME
        if p.is_file():
            return p
    return None


def crop_size(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple:
    """Largest crop with the output's aspect ratio that fits in the source
    (even numbers, as the encoder needs)."""
    target = out_w / out_h
    if src_w / src_h > target:
        ch = src_h
        cw = int(round(ch * target))
    else:
        cw = src_w
        ch = int(round(cw / target))
    cw -= cw % 2
    ch -= ch % 2
    return max(2, min(cw, src_w)), max(2, min(ch, src_h))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def choose_face(faces: list, prev: tuple | None, frame_w: float) -> tuple | None:
    """faces: [(cx, cy, size, score)] -> the speaker's (cx, cy, size).
    Big confident faces win; near the previous position wins ties."""
    if not faces:
        return None
    best, best_val = None, -1e9
    for cx, cy, size, score in faces:
        val = size * score
        if prev is not None:
            dist = math.hypot(cx - prev[0], cy - prev[1]) / frame_w
            val *= 1.0 / (1.0 + 4.0 * dist)
        if val > best_val:
            best, best_val = (cx, cy, size), val
    return best


def detect_samples(video: Path) -> tuple:
    """(src_w, src_h, duration, [(t, (cx, cy) or None)]) in source pixels.
    Raises ImportError / RuntimeError when detection isn't possible."""
    import cv2  # noqa: F401  (opencv-python-headless)
    model = model_path()
    if model is None:
        raise RuntimeError("face model file missing")
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("can't open the video for face tracking")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        scale = min(1.0, DETECT_WIDTH / max(1, src_w))
        dw, dh = max(1, int(src_w * scale)), max(1, int(src_h * scale))
        detector = cv2.FaceDetectorYN.create(str(model), "", (dw, dh), SCORE_THRESHOLD, 0.3, 20)
        step = max(1, int(round(fps / SAMPLE_FPS)))
        samples, prev, idx = [], None, 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                small = cv2.resize(frame, (dw, dh)) if scale < 1.0 else frame
                _, found = detector.detect(small)
                faces = []
                for f in (found if found is not None else []):
                    x, y, w, h, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
                    faces.append(((x + w / 2) / scale, (y + h / 2) / scale, max(w, h) / scale, score))
                pick = choose_face(faces, prev, src_w)
                if pick:
                    prev = pick[:2]
                samples.append((idx / fps, pick[:2] if pick else None))
            idx += 1
        duration = idx / fps
    finally:
        cap.release()
    return src_w, src_h, duration, samples


# --------------------------------------------------------------------------- #
# Camera path
# --------------------------------------------------------------------------- #

def _median(values: list, k: int = 2) -> list:
    out = []
    for i in range(len(values)):
        win = sorted(values[max(0, i - k):i + k + 1])
        out.append(win[len(win) // 2])
    return out


def _gauss(values: list, sigma: float) -> list:
    if sigma <= 0 or len(values) < 3:
        return list(values)
    r = int(3 * sigma)
    kern = [math.exp(-(j * j) / (2 * sigma * sigma)) for j in range(-r, r + 1)]
    out = []
    for i in range(len(values)):
        acc = wsum = 0.0
        for j, w in enumerate(kern):
            k = i + j - r
            if 0 <= k < len(values):
                acc += values[k] * w
                wsum += w
        out.append(acc / wsum)
    return out


def fill_gaps(samples: list, src_w: int, src_h: int) -> tuple:
    """Face centre per sample: last known position through gaps (the first
    position found before it), the centre if no face was ever found.
    Returns (times, xs, ys, found_any)."""
    times = [t for t, _ in samples]
    known = [p for _, p in samples if p]
    if not known:
        return times, [src_w / 2] * len(samples), [src_h / 2] * len(samples), False
    last = known[0]
    xs, ys = [], []
    for _, p in samples:
        if p:
            last = p
        xs.append(last[0])
        ys.append(last[1])
    return times, xs, ys, True


def camera_path(times: list, centers: list, crop: int, src: int) -> list:
    """Crop offset (left or top edge) per sample along one axis."""
    lo, hi = 0.0, float(src - crop)
    if hi <= 0 or not centers:
        return [0.0] * len(centers)
    want = [min(hi, max(lo, c - crop / 2)) for c in _median(centers)]
    cam, seg_starts = [want[0]], [0]
    pending_cut = 0
    for i in range(1, len(want)):
        dt = max(1e-3, times[i] - times[i - 1])
        c = cam[-1]
        d = want[i] - c
        if abs(d) > CUT_JUMP * src:
            pending_cut += 1
            if pending_cut >= 2:          # the jump persisted: the source cut to another shot
                cam[-1] = want[i - 1]
                seg_starts.append(i - 1)
                cam.append(want[i])
                pending_cut = 0
                continue
        else:
            pending_cut = 0
        dead = DEAD_ZONE * crop
        if abs(d) > dead:
            move = min(abs(d) - dead / 2, MAX_PAN_PER_SEC * crop * dt)
            c += math.copysign(move, d)
        cam.append(min(hi, max(lo, c)))
    sigma = SMOOTH_SECS * SAMPLE_FPS
    out = []
    bounds = seg_starts + [len(cam)]
    for a, b in zip(bounds, bounds[1:]):
        out.extend(_gauss(cam[a:b], sigma))       # never smooth across a cut
    return [min(hi, max(lo, v)) for v in out]


def per_frame(times: list, values: list, duration: float, fps: int = OUT_FPS) -> list:
    """Linear interpolation of the sample path to one value per output frame."""
    n = max(1, int(math.ceil(duration * fps)))
    if not times:
        return [0.0] * n
    out, j = [], 0
    for k in range(n):
        t = k / fps
        while j + 1 < len(times) and times[j + 1] <= t:
            j += 1
        if j + 1 < len(times) and times[j + 1] > times[j]:
            a = (t - times[j]) / (times[j + 1] - times[j])
            a = min(1.0, max(0.0, a))
            out.append(values[j] * (1 - a) + values[j + 1] * a)
        else:
            out.append(values[j])
    return out


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

class Plan:
    """Crop size + per-frame offsets for one output shape."""

    def __init__(self, cw, ch, xs, ys, tracked: bool, note: str):
        self.cw, self.ch, self.xs, self.ys = cw, ch, xs, ys
        self.tracked, self.note = tracked, note

    def sendcmd(self, fps: int = OUT_FPS) -> str:
        lines, last = [], None
        moving_y = len(set(int(round(y)) for y in self.ys)) > 1
        for k, x in enumerate(self.xs):
            xi = int(round(x))
            yi = int(round(self.ys[k])) if self.ys else 0
            cur = (xi, yi)
            if cur == last:
                continue
            cmd = f"crop@rf x {xi}" + (f", crop@rf y {yi}" if moving_y else "")
            lines.append(f"{k / fps:.3f} {cmd};")
            last = cur
        return "\n".join(lines) + "\n"

    def filter(self, cmd_file: str, out_w: int, out_h: int) -> str:
        x0 = int(round(self.xs[0])) if self.xs else 0
        y0 = int(round(self.ys[0])) if self.ys else 0
        return (f"sendcmd=f={cmd_file},crop@rf=w={self.cw}:h={self.ch}:x={x0}:y={y0},"
                f"scale={out_w}:{out_h}:flags=lanczos")


_cache: dict = {}


def plan_crop(video: Path, out_w: int, out_h: int, log=print) -> Plan:
    """The crop path for one output shape (face detection runs once per
    source file and is reused for the other shapes)."""
    key = str(video)
    if key not in _cache:
        try:
            _cache[key] = detect_samples(video)
        except Exception as e:                       # no OpenCV, no model, unreadable file
            _cache[key] = e
    data = _cache[key]
    if isinstance(data, Exception):
        w, h, dur = probe_size(video)
        cw, ch = crop_size(w, h, out_w, out_h)
        n = max(1, int(math.ceil(dur * OUT_FPS)))
        return Plan(cw, ch, [(w - cw) / 2] * n, [(h - ch) / 2] * n, False,
                    f"centred crop (face tracking unavailable: {type(data).__name__}: {data})")
    src_w, src_h, dur, samples = data
    cw, ch = crop_size(src_w, src_h, out_w, out_h)
    times, cx, cy, found = fill_gaps(samples, src_w, src_h)
    xs = per_frame(times, camera_path(times, cx, cw, src_w), dur)
    ys = per_frame(times, camera_path(times, cy, ch, src_h), dur)
    hits = sum(1 for _, p in samples if p)
    note = (f"face found in {hits}/{len(samples)} samples" if found
            else "no face found; centred crop")
    return Plan(cw, ch, xs, ys, found, note)


def forget(video: Path) -> None:
    _cache.pop(str(video), None)


def probe_size(video: Path) -> tuple:
    """(width, height, duration) without OpenCV, via ffprobe if present."""
    import json
    import shutil
    import subprocess
    probe = shutil.which("ffprobe")
    if probe:
        try:
            out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=width,height:format=duration", "-of", "json", str(video)],
                                 capture_output=True, text=True, timeout=60).stdout
            d = json.loads(out)
            s = d["streams"][0]
            return int(s["width"]), int(s["height"]), float(d.get("format", {}).get("duration") or 60)
        except Exception:
            pass
    return 1920, 1080, 60.0


if __name__ == "__main__":     # quick manual check: python reframe.py video.mp4
    p = plan_crop(Path(sys.argv[1]), 1080, 1920)
    print(p.note, p.cw, p.ch, os.linesep.join(p.sendcmd().splitlines()[:10]))
