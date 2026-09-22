"""The TrendProfile: what "works" on the target platforms right now.

This object is the bridge between the trend research stage and the two stages
that consume it - clip scoring and caption writing.  It is deliberately a plain
serialisable record so you can hand-edit one, commit it, and diff it over time.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DurationBand:
    low: float
    high: float
    label: str = ""

    def contains(self, seconds: float) -> bool:
        return self.low <= seconds <= self.high

    def distance(self, seconds: float) -> float:
        if self.contains(seconds):
            return 0.0
        return self.low - seconds if seconds < self.low else seconds - self.high


@dataclass
class TrendProfile:
    platforms: List[str] = field(default_factory=lambda: ["tiktok", "instagram"])
    niche: str = ""
    source: str = "local-baseline"
    generated_at: str = ""
    sample_size: int = 0

    # What the winners look like
    duration_bands: List[DurationBand] = field(default_factory=list)
    hook_patterns: List[str] = field(default_factory=list)
    hook_window: float = 3.0            # seconds you have to land the hook
    caption_chars: DurationBand = field(default_factory=lambda: DurationBand(40, 150, "chars"))
    hashtag_pool: List[str] = field(default_factory=list)
    hashtag_count: DurationBand = field(default_factory=lambda: DurationBand(3, 6, "tags"))
    cuts_per_minute: DurationBand = field(default_factory=lambda: DurationBand(14, 40, "cuts/min"))
    words_per_minute: DurationBand = field(default_factory=lambda: DurationBand(150, 215, "wpm"))

    # Free-text guidance handed to the model
    rubric: str = ""
    caption_guidance: str = ""
    avoid: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    # Raw evidence, kept so the profile can be re-derived or audited
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    def duration_fit(self, seconds: float) -> float:
        """0..100 - how close this length is to what performs."""
        if not self.duration_bands:
            return 70.0
        nearest = min(b.distance(seconds) for b in self.duration_bands)
        if nearest <= 0:
            return 100.0
        return max(0.0, 100.0 - nearest * 3.2)

    def summary(self) -> str:
        bands = ", ".join(f"{b.low:.0f}-{b.high:.0f}s" for b in self.duration_bands) or "n/a"
        return (f"{'/'.join(self.platforms)} · {self.niche or 'general'} · "
                f"source={self.source} · n={self.sample_size} · sweet spots {bands}")

    def prompt_block(self) -> str:
        """Compact, token-cheap rendering for the scoring / copy prompts."""
        lines = [
            f"PLATFORMS: {', '.join(self.platforms)}",
            f"NICHE: {self.niche or 'general short-form'}",
            f"EVIDENCE: {self.source}, {self.sample_size} posts"
            if self.sample_size else f"EVIDENCE: {self.source}",
            "DURATION SWEET SPOTS: " + (
                ", ".join(f"{b.low:.0f}-{b.high:.0f}s" for b in self.duration_bands) or "20-45s"),
            f"HOOK WINDOW: first {self.hook_window:.0f}s decide retention",
            f"PACING: {self.cuts_per_minute.low:.0f}-{self.cuts_per_minute.high:.0f} cuts/min, "
            f"{self.words_per_minute.low:.0f}-{self.words_per_minute.high:.0f} words/min",
        ]
        if self.hook_patterns:
            lines.append("HOOK PATTERNS THAT PERFORM:")
            lines += [f"  - {h}" for h in self.hook_patterns[:12]]
        if self.rubric:
            lines.append("WHAT TRAVELS HERE:\n" + self.rubric.strip())
        if self.avoid:
            lines.append("WHAT DIES HERE:")
            lines += [f"  - {a}" for a in self.avoid[:10]]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrendProfile":
        d = dict(d)
        d["duration_bands"] = [DurationBand(**b) for b in d.get("duration_bands", [])]
        for key in ("caption_chars", "hashtag_count", "cuts_per_minute", "words_per_minute"):
            if isinstance(d.get(key), dict):
                d[key] = DurationBand(**d[key])
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "TrendProfile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def stamp(self) -> "TrendProfile":
        self.generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self


@dataclass
class PostSample:
    """One observed short-form post, however it was collected."""

    platform: str
    url: str = ""
    caption: str = ""
    hashtags: List[str] = field(default_factory=list)
    duration: float = 0.0
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    followers: int = 0
    posted_at: str = ""
    author: str = ""

    @property
    def engagement_rate(self) -> float:
        if self.views <= 0:
            return 0.0
        return (self.likes + self.comments * 3 + self.shares * 5 + self.saves * 4) / self.views

    @property
    def velocity(self) -> float:
        """Views normalised by follower count - separates the format from the audience."""
        if self.followers > 0:
            return self.views / max(self.followers, 1)
        return float(self.views)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PostSample":
        known = {f.name for f in dataclasses.fields(cls)}
        clean: Dict[str, Any] = {}
        for k, v in d.items():
            if k not in known:
                continue
            if k in ("views", "likes", "comments", "shares", "saves", "followers"):
                try:
                    clean[k] = int(float(v or 0))
                except (TypeError, ValueError):
                    clean[k] = 0
            elif k == "duration":
                try:
                    clean[k] = float(v or 0.0)
                except (TypeError, ValueError):
                    clean[k] = 0.0
            elif k == "hashtags":
                clean[k] = [str(t).lstrip("#") for t in (v or [])]
            else:
                clean[k] = str(v or "")
        clean.setdefault("platform", "unknown")
        return cls(**clean)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)
