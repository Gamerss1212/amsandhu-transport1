"""Synthetic source material, so the pipeline can be tested without a download.

Produces a 16:9 video with a subject that moves horizontally, speech-shaped
audio with real silences, and a matching SRT - which is enough to exercise
every stage except the model calls.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import List, Tuple

import numpy as np

SPEECH: List[Tuple[float, float, str]] = [
    (0.4, 7.6, "Most people think you need permission to start. "
               "That is the single most expensive idea in this entire industry."),
    (9.2, 19.6, "I lost forty thousand dollars in my first year because I waited "
                "for someone to tell me I was ready. Nobody was ever going to do that."),
    (21.5, 34.6, "Here is what actually changed it. I stopped asking whether the plan "
                 "was good and started asking whether I could survive being wrong. "
                 "That one question rebuilt the whole business."),
    (36.0, 51.6, "The second thing is harder. You have to be willing to be bad at "
                 "something in public for about six months. Everyone quits at month two. "
                 "That is the entire filter."),
    (53.5, 69.0, "So if you are sitting there with a plan you have not started, the plan "
                 "is not the problem. The problem is that you think there is a version "
                 "of this where you do not look stupid first."),
]
DURATION = 70.0
SAMPLE_RATE = 44100
FACE_SIZE = 300
# Kept as both an ffmpeg expression and a Python function so a test can assert
# that the reframe actually followed the subject.
FACE_X_EXPR = "160+1180*(0.5+0.5*sin(2*PI*t/23))"
FACE_Y_EXPR = "240+90*sin(2*PI*t/17)"


def face_center(t: float) -> tuple:
    """Where the face is, in source pixels, at time ``t``."""
    x = 160 + 1180 * (0.5 + 0.5 * np.sin(2 * np.pi * t / 23))
    y = 240 + 90 * np.sin(2 * np.pi * t / 17)
    return x + FACE_SIZE / 2, y + FACE_SIZE / 2


def _speech_audio() -> np.ndarray:
    rng = np.random.default_rng(7)
    n = int(DURATION * SAMPLE_RATE)
    out = rng.normal(0, 1, n).astype(np.float32) * 0.0015     # room tone
    t = np.arange(n) / SAMPLE_RATE

    for start, end, text in SPEECH:
        i0, i1 = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
        span = i1 - i0
        local_t = t[i0:i1] - start
        # Syllable-rate amplitude modulation over band-ish noise reads to an RMS
        # gate the way speech does, which is all the analyser needs.
        syllables = 0.5 + 0.5 * np.sin(2 * np.pi * 4.4 * local_t + rng.random() * 6)
        phrase = 0.6 + 0.4 * np.sin(2 * np.pi * 0.28 * local_t)
        carrier = rng.normal(0, 1, span).astype(np.float32)
        carrier = np.convolve(carrier, np.ones(12, dtype=np.float32) / 12, mode="same")
        out[i0:i1] += (carrier * syllables * phrase * 0.28).astype(np.float32)

    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.82).astype(np.float32)


def _ellipse(canvas: np.ndarray, cx: float, cy: float, rx: float, ry: float,
             value: int) -> None:
    h, w = canvas.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    mask = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
    canvas[mask] = value


def write_face_ppm(path: Path, size: int = 300) -> Path:
    """A face crude enough to draw with numpy, structured enough for a detector.

    Haar and YuNet both key on the same arrangement - dark eye sockets above a
    brighter nose bridge, a darker mouth band below - so a schematic face is
    detected like a real one, which is exactly what a tracking test needs.
    """
    img = np.full((size, size), 200, dtype=np.uint8)
    c = size / 2
    _ellipse(img, c, c, size * 0.32, size * 0.42, 186)            # face
    _ellipse(img, c - size * 0.14, c - size * 0.08, size * 0.10, size * 0.055, 70)   # eyes
    _ellipse(img, c + size * 0.14, c - size * 0.08, size * 0.10, size * 0.055, 70)
    _ellipse(img, c, c + size * 0.04, size * 0.05, size * 0.11, 215)                 # nose
    _ellipse(img, c, c + size * 0.22, size * 0.13, size * 0.045, 95)                 # mouth
    rgb = np.repeat(img[:, :, None], 3, axis=2)
    rgb[:, :, 2] = np.clip(rgb[:, :, 2].astype(np.int16) - 18, 0, 255).astype(np.uint8)
    with open(path, "wb") as fh:
        fh.write(f"P6\n{size} {size}\n255\n".encode("ascii"))
        fh.write(rgb.tobytes())
    return path


def make_test_video(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "source.mp4"
    if video.is_file():
        return video

    wav = directory / "audio.wav"
    samples = (_speech_audio() * 32767).astype(np.int16)
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(samples.tobytes())

    face = write_face_ppm(directory / "face.ppm")

    # The face drifts across the frame so the reframer has something to follow,
    # and a distractor block sits in the corner so "follow the biggest thing"
    # is not the same answer as "follow the face".
    graph = (
        f"[1:v]scale=300:300[face];"
        f"[0:v][face]overlay=x='{FACE_X_EXPR}':y='{FACE_Y_EXPR}'[bg];"
        "[bg][2:v]overlay=x=1560:y=760[v]"
    )
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x16202c:s=1920x1080:r=30:d={DURATION}",
        "-loop", "1", "-t", str(DURATION), "-i", str(face),
        "-f", "lavfi", "-i", f"color=c=0x3f6f9f:s=260x200:d={DURATION}",
        "-i", str(wav),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(video),
    ], check=True, capture_output=True)
    wav.unlink(missing_ok=True)
    return video


def _srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_test_srt(directory: Path) -> Path:
    """One cue per sentence, timed inside its speech block."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "transcript.srt"
    blocks: List[str] = []
    index = 1
    for start, end, text in SPEECH:
        sentences = [s.strip() + "." for s in text.split(". ") if s.strip()]
        sentences = [s[:-2] + "." if s.endswith("..") else s for s in sentences]
        span = (end - start) / max(len(sentences), 1)
        for i, sentence in enumerate(sentences):
            a = start + i * span
            b = min(end, a + span - 0.05)
            blocks.append(f"{index}\n{_srt_time(a)} --> {_srt_time(b)}\n{sentence}\n")
            index += 1
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path
