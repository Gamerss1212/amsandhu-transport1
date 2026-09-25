"""Shared evidence: the facts about one video that every agent may read.

Evidence is what anyone would see or hear (the words, their timing, loudness, laughter, scene cuts, who is
speaking, where topics change) - never anyone's opinion. It is computed once per video so 150 agents do not
redo the same measurements, and it is read-only for everyone except the mappers that fill it in.

Per sentence it keeps word-pattern counts as prefix sums, so any window's totals cost two lookups: a scout
can score thousands of candidate windows in its section in milliseconds.
"""
from __future__ import annotations

import math
import re
import threading
from pathlib import Path

import numpy as np

from ..analysis import local_judge as lj
from . import lexicon as lx

FILLERS = lj.FILLERS


def _prefix(a: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(a, dtype=float)])


class Evidence:
    def __init__(self, meta: dict, transcript: dict, signals: dict, profile: dict | None, cfg,
                 video: Path | None, wav: Path | None) -> None:
        a = cfg["analysis"]
        self.meta, self.signals, self.profile, self.cfg = meta, signals, profile, cfg
        self.video = video if video and not meta.get("audio_only") else None
        self.wav = wav
        self.words = transcript.get("words") or []
        self.segments = transcript.get("segments") or []
        self.source = transcript.get("source", "")
        self.min_s, self.max_s = float(a["min_clip_seconds"]), float(a["max_clip_seconds"])
        self.duration = float(meta.get("duration") or (self.words[-1]["e"] if self.words else 0.0))
        self.sents = lj.build_segments(self.segments, profile)
        n = self.n = len(self.sents)
        self.S = np.array([s.s for s in self.sents], dtype=float)
        self.E = np.array([s.e for s in self.sents], dtype=float)
        self.text = [s.text for s in self.sents]
        self.low = [t.lower() for t in self.text]
        self.nw = np.array([max(1, len(s.words)) for s in self.sents], dtype=float)
        self.hook = np.array([s.hook for s in self.sents], dtype=float)
        self.start_ok = np.array([not s.start_flaw and bool(s.words) for s in self.sents], dtype=bool)
        self.start_flaw = [s.start_flaw for s in self.sents]
        self.ends_open = np.array([s.ends_open for s in self.sents], dtype=bool)
        self.clean_break = np.array([s.clean_break for s in self.sents], dtype=bool)
        self.is_q = np.array([s.question for s in self.sents], dtype=bool)
        self.blunt = np.array([s.blunt for s in self.sents], dtype=bool)
        self.first = [s.words[0] if s.words else "" for s in self.sents]
        self.ctx_start = np.array([f in lx.PRONOUN_START for f in self.first], dtype=bool)
        self.new_speaker = np.array([seg["text"].lstrip().startswith(">>") for seg in self.segments[:n]], dtype=bool)
        self.gap_after = np.r_[self.S[1:] - self.E[:-1], 99.0] if n else np.zeros(0)

        # word-pattern counts per sentence, and prefix sums for O(1) window totals
        self.count: dict[str, np.ndarray] = {}
        for name in lx.PATTERNS:
            self.count[name] = np.array([len(lx.PATTERNS[name].findall(t)) for t in self.text], dtype=float)
        self.count["filler"] = np.array([sum(w in FILLERS for w in s.words) for s in self.sents], dtype=float)
        self.count["short_line"] = np.array([5 <= len(s.words) <= 16 for s in self.sents], dtype=float)
        self.count["deadair"] = (self.gap_after > 1.2).astype(float)
        self.count["blunt"] = self.blunt.astype(float)
        weights = (profile or {}).get("term_weights", {})
        viral = set((profile or {}).get("top_viral_terms", []))
        self.count["trend_terms"] = np.array(
            [sum(weights.get(w, 0.0) for w in set(s.words)) + 0.3 * len(viral & set(s.words)) for s in self.sents])
        self.count["jargon"] = np.array([sum(len(w) >= 11 and w not in lx.STOP for w in s.words) for s in self.sents],
                                        dtype=float)
        self.P = {k: _prefix(v) for k, v in self.count.items()}
        self.P["nw"] = _prefix(self.nw)

        # per-second audience / audio curves (percentiles 0-1) as prefix sums
        d = int(math.ceil(self.duration)) + 2
        self.curves: dict[str, np.ndarray | None] = {}
        for key in ("energy", "heatmap", "comments", "reaction"):
            pct = (signals.get("pct") or {}).get(key)
            self.curves[key] = None if pct is None else np.pad(np.asarray(pct, dtype=float), (0, max(0, d - len(pct))))
        self.Pc = {k: _prefix(v) for k, v in self.curves.items() if v is not None}
        raw = (signals.get("raw") or {}).get("reaction")
        self.reaction_raw = None if raw is None else np.pad(np.asarray(raw, dtype=float), (0, max(0, d - len(raw))))
        if self.reaction_raw is not None:  # seconds with a clear laugh / applause burst
            from ..analysis.signals import LAUGH_STRONG

            self.curves["laughs"] = (self.reaction_raw >= LAUGH_STRONG).astype(float)
            self.Pc["laughs"] = _prefix(self.curves["laughs"])
        self.comedy = float(signals.get("comedy") or 0.0)
        # filled in by the mappers
        self.scenes: list[float] | None = None
        self.scan_speed: float | None = None
        self.speaker = np.full(n, -1, dtype=int)
        self.speakers_found = 0
        self.topic = np.zeros(n, dtype=int)
        self.topics: list[dict] = []
        self.zones: list[dict] = []          # intro / outro / sponsor read / show break
        self.chapters: list[dict] = [{"start": float(c.get("start_time", 0)), "title": c.get("title", "")}
                                     for c in meta.get("chapters") or []]
        self.names: dict[str, list[float]] = {}
        self._frame_lock = threading.Lock()
        self._frames: dict[float, object] = {}
        self._faces: dict[float, list] = {}
        self._detector = None

    # ---------------------------------------------------------------- window helpers (vectorized)
    def wsum(self, name: str, I, J) -> np.ndarray:
        P = self.P[name]
        return P[np.asarray(J) + 1] - P[np.asarray(I)]

    def csum(self, key: str, a, b) -> np.ndarray | None:
        """Mean percentile of a per-second curve over [a, b) seconds (arrays allowed)."""
        P = self.Pc.get(key)
        if P is None:
            return None
        a = np.clip(np.floor(np.asarray(a, dtype=float)).astype(int), 0, len(P) - 2)
        b = np.clip(np.ceil(np.asarray(b, dtype=float)).astype(int), 1, len(P) - 1)
        b = np.maximum(b, a + 1)
        return (P[b] - P[a]) / (b - a)

    def cmax(self, key: str, a: float, b: float) -> float | None:
        c = self.curves.get(key)
        if c is None:
            return None
        s, e = int(max(0, a)), int(min(len(c), math.ceil(b)))
        return float(c[s:e].max()) if e > s else 0.0

    def span_text(self, i: int, j: int) -> str:
        return " ".join(self.text[i:j + 1])

    def idx_at(self, t: float) -> int:
        """Index of the sentence playing at time t (or the next one)."""
        k = int(np.searchsorted(self.E, t, side="left"))
        return min(max(0, k), max(0, self.n - 1))

    def grid(self, i0: int, i1: int) -> tuple[np.ndarray, np.ndarray]:
        """Every candidate clip that starts in sentences [i0, i1): each clean sentence start, ending at every
        finished thought between the minimum and maximum clip length (and near seven target lengths).
        Each window is owned by exactly one section (the one its first sentence starts in)."""
        I, J = [], []
        lo, hi = self.min_s, self.max_s
        # seven lengths from shortest to longest, so a complete story of any length has a window that fits it
        targets = tuple(lo + (hi - lo) * k / 6 for k in range(7))
        for i in range(i0, min(i1, self.n)):
            if not self.start_ok[i]:
                continue
            s = self.S[i]
            jmin = int(np.searchsorted(self.E, s + lo, side="left"))
            jmax = int(np.searchsorted(self.E, s + hi, side="right")) - 1
            jmin = max(jmin, i)
            if jmax < jmin:
                continue
            # every sentence that finishes a thought is a possible ending (capped for very dense transcripts),
            # plus the nearest ending to each target length
            closed = [j for j in range(jmin, jmax + 1) if not self.ends_open[j]]
            if len(closed) > 24:
                closed = closed[:: math.ceil(len(closed) / 24)]
            picks = set(closed)
            for t in targets:
                j = int(np.clip(np.searchsorted(self.E, s + t), jmin, jmax))
                if j > jmin and abs(self.E[j - 1] - s - t) < abs(self.E[j] - s - t):
                    j -= 1
                if self.ends_open[j]:  # prefer a neighbour that ends on a finished thought
                    for alt in (j + 1, j - 1):
                        if jmin <= alt <= jmax and not self.ends_open[alt]:
                            j = alt
                            break
                picks.add(j)
            for j in sorted(picks):
                I.append(i)
                J.append(j)
        return np.array(I, dtype=int), np.array(J, dtype=int)

    # ---------------------------------------------------------------- frames (shared footage)
    def frame(self, t: float, width: int = 320):
        """A small frame of the source at time t (cached: the footage is shared, opinions are not)."""
        if self.video is None:
            return None
        key = round(t, 1)
        with self._frame_lock:
            if key in self._frames:
                return self._frames[key]
        try:
            import cv2

            cap = cv2.VideoCapture(str(self.video))
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok, img = cap.read()
            cap.release()
            if not ok or img is None:
                img = None
            else:
                h, w = img.shape[:2]
                img = cv2.resize(img, (width, max(2, int(h * width / w))))
        except Exception:
            img = None
        with self._frame_lock:
            self._frames[key] = img
        return img

    def faces(self, t: float) -> list | None:
        """Faces in the frame at t as (cx, cy, w) fractions of the picture."""
        img = self.frame(t)
        if img is None:
            return None
        key = round(t, 1)
        with self._frame_lock:
            if key in self._faces:
                return self._faces[key]
        try:
            from ..editing.reframe import _make_detector

            h, w = img.shape[:2]
            with self._frame_lock:
                if self._detector is None or self._detector[0] != (w, h):
                    self._detector = ((w, h), _make_detector(w, h))
                detect = self._detector[1]
                found = [((x + fw / 2) / w, (y + fh / 2) / h, fw / w) for x, y, fw, fh in detect(img)]
        except Exception:
            found = []
        with self._frame_lock:
            self._faces[key] = found
        return found

    def speaker_at(self, i: int) -> str:
        k = int(self.speaker[i]) if 0 <= i < self.n else -1
        return chr(ord("A") + k) if k >= 0 else ""


def words_between(ev: Evidence, a: float, b: float) -> list[dict]:
    return [w for w in ev.words if w["s"] >= a - 0.05 and w["e"] <= b + 0.05]


def norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())
