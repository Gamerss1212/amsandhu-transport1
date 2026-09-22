"""Thin, well-behaved wrappers around the ffmpeg / ffprobe binaries."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence


class FFmpegError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-18:])
        super().__init__(f"ffmpeg exited {returncode}\n{tail}")


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BINARY", "ffmpeg")


def ffprobe_bin() -> str:
    return os.environ.get("FFPROBE_BINARY", "ffprobe")


def require_ffmpeg() -> None:
    missing = [b for b in (ffmpeg_bin(), ffprobe_bin()) if shutil.which(b) is None]
    if missing:
        raise RuntimeError(
            f"Could not find {', '.join(missing)} on PATH. Install ffmpeg "
            "(macOS: `brew install ffmpeg`, Debian/Ubuntu: `sudo apt install ffmpeg`, "
            "Windows: `winget install Gyan.FFmpeg`) or set FFMPEG_BINARY / FFPROBE_BINARY."
        )


def has_filter(name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return False
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M) is not None


def ffprobe_media(path: str) -> Dict[str, Any]:
    """Return {duration, width, height, fps, has_audio, video_codec, ...}."""
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr)
    data = json.loads(proc.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0.0)
    if not duration and video and video.get("duration"):
        duration = float(video["duration"])

    fps = 30.0
    if video:
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = video.get(key) or ""
            if "/" in raw:
                num, den = raw.split("/", 1)
                try:
                    if float(den):
                        fps = float(num) / float(den)
                        break
                except ValueError:
                    pass

    width, height = (int(video.get("width", 0)), int(video.get("height", 0))) if video else (0, 0)
    # Respect rotation metadata - a phone-shot 1920x1080 tagged 90deg is really vertical.
    if video:
        rotation = 0
        for sd in video.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(abs(float(sd["rotation"]))) % 180
        if rotation == 0:
            try:
                rotation = int(abs(float(video.get("tags", {}).get("rotate", 0)))) % 180
            except (TypeError, ValueError):
                rotation = 0
        if rotation == 90:
            width, height = height, width

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": round(fps, 4) if fps else 30.0,
        "has_audio": audio is not None,
        "video_codec": (video or {}).get("codec_name", ""),
        "audio_codec": (audio or {}).get("codec_name", ""),
        "sample_rate": int((audio or {}).get("sample_rate") or 0),
    }


def run_ffmpeg(
    args: Sequence[str],
    *,
    on_progress=None,
    total_duration: Optional[float] = None,
    quiet: bool = True,
) -> None:
    """Run ffmpeg, optionally reporting progress as a 0..1 fraction."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"]
    if quiet:
        cmd += ["-loglevel", "error"]
    if on_progress and total_duration:
        cmd += ["-progress", "pipe:1", "-stats_period", "0.4"]
    cmd += list(args)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if (on_progress and total_duration) else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if on_progress and total_duration and proc.stdout is not None:
        for line in proc.stdout:
            if line.startswith("out_time_ms="):
                raw = line.strip().split("=", 1)[1]
                if raw.isdigit():
                    on_progress(min(1.0, (int(raw) / 1_000_000.0) / max(total_duration, 0.001)))
    stderr = proc.stderr.read() if proc.stderr else ""
    proc.wait()
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, stderr)
    if on_progress:
        on_progress(1.0)


def escape_filter_path(path: str) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument.

    ffmpeg parses filter graphs before the filters see their own arguments, so
    ``:``, ``'``, ``\\`` and ``[`` all need escaping - Windows drive letters
    (``C:\\clips``) break naively-built graphs.
    """
    out = path.replace("\\", "/")
    out = out.replace("'", r"\'")
    out = out.replace(":", r"\:")
    out = out.replace("[", r"\[").replace("]", r"\]")
    out = out.replace(",", r"\,").replace(";", r"\;")
    return out


def extract_audio(src: str, dst: str, sample_rate: int = 16000, mono: bool = True) -> str:
    run_ffmpeg(["-i", src, "-vn", "-ac", "1" if mono else "2",
                "-ar", str(sample_rate), "-c:a", "pcm_s16le", dst])
    return dst


def extract_frame(src: str, timestamp: float, dst: str, width: Optional[int] = None) -> str:
    args = ["-ss", f"{max(0.0, timestamp):.3f}", "-i", src, "-frames:v", "1"]
    if width:
        args += ["-vf", f"scale={width}:-2"]
    args += ["-q:v", "2", dst]
    run_ffmpeg(args)
    return dst
