"""Write the post copy: caption, hashtags, title, first comment.

The copy is generated per platform, because the constraints genuinely differ -
TikTok captions are a search surface and get truncated early, Reels captions
have room but only show ~125 characters before the fold.  The trend profile
supplies both the observed caption length and the hashtag pool, so this stage
inherits whatever grounding the trends stage managed to get.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..models import ClipCandidate, PostCopy, SourceVideo
from ..trends.profile import TrendProfile
from ..utils import warn
from ..analyze.llm import LLMClient, LLMRefused, LLMUnavailable

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "platforms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string"},
                    "caption": {"type": "string"},
                    "alt_captions": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                    "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                    "title": {"type": "string"},
                    "first_comment": {"type": "string"},
                },
                "required": ["platform", "caption", "alt_captions", "hashtags", "title",
                             "first_comment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["platforms"],
    "additionalProperties": False,
}

SYSTEM = """\
You write the post copy for short-form video clips. You are given the clip's \
verbatim transcript, why it was selected, and a trend profile describing what \
performs on the target platforms.

Rules:
- The caption adds something the video does not already say: the stake, the \
question, the part left out, or who it is for. Never describe the video \
("in this clip I talk about..."). Never write "Watch till the end".
- Write the way the niche talks. Match the caption length the trend profile \
reports.
- Hashtags: use the supplied pool where it fits, plus specific topical tags \
drawn from the clip itself. No hashtag stuffing, no banned-adjacent tags, and \
do not include the hashtags inside the caption field - they go in the hashtags \
array, lowercase, without the # character.
- alt_captions: genuinely different angles, not rewordings of the first one.
- title: a short filename-friendly label, 3-7 words.
- first_comment: one line to pin that invites a specific reply, or "" if \
nothing honest fits. Never bait.
- Claim nothing the transcript does not support. If the clip makes a strong \
claim, let the caption attribute it to the speaker rather than asserting it.

Return JSON only, one entry per requested platform."""


def write_copy(candidate: ClipCandidate, source: SourceVideo, profile: TrendProfile,
               cfg: Config, llm: Optional[LLMClient] = None,
               platforms: Optional[Sequence[str]] = None) -> Dict[str, PostCopy]:
    targets = list(platforms or profile.platforms or ["tiktok", "instagram"])

    if llm is None:
        return {p: _fallback_copy(candidate, profile, p, cfg) for p in targets}

    try:
        payload = llm.json(SYSTEM, _prompt(candidate, source, profile, cfg, targets),
                           COPY_SCHEMA, max_tokens=cfg.copy.max_output_tokens)
    except (LLMUnavailable, LLMRefused) as exc:
        warn(f"Copywriting fell back to templates: {exc}")
        return {p: _fallback_copy(candidate, profile, p, cfg) for p in targets}
    except Exception as exc:
        warn(f"Copywriting failed ({type(exc).__name__}: {exc}) - using templates.")
        return {p: _fallback_copy(candidate, profile, p, cfg) for p in targets}

    rows = payload.get("platforms", []) if isinstance(payload, dict) else []
    out: Dict[str, PostCopy] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("platform", "")).strip().lower()
        if name not in targets:
            continue
        out[name] = PostCopy(
            platform=name,
            caption=_clean_caption(str(row.get("caption", "")).strip()),
            alt_captions=[_clean_caption(str(c)) for c in (row.get("alt_captions") or [])][:4],
            hashtags=_clean_tags(row.get("hashtags") or [], cfg.copy.hashtags_per_post),
            title=str(row.get("title", "") or candidate.title)[:80],
            first_comment=str(row.get("first_comment", "") or "")[:200],
        )
    for p in targets:
        out.setdefault(p, _fallback_copy(candidate, profile, p, cfg))
    return out


def _prompt(candidate: ClipCandidate, source: SourceVideo, profile: TrendProfile,
            cfg: Config, targets: Sequence[str]) -> str:
    lines = [
        f"SOURCE VIDEO: {source.title}" + (f" ({source.uploader})" if source.uploader else ""),
        f"CLIP LENGTH: {candidate.duration:.0f}s",
        f"WHY IT WAS PICKED: {candidate.reason or 'strong standalone moment'}",
        f"ON-SCREEN HOOK: {candidate.hook_line or '(none)'}",
        f"TOPIC KEYWORDS: {', '.join(candidate.keywords) or '(none)'}",
        "",
        profile.prompt_block(),
        "",
        f"CAPTION LENGTH TO AIM FOR: {profile.caption_chars.low:.0f}-"
        f"{profile.caption_chars.high:.0f} characters",
        f"HASHTAGS PER POST: {cfg.copy.hashtags_per_post}",
    ]
    if profile.hashtag_pool:
        lines.append(f"OBSERVED HASHTAG POOL: {', '.join(profile.hashtag_pool[:25])}")
    if profile.caption_guidance:
        lines.append(f"CAPTION GUIDANCE: {profile.caption_guidance}")
    if cfg.copy.voice:
        lines.append(f"BRAND VOICE (follow this exactly): {cfg.copy.voice}")
    if not cfg.copy.include_cta:
        lines.append("Do not include any call to action.")
    if candidate.risk_notes:
        lines.append(f"CAUTION FLAGGED ON THIS CLIP: {candidate.risk_notes}")
    lines += [
        "",
        f"PLATFORMS TO WRITE FOR: {', '.join(targets)}",
        f"ALTERNATE CAPTIONS PER PLATFORM: {max(0, cfg.copy.variants - 1)}",
        "",
        "CLIP TRANSCRIPT (verbatim):",
        candidate.transcript,
        "",
        "Write the copy. JSON only.",
    ]
    return "\n".join(lines)


def _clean_caption(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*#\w+", "", text).strip()     # tags belong in their own field
    return text[:2200]


def _clean_tags(tags: Sequence[Any], limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for tag in tags:
        clean = re.sub(r"[^0-9a-z_]", "", str(tag).strip().lower().lstrip("#"))
        if len(clean) < 2 or clean in seen:
            continue
        seen.add(clean)
        out.append(f"#{clean}")
        if len(out) >= limit:
            break
    return out


def _fallback_copy(candidate: ClipCandidate, profile: TrendProfile, platform: str,
                   cfg: Config) -> PostCopy:
    """Template copy, used when there is no model available.

    Deliberately plain: a template that tries to sound clever reads worse than
    one that just states the hook.
    """
    hook = (candidate.hook_line or candidate.title or
            candidate.transcript[:80].rsplit(" ", 1)[0]).strip().rstrip(".")
    pool = profile.hashtag_pool or ["shorts", "clips"]
    tags = _clean_tags(list(dict.fromkeys(candidate.keywords + pool)),
                       cfg.copy.hashtags_per_post)
    return PostCopy(
        platform=platform,
        caption=hook,
        hashtags=tags,
        title=(candidate.title or hook)[:80],
        alt_captions=[],
        first_comment="",
    )
