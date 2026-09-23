"""The one-button pipeline: trends -> discovery -> full-video analysis -> editing."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .analysis import analyze_video, clip_words
from .analysis.download import source_key
from .analysis.signals import peaks
from .config import Config
from .db import Database
from .discovery import YouTubeAPI, discover
from .editing import RenderJob, get_preset, render, write_post_files
from .editing.safety import censor
from .events import Event, Reporter
from .llm import Claude
from .trends import run_trend_analysis
from .ytdl import BotCheck


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


class Pipeline:
    def __init__(self, cfg: Config, rep: Reporter | None = None) -> None:
        self.cfg = cfg
        self.rep = rep or Reporter()
        self.db = Database(cfg.path("paths.db"))
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
        level = level or self.cfg["editing"]["default_level"]
        preset = get_preset(level)
        want = max(1, min(50, int(want or self.cfg["editing"].get("clips_per_run", 5))))
        rep = self.rep

        if sources:
            profile = self.db.latest_profile()
            rep.info("analysis", f"Clipping {len(sources)} video{'s' if len(sources) > 1 else ''} you chose" +
                     (" (using the latest trend analysis)" if profile else ""))
            candidates = [{"video_id": source_key(s), "input": s, "title": "", "channel": ""} for s in sources]
            max_videos = len(candidates)
        else:
            # ---- Step 1: what goes viral right now (fresh every run)
            rep.info("trends", f"Step 1/3 - studying {self.cfg['trends']['min_videos']}+ short videos "
                               "for what goes viral right now")
            profile = run_trend_analysis(self.cfg, self.db, rep)
            # ---- Step 2: find long-form videos and watch them fully
            rep.info("discovery", "Step 2/3 - finding long-form YouTube videos")
            candidates = discover(self.cfg, self.db, rep, profile)
            if not candidates:
                raise RuntimeError("No suitable long-form videos found - widen discovery settings")
            max_videos = max(1, int(self.cfg["discovery"].get("max_videos_per_run", 8)))

        llm = self._llm()
        yt = YouTubeAPI(self.cfg.youtube_key) if self.cfg.youtube_key else None
        rep.info("analysis", f"Goal: the {want} best clip{'s' if want > 1 else ''}, from as many videos as it takes")
        pool: list[tuple] = []  # (clip, analysis result) from every video watched
        analyzed = blocked = 0
        for cand in candidates:
            # videos you pasted are all watched; when finding videos itself it stops once it has enough
            if analyzed >= max_videos or (not sources and len(pool) >= want):
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
            found = result["clips"]
            self.db.mark_processed(cand["video_id"], result["meta"]["title"], "done" if found else "no_clips",
                                   len(found))
            pool += [(c, result) for c in found]
            rep.info("analysis", (f"{len(found)} strong clip{'s' if len(found) != 1 else ''} in this video - "
                                  if found else "Nothing in this video passed the strict review - ") +
                     f"{min(len(pool), want)}/{want} collected")

        if not pool:
            raise RuntimeError(f"No clip passed the strict review in the {analyzed} video(s) watched. "
                               "Try again later, choose other videos, or lower analysis.local_content_threshold.")
        if len(pool) < want:
            rep.info("analysis", f"Only {len(pool)} clip(s) passed the strict review - editing those")
        best = sorted(pool, key=lambda cr: -cr[0].final_score)[:want]

        # ---- Step 3: edit the best clips, whichever videos they came from
        rep.info("editing", f"Step 3/3 - editing {len(best)} clip{'s' if len(best) > 1 else ''} "
                            f"from {len({id(r) for _, r in best})} video(s) at level '{preset.name}'")
        produced: list[dict] = []
        order, seen = [], set()  # videos in the order of their best clip
        for _, r in best:
            if id(r) not in seen:
                seen.add(id(r))
                order.append(r)
        for result in order:
            chosen = [c for c, r in best if r is result]
            produced += self._edit({**result, "clips": chosen}, preset)
        return produced

    def _edit(self, result: dict, preset) -> list[dict]:
        meta, clips = result["meta"], result["clips"]
        out_dir = self.cfg.path("paths.output_dir") / \
            "_".join(x for x in (time.strftime("%Y-%m-%d"), slug(meta.get("channel") or "", 20),
                                 slug(str(meta.get("id") or "video"), 40)) if x != "clip")
        out_dir.mkdir(parents=True, exist_ok=True)
        energy = result["signals"]["raw"].get("energy")
        out = []
        for i, clip in enumerate(clips, 1):
            safe = self.cfg["editing"].get("censor_profanity", True)
            title, hook = (censor(clip.title), censor(clip.hook)) if safe else (clip.title, clip.hook)
            name = f"clip_{i:02d}_{slug(title, 30)}"
            self.rep.progress("editing", (i - 1) / len(clips), f"Editing clip {i}/{len(clips)}: {title}")
            highlights = []
            if energy is not None:
                s, e = int(clip.start), int(clip.end)
                highlights = [p + s for p in peaks(energy[s:e], n=3, min_gap=8)]
            job = RenderJob(source=result["video"], start=clip.start, end=clip.end,
                            words=clip_words(result["transcript"]["words"], clip.start, clip.end),
                            hook=clip.hook, emphasis=clip.emphasis_words, out_dir=out_dir, name=name,
                            highlights=highlights)
            try:
                info = render(job, preset, self.cfg)
            except Exception as exc:
                self.rep.error("editing", f"Render failed for {name}: {exc}")
                continue
            info["level"] = preset.name
            write_post_files(out_dir, name, clip.to_dict(), info, meta, safe)
            item = {"folder": out_dir.name, "name": name, "title": title, "hook": hook,
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
                      "caption": meta.get("post_caption", ""), "reasons": clip.get("judge_reasons", ""),
                      "source": meta.get("source", {})})
    return items
