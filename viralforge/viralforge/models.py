"""Core data structures shared by every stage of the pipeline.

Everything here is a plain dataclass with ``to_dict``/``from_dict`` so that any
intermediate result can be written to JSON, inspected by hand, edited, and fed
back in.  That is what makes the pipeline resumable: each stage caches its
output next to the media it describes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _asdict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _asdict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_asdict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #


@dataclass
class Word:
    text: str
    start: float
    end: float
    prob: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Word":
        return cls(text=d["text"], start=float(d["start"]), end=float(d["end"]),
                   prob=float(d.get("prob", 1.0)))


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    words: List[Word] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TranscriptSegment":
        return cls(start=float(d["start"]), end=float(d["end"]), text=d["text"],
                   words=[Word.from_dict(w) for w in d.get("words", [])])


@dataclass
class Transcript:
    language: str
    segments: List[TranscriptSegment] = field(default_factory=list)
    source: str = "whisper"          # whisper | youtube-subs | imported

    @property
    def words(self) -> List[Word]:
        out: List[Word] = []
        for seg in self.segments:
            out.extend(seg.words)
        return out

    def text_between(self, start: float, end: float) -> str:
        parts = [w.text for w in self.words if w.start >= start - 0.01 and w.end <= end + 0.01]
        if not parts:  # no word-level data in range - fall back to segments
            parts = [s.text for s in self.segments if s.end > start and s.start < end]
        return " ".join(p.strip() for p in parts if p.strip())

    def words_between(self, start: float, end: float) -> List[Word]:
        return [w for w in self.words if w.end > start and w.start < end]

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Transcript":
        return cls(language=d.get("language", "en"),
                   source=d.get("source", "whisper"),
                   segments=[TranscriptSegment.from_dict(s) for s in d.get("segments", [])])


# --------------------------------------------------------------------------- #
# Source media
# --------------------------------------------------------------------------- #


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class SourceVideo:
    path: str
    video_id: str
    title: str
    url: str
    duration: float
    width: int
    height: int
    fps: float
    uploader: str = ""
    description: str = ""
    chapters: List[Chapter] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 16 / 9

    @property
    def is_vertical(self) -> bool:
        return self.aspect < 1.0

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SourceVideo":
        d = dict(d)
        d["chapters"] = [Chapter(**c) for c in d.get("chapters", [])]
        return cls(**d)


# --------------------------------------------------------------------------- #
# Analysis artefacts
# --------------------------------------------------------------------------- #


@dataclass
class AudioAnalysis:
    """Per-frame loudness plus derived speech structure."""

    hop: float                       # seconds between samples
    rms_db: List[float] = field(default_factory=list)
    silences: List[Tuple[float, float]] = field(default_factory=list)
    duration: float = 0.0

    def level_at(self, t: float) -> float:
        if not self.rms_db:
            return -30.0
        i = int(t / self.hop)
        return self.rms_db[min(max(i, 0), len(self.rms_db) - 1)]

    def peak_percentile(self, start: float, end: float, pct: float = 90.0) -> float:
        import numpy as np
        i0, i1 = int(start / self.hop), max(int(end / self.hop), int(start / self.hop) + 1)
        window = self.rms_db[i0:i1]
        return float(np.percentile(window, pct)) if window else -30.0

    def to_dict(self) -> Dict[str, Any]:
        return {"hop": self.hop, "rms_db": [round(v, 2) for v in self.rms_db],
                "silences": [list(s) for s in self.silences], "duration": self.duration}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AudioAnalysis":
        return cls(hop=d["hop"], rms_db=list(d.get("rms_db", [])),
                   silences=[tuple(s) for s in d.get("silences", [])],
                   duration=d.get("duration", 0.0))


@dataclass
class FaceSample:
    """One tracked subject position, normalised to 0..1 of frame width/height."""

    t: float
    cx: float
    cy: float
    size: float = 0.0                # face width as fraction of frame width
    score: float = 0.0


@dataclass
class VisualAnalysis:
    scene_cuts: List[float] = field(default_factory=list)
    faces: List[FaceSample] = field(default_factory=list)
    method: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VisualAnalysis":
        return cls(scene_cuts=[float(c) for c in d.get("scene_cuts", [])],
                   faces=[FaceSample(**f) for f in d.get("faces", [])],
                   method=d.get("method", "none"))


# --------------------------------------------------------------------------- #
# Clip candidates
# --------------------------------------------------------------------------- #


@dataclass
class ClipScores:
    """Every component is 0..100 so they can be blended and compared."""

    hook: float = 0.0                # does the first 3 seconds stop the scroll
    payoff: float = 0.0              # does it deliver something worth the watch
    standalone: float = 0.0          # understandable with zero context
    emotion: float = 0.0             # surprise / conflict / humour / awe
    shareability: float = 0.0        # would someone send this to a friend
    rewatch: float = 0.0             # loopable / dense enough to rewatch
    trend_fit: float = 0.0           # matches what performs on the target platform
    delivery: float = 0.0            # audio energy & pacing (measured, not judged)
    structure: float = 0.0           # clean boundaries, no mid-sentence starts
    total: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ClipScores":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: float(v) for k, v in d.items() if k in known})


@dataclass
class ClipCandidate:
    start: float
    end: float
    title: str = ""
    hook_line: str = ""              # the on-screen hook card text
    reason: str = ""                 # why the model thinks this travels
    transcript: str = ""
    scores: ClipScores = field(default_factory=ClipScores)
    emphasis: List[float] = field(default_factory=list)   # absolute timestamps to punch in on
    keywords: List[str] = field(default_factory=list)
    risk_notes: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "ClipCandidate", tol: float = 0.0) -> bool:
        return self.start < other.end - tol and other.start < self.end - tol

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ClipCandidate":
        d = dict(d)
        d["scores"] = ClipScores.from_dict(d.get("scores", {}))
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------- #
# Edit plan - the thing the renderer executes
# --------------------------------------------------------------------------- #


@dataclass
class CropWindow:
    """A fixed-size crop rectangle in source pixels, animated on x/y only.

    Keeping w/h constant for the life of a shot is deliberate: changing them
    mid-stream forces ffmpeg to reconfigure the whole filter graph on every
    change, which is ~100x slower.  Zoom is expressed by starting a new shot.
    """

    w: int
    h: int
    keyframes: List[Tuple[float, int, int]] = field(default_factory=list)  # (t_local, x, y)

    def to_dict(self) -> Dict[str, Any]:
        return {"w": self.w, "h": self.h, "keyframes": [list(k) for k in self.keyframes]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CropWindow":
        return cls(w=int(d["w"]), h=int(d["h"]),
                   keyframes=[(float(a), int(b), int(c)) for a, b, c in d.get("keyframes", [])])


@dataclass
class Shot:
    """One kept slice of the source, with its own framing."""

    src_start: float
    src_end: float
    out_start: float                 # where it lands on the output timeline
    crop: Optional[CropWindow] = None
    zoom: float = 1.0
    speed: float = 1.0

    @property
    def src_duration(self) -> float:
        return self.src_end - self.src_start

    @property
    def out_duration(self) -> float:
        return self.src_duration / self.speed

    def to_dict(self) -> Dict[str, Any]:
        d = _asdict(self)
        d["crop"] = self.crop.to_dict() if self.crop else None
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Shot":
        crop = CropWindow.from_dict(d["crop"]) if d.get("crop") else None
        return cls(src_start=d["src_start"], src_end=d["src_end"], out_start=d["out_start"],
                   crop=crop, zoom=d.get("zoom", 1.0), speed=d.get("speed", 1.0))


@dataclass
class CaptionWord:
    """A word placed on the *output* timeline."""

    text: str
    start: float
    end: float
    emphasis: bool = False


@dataclass
class EditPlan:
    clip_index: int
    candidate: ClipCandidate
    shots: List[Shot] = field(default_factory=list)
    caption_words: List[CaptionWord] = field(default_factory=list)
    hook_text: str = ""
    hook_duration: float = 0.0
    out_duration: float = 0.0
    style: str = "impact"
    music: Optional[str] = None
    music_gain_db: float = -22.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clip_index": self.clip_index,
            "candidate": self.candidate.to_dict(),
            "shots": [s.to_dict() for s in self.shots],
            "caption_words": [_asdict(w) for w in self.caption_words],
            "hook_text": self.hook_text,
            "hook_duration": self.hook_duration,
            "out_duration": self.out_duration,
            "style": self.style,
            "music": self.music,
            "music_gain_db": self.music_gain_db,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EditPlan":
        return cls(
            clip_index=d["clip_index"],
            candidate=ClipCandidate.from_dict(d["candidate"]),
            shots=[Shot.from_dict(s) for s in d.get("shots", [])],
            caption_words=[CaptionWord(**w) for w in d.get("caption_words", [])],
            hook_text=d.get("hook_text", ""),
            hook_duration=d.get("hook_duration", 0.0),
            out_duration=d.get("out_duration", 0.0),
            style=d.get("style", "impact"),
            music=d.get("music"),
            music_gain_db=d.get("music_gain_db", -22.0),
        )


# --------------------------------------------------------------------------- #
# Deliverables
# --------------------------------------------------------------------------- #


@dataclass
class PostCopy:
    platform: str
    caption: str
    hashtags: List[str] = field(default_factory=list)
    title: str = ""
    alt_captions: List[str] = field(default_factory=list)
    first_comment: str = ""

    @property
    def full_caption(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.caption}\n\n{tags}".strip()

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PostCopy":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Deliverable:
    index: int
    video_path: str
    thumbnail_path: str
    srt_path: str
    duration: float
    candidate: ClipCandidate
    copy: Dict[str, PostCopy] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "video_path": self.video_path,
            "thumbnail_path": self.thumbnail_path,
            "srt_path": self.srt_path,
            "duration": self.duration,
            "candidate": self.candidate.to_dict(),
            "copy": {k: v.to_dict() for k, v in self.copy.items()},
        }
