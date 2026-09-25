"""The 150-agent crew: independent specialist review of every second of a video.

`review_video` runs one video through the crew (see `job.py` for the phases) and returns the clips it
approved or held for human review, each carrying its full decision record for the final package.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..agents import BOARD
from ..analysis import local_judge as lj
from ..analysis.moments import Clip, per_video_cap
from ..media import fmt_ts
from .evidence import Evidence
from .job import JobCtx, VideoReview, write_audit
from .registry import CREW

__all__ = ["CREW", "review_video"]


def _label_speakers(ev: Evidence) -> None:
    """Give every transcript word its speaker (subtitles show who is talking)."""
    if ev.speakers_found < 2:
        return
    k = 0
    for w in ev.words:
        while k + 1 < ev.n and w["s"] >= ev.S[k + 1] - 1e-3:
            k += 1
        w["spk"] = ev.speaker_at(k)


def _transcript(ev: Evidence, i: int, j: int) -> str:
    if ev.speakers_found < 2:
        return ev.span_text(i, j)
    lines, cur = [], None
    for k in range(i, j + 1):
        spk = ev.speaker_at(k)
        if spk != cur:
            lines.append(f"[{spk}] {ev.text[k]}")
            cur = spk
        else:
            lines[-1] += " " + ev.text[k]
    return "\n".join(lines)


def _record(ev: Evidence, c: dict, audit: Path) -> dict:
    d = c["decision"]
    v = c.get("verification", {})
    return {
        "id": c["id"], "status": d["status"], "rank_score": d["rank_score"], "performance": d["performance"],
        "confidence": d["confidence"], "scores": d["scores"], "lens_scores": d["lens_scores"],
        "support": c["support"], "dissent": len(c["dissenters"]), "lens_support": c["lens_support"],
        "supporters": [{"by": s["by"], "agent": s.get("agent"), "why": s.get("why", "")} for s in c["supporters"]],
        "dissenters": [{"by": s["by"], "agent": s.get("agent")} for s in c["dissenters"]],
        "abstained": c["abstained"], "minority": c["minority"],
        "source": {"start": c["t0"], "end": c["t1"], "start_ts": fmt_ts(c["t0"]), "end_ts": fmt_ts(c["t1"])},
        "transcript": _transcript(ev, c["i"], c["j"]), "why": c["why"],
        "risks": d["risks"], "review_because": d["review_because"], "notes": d["notes"],
        "alternatives": c["alternatives"], "adjustments": c.get("adjustments", []),
        "verification": {"passes": c["vsum"].get("passes", 0), "budget": v.get("budget"),
                         "deep_review": v.get("deep_review", False), "summary": c["vsum"]},
        "gates": {k: g["score"] for k, g in c["gates"].items()},
        "platforms": (c["gates"].get("platform_policy") or {}).get("platforms", {}),
        "third_party": bool((c["gates"].get("copyright") or {}).get("third_party")),
        "claims": (c["gates"].get("claims") or {}).get("claims", []),
        "trend_fit": d.get("trend_fit"), "audit": str(audit),
    }


def review_video(meta: dict, transcript: dict, signals: dict, profile: dict | None, cfg, video: Path,
                 wav: Path | None, rep=None, watch=None, already_made=None, peers=None) -> tuple[list[Clip], list[Clip], dict]:
    """Returns (clips to make - approved first, then held for review; every finalist; the crew result)."""
    ev = Evidence(meta, transcript, signals, profile, cfg, video, wav)
    crew_cfg = dict(cfg.get("crew") or {})
    cap = per_video_cap(cfg, transcript)
    ctx = JobCtx(ev=ev, rep=rep, watch=watch, already_made=already_made, peers=list(peers or []))
    job_id = f"{meta.get('id') or meta.get('title') or 'video'}@{time.time():.0f}"
    result = VideoReview(ctx, crew_cfg, cap).run(job_id)
    _label_speakers(ev)
    audit = write_audit(result, meta, video.parent / "crew_audit.json")
    (video.parent / "coverage.json").write_text(__import__("json").dumps(result["coverage"], indent=1), encoding="utf-8")
    segs, words = transcript["segments"], transcript["words"]
    comedy = max(ev.comedy, lj.comedy_prior(meta))
    clips: list[Clip] = []
    finalists: list[Clip] = []
    for c in result["final"]:
        d = c["decision"]
        text = ev.span_text(c["i"], c["j"])
        laughs = float(d["lens_scores"].get("humor", 0.0))
        with BOARD.work("hookwriter", f"Writing the hook for {c['id']}"):
            hook = lj.hook_text(segs, c["t0"], c["t1"], profile, words)
        category = lj.category(text, laughs, comedy)
        caption, tags = lj.caption_and_tags(hook, category, meta, profile, text)
        clip = Clip(start=c["t0"], end=c["t1"], title=hook.rstrip(".")[:70], hook=hook, summary=text[:280],
                    why_viral=c["why"], category=category, ai_score=d["performance"],
                    signal_scores={k: v for k, v in d["scores"].items()}, fused_score=d["performance"],
                    judge_reasons=f"Crew: {c['support']} supporting / {len(c['dissenters'])} dissenting reviewers, "
                                  f"confidence {d['confidence'] * 100:.0f}%",
                    fatal_flaws=d["rejected_because"], caption=caption, hashtags=tags,
                    emphasis_words=lj.emphasis_words(text, profile), final_score=d["rank_score"],
                    status=d["status"], crew=_record(ev, c, audit))
        finalists.append(clip)
        if d["status"] in ("approved", "review"):
            clips.append(clip)
    clips.sort(key=lambda c: (c.status != "approved", -c.final_score))
    return clips[:cap], finalists, result
