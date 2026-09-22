"""The built-in baseline.

No scraping, no API key, no samples - just the shipped starting point, adjusted
for the configured niche and target platforms.  It is a reasonable default and
an honest one: it is labelled ``local-baseline`` everywhere it appears so you
always know the scoring was not grounded in measured data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..profile import DurationBand, PostSample, TrendProfile

DATA = Path(__file__).resolve().parent.parent / "data" / "baseline.json"


def _load() -> Dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def match_niche(niche: str, niches: Dict[str, Any]) -> str:
    """Map free text ('business podcast', 'lifting') onto a baseline bucket."""
    text = (niche or "").lower()
    if not text:
        return ""
    aliases = {
        "business": ["business", "entrepreneur", "startup", "money", "finance", "marketing",
                     "sales", "founder", "saas", "ecommerce"],
        "fitness": ["fitness", "gym", "lifting", "workout", "health", "nutrition", "bodybuild",
                    "running", "yoga"],
        "podcast": ["podcast", "interview", "conversation", "talk show", "guest"],
        "education": ["education", "explain", "science", "history", "teach", "learn", "tutorial",
                      "coding", "math"],
        "gaming": ["gaming", "game", "stream", "esports", "fps", "minecraft"],
        "comedy": ["comedy", "funny", "humor", "humour", "sketch", "standup", "stand-up"],
    }
    best, best_hits = "", 0
    for bucket, keys in aliases.items():
        if bucket not in niches:
            continue
        hits = sum(1 for k in keys if k in text)
        if hits > best_hits:
            best, best_hits = bucket, hits
    return best


class LocalProvider:
    name = "local"

    def collect(self, niche: str, platforms: List[str], limit: int) -> List[PostSample]:
        return []      # the baseline is a profile, not a set of observations

    def profile(self, niche: str, platforms: List[str]) -> TrendProfile:
        return build_baseline_profile(niche, platforms)


def build_baseline_profile(niche: str, platforms: List[str]) -> TrendProfile:
    data = _load()
    general = data["general"]
    bucket = match_niche(niche, data.get("niches", {}))
    tuned = data.get("niches", {}).get(bucket, {}) if bucket else {}

    bands = [DurationBand(**b) for b in (tuned.get("duration_bands") or general["duration_bands"])]
    hooks = list(tuned.get("hook_patterns") or []) + list(general["hook_patterns"])
    hashtags = list(tuned.get("hashtag_pool") or [])

    caption_chars = DurationBand(**general["caption_chars"])
    hashtag_count = DurationBand(**general["hashtag_count"])
    notes: List[str] = [data.get("note", "")]
    for platform in platforms:
        override = data.get("platform_overrides", {}).get(platform, {})
        if "caption_chars" in override:
            caption_chars = DurationBand(**override["caption_chars"])
        if "hashtag_count" in override:
            hashtag_count = DurationBand(**override["hashtag_count"])
        notes.extend(override.get("notes", []))

    return TrendProfile(
        platforms=list(platforms),
        niche=niche,
        source=f"local-baseline v{data.get('version', '?')}" + (f" ({bucket})" if bucket else ""),
        sample_size=0,
        duration_bands=bands,
        hook_patterns=hooks[:14],
        hook_window=float(general.get("hook_window", 3.0)),
        caption_chars=caption_chars,
        hashtag_pool=hashtags,
        hashtag_count=hashtag_count,
        cuts_per_minute=DurationBand(**general["cuts_per_minute"]),
        words_per_minute=DurationBand(**general["words_per_minute"]),
        rubric=general["rubric"],
        caption_guidance=general["caption_guidance"],
        avoid=list(general.get("avoid", [])),
        notes=[n for n in notes if n],
    ).stamp()
