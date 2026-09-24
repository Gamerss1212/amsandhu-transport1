"""Self-review: every finished clip is checked the way a second editor would check it, and whatever can
be fixed is fixed automatically (EDIT -> REVIEW -> FIX -> REVIEW AGAIN -> FINALIZE).

Checks: picture size and frame rate, length, audio present, loudness (TikTok/Reels target about
-14 LUFS), true peak (no distortion), black frames, frozen frames, subtitle timing.
Fixes:  loudness / peak -> exact gain correction + limiter (video stream copied, no quality loss);
        black frames at the start or end -> trimmed off (captions shifted to match);
        anything structural (size, length, missing audio, frozen picture) -> the editor re-renders
        with a safer set of effects.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..media import ffmpeg_exe, probe, run_ffmpeg

TARGET_LUFS = -14.0


@dataclass
class Finding:
    code: str        # e.g. "loudness", "black_start", "frozen"
    detail: str
    action: str      # "fix" (patched in place) | "retry" (re-render needed) | "note" (nothing to do)


@dataclass
class Measure:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    has_audio: bool = False
    lufs: float | None = None
    true_peak: float | None = None
    black: list[tuple[float, float]] = field(default_factory=list)
    frozen: list[tuple[float, float]] = field(default_factory=list)


def _run(args: list[str]) -> str:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", *args], capture_output=True, text=True).stderr


def measure(video: Path) -> Measure:
    pr = probe(video)
    m = Measure(pr["width"], pr["height"], pr["fps"], pr["duration"], pr["has_audio"])
    # one decode for everything: loudness + true peak from the audio, black / frozen frames from a small
    # copy of the picture
    args = ["-i", str(video)]
    if m.has_audio:
        args += ["-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"]
    args += ["-map", "0:v:0", "-vf", "scale=180:-2,blackdetect=d=0.25:pix_th=0.08,freezedetect=n=-60dB:d=2.5",
             "-f", "null", "-"]
    err = _run(args)
    if m.has_audio:
        i = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
        tp = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", err)
        m.lufs = float(i[-1]) if i else None
        m.true_peak = float(tp[-1]) if tp and tp[-1] != "-inf" else None
    m.black = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    m.frozen = [(a, ends[k] if k < len(ends) else m.duration) for k, a in enumerate(starts)]
    return m


def findings(m: Measure, expected: float, W: int, H: int, fps: int, srt: Path | None = None) -> list[Finding]:
    out: list[Finding] = []
    if (m.width, m.height) != (W, H):
        out.append(Finding("size", f"{m.width}x{m.height}, expected {W}x{H}", "retry"))
    if abs(m.fps - fps) > 0.5:
        out.append(Finding("fps", f"{m.fps} fps, expected {fps}", "retry"))
    if abs(m.duration - expected) > 0.4:
        out.append(Finding("duration", f"{m.duration:.2f}s, planned {expected:.2f}s", "retry"))
    if not m.has_audio:
        out.append(Finding("no_audio", "no audio track", "retry"))
    elif m.lufs is not None and m.lufs > -40:  # below -40 LUFS there is no speech to level
        if not -15.5 <= m.lufs <= -12.5:
            out.append(Finding("loudness", f"{m.lufs:.1f} LUFS (target {TARGET_LUFS:.0f})", "fix"))
        if m.true_peak is not None and m.true_peak > -0.5:
            out.append(Finding("peak", f"true peak {m.true_peak:.1f} dBFS - would distort on phones", "fix"))
    for a, b in m.black:
        if a <= 0.05:
            out.append(Finding("black_start", f"black {a:.2f}-{b:.2f}s", "fix"))
        elif b >= m.duration - 0.1:
            out.append(Finding("black_end", f"black {a:.2f}-{b:.2f}s", "fix"))
        elif b - a >= 0.5:
            out.append(Finding("black_mid", f"black {a:.2f}-{b:.2f}s (source fade?)", "note"))
    for a, b in m.frozen:
        out.append(Finding("frozen", f"picture frozen {a:.1f}-{b:.1f}s", "retry"))
    if srt and srt.exists():
        last = -1.0
        for t in re.findall(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)",
                            srt.read_text(encoding="utf-8")):
            a = int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2]) + int(t[3]) / 1000
            b = int(t[4]) * 3600 + int(t[5]) * 60 + int(t[6]) + int(t[7]) / 1000
            if b <= a or a < last - 0.01 or a > m.duration + 0.5:
                out.append(Finding("captions", f"subtitle timing broken at {a:.2f}s", "retry"))
                break
            last = b
    return out


def fix_audio(video: Path, m: Measure) -> None:
    """Exact loudness correction plus a -1 dBTP limiter; the picture is copied untouched."""
    gain = TARGET_LUFS - (m.lufs if m.lufs is not None else TARGET_LUFS)
    tmp = video.with_name(video.stem + ".fix.mp4")
    run_ffmpeg(["-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", f"volume={gain:.2f}dB,alimiter=limit=0.89:level=false,aresample=48000",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)])
    tmp.replace(video)


def trim(video: Path, start: float, end: float, srt: Path | None, encode: list[str]) -> None:
    """Cut black frames off the ends (re-encoded, so the cut is frame-exact) and shift the subtitles."""
    tmp = video.with_name(video.stem + ".trim.mp4")
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end - start:.3f}", *encode,
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)])
    tmp.replace(video)
    if srt and srt.exists() and start > 0:
        def shift(mt: re.Match) -> str:
            t = int(mt.group(1)) * 3600 + int(mt.group(2)) * 60 + int(mt.group(3)) + int(mt.group(4)) / 1000
            t = max(0.0, t - start)
            ms = int(round(t * 1000))
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
        srt.write_text(re.sub(r"(\d\d):(\d\d):(\d\d),(\d\d\d)", shift, srt.read_text(encoding="utf-8")),
                       encoding="utf-8")


def review_and_fix(video: Path, expected: float, W: int, H: int, fps: int, srt: Path | None,
                   encode: list[str]) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """REVIEW -> FIX -> REVIEW AGAIN. Returns (fixed, still wrong and needing a re-render, notes)."""
    m = measure(video)
    first = findings(m, expected, W, H, fps, srt)
    fixed: list[Finding] = []
    black_start = next((f for f in first if f.code == "black_start"), None)
    black_end = next((f for f in first if f.code == "black_end"), None)
    if black_start or black_end:
        a = next((b for s, b in m.black if s <= 0.05), 0.0) if black_start else 0.0
        z = next((s for s, b in m.black if b >= m.duration - 0.1), m.duration) if black_end else m.duration
        if z - a >= max(3.0, 0.6 * m.duration):  # never trim away the clip itself
            trim(video, a, z, srt, encode)
            fixed += [f for f in (black_start, black_end) if f]
            expected = z - a
            m = measure(video)
    if any(f.code in ("loudness", "peak") for f in findings(m, expected, W, H, fps)):
        fix_audio(video, m)
        fixed += [f for f in first if f.code in ("loudness", "peak")]
        m = measure(video)
    final = findings(m, expected, W, H, fps, srt)  # review again, after the fixes
    retry = [f for f in final if f.action == "retry"]  # a re-render can fix these
    notes = [f for f in final if f.action != "retry"]  # a second audio pass would not change anything
    return fixed, retry, notes
