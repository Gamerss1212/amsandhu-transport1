"""The one-button pipeline: trends -> discovery -> full-video analysis -> editing."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .analysis import analyze_video, clip_words
from .analysis.signals import peaks
from .config import Config
from .db import Database
from .discovery import YouTubeAPI, discover
from .editing import RenderJob, get_preset, render, write_post_files
from .events import Event, Reporter
from .llm import Claude
from .trends import run_trend_analysis


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
            self.rep.info("analysis", "ANTHROPIC_API_KEY not set - using signal-only clip picking "
                                      "(much less accurate; the strict AI judge is skipped)")
            return None
        return Claude(self.cfg["llm"]["model"], self.cfg["llm"]["effort"])

    def run(self, level: str | None = None) -> list[dict]:
        """Everything that happens after pressing "Get clips"."""
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("A run is already in progress")
        self.running = True
        started = time.time()
        produced: list[dict] = []
        try:
            level = level or self.cfg["editing"]["default_level"]
            preset = get_preset(level)
            rep = self.rep

            # ---- Step 1: what goes viral right now (fresh every run)
            rep.info("trends", "Step 1/3 - analyzing TikTok + Instagram for what goes viral")
            profile = run_trend_analysis(self.cfg, self.db, rep)

            # ---- Step 2: find long-form videos and watch them fully
            rep.info("discovery", "Step 2/3 - finding long-form YouTube videos")
            candidates = discover(self.cfg, self.db, rep, profile)
            if not candidates:
                raise RuntimeError("No suitable long-form videos found - widen discovery settings")

            llm = self._llm()
            yt = YouTubeAPI(self.cfg.youtube_key) if self.cfg.youtube_key else None
            wanted = self.cfg["discovery"]["videos_per_run"]
            done = 0
            for cand in candidates[: wanted * 3]:
                if done >= wanted:
                    break
                rep.info("analysis", f"Analyzing: {cand['channel']} - {cand['title']}")
                try:
                    result = analyze_video(cand, self.cfg, rep, profile, llm, yt)
                except Exception as exc:
                    rep.error("analysis", f"Skipping {cand['video_id']}: {exc}")
                    self.db.mark_processed(cand["video_id"], cand.get("title", ""), "failed")
                    continue
                if not result["clips"]:
                    rep.info("analysis", "Nothing in this video passed the strict review - trying the next one")
                    self.db.mark_processed(cand["video_id"], result["meta"]["title"], "no_clips")
                    continue

                # ---- Step 3: edit
                rep.info("editing", f"Step 3/3 - editing {len(result['clips'])} clips at level '{preset.name}'")
                clips = self._edit(result, preset)
                produced += clips
                self.db.mark_processed(cand["video_id"], result["meta"]["title"], "done", len(clips))
                done += 1

            minutes = (time.time() - started) / 60
            rep.emit(Event("done", "editing", f"Finished: {len(produced)} clips in {minutes:.1f} min",
                           data={"clips": produced}))
            return produced
        except Exception as exc:
            self.rep.emit(Event("error", "pipeline", str(exc)))
            raise
        finally:
            self.running = False
            self.lock.release()

    def _edit(self, result: dict, preset) -> list[dict]:
        meta, clips = result["meta"], result["clips"]
        out_dir = self.cfg.path("paths.output_dir") / \
            f"{time.strftime('%Y-%m-%d')}_{slug(meta.get('channel', ''), 20)}_{meta.get('id', 'video')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        energy = result["signals"]["raw"].get("energy")
        out = []
        for i, clip in enumerate(clips, 1):
            name = f"clip_{i:02d}_{slug(clip.title, 30)}"
            self.rep.progress("editing", (i - 1) / len(clips), f"Editing clip {i}/{len(clips)}: {clip.title}")
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
            write_post_files(out_dir, name, clip.to_dict(), info, meta)
            item = {"folder": out_dir.name, "name": name, "title": clip.title, "hook": clip.hook,
                    "score": clip.final_score, "judge": clip.judge_score, **info}
            self.rep.emit(Event("clip", "editing", f"Clip ready: {clip.title} (score {clip.final_score})",
                                data=item))
            out.append(item)
        self.rep.progress("editing", 1.0, "Editing complete")
        return out


def list_outputs(output_dir: Path) -> list[dict]:
    import json

    items = []
    for meta_file in sorted(output_dir.glob("*/clip_*.json"), reverse=True):
        try:
            meta = json.loads(meta_file.read_text())
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
