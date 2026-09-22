"""Scene cuts and subject tracking, used to drive the auto-reframe.

Frames are pulled straight from an ffmpeg pipe at a low rate and small size -
that is far cheaper than seeking with an OpenCV VideoCapture, and it means the
motion fallback works with no OpenCV installed at all.
"""

from __future__ import annotations

import re
import subprocess
from typing import List, Optional, Sequence, Tuple

import numpy as np

from ..config import Config
from ..models import FaceSample, SourceVideo, VisualAnalysis
from ..utils import info, warn
from ..utils.ffmpeg import ffmpeg_bin

ANALYSIS_WIDTH = 640   # detection floor for a talking head, still cheap to decode


def analyze_visual(source: SourceVideo, ranges: Sequence[Tuple[float, float]],
                   cfg: Config) -> VisualAnalysis:
    if not cfg.reframe.enabled or cfg.reframe.mode in ("none", "center"):
        return VisualAnalysis(method=cfg.reframe.mode or "none")

    cuts: List[float] = []
    faces: List[FaceSample] = []
    method = "motion"
    detector = (_load_face_detector(cfg.reframe.download_models)
                if cfg.reframe.mode in ("auto", "face") else None)
    if detector:
        method = detector.name
    elif cfg.reframe.mode == "face":
        warn("OpenCV is not installed - reframing falls back to motion tracking. "
             "Install it with `pip install 'viralforge[vision]'` for speaker-aware framing.")

    for start, end in ranges:
        cuts.extend(detect_scene_cuts(source.path, start, end))
        faces.extend(_track_range(source, start, end, cfg, detector))

    cuts = sorted(set(round(c, 3) for c in cuts))
    faces.sort(key=lambda f: f.t)
    return VisualAnalysis(scene_cuts=cuts, faces=faces, method=method)


# --------------------------------------------------------------------------- #
# Scene cuts
# --------------------------------------------------------------------------- #

_SCENE_RE = re.compile(r"pts_time:([0-9.]+)")


def detect_scene_cuts(path: str, start: float, end: float, threshold: float = 0.32) -> List[float]:
    """Absolute timestamps where the shot changes."""
    duration = max(0.0, end - start)
    if duration < 1.0:
        return []
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", path,
        "-filter_complex",
        f"[0:v]scale={ANALYSIS_WIDTH}:-2,select='gt(scene,{threshold})',metadata=print:file=-",
        "-an", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    out: List[float] = []
    for m in _SCENE_RE.finditer(proc.stdout or ""):
        t = start + float(m.group(1))
        if start + 0.25 < t < end - 0.25:
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
# Frame source
# --------------------------------------------------------------------------- #


def _iter_frames(path: str, start: float, end: float, fps: float, width: int):
    """Yield (timestamp, HxWx3 uint8 BGR array) at roughly ``fps``."""
    duration = max(0.0, end - start)
    if duration <= 0:
        return
    height_expr = "-2"
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", path,
        "-vf", f"fps={fps},scale={width}:{height_expr}",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
    ]
    probe = _probe_scaled_size(path, width)
    if not probe:
        return
    w, h = probe
    frame_bytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    index = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes) if proc.stdout else b""
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            yield start + index / fps, frame
            index += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


_SIZE_CACHE: dict = {}


def _probe_scaled_size(path: str, width: int) -> Optional[Tuple[int, int]]:
    key = (path, width)
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    from ..utils.ffmpeg import ffprobe_media
    media = ffprobe_media(path)
    if not media["width"] or not media["height"]:
        return None
    h = int(round(media["height"] * width / media["width"] / 2.0)) * 2
    _SIZE_CACHE[key] = (width, h)
    return _SIZE_CACHE[key]


# --------------------------------------------------------------------------- #
# Face detection
# --------------------------------------------------------------------------- #


class _CascadeDetector:
    """Haar cascades - bundled with OpenCV 4.x, removed in 5.x."""

    name = "haar"

    def __init__(self, cv2_mod):
        self.cv2 = cv2_mod
        base = cv2_mod.data.haarcascades
        self.front = cv2_mod.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
        self.profile = cv2_mod.CascadeClassifier(base + "haarcascade_profileface.xml")
        if self.front.empty():
            raise RuntimeError("OpenCV cascade data is missing")
        # CLAHE, not equalizeHist.  Global equalisation is actively harmful
        # here: on a frame with a large flat dark backdrop - a studio set, a
        # night shot - it stretches the background across the histogram and
        # compresses the face's tonal range until the Haar features vanish.
        # Measured on a dark-backdrop frame: equalizeHist 0 detections,
        # CLAHE 1, raw 1.
        try:
            self.clahe = cv2_mod.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        except Exception:
            self.clahe = None

    def detect(self, frame) -> List[Tuple[int, int, int, int, float]]:
        cv2 = self.cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.clahe is not None:
            gray = self.clahe.apply(gray)
        h = gray.shape[0]
        min_size = max(24, int(h * 0.06))
        found: List[Tuple[int, int, int, int, float]] = []
        for cascade, weight in ((self.front, 1.0), (self.profile, 0.75)):
            if cascade is None or cascade.empty():
                continue
            rects = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4,
                                             minSize=(min_size, min_size))
            for (x, y, w, hh) in rects:
                found.append((int(x), int(y), int(w), int(hh), weight * w * hh))
        return found


class _YuNetDetector:
    """YuNet (OpenCV Zoo) - a small DNN detector, better than Haar in every way.

    It needs a 340 KB ONNX file that OpenCV does not ship, so the model is
    resolved from disk or fetched once and cached.
    """

    name = "yunet"

    def __init__(self, cv2_mod, model_path: str):
        self.cv2 = cv2_mod
        self.model_path = model_path
        self._size = None
        self.detector = cv2_mod.FaceDetectorYN.create(model_path, "", (320, 320), 0.6, 0.3, 5000)

    def detect(self, frame) -> List[Tuple[int, int, int, int, float]]:
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self.detector.setInputSize((w, h))
            self._size = (w, h)
        ok, faces = self.detector.detect(frame)
        if not ok or faces is None:
            return []
        out: List[Tuple[int, int, int, int, float]] = []
        for row in faces:
            x, y, fw, fh = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
            confidence = float(row[-1])
            if fw <= 1 or fh <= 1:
                continue
            out.append((int(x), int(y), int(fw), int(fh), confidence * fw * fh))
        return out


YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"


def _yunet_model_path(allow_download: bool) -> Optional[str]:
    import os
    from pathlib import Path

    env = os.environ.get("VF_YUNET_MODEL")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parent.parent.parent / "assets" / "models"
                      / YUNET_FILENAME)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "viralforge"
    candidates.append(cache / YUNET_FILENAME)
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 50_000:
            return str(candidate)

    if not allow_download:
        return None
    try:
        import requests
        info("Fetching the YuNet face model (once, ~340 KB)...")
        response = requests.get(YUNET_URL, timeout=60)
        response.raise_for_status()
        if len(response.content) < 50_000:
            raise ValueError(f"unexpected response of {len(response.content)} bytes")
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / YUNET_FILENAME
        target.write_bytes(response.content)
        return str(target)
    except Exception as exc:
        warn(f"Could not fetch the face model ({exc}). Set VF_YUNET_MODEL to a local copy "
             f"of {YUNET_FILENAME}, or let reframing fall back to motion tracking.")
        return None


def _load_face_detector(allow_download: bool = True):
    """Best available face detector, or None to fall back to motion tracking."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return None

    if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
        try:
            return _CascadeDetector(cv2)
        except Exception:
            pass                      # OpenCV 5 keeps the symbol but drops the data

    if hasattr(cv2, "FaceDetectorYN"):
        model = _yunet_model_path(allow_download)
        if model:
            try:
                return _YuNetDetector(cv2, model)
            except Exception as exc:
                warn(f"YuNet failed to load ({exc}) - falling back to motion tracking.")
    return None


def _track_range(source: SourceVideo, start: float, end: float, cfg: Config,
                 detector) -> List[FaceSample]:
    samples: List[FaceSample] = []
    prev_gray: Optional[np.ndarray] = None
    last_cx, last_cy = 0.5, 0.42

    for t, frame in _iter_frames(source.path, start, end, cfg.reframe.detect_fps, ANALYSIS_WIDTH):
        h, w = frame.shape[:2]
        picked: Optional[FaceSample] = None

        if detector is not None:
            faces = detector.detect(frame)
            if faces:
                # Prefer big and central: the on-camera speaker, not a poster
                # on the back wall.
                def rank(f):
                    x, y, fw, fh, area = f
                    cx = (x + fw / 2) / w
                    return area * (1.0 - 0.55 * abs(cx - 0.5))
                x, y, fw, fh, area = max(faces, key=rank)
                picked = FaceSample(t=round(t, 3), cx=(x + fw / 2) / w,
                                    cy=(y + fh / 2) / h, size=fw / w, score=1.0)

        if picked is None:
            gray = frame[:, :, 1].astype(np.float32)   # green channel ~ luma, no cv2 needed
            if prev_gray is not None:
                diff = np.abs(gray - prev_gray)
                total = float(diff.sum())
                if total > 1e3:
                    ys, xs = np.nonzero(diff > max(12.0, float(np.percentile(diff, 99))))
                    if xs.size > 12:
                        picked = FaceSample(t=round(t, 3), cx=float(xs.mean()) / w,
                                            cy=float(ys.mean()) / h, size=0.0, score=0.35)
            prev_gray = gray

        if picked is None:
            picked = FaceSample(t=round(t, 3), cx=last_cx, cy=last_cy, size=0.0, score=0.0)
        last_cx, last_cy = picked.cx, picked.cy
        samples.append(picked)

    return samples
