"""Find the best moments in a long video and strictly judge them.

1. Claude reads the *entire* transcript in overlapping windows, with audience
   signals (most-replayed peaks, comment timestamps, loudness spikes) and the
   current viral playbook, and proposes candidate clips.
2. Candidates are snapped to sentence boundaries, length-limited, de-duplicated,
   and scored by fusing the AI score with every measurable signal.
3. A second, much stricter Claude pass - with real frames from each clip - acts
   as a harsh judge. Only clips that clear BOTH thresholds are delivered.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..llm import Claude, image_block
from ..media import extract_frame, fmt_ts
from ..trends.analyzer import trend_fit
from . import local_judge
from .signals import peaks, window_score

CATEGORIES = ["funny", "shocking", "emotional", "insightful", "controversial", "story",
              "motivational", "drama", "educational", "wholesome", "other"]

FINDER_SYSTEM = """You are the best short-form video clipper in the world. You turn long YouTube \
videos (podcasts, interviews, streams, commentary) into TikTok / Instagram Reels clips that go viral.

You will read a window of a long video's transcript. Every line starts with its start time in \
seconds, e.g. "[1834.2] ...". Propose the strongest clip candidates in that window.

What a winning clip needs:
- A scroll-stopping first 1-3 seconds: a bold claim, a question, conflict, a shocking line, or the \
middle of an intense moment. Never start on greetings, filler, "so", "um", or setup nobody cares about.
- Fully self-contained: a stranger with zero context understands it. No "as I said earlier", no \
unexplained names or references the clip does not explain.
- A payoff: punchline, reveal, twist, strong opinion, emotional peak, or a satisfying conclusion. \
End right after the payoff lands - never mid-sentence, never trailing off.
- High emotion or high value: laughter, tension, vulnerability, controversy, surprising facts, \
actionable insight.
- Length between {min_s} and {max_s} seconds.

Audience signals (when given) are real data: "most replayed" peaks and timestamps viewers quoted \
in comments are moments people already loved - check them carefully, but only propose them if the \
words also make a great standalone clip.

Be precise: `start` must be the exact time of the first word of the clip, `end` the exact end time \
of its last sentence (use the next line's start time). Score honestly (0-100); most moments in any \
video are NOT clip-worthy, so it is fine to return few or zero candidates. Never invent content \
that is not in the transcript.

What is going viral on TikTok and Instagram right now (from a fresh analysis of real videos):
{playbook}"""

JUDGE_SYSTEM = """You are the harshest short-form content judge on the internet. A creator will \
post whatever you approve to TikTok and Instagram Reels, and every weak clip hurts their account. \
Your default answer is REJECT. Approve a clip only if you are confident it would clearly \
outperform typical clips in its niche.

For each candidate you get its exact transcript (with timestamps), audience-signal scores, and \
frames from the clip. Reject on ANY fatal flaw:
- the first 3 seconds would not stop a scroll
- it needs outside context to make sense, or references things not explained in the clip
- no clear payoff, or it ends before / long after the payoff
- starts or ends mid-sentence or mid-thought
- rambling, slow, low energy, or repetitive
- visually unusable (black screen, slides only, heavy on-screen text, wrong speaker in frame)
- would likely be flagged or be harmful/misleading when cut out of context

You may tighten boundaries: return `start`/`end` (seconds) inside or up to 5 s outside the given \
range to cut a slow opening or land exactly on the payoff. Keep the result between {min_s} and \
{max_s} seconds.

For approved clips also write: a short on-screen hook (max 8 words, no emojis, grabs attention in \
the first second), a title, a posting caption that fits TikTok and Instagram (1-2 lines, may end \
with a question to drive comments), 5-8 relevant hashtags without the # sign, and up to 8 words \
from the transcript that deserve visual emphasis.

Viral context right now:
{playbook}"""

_SCORES_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "integer"} for k in
                   ("hook", "payoff", "emotion", "standalone", "shareability", "trend_match")},
    "required": ["hook", "payoff", "emotion", "standalone", "shareability", "trend_match"],
    "additionalProperties": False,
}
FINDER_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "summary": {"type": "string"},
                    "why_viral": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "needs_context": {"type": "boolean"},
                    "scores": _SCORES_SCHEMA,
                    "overall": {"type": "integer"},
                },
                "required": ["start", "end", "title", "hook", "summary", "why_viral", "category",
                             "needs_context", "scores", "overall"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "approve": {"type": "boolean"},
                    "score": {"type": "integer"},
                    "fatal_flaws": {"type": "array", "items": {"type": "string"}},
                    "reasons": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "hook_text": {"type": "string"},
                    "title": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "emphasis_words": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "approve", "score", "fatal_flaws", "reasons", "start", "end",
                             "hook_text", "title", "caption", "hashtags", "emphasis_words"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


@dataclass
class Clip:
    start: float
    end: float
    title: str
    hook: str
    summary: str = ""
    why_viral: str = ""
    category: str = "other"
    ai_score: float = 0.0
    signal_scores: dict = field(default_factory=dict)
    fused_score: float = 0.0
    judge_score: float | None = None
    judge_reasons: str = ""
    fatal_flaws: list = field(default_factory=list)
    caption: str = ""
    hashtags: list = field(default_factory=list)
    emphasis_words: list = field(default_factory=list)
    final_score: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ transcript helpers
def transcript_lines(segments: list[dict], start: float, end: float) -> str:
    return "\n".join(f"[{s['s']:.1f}] {s['text']}" for s in segments if s["e"] > start and s["s"] < end)


def windows(duration: float, chunk_s: float, overlap_s: float = 90.0) -> list[tuple[float, float]]:
    out, s = [], 0.0
    while s < duration:
        e = min(duration, s + chunk_s)
        out.append((s, e))
        if e >= duration:
            break
        s = e - overlap_s
    return out


def snap(start: float, end: float, segments: list[dict], words: list[dict],
         min_s: float, max_s: float) -> tuple[float, float] | None:
    """Move boundaries onto sentence starts/ends and enforce the length limits."""
    if not segments:
        return None
    starts = [s for s in segments if abs(s["s"] - start) <= 4.0]
    first = min(starts, key=lambda s: abs(s["s"] - start)) if starts else \
        min(segments, key=lambda s: abs(s["s"] - start))
    ends = [s for s in segments if abs(s["e"] - end) <= 4.0 and s["e"] > first["s"]]
    last = min(ends, key=lambda s: abs(s["e"] - end)) if ends else \
        min((s for s in segments if s["e"] > first["s"]), key=lambda s: abs(s["e"] - end), default=first)
    s, e = first["s"], last["e"]

    following = [seg for seg in segments if seg["s"] >= first["s"]]
    if e - s > max_s:  # too long: stop at the last sentence end that fits
        fits = [seg["e"] for seg in following if seg["e"] - s <= max_s]
        if not fits:
            return None
        e = max(fits)
    while e - s < min_s:  # too short: extend by whole sentences
        nxt = [seg["e"] for seg in following if seg["e"] > e]
        if not nxt or min(nxt) - s > max_s:
            return None
        e = min(nxt)

    # small breathing room, without swallowing neighbouring words
    prev_end = max((w["e"] for w in words if w["e"] <= s + 1e-3), default=s - 0.3)
    next_start = min((w["s"] for w in words if w["s"] >= e - 1e-3), default=e + 0.6)
    s = max(prev_end, s - 0.15, 0.0)
    e = min(next_start, e + 0.45)
    return round(s, 2), round(e, 2)


def let_reaction_land(clip: Clip, signals: dict, words: list[dict], max_s: float, most: float = 3.0) -> None:
    """A punchline's laugh / applause is part of the moment: when the audience reacts right after the
    last line, keep the clip running through the reaction (up to `most` s, never into the next words)."""
    curve = (signals.get("raw") or {}).get("reaction")
    if curve is None or len(curve) == 0:
        return
    e = clip.end
    k0 = int(e)
    if k0 >= len(curve) or not any(curve[k] > 0 for k in range(k0, min(len(curve), k0 + 2))):
        return
    k = k0
    while k + 1 < len(curve) and curve[k + 1] > 0 and k + 1 - e < most:
        k += 1
    next_word = min((w["s"] for w in words if w["s"] >= e - 1e-3), default=e + most + 1)
    new_end = min(k + 1.0, e + most, next_word - 0.05, clip.start + max_s + most)
    if new_end > e + 0.2:
        clip.end = round(new_end, 2)


def overlap_ratio(a: Clip, b: Clip) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def dedupe(clips: list[Clip], key: str = "fused_score", max_overlap: float = 0.4) -> list[Clip]:
    kept: list[Clip] = []
    for c in sorted(clips, key=lambda c: -getattr(c, key)):
        if all(overlap_ratio(c, k) <= max_overlap for k in kept):
            kept.append(c)
    return kept


def fuse(clip: Clip, signals: dict, profile: dict | None, transcript_text: str, weights: dict) -> None:
    pct = signals.get("pct", {})
    scores = {
        "ai": clip.ai_score,
        "heatmap": window_score(pct.get("heatmap"), clip.start, clip.end),
        "comments": window_score(pct.get("comments"), clip.start, clip.end),
        "energy": window_score(pct.get("energy"), clip.start, clip.end),
        "trend_fit": trend_fit(profile, f"{clip.hook} {transcript_text}", clip.duration) if profile else None,
    }
    clip.signal_scores = {k: v for k, v in scores.items() if v is not None}
    total_w = sum(weights[k] for k in clip.signal_scores)
    clip.fused_score = round(sum(weights[k] * v for k, v in clip.signal_scores.items()) / total_w, 1)


def _signal_hints(signals: dict, start: float, end: float) -> str:
    raw = signals.get("raw", {})
    lines = []
    for name, label in (("heatmap", "Most replayed by viewers"), ("comments", "Quoted in viewer comments"),
                        ("energy", "Loudness/energy spikes")):
        curve = raw.get(name)
        if curve is None:
            continue
        s, e = int(start), int(min(len(curve), end))
        local = [p + s for p in peaks(curve[s:e], n=6, min_gap=45)]
        if local:
            lines.append(f"{label}: " + ", ".join(f"{p}s ({fmt_ts(p)})" for p in local))
    return "\n".join(lines) or "No audience signals available for this window."


# ------------------------------------------------------------------ main entry points
def find_candidates(llm: Claude, meta: dict, transcript: dict, signals: dict, playbook: str,
                    cfg: dict, progress=None) -> list[Clip]:
    a = cfg["analysis"]
    duration = meta["duration"]
    system = FINDER_SYSTEM.format(min_s=a["min_clip_seconds"], max_s=a["max_clip_seconds"],
                                  playbook=playbook or "(no trend data)")
    chapters = "\n".join(f"- {fmt_ts(c['start_time'])} {c['title']}" for c in meta.get("chapters") or [])
    wins = windows(duration, a["chunk_minutes"] * 60)
    clips: list[Clip] = []
    for i, (ws, we) in enumerate(wins):
        if progress:
            progress(i / len(wins), f"AI is watching {fmt_ts(ws)}-{fmt_ts(we)} of {fmt_ts(duration)}")
        lines = transcript_lines(transcript["segments"], ws, we)
        if len(lines) < 200:  # near-silent window (intro music, breaks)
            continue
        user = (f"Video: {meta['title']}\nChannel: {meta.get('channel', '')}\n"
                f"Full length: {fmt_ts(duration)}. This window: {fmt_ts(ws)} to {fmt_ts(we)}.\n"
                + (f"Chapters:\n{chapters}\n" if chapters else "")
                + f"\nAudience signals in this window:\n{_signal_hints(signals, ws, we)}\n"
                f"\nPropose up to {a['candidates_per_chunk']} candidates.\n\nTranscript:\n{lines}")
        result = llm.json(system, user, FINDER_SCHEMA)
        for c in result["candidates"]:
            if c["needs_context"] or c["end"] <= c["start"]:
                continue
            clips.append(Clip(start=float(c["start"]), end=float(c["end"]), title=c["title"],
                              hook=c["hook"], summary=c["summary"], why_viral=c["why_viral"],
                              category=c["category"], ai_score=float(max(0, min(100, c["overall"])))))
    return clips


def signal_only_candidates(transcript: dict, signals: dict, cfg: dict) -> list[Clip]:
    """No-AI fallback: build candidates around the strongest audience/energy peaks."""
    a = cfg["analysis"]
    raw = signals.get("raw", {})
    centers: list[int] = []
    for name in ("heatmap", "comments", "energy"):
        centers += peaks(raw.get(name), n=10, min_gap=90)
    target = (a["min_clip_seconds"] + a["max_clip_seconds"]) / 2
    out = []
    for t in sorted(set(centers)):
        text = transcript_lines(transcript["segments"], t - target * 0.4, t + target * 0.6)
        first = text.split("\n")[0].split("] ", 1)[-1] if text else ""
        out.append(Clip(start=t - target * 0.4, end=t + target * 0.6, title=first[:60], hook=first[:60],
                        ai_score=50.0))
    return out


def judge(llm: Claude, clips: list[Clip], meta: dict, transcript: dict, video: Path, playbook: str,
          cfg: dict, frames_dir: Path) -> None:
    a = cfg["analysis"]
    system = JUDGE_SYSTEM.format(min_s=a["min_clip_seconds"], max_s=a["max_clip_seconds"],
                                 playbook=playbook or "(no trend data)")
    content: list[dict] = [{"type": "text", "text": f"Source video: {meta['title']} "
                                                    f"({meta.get('channel', '')})\n"
                                                    f"Judge these {len(clips)} candidate clips."}]
    for i, c in enumerate(clips):
        text = transcript_lines(transcript["segments"], c.start, c.end)
        sig = ", ".join(f"{k} {v:.0f}" for k, v in c.signal_scores.items())
        content.append({"type": "text", "text": (
            f"\n=== Candidate id={i} | {c.start:.1f}s-{c.end:.1f}s ({c.duration:.0f}s) | {c.category}\n"
            f"Proposed hook: {c.hook}\nWhy it might work: {c.why_viral}\nSignal scores (0-100): {sig}\n"
            f"Transcript:\n{text}")})
        if a["judge_with_frames"]:
            for j, frac in enumerate((0.05, 0.5, 0.9)):
                try:
                    img = extract_frame(video, c.start + frac * c.duration, frames_dir / f"c{i}_{j}.jpg", 512)
                    content.append(image_block(img))
                except Exception:
                    pass
    result = llm.json(system, content, JUDGE_SCHEMA, effort=cfg["llm"]["judge_effort"])
    by_id = {v["id"]: v for v in result["verdicts"]}
    for i, c in enumerate(clips):
        v = by_id.get(i)
        if not v:
            c.judge_score, c.judge_reasons = 0.0, "not judged"
            continue
        c.judge_score = float(max(0, min(100, v["score"]))) if v["approve"] else min(float(v["score"]), 50.0)
        c.judge_reasons, c.fatal_flaws = v["reasons"], v["fatal_flaws"]
        if v["approve"]:
            if abs(v["start"] - c.start) <= 6 and abs(v["end"] - c.end) <= 6:
                c.start, c.end = float(v["start"]), float(v["end"])
            c.hook, c.title, c.caption = v["hook_text"] or c.hook, v["title"] or c.title, v["caption"]
            c.hashtags = [h.lstrip("#").replace(" ", "") for h in v["hashtags"]][:8]
            c.emphasis_words = v["emphasis_words"][:8]


def local_candidates(transcript: dict, signals: dict, profile: dict | None, cfg: dict) -> list[Clip]:
    """No-key mode: the built-in judge scans every sentence, plus audience/loudness peaks."""
    a = cfg["analysis"]
    wins = local_judge.find_windows(transcript["segments"], signals, profile, a["min_clip_seconds"],
                                    a["max_clip_seconds"], top_n=max(12, a["max_clips_per_video"] * 5))
    clips = [Clip(w["start"], w["end"], "", "") for w in wins]
    return clips + signal_only_candidates(transcript, signals, cfg)


def local_review(clips: list[Clip], meta: dict, transcript: dict, signals: dict, profile: dict | None,
                 weights: dict) -> None:
    """Scores, titles and captions clips with the built-in judge (sets ai_score / fatal_flaws)."""
    segs = transcript["segments"]
    for c in clips:
        r = local_judge.review("", c.start, c.end, segs, signals, profile)
        text = r["text"]
        c.ai_score = r["score"]
        c.fatal_flaws = r["flaws"]
        c.judge_reasons = "Built-in judge: " + ", ".join(f"{k} {v * 100:.0f}" for k, v in r["parts"].items())
        c.category = local_judge.category(text, r["parts"].get("laughs", 0.0))
        c.hook = local_judge.hook_text(segs, c.start, c.end, profile, transcript["words"])
        c.title = c.hook.rstrip(".")[:70]
        c.summary = text[:280]
        c.caption, c.hashtags = local_judge.caption_and_tags(c.hook, c.category, meta, profile)
        c.emphasis_words = local_judge.emphasis_words(text, profile)
        fuse(c, signals, profile, transcript_lines(segs, c.start, c.end), weights)


def select_moments(meta: dict, transcript: dict, signals: dict, profile: dict | None, cfg: dict,
                   video: Path, llm: Claude | None, progress=None, log=None) -> tuple[list[Clip], list[Clip]]:
    """Returns (approved clips, all judged candidates)."""
    a = cfg["analysis"]
    playbook = (profile or {}).get("playbook", "")
    segs, words = transcript["segments"], transcript["words"]
    progress = progress or (lambda f, m="": None)
    log = log or (lambda m: None)

    if llm:
        raw = find_candidates(llm, meta, transcript, signals, playbook, cfg, progress)
    else:
        progress(0.3, "Built-in judge scanning every sentence of the video...")
        raw = local_candidates(transcript, signals, profile, cfg)
    log(f"{len(raw)} raw candidates proposed")

    clips = []
    for c in raw:
        snapped = snap(c.start, c.end, segs, words, a["min_clip_seconds"], a["max_clip_seconds"])
        if not snapped:
            continue
        c.start, c.end = snapped
        if llm:
            fuse(c, signals, profile, transcript_lines(segs, c.start, c.end), a["weights"])
        clips.append(c)
    if llm:
        clips = dedupe(clips)
    else:
        local_review(clips, meta, transcript, signals, profile, a["weights"])
        clips = dedupe([c for c in clips if not c.fatal_flaws]) + dedupe([c for c in clips if c.fatal_flaws])
    log(f"{len(clips)} unique candidates after snapping and de-duplication")

    shortlist = clips[: max(4, a["max_clips_per_video"] * 2)]
    if llm and shortlist:
        progress(0.9, f"Strict judge reviewing {len(shortlist)} candidates...")
        frames_dir = video.parent / "judge_frames"
        frames_dir.mkdir(exist_ok=True)
        judge(llm, shortlist, meta, transcript, video, playbook, cfg, frames_dir)
        for c in shortlist:  # boundaries may have moved - re-snap and re-score
            snapped = snap(c.start, c.end, segs, words, a["min_clip_seconds"], a["max_clip_seconds"])
            if snapped:
                c.start, c.end = snapped
                fuse(c, signals, profile, transcript_lines(segs, c.start, c.end), a["weights"])

    approved = []
    best_local = max((c.ai_score for c in shortlist if not c.fatal_flaws), default=0.0)
    for c in shortlist:
        if llm:
            ok = (c.judge_score is None or c.judge_score >= a["judge_threshold"]) and \
                c.fused_score >= a["fused_threshold"]
        else:
            # strict twice over: good in absolute terms AND one of this video's standout moments
            ok = not c.fatal_flaws and c.ai_score >= a["local_content_threshold"] and \
                c.ai_score >= best_local - a["local_band"] and c.fused_score >= a["local_fused_threshold"]
        if ok:
            c.final_score = round(0.6 * c.judge_score + 0.4 * c.fused_score, 1) if c.judge_score is not None \
                else c.fused_score
            approved.append(c)
    approved = dedupe(approved, key="final_score")[: a["max_clips_per_video"]]
    for c in approved:
        let_reaction_land(c, signals, transcript["words"], a["max_clip_seconds"])
        c.emphasis_words = c.emphasis_words or []
        if not c.hashtags and profile:
            c.hashtags = [r["feature"].lstrip("#") for r in profile.get("hashtag_lift", [])[:6]]
    if llm:
        log(f"{len(approved)} clips passed the strict review (judge >= {a['judge_threshold']}, "
            f"fused >= {a['fused_threshold']})")
    else:
        log(f"{len(approved)} clips passed the built-in strict review (content >= "
            f"{a['local_content_threshold']}, combined >= {a['local_fused_threshold']})")
    return approved, shortlist


def clip_words(words: list[dict], start: float, end: float) -> list[dict]:
    # a word belongs to the clip when its middle is inside it: whisper sometimes gives the last word of the
    # previous sentence zero length exactly at the clip start ("all." 1936.88-1936.88), which must not show
    return [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05
            and start < (w["s"] + w["e"]) / 2 < end or (w["s"] > start and w["e"] < end)]

