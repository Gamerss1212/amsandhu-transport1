"""ffmpeg helpers. Uses a system ffmpeg if present, else the one bundled by imageio-ffmpeg."""
from __future__ import annotations

import functools
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: list[str], timeout: float | None = None) -> str:
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return proc.stderr


def probe(path: Path) -> dict:
    """Duration, size and fps, parsed from `ffmpeg -i` (ffprobe isn't always available)."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True)
    err = proc.stderr
    info: dict = {"duration": 0.0, "width": 0, "height": 0, "fps": 30.0, "has_audio": False}
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", err)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?) fps", err)
    if m:
        info["fps"] = float(m.group(1))
    info["has_audio"] = bool(re.search(r"Stream #.*Audio:", err))
    return info


def extract_audio(video: Path, out_wav: Path, sr: int = 16000) -> Path:
    if not out_wav.exists():
        run_ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", str(sr), "-f", "wav", str(out_wav)])
    return out_wav


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def extract_frame(video: Path, t: float, out: Path, width: int = 640) -> Path:
    run_ffmpeg(["-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1",
                "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])
    return out


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(text: str) -> float | None:
    parts = text.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None
