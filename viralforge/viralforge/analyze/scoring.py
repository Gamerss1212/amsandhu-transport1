"""Rank candidate clips, then pick a non-overlapping set.

Scoring is a blend, not a vote.  The measured half (structure, delivery,
length-vs-trend-profile) is cheap, objective, and catches clips that start
mid-word or sit in dead air.  The judged half is Claude reading the actual
words against the trend rubric.  ``scoring.heuristic_weight`` sets the mix; at
0 you trust the model completely, at 1 you never call it.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..models import AudioAnalysis, ClipCandidate, ClipScores, SourceVideo
from ..trends.profile import TrendProfile
from ..utils import info, warn
from .audio import emphasis_times
from .candidates import words_per_minute
from .llm import LLMClient, LLMRefused, LLMUnavailable

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "hook": {"type": "number"},
                    "payoff": {"type": "number"},
                    "standalone": {"type": "number"},
                    "emotion": {"type": "number"},
                    "shareability": {"type": "number"},
                    "rewatch": {"type": "number"},
                    "trend_fit": {"type": "number"},
                    "title": {"type": "string"},
                    "hook_line": {"type": "string"},
                    "reason": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "trim_start_seconds": {"type": "number"},
                    "trim_end_seconds": {"type": "number"},
                    "risk_notes": {"type": "string"},
                },
                "required": ["id", "hook", "payoff", "standalone", "emotion", "shareability",
                             "rewatch", "trend_fit", "title", "hook_line", "reason",
                             "keywords", "trim_start_seconds", "trim_end_seconds", "risk_notes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM = """\
You pick the moments from a long-form video that will perform as standalone \
short-form posts, and you are hard to impress.

You will be given a trend profile for the target platforms and a batch of \
candidate excerpts taken from one video's transcript. Each candidate is a \
verbatim span with a start and end timestamp.

Score every candidate on each axis from 0 to 100. Use the whole range. A \
typical excerpt from a good video is a 40, not a 70 - most moments in any video \
are not postable, and saying so is the entire job. Reserve scores above 85 for \
clips you would bet money on.

Axes:
- hook: does the first sentence stop a scrolling thumb, on its own, with no \
  context? If the first line is a windup, this is below 30.
- payoff: does the clip deliver something concrete - a number, a turn, a \
  punchline, a usable idea - rather than gesturing at one?
- standalone: can someone who has never heard of this video or these people \
  follow it completely? Unresolved pronouns, callbacks and guest-name drops \
  are fatal here.
- emotion: surprise, conflict, humour, awe, indignation, recognition. Flat \
  competence scores low.
- shareability: would a real person send this to one specific friend, or \
  comment to argue with it?
- rewatch: is it dense or loopable enough to watch twice?
- trend_fit: how well it matches the supplied trend profile specifically - not \
  short-form in general.

Also return:
- title: 3-7 words, for the operator's file listing. Not a caption.
- hook_line: the on-screen text card for the first ~1.5 seconds. Under 60 \
  characters, no period at the end, no hashtags, no emoji. It must be true to \
  the clip.
- reason: one sentence on why this travels or why it does not. Be specific.
- keywords: up to 8 searchable topic words from the clip itself.
- trim_start_seconds / trim_end_seconds: seconds to shave off the start and end \
  to land on a cleaner boundary. Use 0 when the boundary is already right; \
  never exceed 6, and never trim so far that the clip loses its point. Positive \
  values only - they move the start later and the end earlier.
- risk_notes: empty string, or a short flag if the clip is likely to be \
  demonetised, misread out of context, or is a claim you would not want \
  amplified without the surrounding argument.

Return JSON only."""


def score_candidates(candidates: Sequence[ClipCandidate], source: SourceVideo,
                     profile: TrendProfile, audio: Optional[AudioAnalysis],
                     cfg: Config, llm: Optional[LLMClient] = None) -> List[ClipCandidate]:
    if not candidates:
        return []

    # 1. Measured pass over everything - this also builds the shortlist.
    for cand in candidates:
        cand.scores.trend_fit = profile.duration_fit(cand.duration)
        cand.scores.total = _heuristic_total(cand, profile)

    ranked = sorted(candidates, key=lambda c: c.scores.total, reverse=True)
    if not cfg.scoring.enable_llm or llm is None:
        info("LLM scoring disabled - ranking on measured signal only.")
        return ranked

    shortlist = _diverse_shortlist(ranked, cfg.scoring.shortlist_size)
    info(f"Scoring {len(shortlist)} shortlisted moments with {cfg.scoring.model}.")

    scored: Dict[int, Dict[str, Any]] = {}
    batches = [shortlist[i:i + cfg.scoring.batch_size]
               for i in range(0, len(shortlist), cfg.scoring.batch_size)]
    for n, batch in enumerate(batches, 1):
        try:
            result = _score_batch(llm, batch, source, profile, n, len(batches))
        except (LLMUnavailable, LLMRefused) as exc:
            warn(f"{exc} Falling back to measured scoring for the rest.")
            break
        except Exception as exc:
            warn(f"Batch {n}/{len(batches)} failed ({type(exc).__name__}: {exc}) - skipping it.")
            continue
        for row in result:
            idx = row.get("id")
            if isinstance(idx, int) and 0 <= idx < len(batch):
                scored[id(batch[idx])] = row

    if not scored:
        warn("No LLM scores came back - using measured ranking.")
        return ranked

    for cand in shortlist:
        row = scored.get(id(cand))
        if row:
            _apply_llm_scores(cand, row, profile, cfg)

    # Unscored candidates keep their measured total but are pushed below the
    # judged ones, so an unreviewed clip never outranks a reviewed one.
    for cand in candidates:
        if id(cand) not in scored:
            cand.scores.total = min(cand.scores.total, 58.0)

    if audio is not None:
        for cand in candidates:
            if not cand.emphasis:
                cand.emphasis = emphasis_times(audio, cand.start, cand.end)

    return sorted(candidates, key=lambda c: c.scores.total, reverse=True)


# --------------------------------------------------------------------------- #


def _heuristic_total(cand: ClipCandidate, profile: TrendProfile) -> float:
    wpm = words_per_minute(cand)
    band = profile.words_per_minute
    if band.contains(wpm):
        pace = 100.0
    else:
        pace = max(0.0, 100.0 - band.distance(wpm) * 1.1)
    return (0.34 * cand.scores.structure
            + 0.30 * cand.scores.delivery
            + 0.22 * cand.scores.trend_fit
            + 0.14 * pace)


def _diverse_shortlist(ranked: Sequence[ClipCandidate], size: int) -> List[ClipCandidate]:
    """Take the best, but never two near-copies of the same moment.

    Without this the shortlist fills with twenty variants of one good minute and
    the model never sees the rest of the video.
    """
    picked: List[ClipCandidate] = []
    for cand in ranked:
        if any(_overlaps(cand, other, 0.5) for other in picked):
            continue
        picked.append(cand)
        if len(picked) >= size:
            break
    if len(picked) < size:                      # top up if the video is short
        for cand in ranked:
            if cand not in picked:
                picked.append(cand)
            if len(picked) >= size:
                break
    return picked


def _overlaps(a: ClipCandidate, b: ClipCandidate, threshold: float) -> bool:
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = min(a.duration, b.duration)
    return shortest > 0 and overlap / shortest >= threshold


def _score_batch(llm: LLMClient, batch: Sequence[ClipCandidate], source: SourceVideo,
                 profile: TrendProfile, n: int, total: int) -> List[Dict[str, Any]]:
    lines = [
        f"SOURCE: {source.title}" + (f" - {source.uploader}" if source.uploader else ""),
        f"SOURCE LENGTH: {source.duration / 60:.0f} min",
        "",
        profile.prompt_block(),
        "",
        f"CANDIDATES (batch {n} of {total}):",
    ]
    for i, cand in enumerate(batch):
        lines.append(
            f"\n--- id={i} | {_ts(cand.start)}-{_ts(cand.end)} | {cand.duration:.0f}s "
            f"| {words_per_minute(cand):.0f} wpm ---\n{cand.transcript}"
        )
    lines.append("\nScore every candidate. Return JSON only.")
    payload = llm.json(SYSTEM, "\n".join(lines), SCORE_SCHEMA)
    clips = payload.get("clips") if isinstance(payload, dict) else payload
    return clips if isinstance(clips, list) else []


def _apply_llm_scores(cand: ClipCandidate, row: Dict[str, Any],
                      profile: TrendProfile, cfg: Config) -> None:
    def num(key: str) -> float:
        try:
            return max(0.0, min(100.0, float(row.get(key, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    s = cand.scores
    s.hook, s.payoff, s.standalone = num("hook"), num("payoff"), num("standalone")
    s.emotion, s.shareability, s.rewatch = num("emotion"), num("shareability"), num("rewatch")
    s.trend_fit = num("trend_fit") if row.get("trend_fit") is not None else s.trend_fit

    cand.title = str(row.get("title") or cand.title)[:120]
    cand.hook_line = str(row.get("hook_line") or cand.hook_line)[:90]
    cand.reason = str(row.get("reason") or "")[:400]
    cand.risk_notes = str(row.get("risk_notes") or "")[:300]
    cand.keywords = [str(k)[:40] for k in (row.get("keywords") or [])][:8]

    _apply_trims(cand, row, cfg)

    # Hook and standalone are gates, not ingredients: a clip nobody watches past
    # second three cannot be rescued by a strong payoff, and a clip that needs
    # context is unpostable however good it is.
    judged = (0.26 * s.hook + 0.16 * s.payoff + 0.14 * s.standalone + 0.14 * s.emotion
              + 0.16 * s.shareability + 0.06 * s.rewatch + 0.08 * s.trend_fit)
    gate = min(1.0, (s.hook / 55.0) ** 0.5) * min(1.0, (s.standalone / 50.0) ** 0.5)
    judged *= 0.55 + 0.45 * gate

    w = max(0.0, min(1.0, cfg.scoring.heuristic_weight))
    measured = _heuristic_total(cand, profile)
    s.total = round(w * measured + (1 - w) * judged, 2)


def _apply_trims(cand: ClipCandidate, row: Dict[str, Any], cfg: Config) -> None:
    def trim(key: str) -> float:
        try:
            return max(0.0, min(6.0, float(row.get(key, 0.0) or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    head, tail = trim("trim_start_seconds"), trim("trim_end_seconds")
    if head + tail <= 0:
        return
    new_start, new_end = cand.start + head, cand.end - tail
    if new_end - new_start >= max(cfg.candidates.min_duration * 0.8, 8.0):
        cand.start, cand.end = round(new_start, 3), round(new_end, 3)


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def select_clips(candidates: Sequence[ClipCandidate], count: int,
                 min_gap: float = 10.0) -> List[ClipCandidate]:
    """Highest-scoring non-overlapping set, spread across the source.

    Greedy by score with a gap constraint. A globally optimal packing is not
    worth it here: clips are chosen for quality, and forcing a lower-scoring
    clip in to satisfy an optimiser is the wrong trade.
    """
    chosen: List[ClipCandidate] = []
    for cand in sorted(candidates, key=lambda c: c.scores.total, reverse=True):
        if len(chosen) >= count:
            break
        if any(cand.start < other.end + min_gap and other.start < cand.end + min_gap
               for other in chosen):
            continue
        chosen.append(cand)
    chosen.sort(key=lambda c: c.start)
    return chosen
