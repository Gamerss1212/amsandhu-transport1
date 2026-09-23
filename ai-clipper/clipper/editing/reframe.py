"""Speaker-following vertical reframing.

Samples the clip a few times per second, finds faces (OpenCV YuNet), detects scene
cuts, and produces a smooth horizontal camera path plus a layout decision:
  * "crop" - a 9:16 window that follows the main face
  * "fit"  - two people far apart / wide shot: full frame over a blurred fill
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # hide OpenCV's harmless backend warnings in the console

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from ..config import ROOT


@dataclass
class Track:
    layout: str                          # "crop" | "fit"
    keyframes: list[tuple[float, float]]  # (time s, face centre as fraction of width)


MODEL_URLS = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx",
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
)
MODEL_PATH = ROOT / "data" / "models" / "face_detection_yunet_2023mar.onnx"


def _yunet_model() -> Path | None:
    if MODEL_PATH.exists():
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    for url in MODEL_URLS:
        try:
            r = requests.get(url, timeout=60)
            if r.ok and len(r.content) > 100_000:
                MODEL_PATH.write_bytes(r.content)
                return MODEL_PATH
        except requests.RequestException:
            continue
    return None


def _make_detector(width: int, height: int):
    """Returns detect(frame) -> list of (x, y, w, h). YuNet (neural) if possible, else Haar."""
    import cv2

    model = _yunet_model()
    if model and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(str(model), "", (width, height), 0.7, 0.3, 50)

        def detect(frame):
            _, faces = det.detect(frame)
            return [] if faces is None else [tuple(f[:4]) for f in faces]
        return detect
    if hasattr(cv2, "CascadeClassifier"):
        frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

        def detect(frame):
            gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            size = (int(frame.shape[0] * 0.08),) * 2
            return list(frontal.detectMultiScale(gray, 1.1, 5, minSize=size))
        return detect
    return lambda frame: []


def analyze_faces(video: str, sample_fps: float = 3.0) -> tuple[list[float], list[list[tuple[float, float]]], list[float]]:
    """Returns (sample times, faces per sample as (cx_frac, width_frac), scene-cut times)."""
    import cv2

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    sw, sh = 480, max(2, int(src_h * 480 / src_w))
    detect = _make_detector(sw, sh)
    step = max(1, int(round(fps / sample_fps)))
    times, faces, cuts = [], [], []
    prev_hist = None
    idx = 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            small = cv2.resize(frame, (sw, sh))
            faces.append([((x + fw / 2) / sw, fw / sw) for x, y, fw, fh in detect(small)])
            t = idx / fps
            times.append(t)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < 0.55:
                cuts.append(t)
            prev_hist = hist
        idx += 1
    cap.release()
    return times, faces, cuts


def plan_track(times: list[float], faces: list[list[tuple[float, float]]], cuts: list[float],
               mode: str) -> Track:
    if mode == "center" or not times:
        return Track("crop", [(0.0, 0.5)])

    wide = 0
    raw = []
    last = 0.5
    for fs in faces:
        if len(fs) >= 2:
            fs_sorted = sorted(fs, key=lambda f: -f[1])
            a, b = fs_sorted[0], fs_sorted[1]
            if b[1] >= 0.5 * a[1] and abs(a[0] - b[0]) > 0.4:
                wide += 1
        if fs:
            last = max(fs, key=lambda f: f[1])[0]
        raw.append(last)
    if wide > 0.55 * len(faces):
        return Track("fit", [(0.0, 0.5)])

    # median filter to kill detector jitter
    arr = np.array(raw)
    k = 5
    padded = np.pad(arr, (k // 2, k // 2), mode="edge")
    smooth = np.array([np.median(padded[i:i + k]) for i in range(len(arr))])

    dead_zone, ramp = (0.10, 0.35) if mode == "face" else (0.05, 0.7)
    keyframes: list[tuple[float, float]] = [(0.0, float(smooth[0]))]
    cur = float(smooth[0])
    cut_set = sorted(cuts)
    for t, target in zip(times, smooth):
        target = float(target)
        at_cut = any(abs(t - c) < 0.2 for c in cut_set)
        if at_cut and abs(target - cur) > 0.02:
            keyframes += [(max(t - 0.001, keyframes[-1][0] + 0.001), cur), (t, target)]  # hard cut
            cur = target
        elif abs(target - cur) > dead_zone:
            start = max(t, keyframes[-1][0] + 0.001)
            keyframes += [(start, cur), (start + ramp, target)]  # smooth pan
            cur = target
    return Track("crop", _simplify(keyframes))


def _simplify(kf: list[tuple[float, float]], tol: float = 0.01) -> list[tuple[float, float]]:
    """Drop keyframes that a straight line between neighbours already explains."""
    kf = sorted(kf)
    out = [kf[0]]
    for i in range(1, len(kf) - 1):
        (t0, x0), (t1, x1), (t2, x2) = out[-1], kf[i], kf[i + 1]
        if t2 - t0 > 0:
            interp = x0 + (x2 - x0) * (t1 - t0) / (t2 - t0)
            if abs(interp - x1) < tol:
                continue
        out.append(kf[i])
    if len(kf) > 1:
        out.append(kf[-1])
    if len(out) > 150:  # keep the ffmpeg expression shallow
        step = len(out) / 150
        out = [out[int(i * step)] for i in range(150)] + [out[-1]]
    return out


def _px(frac_expr: str, in_w: str, crop_w: str) -> str:
    return f"max(0,min({in_w}-{crop_w},({frac_expr})*{in_w}-{crop_w}/2))"


def x_expression(track: Track, in_w: str, crop_w: str) -> str:
    """ffmpeg expression for the crop's left edge over time (piecewise-linear)."""
    kf = track.keyframes
    expr = _px(f"{kf[-1][1]:.4f}", in_w, crop_w)
    for (t0, x0), (t1, x1) in reversed(list(zip(kf, kf[1:]))):
        dt = max(t1 - t0, 1e-3)
        seg = f"{x0:.4f}+({x1 - x0:.4f})*(t-{t0:.3f})/{dt:.3f}"
        expr = f"if(lt(t,{t1:.3f}),{_px(seg, in_w, crop_w)},{expr})"
    return expr
