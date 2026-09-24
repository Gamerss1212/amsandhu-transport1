"""The one-button pipeline: trends -> discovery -> full-video analysis -> editing."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

from .analysis import analyze_video, clip_words
from .analysis.download import download_section, source_key
from .analysis.signals import peaks
from .brain import Brain
from .config import Config
from .db import Database
from .discovery import YouTubeAPI, discover
from .editing import RenderJob, get_preset, render, write_post_files
from .editing.auto import choose_level
from .editing.safety import censor
from .events import Event, Reporter
from .llm import Claude
from .trends import run_trend_analysis
from .ytdl import BotCheck


MAX_CLIPS = 100
LAYER = 5  # clips are made and shown in layers of 5


def split_sources(text: str) -> list[str]:
    """Links and file paths, one per line (several links on one line are fine; paths may contain spaces)."""
    out: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip().strip('"').strip("'").strip()
        if not line:
            continue
        urls = re.findall(r"https?://\S+", line)
        out += urls if len(urls) > 1 else [line]
    return list(dict.fromkeys(out))


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "clip"


def _vid(result: dict) -> str:
    meta = result["meta"]
    return str(meta.get("id") or meta.get("webpage_url") or meta.get("title") or "")


class Pipeline:
    def __init__(self, cfg: Config, rep: Reporter | None = None) -> None:
        self.cfg = cfg
        self.rep = rep or Reporter()
        self.db = Database(cfg.path("paths.db"))
        self.brain = Brain(cfg.path("paths.db").parent / "brain.json")
        self.lock = threading.Lock()
        self.running = False

    def _llm(self) -> Claude | None:
        if not self.cfg.anthropic_key:
            self.rep.info("analysis", "Free mode: the built-in judge reads the whole transcript and picks clips "
                                      "(add an ANTHROPIC_API_KEY to also use Claude)")
            return None
        return Claude(self.cfg["llm"]["model"], self.cfg["llm"]["effort"])

    def run(self, level: str | None = None, clips: int | None = None) -> list[dict]:
        """Everything that happens after pressing "Get clips"."""
        return self._guarded(level, [], clips)

    def clip_video(self, source: str, level: str | None = None, clips: int | None = None) -> list[dict]:
        """Clip videos you choose: one or more links (any site yt-dlp supports) or video files."""
        sources = split_sources(source)
        if not sources:
            raise ValueError("Paste a video link or a file path")
        return self._guarded(level, sources, clips)

    def _guarded(self, level: str | None, sources: list[str], clips: int | None) -> list[dict]:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("A run is already in progress")
        self.running = True
        started = time.time()
        try:
            produced = self._run(level, sources, clips)
            minutes = (time.time() - started) / 60
            self.rep.emit(Event("done", "editing", f"Finished: {len(produced)} clips in {minutes:.1f} min",
                                data={"clips": produced}))
            return produced
        except Exception as exc:
            self.rep.emit(Event("error", "pipeline", str(exc)))
            raise
        finally:
            self.running = False
            self.lock.release()

    def _run(self, level: str | None, sources: list[str], want: int | None) -> list[dict]:
        # "auto" (the default): every clip gets the editing style its moment needs
        level = level if level and level != "auto" else None
        want = max(1, min(MAX_CLIPS, int(want or self.cfg["editing"].get("clips_per_run", 5))))
        layers = -(-want // LAYER)
        rep = self.rep

        if sources:
            profile = self.db.latest_profile()
            rep.info("analysis", f"Clipping {len(sources)} video{'s' if len(sources) > 1 else ''} you chose" +
                     (" (using the latest trend analysis)" if profile else ""))
            candidates = [{"video_id": source_key(s), "input": s, "title": "", "channel": ""} for s in sources]
            max_videos = len(candidates)
        else:
            # more clips need more videos: roughly one video for every 2 clips
            max_videos = max(int(self.cfg["discovery"].get("max_videos_per_run", 8)), min(60, want // 2 + 3))
            # ---- Step 1: what goes viral right now (fresh every run)
            rep.info("trends", f"Step 1/3 - studying {self.cfg['trends']['min_videos']}+ short videos "
                               "for what goes viral right now")
            profile = run_trend_analysis(self.cfg, self.db, rep)
            # ---- Step 2: find long-form videos and watch them fully
            rep.info("discovery", "Step 2/3 - finding long-form YouTube videos")
            candidates = discover(self.cfg, self.db, rep, profile, max_videos)
            if not candidates:
                raise RuntimeError("No suitable long-form videos found - widen discovery settings")

        llm = self._llm()
        yt = YouTubeAPI(self.cfg.youtube_key) if self.cfg.youtube_key else None
        run_id = time.strftime("%Y-%m-%d %H:%M:%S")
        rep.info("analysis", f"Goal: {want} clip{'s' if want > 1 else ''}" +
                 (f" in {layers} layers of up to {LAYER}" if layers > 1 else "") +
                 " - from as many videos as it takes")
        pool: list[tuple] = []  # (clip, analysis result) watched but not edited yet
        produced: list[dict] = []
        analyzed = blocked = repeats = 0

        def edit_layer() -> None:
            nonlocal pool
            n = min(LAYER, want - len(produced))
            take, kinds = [], Counter(p.get("category") for p in produced)
            for _ in range(min(n, len(pool))):  # best first, but learned taste + variety move clips up or down
                best = max(pool, key=lambda cr: cr[0].final_score + self.brain.adjust(
                    cr[0].category, cr[1]["meta"].get("channel") or "", cr[0].duration, kinds))
                pool.remove(best)
                take.append(best)
                kinds[best[0].category] += 1
            layer = len(produced) // LAYER + 1
            rep.info("editing", f"Step 3/3 - editing layer {layer}/{layers}: {len(take)} clip"
                                f"{'s' if len(take) > 1 else ''} from {len({id(r) for _, r in take})} video(s)")
            order: list = []
            for _, r in take:  # videos in the order of their best clip
                if not any(r is o for o in order):
                    order.append(r)
            for result in order:
                chosen = [c for c, r in take if r is result]
                produced.extend(self._edit({**result, "clips": chosen}, level, run_id, layer))

        for cand in candidates:
            if analyzed >= max_videos or len(produced) >= want:
                break
            rep.info("analysis", f"Analyzing: {cand['channel']} - {cand['title']}".rstrip(" -")
                     if cand["title"] else f"Analyzing: {cand.get('input') or cand['video_id']}")
            try:
                result = analyze_video(cand, self.cfg, rep, profile, llm, yt)
            except BotCheck as exc:
                blocked += 1
                rep.error("analysis", str(exc))
                if blocked >= 2 or len(candidates) == 1:
                    raise RuntimeError(str(exc)) from exc
                continue
            except Exception as exc:
                rep.error("analysis", f"Skipping {cand.get('input') or cand['video_id']}: {exc}")
                if len(candidates) == 1:
                    raise
                if not sources:
                    self.db.mark_processed(cand["video_id"], cand.get("title", ""), "failed")
                continue
            analyzed += 1
            vid = _vid(result)
            fresh = [c for c in result["clips"] if not self.brain.already_made(vid, c.start, c.end)]
            repeats += len(result["clips"]) - len(fresh)
            if len(fresh) < len(result["clips"]):
                rep.info("analysis", f"Skipped {len(result['clips']) - len(fresh)} moment(s) you already got before")
            found = result["clips"] = fresh
            self.db.mark_processed(cand["video_id"], result["meta"]["title"], "done" if found else "no_clips",
                                   len(found))
            pool += [(c, result) for c in found]
            rep.info("analysis", (f"{len(found)} strong clip{'s' if len(found) != 1 else ''} in this video - "
                                  if found else "Nothing in this video passed the strict review - ") +
                     f"{min(len(produced) + len(pool), want)}/{want} collected")
            # finding videos itself: every full layer is edited right away so clips arrive early.
            # videos you pasted are all watched first, so the best clips across them win.
            while not sources and len(pool) >= LAYER and len(produced) < want:
                edit_layer()

        while pool and len(produced) < want:
            edit_layer()
        if not produced and repeats:
            raise RuntimeError("You already got every strong moment in these videos - "
                               "paste other videos, or press Get clips to let it find new ones.")
        if not produced and analyzed and not pool:
            raise RuntimeError(f"No clip passed the strict review in the {analyzed} video(s) watched. "
                               "Try again later, choose other videos, or lower analysis.local_content_threshold.")
        if len(produced) < want:
            rep.info("editing", f"Made {len(produced)} of {want} clips - only these passed the strict review")
        return produced

    def _edit(self, result: dict, level: str | None, run_id: str = "", layer: int = 1) -> list[dict]:
        meta, clips = result["meta"], result["clips"]
        out_dir = self.cfg.path("paths.output_dir") / \
            "_".join(x for x in (time.strftime("%Y-%m-%d"), slug(meta.get("channel") or "", 20),
                                 slug(str(meta.get("id") or "video"), 40)) if x != "clip")
        out_dir.mkdir(parents=True, exist_ok=True)
        energy = result["signals"]["raw"].get("energy")
        comedy = float(result["signals"].get("comedy") or 0.0)
        taken = {int(m.group(1)) for f in out_dir.glob("clip_*.json") if (m := re.match(r"clip_(\d+)", f.name))}
        out = []
        for i, clip in enumerate(clips, 1):
            safe = self.cfg["editing"].get("censor_profanity", True)
            title, hook = (censor(clip.title), censor(clip.hook)) if safe else (clip.title, clip.hook)
            num = next(n for n in range(1, 1000) if n not in taken)  # never overwrite an earlier clip
            taken.add(num)
            name = f"clip_{num:02d}_{slug(title, 30)}"
            if level:
                preset, why = get_preset(level), "level you chose"
            else:
                chosen, why = choose_level(clip.category, clip.duration, comedy,
                                           (clip.signal_scores.get("energy") or 50) / 100)
                preset = get_preset(chosen)
            self.rep.progress("editing", (i - 1) / len(clips),
                              f"Editing clip {i}/{len(clips)} ({preset.name} edit: {why}): {title}")
            highlights = []
            if energy is not None:
                s, e = int(clip.start), int(clip.end)
                highlights = [p + s for p in peaks(energy[s:e], n=3, min_gap=8)]
            words = clip_words(result["transcript"]["words"], clip.start, clip.end)
            source, offset = result["video"], 0.0
            try:
                if meta.get("audio_only"):  # very long video: only the audio was downloaded
                    offset = max(0.0, clip.start - 3.0)
                    self.rep.progress("editing", (i - 1) / len(clips),
                                      f"Downloading just this clip in full quality ({clip.duration:.0f}s)...")
                    source = download_section(meta.get("webpage_url") or str(meta.get("id")), offset,
                                              clip.end + 3.0, result["video"].parent / f"section_{int(clip.start)}.mp4",
                                              log=lambda m: self.rep.info("editing", m))
                job = RenderJob(source=source, start=clip.start - offset, end=clip.end - offset,
                                words=[{**w, "s": w["s"] - offset, "e": w["e"] - offset} for w in words],
                                hook=clip.hook, emphasis=clip.emphasis_words, out_dir=out_dir, name=name,
                                highlights=[h - offset for h in highlights])
                info = render(job, preset, self.cfg)
            except Exception as exc:
                self.rep.error("editing", f"Render failed for {name}: {exc}")
                continue
            self.brain.record_made(_vid(result), clip.start, clip.end, clip.category,
                                   meta.get("channel") or "", name)
            info.update(level=preset.name, style_reason=why, run=run_id, layer=layer)
            write_post_files(out_dir, name, clip.to_dict(), info, meta, safe)
            item = {"folder": out_dir.name, "name": name, "title": title, "hook": hook, "category": clip.category,
                    "score": clip.final_score, "judge": clip.judge_score, **info}
            self.rep.emit(Event("clip", "editing", f"Clip ready: {title} (score {clip.final_score})",
                                data=item))
            out.append(item)
        self.rep.progress("editing", 1.0, "Editing complete")
        return out


def list_outputs(output_dir: Path) -> list[dict]:
    import json

    items = []
    files = sorted(output_dir.glob("*/clip_*.json"), key=lambda f: f.name)  # best clip of each video first
    files.sort(key=lambda f: f.parent.name, reverse=True)  # newest video first (stable sort)
    for meta_file in files:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        clip = meta.get("clip", {})
        items.append({"folder": meta_file.parent.name, "name": meta["name"], "video": meta["video"],
                      "thumbnail": meta.get("thumbnail"), "title": clip.get("title"), "hook": clip.get("hook"),
                      "score": clip.get("final_score"), "judge": clip.get("judge_score"),
                      "duration": meta.get("duration"), "level": meta.get("level"),
                      "style_reason": meta.get("style_reason", ""), "run": meta.get("run", ""),
                      "layer": meta.get("layer"),
                      "caption": meta.get("post_caption", ""), "reasons": clip.get("judge_reasons", ""),
                      "source": meta.get("source", {})})
    return items


def delete_clip(output_dir: Path, folder: str, name: str, brain: Brain | None = None) -> int:
    """Remove a finished clip and its files (video, thumbnail, caption, subtitles, info)."""
    if not re.fullmatch(r"[\w.-]+", folder) or not re.fullmatch(r"clip_[\w-]+", name):
        raise ValueError("bad clip name")
    d = output_dir / folder
    removed = 0
    meta_file = d / f"{name}.json"
    if brain and meta_file.exists():  # learn from what you throw away
        try:
            brain.learn_removed(json.loads(meta_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    for ext in (".mp4", ".jpg", ".txt", ".srt", ".json"):
        f = d / f"{name}{ext}"
        if f.exists():
            f.unlink()
            removed += 1
    if d.exists() and not any(d.iterdir()):
        d.rmdir()
    return removed
