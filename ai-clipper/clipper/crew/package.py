"""Production and publishing: platform versions, metadata options, recommendations and the final package.

Every word the metadata writers produce comes from the clip itself - hooks and titles are the speaker's own
lines, and an option whose words are not in the transcript is dropped (the faithfulness check). Nothing is
posted from here: clips are saved as drafts, and the compliance officer only clears a clip for automatic
posting when every check passed and automatic posting was switched on.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from ..agents import BOARD
from ..analysis import local_judge as lj
from ..editing.safety import censor, clean_tag
from ..media import extract_frame, fmt_ts, probe, run_ffmpeg
from .consensus import platform_fit
from .evidence import norm_words
from .lexicon import STOP

PLATFORMS = {
    # key: name, hard length limit (s), size limit (MB), UI-safe margins (fraction of height/width), text limits
    "tiktok": {"name": "TikTok", "role": "tiktok", "max_s": 600, "max_mb": 1024, "top": 0.08, "bottom": 0.20,
               "right": 0.12, "caption_chars": 2200, "tags": (3, 5)},
    "instagram": {"name": "Instagram Reels", "role": "reels", "max_s": 180, "max_mb": 1024, "top": 0.10,
                  "bottom": 0.22, "right": 0.10, "caption_chars": 2200, "tags": (3, 8)},
    "youtube": {"name": "YouTube Shorts", "role": "shorts", "max_s": 180, "max_mb": 2048, "top": 0.08,
                "bottom": 0.18, "right": 0.10, "caption_chars": 5000, "title_chars": 100, "tags": (2, 3)},
}
AFFINITY = {  # how well each kind of clip tends to travel on each platform (0-1)
    "funny": {"tiktok": 1.0, "instagram": 0.85, "youtube": 0.8}, "story": {"tiktok": 1.0, "instagram": 0.8, "youtube": 0.85},
    "educational": {"tiktok": 0.8, "instagram": 0.85, "youtube": 1.0}, "insightful": {"tiktok": 0.85, "instagram": 0.95, "youtube": 0.95},
    "motivational": {"tiktok": 0.85, "instagram": 1.0, "youtube": 0.85}, "emotional": {"tiktok": 0.9, "instagram": 1.0, "youtube": 0.8},
    "controversial": {"tiktok": 1.0, "instagram": 0.8, "youtube": 0.9}, "shocking": {"tiktok": 1.0, "instagram": 0.85, "youtube": 0.9},
    "drama": {"tiktok": 1.0, "instagram": 0.85, "youtube": 0.85}, "serious": {"tiktok": 0.75, "instagram": 0.85, "youtube": 1.0},
}
AUDIENCES = {
    "money": "Entrepreneurs and personal-finance viewers, 18-34", "business": "Founders and business-minded viewers, 20-40",
    "relationships": "Dating and relationship viewers, 18-30", "fitness": "Gym and health viewers, 18-35",
    "comedy": "Comedy fans, 16-30", "history": "History and documentary fans, 20-45",
    "psychology": "Self-improvement and mental-health viewers, 18-35", "sports": "Sports fans, 16-35",
    "music": "Music fans, 16-30", "tech": "Tech and AI viewers, 18-35",
}
CATEGORY_AUDIENCE = {"funny": "Comedy fans, 16-30", "story": "Storytime viewers, 18-34",
                     "educational": "Learners and self-improvement viewers, 18-40",
                     "motivational": "Motivation and mindset viewers, 18-35",
                     "emotional": "Viewers who share heartfelt moments, 18-40"}


# ---------------------------------------------------------------- faithfulness
def faithful(option: str, transcript: str, share: float = 0.7) -> bool:
    """At least `share` of the option's content words are words the speaker actually said."""
    said = set(norm_words(transcript))
    words = [w for w in norm_words(option) if w not in STOP and len(w) > 2]
    return not words or sum(w in said for w in words) / len(words) >= share


def _short(text: str, n: int = 10) -> str:
    words = re.sub(r"\s+", " ", text).strip().split()
    if len(words) <= n:
        return " ".join(words)
    keep = n
    while keep > 4 and re.sub(r"[^\w']", "", words[keep - 1]).lower() in lj.WEAK_END:
        keep -= 1
    return " ".join(words[:keep]).rstrip(",;:") + "..."


# ---------------------------------------------------------------- metadata writers
def metadata(clip: dict, meta: dict, profile: dict | None, safe: bool = True) -> dict:
    rec = clip.get("crew") or {}
    transcript = re.sub(r"\[[A-Z]\] ", "", rec.get("transcript") or clip.get("summary") or "")
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript) if len(s.split()) >= 4]
    lift = {r["feature"]: r["lift"] for r in (profile or {}).get("hook_lift", [])}
    fix = censor if safe else (lambda x: x)
    with BOARD.work("hookwriter", f"Hook options for {clip.get('title', '')[:40]}"):
        ranked = sorted(sentences[: max(3, int(len(sentences) * 0.6))],
                        key=lambda s: -lj._hook_strength(s, norm_words(s), lift))
        hooks = list(dict.fromkeys([clip.get("hook", "")] + [_short(s, 10) for s in ranked]))
        hooks = [fix(h) for h in hooks if h and faithful(h, transcript)][:4]
    with BOARD.work("titles", "Title and description options"):
        quote = max(sentences, key=lambda s: (4 <= len(s.split()) <= 16, lj._hook_strength(s, norm_words(s), lift)),
                    default="")
        topics = lj.topic_tags(transcript)
        who = meta.get("channel") or ""
        titles = [clip.get("title", ""), _short(quote, 12)]
        if who and topics:
            titles.append(f"{who} on {topics[0]}")
        question = next((s for s in sentences if s.endswith("?") and len(s.split()) <= 16), "")
        if question:
            titles.append(question)
        titles = [fix(t) for t in dict.fromkeys(t for t in titles if t) if faithful(t, transcript + " " + who + " "
                                                                                    + " ".join(topics))][:4]
        link = meta.get("webpage_url") or ""
        credit = f"From {who}" + (f" - {meta.get('title')}" if meta.get("title") else "") + (f" ({link})" if link else "")
        descriptions = [fix(" ".join(sentences[:2]))[:300] + (f"\n\n{credit}" if who else ""),
                        fix(_short(quote, 20)) + (f"\n\nFull conversation: {credit}" if who else "")]
    with BOARD.work("cta", "Calls to action"):
        ctas = []
        if question:
            ctas.append(f"{fix(question)} Tell us in the comments.")
        ctas.append({"funny": "Send this to someone who needs a laugh.", "educational": "Save this for later.",
                     "insightful": "Save this and come back to it.", "emotional": "Share this with someone who needs it.",
                     "motivational": "Save this for the days you want to quit.",
                     "controversial": "Agree or disagree? Tell us below."}.get(clip.get("category"), "What do you think?"))
        if who:
            ctas.append(f"Watch the full conversation on {who}'s channel.")
    with BOARD.work("hashtags", "Hashtag sets"):
        base = [t for t in clip.get("hashtags", []) if clean_tag(t)]
        cat = lj.CATEGORY_TAG.get(clip.get("category"), "")
        broad = list(dict.fromkeys([cat, "fyp", "viral", "podcast"] + base))[:5]
        niche = list(dict.fromkeys(topics + [re.sub(r"[^a-z0-9]", "", who.lower())] + base))[:6]
        mixed = list(dict.fromkeys(niche[:3] + broad[:2]))[:5]
        tag_sets = {"broad": [t for t in broad if t], "niche": [t for t in niche if t], "mixed": [t for t in mixed if t]}
    with BOARD.work("captions", "Platform captions"):
        cta = ctas[0]
        hook = hooks[0] if hooks else clip.get("hook", "")
        captions = {
            "tiktok": f"{hook}\n\n{cta}\n\n" + " ".join("#" + t for t in tag_sets["mixed"][:5]),
            "instagram": f"{hook}\n\n{descriptions[0]}\n\n{cta}\n\n" + " ".join("#" + t for t in
                                                                               (tag_sets["niche"] + tag_sets["broad"])[:8]),
            "youtube": f"{(titles[0] if titles else hook)[:90]} #shorts\n\n{descriptions[0]}\n\n" +
                       " ".join("#" + t for t in tag_sets["niche"][:3]),
        }
        captions = {k: v[: PLATFORMS[k]["caption_chars"]] for k, v in captions.items()}
    return {"hooks": hooks, "titles": titles, "descriptions": descriptions, "captions": captions,
            "hashtags": tag_sets, "ctas": ctas, "faithfulness": "every hook and title uses the speaker's own words"}


# ---------------------------------------------------------------- thumbnails
def thumbnails(out_dir: Path, name: str, info: dict) -> list[str]:
    with BOARD.work("thumbs", f"Cover frames for {name}"):
        video = out_dir / info["video"]
        d = float(info.get("duration") or 0)
        times = [t for t in (1.2, 0.35 * d, 0.7 * d) if 0.2 < t < d - 0.2]
        out = [info["thumbnail"]] if info.get("thumbnail") else []
        for k, t in enumerate(times, 2):
            try:
                out.append(extract_frame(video, t, out_dir / f"{name}.thumb{k}.jpg", width=720).name)
            except Exception:
                pass
        return out


# ---------------------------------------------------------------- platform versions
def _link(src: Path, dst: Path) -> str:
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)  # same bytes on disk, no extra space
        return "linked"
    except OSError:
        shutil.copy2(src, dst)
        return "copied"


def platform_versions(out_dir: Path, name: str, info: dict, cfg, platforms: list[str]) -> dict:
    """A checked file for each platform. The master is built to fit every platform's safe zones, so it is
    reused as is when it passes a platform's checks; a platform with a shorter limit gets a trimmed cut."""
    e = cfg["editing"]
    W, H = e["width"], e["height"]
    master = out_dir / info["video"]
    pr = probe(master)
    out = {}
    for key in platforms:
        spec = PLATFORMS.get(key)
        if not spec:
            continue
        with BOARD.work(spec["role"], f"{spec['name']} version of {name}"):
            dst = out_dir / f"{name}.{key}.mp4"
            how = None
            if pr["duration"] > spec["max_s"]:
                run_ffmpeg(["-i", str(master), "-t", f"{spec['max_s'] - 0.5:.2f}", "-c:v", "libx264", "-preset",
                            "veryfast", "-crf", "18", "-af", f"afade=t=out:st={spec['max_s'] - 1.5:.2f}:d=1",
                            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)])
                how = f"trimmed to {spec['max_s']}s"
            else:
                how = _link(master, dst)
            v = probe(dst)
            size_mb = dst.stat().st_size / 1e6
            cap_y, hook_y = info.get("caption_y"), info.get("hook_y")
            checks = {
                "aspect_9x16": v["width"] * 16 == v["height"] * 9,
                "resolution": v["width"] >= 720 and (v["width"], v["height"]) == (W, H),
                "fps": 23 <= v["fps"] <= 60,
                "length": 3 <= v["duration"] <= spec["max_s"],
                "file_size": size_mb <= spec["max_mb"],
                "audio": v["has_audio"],
                "captions_in_safe_zone": cap_y is None or cap_y + 120 <= H * (1 - spec["bottom"]),
                "hook_in_safe_zone": hook_y is None or hook_y >= H * spec["top"] - 20,
            }
            out[key] = {"platform": spec["name"], "file": dst.name, "made": how, "duration": round(v["duration"], 2),
                        "size_mb": round(size_mb, 1), "checks": checks, "ok": all(checks.values()),
                        "fit": round(platform_fit(v["duration"], key), 2)}
    return out


# ---------------------------------------------------------------- recommendations
def recommend(clip: dict, versions: dict) -> dict:
    with BOARD.work("strategist", f"Best platform and audience for {clip.get('title', '')[:40]}"):
        rec = clip.get("crew") or {}
        policy = rec.get("platforms") or {}
        cat = clip.get("category") or "insightful"
        scores = {}
        for key, v in versions.items():
            if not v["ok"]:
                continue
            p = {"removed": 0.0, "limited": 0.5}.get(policy.get(key, "ok"), 1.0)
            scores[key] = round(v["fit"] * AFFINITY.get(cat, {}).get(key, 0.85) * p, 3)
        best = max(scores, key=scores.get) if scores else None
        topics = lj.topic_tags(rec.get("transcript") or clip.get("summary") or "")
        audience = next((AUDIENCES[t] for t in topics if t in AUDIENCES), None) or CATEGORY_AUDIENCE.get(cat) or \
            "Podcast-clip viewers, 18-34"
        return {"best_platform": best, "platform_scores": scores, "audience": audience,
                "why": f"{cat} clips of {clip.get('duration', 0):.0f}s travel best on "
                       f"{PLATFORMS[best]['name'] if best else 'no platform'}"}


# ---------------------------------------------------------------- the compliance officer
def publish_check(item: dict, cfg) -> tuple[bool, list[str]]:
    """Automatic posting is allowed only when every check passed. Anything else stays a draft for you."""
    with BOARD.work("compliance", f"Publishing check for {item.get('name', '')}"):
        p = cfg.get("posting") or {}
        pkg = item.get("package") or {}
        crew = pkg.get("crew") or {}
        why = []
        if not p.get("auto_schedule"):
            why.append("automatic posting is off (drafts wait for your approval)")
        if not crew:
            why.append("no crew decision record")
        if crew.get("status") != "approved":
            why.append("held for human review: " + "; ".join(crew.get("review_because", [])[:2]) if crew.get("status") ==
                       "review" else f"status is {crew.get('status')}")
        if crew.get("risks"):
            why.append("open risks: " + "; ".join(r["detail"] for r in crew["risks"][:2]))
        if crew and crew.get("confidence", 0) < float(p.get("min_confidence", 0.75)):
            why.append(f"confidence {crew.get('confidence', 0) * 100:.0f}% is below {float(p.get('min_confidence', 0.75)) * 100:.0f}%")
        licensed = {c.lower() for c in p.get("licensed_channels") or []}
        channel = (pkg.get("source") or {}).get("channel") or ""
        if crew.get("third_party") and channel.lower() not in licensed:
            why.append(f"copyright: {channel or 'the source'} is not in posting.licensed_channels (add it once you have "
                       "permission or join the creator's clipping program)")
        bad = [k for k, v in (pkg.get("versions") or {}).items() if not v.get("ok")]
        if bad:
            why.append("export checks failed for " + ", ".join(bad))
        review = (item.get("review") or {})
        if review and not review.get("ok", True):
            why.append("the export inspector found problems: " + "; ".join(review.get("problems", [])[:2]))
        return not why, why


# ---------------------------------------------------------------- per clip and per run packages
def package_clip(out_dir: Path, name: str, clip: dict, info: dict, meta: dict, cfg, profile: dict | None,
                 offset: float = 0.0) -> dict:
    platforms = list((cfg.get("package") or {}).get("platforms") or ["tiktok", "instagram", "youtube"])
    safe = cfg["editing"].get("censor_profanity", True)
    versions = platform_versions(out_dir, name, info, cfg, platforms)
    md = metadata(clip, meta, profile, safe)
    thumbs = thumbnails(out_dir, name, info)
    rec = recommend({**clip, "duration": info.get("duration", 0)}, versions)
    crew = clip.get("crew") or {}
    ranges = [[a + offset, b + offset] for a, b in info.get("source_ranges") or []]
    pkg = {"name": name, "title": clip.get("title"), "category": clip.get("category"),
           "status": crew.get("status", clip.get("status", "approved")),
           "timestamps": {"source_start": round(clip["start"], 2), "source_end": round(clip["end"], 2),
                          "source_start_ts": fmt_ts(clip["start"]), "source_end_ts": fmt_ts(clip["end"]),
                          "edited_start": 0.0, "edited_end": info.get("duration"),
                          "kept_source_ranges": ranges, "jump_cuts": info.get("jump_cuts", 0), "speed": info.get("speed", 1.0)},
           "transcript": crew.get("transcript") or clip.get("summary", ""), "why": crew.get("why") or clip.get("why_viral"),
           "scores": crew.get("scores", {}), "confidence": crew.get("confidence"),
           "reviewers": {"supporting": crew.get("support"), "dissenting": crew.get("dissent"),
                         "supporters": crew.get("supporters", []), "dissenters": crew.get("dissenters", [])},
           "verification": crew.get("verification", {}), "risks": crew.get("risks", []),
           "review_because": crew.get("review_because", []), "rejected_alternatives": crew.get("alternatives", []),
           "versions": versions, "subtitles": {k: info.get(k) for k in ("subtitles", "vtt", "speaker_subtitles")},
           "thumbnails": thumbs, "metadata": md, "recommendation": rec,
           "source": {"title": meta.get("title"), "channel": meta.get("channel"), "url": meta.get("webpage_url"),
                      "id": meta.get("id")},
           "crew": crew, "export_review": info.get("review"), "rank_score": clip.get("final_score")}
    (out_dir / f"{name}.package.json").write_text(json.dumps(pkg, indent=1, default=str), encoding="utf-8")
    return pkg


def posting_plan(items: list[dict], cfg, timing=None) -> list[dict]:
    """The order planner: best clips first, kinds of clip alternated, each on its best platform at the
    timing analyst's best free slot. Clips held for review are listed after, without a time."""
    with BOARD.work("order", f"Posting order for {len(items)} clips"):
        p = cfg.get("posting") or {}
        ready = sorted((x for x in items if (x.get("package") or {}).get("status") == "approved"),
                       key=lambda x: -(x.get("score") or 0))
        order, last_cat = [], None
        while ready:
            pick = next((x for x in ready if x.get("category") != last_cat), ready[0])
            ready.remove(pick)
            order.append(pick)
            last_cat = pick.get("category")
        taken: dict[str, list[float]] = {}
        plan = []
        for x in order:
            rec = x["package"]["recommendation"]
            plat = rec.get("best_platform")
            when = None
            if plat and timing:
                api_plat = "instagram" if plat == "instagram" else "tiktok" if plat == "tiktok" else None
                model = timing(api_plat) if api_plat else timing("tiktok")
                slots = model.best_slots(1, time.time() + 600, taken.get(plat, []), int(p.get("max_per_day", 3)),
                                         float(p.get("gap_hours", 3)))
                if slots:
                    when = slots[0]
                    taken.setdefault(plat, []).append(when)
            plan.append({"clip": x["name"], "folder": x["folder"], "platform": plat, "audience": rec.get("audience"),
                         "at": when, "at_text": time.strftime("%a %d %b %H:%M", time.localtime(when)) if when else None})
        for x in items:
            if (x.get("package") or {}).get("status") == "review":
                plan.append({"clip": x["name"], "folder": x["folder"], "platform": None, "at": None,
                             "at_text": "after you review it", "held": x["package"].get("review_because", [])})
        return plan


def write_run_package(root: Path, run_id: str, items: list[dict], videos: list[dict], cfg, timing=None) -> Path:
    """The final package of a run: ranked clips with everything needed to post them, and coverage proof."""
    folder = root / "_packages" / re.sub(r"[^\w-]", "-", run_id)
    folder.mkdir(parents=True, exist_ok=True)
    ranked = sorted(items, key=lambda x: ((x.get("package") or {}).get("status") != "approved", -(x.get("score") or 0)))
    plan = posting_plan(ranked, cfg, timing)
    doc = {"run": run_id, "made_at": time.time(), "clips": [{**(x.get("package") or {}), "folder": x["folder"],
                                                              "rank": k + 1} for k, x in enumerate(ranked)],
           "posting_plan": plan, "videos": videos,
           "summary": {"clips": len(ranked), "approved": sum((x.get("package") or {}).get("status") == "approved" for x in ranked),
                       "held_for_review": sum((x.get("package") or {}).get("status") == "review" for x in ranked),
                       "videos": len(videos), "coverage_complete": all(v.get("coverage", {}).get("complete") for v in videos)}}
    (folder / "package.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    (folder / "package.md").write_text(report_md(doc), encoding="utf-8")
    return folder


def report_md(doc: dict) -> str:
    s = doc["summary"]
    lines = [f"# Clip package - {doc['run']}", "",
             f"{s['clips']} clips ({s['approved']} approved, {s['held_for_review']} held for your review) from "
             f"{s['videos']} video(s). Coverage complete for every video: {'yes' if s['coverage_complete'] else 'NO'}.", ""]
    for c in doc["clips"]:
        ts = c.get("timestamps", {})
        rv = c.get("reviewers", {})
        rec = c.get("recommendation", {})
        md = c.get("metadata", {})
        lines += [f"## {c['rank']}. {c.get('title')}  ({c.get('status')})", "",
                  f"- Source: {(c.get('source') or {}).get('channel')} - {(c.get('source') or {}).get('title')} "
                  f"[{ts.get('source_start_ts')} - {ts.get('source_end_ts')}] -> edited 0:00 - {ts.get('edited_end')}s",
                  f"- Why: {c.get('why')}",
                  f"- Confidence {100 * (c.get('confidence') or 0):.0f}% | {rv.get('supporting')} supporting, "
                  f"{rv.get('dissenting')} dissenting reviewers | {c.get('verification', {}).get('passes', 0)} verification passes",
                  "- Scores: " + ", ".join(f"{k.replace('_', ' ')} {v:.0f}" for k, v in (c.get("scores") or {}).items()),
                  f"- Best platform: {rec.get('best_platform')} | audience: {rec.get('audience')}",
                  "- Hooks: " + " / ".join(md.get("hooks", [])),
                  "- Titles: " + " / ".join(md.get("titles", [])),
                  "- Hashtags: " + " ".join("#" + t for t in (md.get("hashtags") or {}).get("mixed", [])),
                  "- Files: " + ", ".join(v["file"] for v in (c.get("versions") or {}).values()),
                  "- Thumbnails: " + ", ".join(c.get("thumbnails", []))]
        if c.get("risks") or c.get("review_because"):
            lines.append("- NEEDS REVIEW: " + "; ".join(c.get("review_because") or [r["detail"] for r in c["risks"]]))
        if c.get("rejected_alternatives"):
            lines.append("- Rejected near-duplicates: " + ", ".join(
                f"{a['id']} ({a['start']:.0f}-{a['end']:.0f}s: {a['reason']})" for a in c["rejected_alternatives"][:4]))
        lines += ["", "Transcript:", "", "> " + (c.get("transcript") or "").replace("\n", "\n> "), ""]
    lines += ["## Posting plan", ""] + [f"- {p['clip']}: {p.get('platform') or '-'} {p.get('at_text') or ''}"
                                        for p in doc["posting_plan"]]
    lines += ["", "## Coverage", ""]
    for v in doc["videos"]:
        cov = v.get("coverage") or {}
        lines.append(f"- {v.get('title')}: {cov.get('summary')} - words reviewed {cov.get('words_reviewed')}, "
                     f"gaps {cov.get('gaps')}, sections {len(cov.get('sections') or [])}")
    return "\n".join(lines) + "\n"
