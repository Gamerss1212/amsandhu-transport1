"""Build the EditPlan: what to keep, how to frame it, where the words land.

Order matters here.  Dead air is removed first, because that changes the output
timeline; shots are cut from what survives; framing is computed per shot; and
only then are word timings mapped onto the new timeline.  Doing captions before
the cuts is the classic way to end up with subtitles that drift.
"""

from __future__ import annotations

import bisect
from typing import List, Optional, Sequence, Tuple

from ..config import Config
from ..models import (
    AudioAnalysis, CaptionWord, ClipCandidate, CropWindow, EditPlan, Shot,
    SourceVideo, Transcript, VisualAnalysis, Word,
)
from .reframe import build_crop_window


def build_plan(index: int, candidate: ClipCandidate, source: SourceVideo,
               transcript: Transcript, audio: Optional[AudioAnalysis],
               visual: Optional[VisualAnalysis], cfg: Config) -> EditPlan:
    kept = _kept_ranges(candidate, audio, cfg)
    if not kept:
        kept = [(candidate.start, candidate.end)]

    cut_points = _shot_boundaries(candidate, visual, cfg)
    shots = _build_shots(kept, cut_points, cfg)
    _assign_zoom(shots, candidate, cfg)

    faces = visual.faces if visual else []
    for shot in shots:
        shot.crop = build_crop_window(source, shot.src_start, shot.src_end, faces, cfg,
                                      zoom=shot.zoom, speed=shot.speed)

    out_duration = sum(s.out_duration for s in shots)
    caption_words = _map_words(transcript, candidate, shots, out_duration)

    hook_text = candidate.hook_line or candidate.title
    hook_duration = min(cfg.render.hook_duration, max(0.0, out_duration - 0.5)) \
        if (cfg.render.hook_card and hook_text) else 0.0

    return EditPlan(
        clip_index=index,
        candidate=candidate,
        shots=shots,
        caption_words=caption_words,
        hook_text=hook_text,
        hook_duration=hook_duration,
        out_duration=round(out_duration, 3),
        style=cfg.captions.style,
        music=cfg.audio.music_path,
        music_gain_db=cfg.audio.music_gain_db,
    )


# --------------------------------------------------------------------------- #
# Dead air
# --------------------------------------------------------------------------- #


def _kept_ranges(candidate: ClipCandidate, audio: Optional[AudioAnalysis],
                 cfg: Config) -> List[Tuple[float, float]]:
    """The clip minus its long silences, with a little air left around each cut."""
    start, end = candidate.start, candidate.end
    if not cfg.audio.de_silence or audio is None or not audio.silences:
        return [(start, end)]

    pad = max(0.0, cfg.audio.keep_padding)
    removals: List[Tuple[float, float]] = []
    for s_start, s_end in audio.silences:
        a, b = max(s_start + pad, start), min(s_end - pad, end)
        if b - a >= max(0.12, cfg.audio.min_silence - 2 * pad):
            removals.append((a, b))

    kept: List[Tuple[float, float]] = []
    cursor = start
    for a, b in sorted(removals):
        if a > cursor + 0.25:            # a sliver of video is worse than the pause
            kept.append((cursor, a))
        cursor = max(cursor, b)
    if end > cursor + 0.25:
        kept.append((cursor, end))

    if not kept:
        return [(start, end)]
    # Removing more than a third of a clip means the silence gate is wrong for
    # this audio, not that the clip is two-thirds dead air.
    if sum(b - a for a, b in kept) < (end - start) * 0.62:
        return [(start, end)]
    return kept


# --------------------------------------------------------------------------- #
# Shots
# --------------------------------------------------------------------------- #


def _shot_boundaries(candidate: ClipCandidate, visual: Optional[VisualAnalysis],
                     cfg: Config) -> List[float]:
    """Source timestamps where the framing should be allowed to jump."""
    points: List[float] = []
    if visual:
        points += [c for c in visual.scene_cuts if candidate.start < c < candidate.end]

    if cfg.reframe.punch_in and candidate.emphasis:
        last = -1e9
        added = 0
        for t in candidate.emphasis:
            if not (candidate.start + 1.5 < t < candidate.end - 1.5):
                continue
            if t - last < cfg.reframe.punch_in_min_gap:
                continue
            points.append(t)
            end = t + cfg.reframe.punch_in_duration
            if end < candidate.end - 1.0:
                points.append(end)
            last = t
            added += 1
            if added >= cfg.reframe.punch_in_max_per_clip:
                break
    return sorted(set(round(p, 3) for p in points))


def _build_shots(kept: Sequence[Tuple[float, float]], cut_points: Sequence[float],
                 cfg: Config) -> List[Shot]:
    shots: List[Shot] = []
    out_cursor = 0.0
    min_shot = 0.55                  # anything shorter reads as a glitch, not a cut

    for src_start, src_end in kept:
        inner = [c for c in cut_points if src_start + min_shot < c < src_end - min_shot]
        bounds = [src_start, *inner, src_end]
        for a, b in zip(bounds, bounds[1:]):
            if b - a < 0.08:
                continue
            shot = Shot(src_start=round(a, 3), src_end=round(b, 3),
                        out_start=round(out_cursor, 3))
            shots.append(shot)
            out_cursor += shot.out_duration
    return shots


def _assign_zoom(shots: List[Shot], candidate: ClipCandidate, cfg: Config) -> None:
    """Alternate between the base framing and a tighter one on emphasis beats."""
    if not cfg.reframe.punch_in or not candidate.emphasis:
        return
    beats = sorted(candidate.emphasis)
    for shot in shots:
        idx = bisect.bisect_right(beats, shot.src_start + 0.05) - 1
        if idx < 0:
            continue
        beat = beats[idx]
        if shot.src_start + 0.05 >= beat and shot.src_start < beat + cfg.reframe.punch_in_duration:
            shot.zoom = cfg.reframe.punch_in_zoom


# --------------------------------------------------------------------------- #
# Timeline mapping
# --------------------------------------------------------------------------- #


class TimelineMap:
    """Maps a source timestamp onto the output timeline after cuts."""

    def __init__(self, shots: Sequence[Shot]):
        self.shots = list(shots)
        self._starts = [s.src_start for s in self.shots]

    def to_out(self, src_t: float) -> Optional[float]:
        if not self.shots:
            return None
        i = bisect.bisect_right(self._starts, src_t) - 1
        if i < 0:
            return self.shots[0].out_start
        shot = self.shots[i]
        if src_t <= shot.src_end:
            return shot.out_start + (src_t - shot.src_start) / max(shot.speed, 0.01)
        # Landed in a removed gap - pin to the seam so words never overlap a cut.
        return shot.out_start + shot.out_duration


def _map_words(transcript: Transcript, candidate: ClipCandidate, shots: Sequence[Shot],
               out_duration: float) -> List[CaptionWord]:
    mapping = TimelineMap(shots)
    emphasis = set(round(t, 1) for t in candidate.emphasis)
    out: List[CaptionWord] = []

    for word in transcript.words_between(candidate.start, candidate.end):
        text = word.text.strip()
        if not text:
            continue
        start = mapping.to_out(max(word.start, candidate.start))
        end = mapping.to_out(min(word.end, candidate.end))
        if start is None or end is None:
            continue
        start = max(0.0, min(start, out_duration))
        end = max(start + 0.06, min(end, out_duration))
        if start >= out_duration - 0.02:
            continue
        out.append(CaptionWord(
            text=text, start=round(start, 3), end=round(end, 3),
            emphasis=round(word.start, 1) in emphasis))

    out.sort(key=lambda w: w.start)
    # Words that fell into the same seam share a timestamp; nudge them apart so
    # the highlight still steps through them.
    for i in range(1, len(out)):
        if out[i].start < out[i - 1].start + 0.04:
            out[i].start = min(out_duration - 0.02, out[i - 1].start + 0.04)
            out[i].end = max(out[i].end, out[i].start + 0.06)
    return out
