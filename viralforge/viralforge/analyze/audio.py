"""Loudness curve and speech/silence structure.

Everything downstream that talks about "pacing" or "energy" is grounded in
this one measurement: a short-window RMS curve over the whole source.  It
drives dead-air trimming, emphasis detection for punch-ins, and the delivery
component of the clip score.
"""

from __future__ import annotations

import subprocess
from typing import List, Tuple

import numpy as np

from ..config import Config
from ..models import AudioAnalysis
from ..utils.ffmpeg import FFmpegError, ffmpeg_bin

SAMPLE_RATE = 8000       # plenty for an energy envelope
HOP = 0.02               # 20 ms analysis hop


def _decode_pcm(path: str, highpass_hz: int) -> np.ndarray:
    """Decode the whole audio track to mono float32 at SAMPLE_RATE."""
    filters = [f"aresample={SAMPLE_RATE}"]
    if highpass_hz:
        # Rumble and handling noise otherwise read as "speech" to an RMS gate.
        filters.insert(0, f"highpass=f={highpass_hz}")
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", path, "-vn", "-ac", "1", "-af", ",".join(filters),
           "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    if not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def analyze_audio(path: str, cfg: Config) -> AudioAnalysis:
    samples = _decode_pcm(path, cfg.audio.highpass_hz)
    if samples.size == 0:
        return AudioAnalysis(hop=HOP)

    hop_n = int(SAMPLE_RATE * HOP)
    frames = samples.size // hop_n
    if frames == 0:
        return AudioAnalysis(hop=HOP, duration=samples.size / SAMPLE_RATE)

    trimmed = samples[: frames * hop_n].reshape(frames, hop_n)
    rms = np.sqrt(np.mean(np.square(trimmed), axis=1) + 1e-12)
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-6))

    # A 5-frame (100 ms) median kills click transients without smearing plosives.
    smoothed = _median_filter(rms_db, 5)
    duration = samples.size / SAMPLE_RATE

    silences = _find_silences(
        smoothed,
        threshold_db=_adaptive_threshold(smoothed, cfg.audio.silence_threshold_db),
        min_len=cfg.audio.min_silence,
    )
    return AudioAnalysis(hop=HOP, rms_db=[float(v) for v in smoothed],
                         silences=silences, duration=duration)


def _median_filter(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or x.size < k:
        return x
    pad = k // 2
    padded = np.pad(x, (pad, pad), mode="edge")
    strided = np.lib.stride_tricks.sliding_window_view(padded, k)
    return np.median(strided, axis=1)


def _adaptive_threshold(rms_db: np.ndarray, configured: float) -> float:
    """Anchor the silence gate to this recording's own noise floor.

    A fixed -34 dB gate is wrong in both directions: it never triggers on a
    quiet lav-mic recording and swallows soft speech on a loud one.  Sitting
    ~8 dB above the 12th percentile tracks the actual room tone instead.
    """
    floor = float(np.percentile(rms_db, 12))
    speech = float(np.percentile(rms_db, 85))
    if speech - floor < 8.0:            # compressed/normalised audio - trust the config
        return configured
    return float(min(configured, max(floor + 8.0, speech - 26.0)))


def _find_silences(rms_db: np.ndarray, threshold_db: float, min_len: float) -> List[Tuple[float, float]]:
    quiet = rms_db < threshold_db
    silences: List[Tuple[float, float]] = []
    start = None
    for i, is_quiet in enumerate(quiet):
        if is_quiet and start is None:
            start = i
        elif not is_quiet and start is not None:
            if (i - start) * HOP >= min_len:
                silences.append((start * HOP, i * HOP))
            start = None
    if start is not None and (quiet.size - start) * HOP >= min_len:
        silences.append((start * HOP, quiet.size * HOP))
    return silences


def emphasis_times(audio: AudioAnalysis, start: float, end: float,
                   min_gap: float = 3.0, limit: int = 8) -> List[float]:
    """Absolute timestamps where the speaker leans in - good punch-in points."""
    if not audio.rms_db:
        return []
    i0 = max(0, int(start / audio.hop))
    i1 = min(len(audio.rms_db), int(end / audio.hop))
    if i1 - i0 < 10:
        return []
    window = np.array(audio.rms_db[i0:i1])
    baseline = float(np.percentile(window, 55))
    spread = float(np.percentile(window, 95)) - baseline
    if spread < 3.0:
        return []
    threshold = baseline + spread * 0.62

    hits: List[float] = []
    last = -1e9
    for i, value in enumerate(window):
        t = start + i * audio.hop
        if value >= threshold and t - last >= min_gap:
            hits.append(round(t, 3))
            last = t
            if len(hits) >= limit:
                break
    return hits
