"""ffmpeg helpers. Uses a system ffmpeg if present, else the one bundled by imageio-ffmpeg."""
from __future__ import annotations

import functools
import re
import shutil
import subprocess
import threading
import time
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


def ensure_ffmpeg_on_path(bin_dir: Path) -> None:
    """yt-dlp's partial downloads (just one clip of a very long video) look for a program literally
    named ffmpeg on PATH; the bundled one has a versioned file name, so expose it under that name."""
    import os
    import sys

    if shutil.which("ffmpeg"):
        return
    exe = ffmpeg_exe()
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    target = bin_dir / name
    if not target.exists():
        bin_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.link(exe, target)
        except OSError:
            shutil.copy2(exe, target)
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class Cancelled(RuntimeError):
    """The user pressed Stop."""


CANCEL = threading.Event()  # set by the Stop button: every long step checks it and stops right away


def check_cancel() -> None:
    if CANCEL.is_set():
        raise Cancelled("Stopped")


def run_ffmpeg(args: list[str], timeout: float | None = None) -> str:
    check_cancel()
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    started = time.time()
    while True:  # wait in short steps so Stop can kill a long render immediately
        try:
            _, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if CANCEL.is_set() or (timeout and time.time() - started > timeout):
                proc.kill()
                proc.communicate()
                if CANCEL.is_set():
                    raise Cancelled("Stopped") from None
                raise subprocess.TimeoutExpired(cmd, timeout) from None
    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-25:])
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return err


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


LONG_AUDIO_HOURS = 3.0  # longer than this, audio is streamed from the source instead of copied to a .wav


def media_seconds(path: Path) -> float:
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    return probe(path)["duration"]


def pcm_chunks(path: Path, seconds: float, sr: int = 16000):
    """16 kHz mono samples (int16) of any audio/video file, `seconds` at a time. Works on a
    200-hour file with flat memory and no giant temporary .wav."""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            if w.getframerate() == sr and w.getnchannels() == 1:
                while raw := w.readframes(int(seconds * sr)):
                    yield np.frombuffer(raw, dtype=np.int16)
                return
    proc = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
                             "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    need = int(seconds * sr) * 2
    try:
        while True:
            buf = proc.stdout.read(need)
            if not buf:
                break
            yield np.frombuffer(buf[: len(buf) // 2 * 2], dtype=np.int16)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def audio_for_analysis(source: Path, out_wav: Path) -> Path:
    """A 16 kHz .wav for normal videos (fast repeated reads); the source itself for very long ones
    (a 200-hour .wav would be ~23 GB)."""
    if out_wav.exists():
        return out_wav
    if media_seconds(source) > LONG_AUDIO_HOURS * 3600:
        return source
    return extract_audio(source, out_wav)


def frame_power(path: Path, hop: float = 0.1) -> tuple[np.ndarray, float]:
    """Mean square level of every `hop`-second frame, read in pieces so even a 200-hour
    video's audio never has to fit in memory at once."""
    sr = 16000
    step = int(sr * hop)
    out = []
    for chunk in pcm_chunks(path, 60.0, sr):
        a = chunk.astype(np.float32) / 32768.0
        n = len(a) // step
        if n:
            out.append(np.mean(a[: n * step].reshape(n, step) ** 2, axis=1))
    return (np.concatenate(out) if out else np.zeros(0, dtype=np.float32)), hop


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
