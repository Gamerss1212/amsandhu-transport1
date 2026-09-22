"""Turn observed posts into a TrendProfile.

Two halves, and the split is deliberate:

* The numbers - duration bands, caption length, hashtag pools, posting cadence
  - come from arithmetic on the samples.  No model can improve on counting.
* The judgement - *why* the winners won, what the hooks have in common, what to
  avoid - comes from Claude reading the top and bottom deciles side by side.

If there is no API key, the arithmetic half still runs and the qualitative half
falls back to the shipped baseline.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..analyze.llm import LLMClient, LLMRefused, LLMUnavailable
from ..utils import info, warn
from .profile import DurationBand, PostSample, TrendProfile
from .providers.local import build_baseline_profile

RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "rubric": {"type": "string"},
        "hook_patterns": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "caption_guidance": {"type": "string"},
        "avoid": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "notes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["rubric", "hook_patterns", "caption_guidance", "avoid", "notes"],
    "additionalProperties": False,
}

RUBRIC_SYSTEM = """\
You analyse short-form video performance data and write the rubric another \
system will use to pick clips out of long-form video.

You will be given two sets of real posts from the same niche: the top performers \
and the bottom performers, each with its caption, hashtags, duration and \
engagement numbers. Views are normalised against follower count where known, so \
compare formats, not audience sizes.

Find what actually separates the two sets. Be concrete and falsifiable. \
"Be engaging" is useless; "the top posts state a number in the first sentence, \
the bottom ones state it at 0:12" is useful. If the data does not support a \
claim, do not make it - say the sample is thin instead.

Return JSON only:
- rubric: 120-200 words on what travels in this niche and why, written as \
instructions to a clip picker.
- hook_patterns: up to 12 concrete opening patterns observed in the winners.
- caption_guidance: 50-90 words on how the winning captions are written.
- avoid: specific things the losing posts did.
- notes: anything about the data itself worth recording (sample size, skew, \
date range, a caveat)."""


def build_profile(samples: Sequence[PostSample], niche: str, platforms: List[str],
                  source: str, llm: Optional[LLMClient] = None) -> TrendProfile:
    """Derive a profile from samples, falling back to the baseline as needed."""
    baseline = build_baseline_profile(niche, platforms)
    usable = [s for s in samples if s.views > 0]
    if len(usable) < 8:
        if samples:
            warn(f"Only {len(usable)} usable samples - keeping the baseline profile. "
                 "Trend-fit scoring will not be grounded in this data.")
        return baseline

    top, bottom = _split_performers(usable)
    profile = TrendProfile(
        platforms=platforms,
        niche=niche,
        source=source,
        sample_size=len(usable),
        duration_bands=_duration_bands(top) or baseline.duration_bands,
        hook_window=baseline.hook_window,
        caption_chars=_caption_band(top) or baseline.caption_chars,
        hashtag_pool=_hashtag_pool(top),
        hashtag_count=_hashtag_band(top) or baseline.hashtag_count,
        cuts_per_minute=baseline.cuts_per_minute,      # not observable from metadata
        words_per_minute=baseline.words_per_minute,
        rubric=baseline.rubric,
        caption_guidance=baseline.caption_guidance,
        avoid=list(baseline.avoid),
        notes=[],
        evidence=[_evidence(s) for s in top[:25]],
    )

    if llm is not None:
        try:
            qualitative = _llm_rubric(llm, top, bottom, niche, platforms)
            profile.rubric = qualitative.get("rubric") or profile.rubric
            hooks = [h for h in qualitative.get("hook_patterns", []) if h.strip()]
            profile.hook_patterns = hooks or baseline.hook_patterns
            profile.caption_guidance = (qualitative.get("caption_guidance")
                                        or profile.caption_guidance)
            profile.avoid = [a for a in qualitative.get("avoid", []) if a.strip()] or profile.avoid
            profile.notes = [n for n in qualitative.get("notes", []) if n.strip()]
        except (LLMUnavailable, LLMRefused) as exc:
            warn(f"Could not derive a rubric from the samples ({exc}). Using the baseline rubric.")
            profile.hook_patterns = baseline.hook_patterns
        except Exception as exc:
            warn(f"Rubric generation failed ({type(exc).__name__}: {exc}). Using the baseline.")
            profile.hook_patterns = baseline.hook_patterns
    else:
        profile.hook_patterns = baseline.hook_patterns

    profile.notes.insert(0, f"Derived from {len(usable)} posts via {source}.")
    return profile.stamp()


# --------------------------------------------------------------------------- #
# Arithmetic half
# --------------------------------------------------------------------------- #


def _split_performers(samples: Sequence[PostSample]) -> Tuple[List[PostSample], List[PostSample]]:
    ranked = sorted(samples, key=lambda s: (s.velocity, s.engagement_rate), reverse=True)
    n = len(ranked)
    cut = max(4, n // 4)
    return ranked[:cut], ranked[-cut:]


def _duration_bands(top: Sequence[PostSample]) -> List[DurationBand]:
    durations = sorted(s.duration for s in top if s.duration > 3)
    if len(durations) < 6:
        return []
    def pct(p: float) -> float:
        idx = min(len(durations) - 1, max(0, int(round((p / 100.0) * (len(durations) - 1)))))
        return durations[idx]
    bands = [DurationBand(round(pct(20), 1), round(pct(55), 1), "core")]
    hi_low, hi_high = round(pct(60), 1), round(pct(88), 1)
    if hi_high - hi_low > 4 and hi_low > bands[0].high + 2:
        bands.append(DurationBand(hi_low, hi_high, "long tail"))
    return bands


def _caption_band(top: Sequence[PostSample]) -> Optional[DurationBand]:
    lengths = sorted(len(_strip_tags(s.caption)) for s in top if s.caption)
    if len(lengths) < 6:
        return None
    lo = lengths[int(0.2 * (len(lengths) - 1))]
    hi = lengths[int(0.8 * (len(lengths) - 1))]
    return DurationBand(float(max(10, lo)), float(max(lo + 10, hi)), "chars")


def _hashtag_band(top: Sequence[PostSample]) -> Optional[DurationBand]:
    counts = sorted(len(s.hashtags) for s in top)
    if len(counts) < 6:
        return None
    lo = counts[int(0.25 * (len(counts) - 1))]
    hi = counts[int(0.8 * (len(counts) - 1))]
    return DurationBand(float(lo), float(max(lo, hi)), "tags")


GENERIC_TAGS = {"fyp", "foryou", "foryoupage", "viral", "trending", "explore",
                "reels", "reel", "tiktok", "instagram", "video", "fy", "shorts"}


def _hashtag_pool(top: Sequence[PostSample]) -> List[str]:
    counter: Counter = Counter()
    for s in top:
        for tag in s.hashtags:
            clean = tag.strip().lstrip("#").lower()
            if clean and clean not in GENERIC_TAGS and len(clean) > 2:
                counter[clean] += 1
    return [tag for tag, _ in counter.most_common(30)]


def _strip_tags(caption: str) -> str:
    return " ".join(w for w in caption.split() if not w.startswith("#")).strip()


def _evidence(s: PostSample) -> Dict[str, Any]:
    return {
        "platform": s.platform,
        "caption": _strip_tags(s.caption)[:220],
        "hashtags": s.hashtags[:10],
        "duration": round(s.duration, 1),
        "views": s.views,
        "engagement_rate": round(s.engagement_rate, 4),
        "url": s.url,
    }


# --------------------------------------------------------------------------- #
# Judgement half
# --------------------------------------------------------------------------- #


def _llm_rubric(llm: LLMClient, top: Sequence[PostSample], bottom: Sequence[PostSample],
                niche: str, platforms: List[str]) -> Dict[str, Any]:
    def render(group: Sequence[PostSample], label: str) -> str:
        lines = [f"### {label} (n={len(group)})"]
        for s in group[:35]:
            lines.append(
                f"- [{s.platform}] {s.duration:.0f}s | views={s.views:,} "
                f"| er={s.engagement_rate:.3f} | velocity={s.velocity:.2f}\n"
                f"  caption: {_strip_tags(s.caption)[:200] or '(none)'}\n"
                f"  tags: {', '.join(s.hashtags[:8]) or '(none)'}"
            )
        return "\n".join(lines)

    user = (
        f"NICHE: {niche or 'general short-form'}\n"
        f"PLATFORMS: {', '.join(platforms)}\n\n"
        f"{render(top, 'TOP PERFORMERS')}\n\n{render(bottom, 'BOTTOM PERFORMERS')}\n\n"
        "Write the rubric."
    )
    return llm.json(RUBRIC_SYSTEM, user, RUBRIC_SCHEMA)


# --------------------------------------------------------------------------- #
# Entry point used by the CLI and the pipeline
# --------------------------------------------------------------------------- #


def resolve_profile(cfg, llm: Optional[LLMClient] = None, force_refresh: bool = False) -> TrendProfile:
    """Load a cached profile, or collect samples and build one."""
    from .providers import ApifyProvider, FileProvider

    path = cfg.trends.profile_path
    if path and not force_refresh:
        try:
            profile = TrendProfile.load(path)
            info(f"Trend profile: {profile.summary()}")
            return profile
        except FileNotFoundError:
            info(f"No cached trend profile at {path} - building one.")
        except (ValueError, KeyError) as exc:
            warn(f"Trend profile at {path} is unreadable ({exc}) - rebuilding.")

    provider_name = cfg.trends.provider
    platforms = list(cfg.trends.platforms)
    niche = cfg.trends.niche

    if provider_name == "local":
        profile = build_baseline_profile(niche, platforms)
    else:
        if provider_name == "file":
            if not cfg.trends.samples_path:
                raise ValueError("trends.provider is 'file' but trends.samples_path is not set.")
            provider = FileProvider(cfg.trends.samples_path)
        elif provider_name == "apify":
            provider = ApifyProvider(cfg.trends.apify_token_env, cfg.trends.apify_tiktok_actor,
                                     cfg.trends.apify_instagram_actor)
        else:
            raise ValueError(f"Unknown trends.provider: {provider_name!r} "
                             "(expected local, file or apify).")
        info(f"Collecting trend samples via {provider_name}...")
        samples = provider.collect(niche, platforms, cfg.trends.sample_limit)
        info(f"Collected {len(samples)} posts.")
        profile = build_profile(samples, niche, platforms, source=provider_name, llm=llm)

    if path:
        profile.save(path)
        info(f"Saved trend profile to {path}")
    info(f"Trend profile: {profile.summary()}")
    return profile
