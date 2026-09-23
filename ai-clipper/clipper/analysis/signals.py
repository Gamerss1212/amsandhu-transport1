"""Per-second "interest" curves from sources other than the words themselves.

  * heatmap  - YouTube's "most replayed" graph (real audience re-watch data)
  * comments - timestamps viewers mention in comments ("23:14 had me dying"), like-weighted
  * energy   - loudness spikes (laughter, shouting, applause, heated moments)
  * reaction - loud moments while nobody is saying words: laughter, applause, gasps
               (Whisper does not write these down, so they are found in the audio)
  * pace     - speech rate (words per second)
"""
from __future__ import annotations

import math
import re

import numpy as np

from ..media import parse_ts, read_wav
from ..trends.analyzer import pct_rank

_TS = re.compile(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])")


def heatmap_curve(info: dict, duration: int) -> np.ndarray | None:
    points = info.get("heatmap") or []
    if not points:
        return None
    curve = np.zeros(duration)
    for p in points:
        s, e = int(p["start_time"]), max(int(p["start_time"]) + 1, int(math.ceil(p["end_time"])))
        curve[s:min(e, duration)] = p["value"]
    return curve


def comment_curve(comments: list[dict], duration: int, spread: float = 8.0) -> np.ndarray | None:
    curve = np.zeros(duration)
    hits = 0
    for c in comments:
        weight = 1.0 + math.log1p(c.get("likes", 0))
        for m in _TS.findall(c.get("text", "")):
            t = parse_ts(m)
            if t is None or t >= duration:
                continue
            hits += 1
            lo, hi = int(max(0, t - 3 * spread)), int(min(duration, t + 3 * spread))
            xs = np.arange(lo, hi)
            curve[lo:hi] += weight * np.exp(-0.5 * ((xs - t) / spread) ** 2)
    return curve if hits >= 3 else None


def energy_curve(wav_path, duration: int) -> np.ndarray | None:
    try:
        audio, sr = read_wav(wav_path)
    except Exception:
        return None
    n = min(duration, len(audio) // sr)
    if n <= 0:
        return None
    frames = audio[: n * sr].reshape(n, sr)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-10)
    db = 20 * np.log10(rms)
    # loudness relative to the local 2-minute context, so quiet and loud shows compare fairly
    k = 121
    pad = np.pad(db, (k // 2, k // 2), mode="edge")
    local = np.convolve(pad, np.ones(k) / k, mode="valid")[:n]
    out = np.zeros(duration)
    out[:n] = np.clip(db - local, 0, None)
    return out


def reaction_curve(wav_path, words: list[dict], duration: int, hop: float = 0.1) -> np.ndarray | None:
    try:
        audio, sr = read_wav(wav_path)
    except Exception:
        return None
    step = int(sr * hop)
    n = len(audio) // step
    if n < 50:
        return None
    frames = audio[: n * step].reshape(n, step)
    db = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1) + 1e-10))
    # speech level = typical loudness while words are being spoken
    speaking = np.zeros(n, dtype=bool)
    for w in words:
        a, b = int((w["s"] - 0.15) / hop), int((w["e"] + 0.15) / hop) + 1
        speaking[max(0, a):min(n, b)] = True
    if speaking.sum() < 50 or (~speaking).sum() < 10:
        return None
    speech_db = float(np.median(db[speaking]))
    # a reaction is a gap between words that is nearly as loud as (or louder than) the speech
    excess = np.clip(db - (speech_db - 6.0), 0, None) * ~speaking
    # laughter and applause come in bursts; a long loud stretch without words is music (intros, breaks)
    loud = excess > 0
    edges = np.flatnonzero(np.diff(np.r_[0, loud.astype(np.int8), 0]))
    for a, b in zip(edges[::2], edges[1::2]):
        if (b - a) * hop > 8.0:
            excess[a:b] = 0
    per_s = int(round(1 / hop))
    secs = min(duration, n // per_s)
    curve = np.zeros(duration)
    curve[:secs] = excess[: secs * per_s].reshape(secs, per_s).sum(axis=1)
    return curve if np.any(curve) else None


def pace_curve(words: list[dict], duration: int) -> np.ndarray:
    curve = np.zeros(duration)
    for w in words:
        i = int(w["s"])
        if 0 <= i < duration:
            curve[i] += 1
    k = 5
    return np.convolve(curve, np.ones(k) / k, mode="same")


def to_percentiles(curve: np.ndarray | None) -> np.ndarray | None:
    """Each second's percentile rank (0-1) within the whole video."""
    if curve is None or not np.any(curve):
        return None
    return pct_rank(curve)


def window_score(pct: np.ndarray | None, start: float, end: float) -> float | None:
    """0-100: blend of the window's average and peak percentile."""
    if pct is None:
        return None
    s, e = int(max(0, start)), int(min(len(pct), math.ceil(end)))
    if e <= s:
        return None
    seg = pct[s:e]
    return round(100 * (0.6 * float(np.mean(seg)) + 0.4 * float(np.max(seg))), 1)


def peaks(curve: np.ndarray | None, n: int = 8, min_gap: int = 60) -> list[int]:
    """Top-n seconds of a curve, at least `min_gap` apart."""
    if curve is None:
        return []
    chosen: list[int] = []
    for i in np.argsort(curve)[::-1]:
        if curve[i] <= 0:
            break
        if all(abs(int(i) - c) >= min_gap for c in chosen):
            chosen.append(int(i))
        if len(chosen) >= n:
            break
    return sorted(chosen)


def compute_signals(info: dict, transcript: dict, wav_path, comments: list[dict], duration: float) -> dict:
    d = int(math.ceil(duration)) + 1
    raw = {
        "heatmap": heatmap_curve(info, d),
        "comments": comment_curve(comments, d),
        "energy": energy_curve(wav_path, d) if wav_path else None,
        "reaction": reaction_curve(wav_path, transcript["words"], d) if wav_path else None,
        "pace": pace_curve(transcript["words"], d),
    }
    return {"raw": raw, "pct": {k: to_percentiles(v) for k, v in raw.items()},
            "comedy": comedy_factor(raw["reaction"], duration)}


LAUGH_STRONG = 40.0  # reaction strength of a clear laugh (dB above speech level, summed per second)


def comedy_factor(reaction: np.ndarray | None, duration: float) -> float:
    """0-1: how much this video runs on laughs. A comedy show has well over one clear laugh a minute;
    an interview or report has the odd chuckle, applause or background sound."""
    if reaction is None or duration <= 0:
        return 0.0
    events, last = 0, -99
    for t in np.flatnonzero(reaction >= LAUGH_STRONG):
        if t - last >= 3:
            events += 1
        last = t
    rate = events / (duration / 60)
    return float(np.clip((rate - 0.6) / 0.8, 0.0, 1.0))
