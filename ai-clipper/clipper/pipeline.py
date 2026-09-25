"""The one-button pipeline: trends -> discovery -> full-video analysis -> editing."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .agents import BOARD, parallel_videos
from .analysis import analyze_video, clip_words
from .analysis import transcribe as transcribe_mod
from .analysis.download import download_section, source_key
from .analysis.signals import peaks
from .brain import Brain
from .config import Config
from .db import Database
from .discovery import YouTubeAPI, discover
from .editing import RenderJob, get_preset, render, write_post_files
from .editing.auto import choose_level, tune
from .editing.safety import censor
from .events import Event, Reporter
from .llm import Claude
from .trends import run_trend_analysis
from .media import CANCEL, Cancelled, check_cancel
from .ytdl import BotCheck
from .crew.package import package_clip, write_run_package


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
        self.profile: dict | None = None
        self.last_package: Path | None = None

    queue_hours = 2.0
    queue_at = 0.0
    queue: list[dict] | None = None

    def queue_fresh(self) -> list[dict] | None:
        """Videos the video scouts found in the background, if recent enough to use."""
        if self.queue is None or time.time() - self.queue_at > self.queue_hours * 3600:
            return None
        return [c for c in self.queue if not self.db.is_processed(c["video_id"])]

    def refill_queue(self) -> None:
        profile = self.fresh_trends() or self.db.latest_profile()
        want = max(12, int(self.cfg["discovery"].get("max_videos_per_run", 8)) * 3)
        found = discover(self.cfg, self.db, self.rep, profile, want, board=BOARD)
        self.queue, self.queue_at = found, time.time()

    def fresh_trends(self) -> dict | None:
        """The latest trend study, if the trend scouts made it recently enough to reuse."""
        profile = self.db.latest_profile()
        t = self.cfg["trends"]
        hours = float(t.get("reuse_hours", 6))
        if t.get("live", {}).get("enabled"):  # live data goes stale fast
            hours = min(hours, float(t["live"].get("fresh_minutes", 90)) / 60)
        if profile and time.time() - profile.get("created_at", 0) < hours * 3600:
            return profile
        return None

    def _llm(self) -> Claude | None:
        if not self.cfg.anthropic_key:
            self.rep.info("analysis", "Free mode: the built-in judge reads the whole transcript and picks clips "
                                      "(add an ANTHROPIC_API_KEY to also use Claude)")
            return None
        return Claude(self.cfg["llm"]["model"], self.cfg["llm"]["effort"])

    def run(self, level: str | None = None, clips: int | None = None) -> list[dict]:
        """Everything that happens after pressing "Get clips"."""
        return self._guarded(level, [], clips)

    def stop(self) -> None:
        """The Stop button: downloads, listening and renders in progress are halted right away."""
        CANCEL.set()

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
        CANCEL.clear()
        started = time.time()
        try:
            produced = self._run(level, sources, clips)
            minutes = (time.time() - started) / 60
            self.rep.emit(Event("done", "editing", f"Finished: {len(produced)} clips in {minutes:.1f} min",
                                data={"clips": produced}))
            return produced
        except Cancelled:
            self.rep.emit(Event("stopped", "pipeline", "Stopped - the clips finished so far are kept"))
            return []
        except Exception as exc:
            if CANCEL.is_set():
                self.rep.emit(Event("stopped", "pipeline", "Stopped - the clips finished so far are kept"))
                return []
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
            per_video = max(1, int(self.cfg["editing"].get("clips_per_video_per_run", 2)))
            max_videos = max(int(self.cfg["discovery"].get("max_videos_per_run", 8)),
                             min(80, -(-want // per_video) + 4))
            # ---- Step 1: what goes viral right now (fresh every run)
            profile = self.fresh_trends()
            if profile:
                age = (time.time() - profile.get("created_at", time.time())) / 60
                rep.info("trends", f"Step 1/3 - using the trend study the trend scouts made {age:.0f} min ago "
                                   f"({profile.get('n_videos', 0)} short videos)")
            else:
                lv = self.cfg["trends"].get("live", {})
                if lv.get("enabled"):
                    rep.info("trends", f"Step 1/3 - live internet scan of TikTok, Instagram and YouTube Shorts "
                                       f"({lv.get('min_minutes', 5)}-{lv.get('max_minutes', 10)} min): what is going "
                                       "viral right now, and whose clips")
                else:
                    rep.info("trends", f"Step 1/3 - studying {self.cfg['trends']['min_videos']}+ short videos "
                                       "for what goes viral right now")
                profile = run_trend_analysis(self.cfg, self.db, rep)
            # ---- Step 2: find long-form videos and watch them fully
            queued = self.queue_fresh()
            if queued and len(queued) >= min(max_videos, 6):
                rep.info("discovery", f"Step 2/3 - the video scouts already queued {len(queued)} viral candidates")
                candidates = queued
            else:
                rep.info("discovery", "Step 2/3 - finding long-form YouTube videos (famous creators first)")
                candidates = discover(self.cfg, self.db, rep, profile, max_videos, board=BOARD)
                self.queue, self.queue_at = candidates, time.time()
            if not candidates:
                raise RuntimeError("No suitable long-form videos found - widen discovery settings")

        llm = self._llm()
        yt = YouTubeAPI(self.cfg.youtube_key) if self.cfg.youtube_key else None
        run_id = time.strftime("%Y-%m-%d %H:%M:%S")
        self.profile = profile
        videos: list[dict] = []  # each watched video's coverage report, for the final package
        rep.info("analysis", f"Goal: {want} clip{'s' if want > 1 else ''}" +
                 (f" in {layers} layers of up to {LAYER}" if layers > 1 else "") +
                 " - from as many videos as it takes")
        pool: list[tuple] = []  # (clip, analysis result) watched but not edited yet
        produced: list[dict] = []
        analyzed = blocked = repeats = 0

        # variety: when finding videos itself, at most `cap` clips come from any one video per run, so a
        # batch is spread over many videos (pasted videos: as many as needed from what you chose)
        if sources:
            cap = want if len(sources) == 1 else max(2, -(-want // len(sources)))
        else:
            cap = max(1, int(self.cfg["editing"].get("clips_per_video_per_run", 2)))
        made_from: Counter = Counter()

        def room(counts: Counter) -> list:
            return [cr for cr in pool if counts[_vid(cr[1])] < cap]

        def ready() -> int:  # clips that can still be used under the per-video limit
            per: Counter = Counter(_vid(r) for _, r in pool)
            return sum(min(n, max(0, cap - made_from[v])) for v, n in per.items())

        def edit_layer() -> None:
            nonlocal pool
            n = min(LAYER, want - len(produced))
            take, kinds = [], Counter(p.get("category") for p in produced)
            counts = Counter(made_from)
            for _ in range(n):  # best first, but learned taste + variety move clips up or down
                options = room(counts)
                if not options:
                    break
                best = max(options, key=lambda cr: cr[0].final_score + self.brain.adjust(
                    cr[0].category, cr[1]["meta"].get("channel") or "", cr[0].duration, kinds))
                pool.remove(best)
                take.append(best)
                kinds[best[0].category] += 1
                counts[_vid(best[1])] += 1
            if not take:
                pool = []  # everything left comes from videos that already gave their share
                return
            made_from.update(_vid(r) for _, r in take)
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

        # the agent team watches several videos at once (downloads overlap with listening); results are
        # handled here, in one place, as each video finishes
        todo = iter(candidates)
        running: dict = {}
        workers = parallel_videos(self.cfg)
        if workers > 1:
            rep.info("analysis", f"Agent team: watching {workers} videos at the same time")
        transcribe_mod.THREAD_SHARE = workers

        def start_next(ex) -> bool:
            if CANCEL.is_set() or analyzed + len(running) >= max_videos or len(produced) >= want:
                return False
            cand = next(todo, None)
            if cand is None:
                return False
            rep.info("analysis", f"Analyzing: {cand['channel']} - {cand['title']}".rstrip(" -")
                     if cand["title"] else f"Analyzing: {cand.get('input') or cand['video_id']}")
            # what was already chosen (this run, earlier runs): the duplicate verifier compares against it
            peers = [(f"{_vid(r)}@{c.start:.0f}", c.summary) for c, r in pool] + \
                [(p.get("name", ""), p.get("summary", "")) for p in produced] + self.brain.history_texts()
            running[ex.submit(analyze_video, cand, self.cfg, rep, profile, llm, yt, brain=self.brain,
                              peers=peers)] = cand
            return True

        with ThreadPoolExecutor(max_workers=workers) as ex:
            while len(running) < workers and start_next(ex):
                pass
            while running:
                finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in finished:
                    cand = running.pop(fut)
                    try:
                        result = fut.result()
                    except Cancelled:
                        for f in running:
                            f.cancel()
                        raise
                    except BotCheck as exc:
                        blocked += 1
                        rep.error("analysis", str(exc))
                        if blocked >= 2 or len(candidates) == 1:
                            for f in running:
                                f.cancel()
                            raise RuntimeError(str(exc)) from exc
                        start_next(ex)
                        continue
                    except Exception as exc:
                        rep.error("analysis", f"Skipping {cand.get('input') or cand['video_id']}: {exc}")
                        if len(candidates) == 1:
                            raise
                        if not sources:
                            self.db.mark_processed(cand["video_id"], cand.get("title", ""), "failed")
                        start_next(ex)
                        continue
                    analyzed += 1
                    vid = _vid(result)
                    if result.get("crew"):
                        videos.append({"id": vid, "title": result["meta"].get("title"),
                                       "channel": result["meta"].get("channel"), **result["crew"]})
                    with BOARD.work("brain", "Checking the shared memory for repeats"):
                        fresh = [c for c in result["clips"] if not self.brain.already_made(vid, c.start, c.end)]
                    repeats += len(result["clips"]) - len(fresh) + (result.get("crew") or {}).get("repeats", 0)
                    if len(fresh) < len(result["clips"]):
                        rep.info("analysis", f"Skipped {len(result['clips']) - len(fresh)} moment(s) "
                                             "you already got before")
                    found = result["clips"] = fresh
                    self.db.mark_processed(cand["video_id"], result["meta"]["title"],
                                           "done" if found else "no_clips", len(found))
                    pool += [(c, result) for c in found]
                    rep.info("analysis", (f"{len(found)} strong clip{'s' if len(found) != 1 else ''} in this "
                                          "video - " if found else "Nothing in this video passed the strict "
                                                                   "review - ") +
                             f"{min(len(produced) + len(pool), want)}/{want} collected")
                    # finding videos itself: every full layer is edited right away so clips arrive early.
                    # videos you pasted are all watched first, so the best clips across them win.
                    while not sources and ready() >= LAYER and len(produced) < want and not CANCEL.is_set():
                        edit_layer()
                    start_next(ex)

        check_cancel()
        while pool and len(produced) < want and not CANCEL.is_set():
            edit_layer()
        check_cancel()
        if not produced and repeats:
            raise RuntimeError("You already got every strong moment in these videos - "
                               "paste other videos, or press Get clips to let it find new ones.")
        if not produced and analyzed and not pool:
            raise RuntimeError(f"No clip passed the strict review in the {analyzed} video(s) watched. "
                               "Try again later, choose other videos, or lower analysis.local_content_threshold.")
        if len(produced) < want:
            rep.info("editing", f"Made {len(produced)} of {want} clips - only these passed the strict review")
        if produced:
            self._final_package(run_id, produced, videos)
        return produced

    def _final_package(self, run_id: str, produced: list[dict], videos: list[dict]) -> None:
        """Ranked clips, ready-to-post versions and metadata, posting plan, coverage proof."""
        try:
            from .publish import PostingService

            timing = PostingService(self.cfg, self.db, self.rep).timing
            folder = write_run_package(self.cfg.path("paths.output_dir"), run_id, produced, videos, self.cfg, timing)
            self.last_package = folder
            self.rep.emit(Event("package", "editing", f"Final package ready: {folder.name} (package.json + package.md)",
                                data={"folder": folder.name}))
        except Exception as exc:
            self.rep.error("editing", f"Could not write the final package: {exc}")

    def _edit(self, result: dict, level: str | None, run_id: str = "", layer: int = 1) -> list[dict]:
        meta, clips = result["meta"], result["clips"]
        out_dir = self.cfg.path("paths.output_dir") / \
            "_".join(x for x in (time.strftime("%Y-%m-%d"), slug(meta.get("channel") or "", 20),
                                 slug(str(meta.get("id") or "video"), 40)) if x != "clip")
        out_dir.mkdir(parents=True, exist_ok=True)
        if result.get("video"):  # the crew's audit trail and coverage proof travel with the clips
            for f in ("crew_audit.json", "coverage.json"):
                src = Path(result["video"]).parent / f
                if src.exists():
                    shutil.copy2(src, out_dir / f)
        energy = result["signals"]["raw"].get("energy")
        comedy = float(result["signals"].get("comedy") or 0.0)
        taken = {int(m.group(1)) for f in out_dir.glob("clip_*.json") if (m := re.match(r"clip_(\d+)", f.name))}
        safe = self.cfg["editing"].get("censor_profanity", True)
        plans = []
        director = BOARD.work("director", f"Choosing the edit for {len(clips)} clip(s)")
        director.__enter__()
        for clip in clips:  # decide names and edits in order ...
            title, hook = (censor(clip.title), censor(clip.hook)) if safe else (clip.title, clip.hook)
            num = next(n for n in range(1, 1000) if n not in taken)  # never overwrite an earlier clip
            taken.add(num)
            if level:
                preset, why = get_preset(level), "level you chose"
            else:
                chosen, why = choose_level(clip.category, clip.duration, comedy,
                                           (clip.signal_scores.get("energy") or 50) / 100)
                preset = tune(get_preset(chosen), clip.category)  # calm captions for heartfelt moments
            plans.append((clip, title, hook, f"clip_{num:02d}_{slug(title, 30)}", preset, why))
        director.__exit__(None, None, None)
        done = [0]
        lock = threading.Lock()

        def work(plan) -> dict | None:  # ... then edit, review and fix several clips at the same time
            clip, title, hook, name, preset, why = plan
            if CANCEL.is_set():
                return None
            self.rep.info("editing", f"Editing: {title} ({preset.name} edit: {why})")
            slot = BOARD.work("editor", f"Editing: {title}")
            slot.__enter__()
            highlights = []
            if energy is not None:
                s, e = int(clip.start), int(clip.end)
                highlights = [p + s for p in peaks(energy[s:e], n=3, min_gap=8)]
            words = clip_words(result["transcript"]["words"], clip.start, clip.end)
            source, offset = result["video"], 0.0
            try:
                if meta.get("audio_only"):  # very long video: only the audio was downloaded
                    offset = max(0.0, clip.start - 3.0)
                    self.rep.info("editing", f"Downloading just this clip in full quality ({clip.duration:.0f}s)...")
                    source = download_section(meta.get("webpage_url") or str(meta.get("id")), offset,
                                              clip.end + 3.0, result["video"].parent / f"section_{int(clip.start)}.mp4",
                                              log=lambda m: self.rep.info("editing", m))
                job = RenderJob(source=source, start=clip.start - offset, end=clip.end - offset,
                                words=[{**w, "s": w["s"] - offset, "e": w["e"] - offset} for w in words],
                                hook=clip.hook, emphasis=clip.emphasis_words, out_dir=out_dir, name=name,
                                highlights=[h - offset for h in highlights])
                info = render(job, preset, self.cfg, log=lambda m: self.rep.info("editing", f"{title}: {m}"))
            except Cancelled:
                return None
            except Exception as exc:
                self.rep.error("editing", f"Render failed for {name}: {exc}")
                return None
            finally:
                slot.__exit__(None, None, None)
            self.brain.record_made(_vid(result), clip.start, clip.end, clip.category,
                                   meta.get("channel") or "", name, clip.summary)
            info.update(level=preset.name, style_reason=why, run=run_id, layer=layer)
            write_post_files(out_dir, name, clip.to_dict(), info, meta, safe)
            try:  # production agents: platform versions, metadata options, thumbnails, recommendation
                pkg = package_clip(out_dir, name, {**clip.to_dict(), "title": title, "hook": hook}, info, meta,
                                   self.cfg, self.profile, offset)
            except Cancelled:
                return None
            except Exception as exc:
                self.rep.error("editing", f"Packaging {name} failed: {exc}")
                pkg = {"status": clip.status, "crew": clip.crew}
            item = {"folder": out_dir.name, "name": name, "title": title, "hook": hook, "category": clip.category,
                    "score": clip.final_score, "judge": clip.judge_score, "status": clip.status,
                    "summary": clip.summary, "package": pkg, **info,
                    "start": round(clip.start, 3), "end": round(clip.end, 3)}  # where it sits in the source
            rv = info.get("review", {})
            checked = "self-review passed" if rv.get("ok", True) else "review: " + "; ".join(rv.get("problems", []))
            if rv.get("fixed"):
                checked += f" (auto-fixed: {', '.join(rv['fixed'])})"
            with lock:
                done[0] += 1
                self.rep.progress("editing", done[0] / len(plans), f"Edited {done[0]}/{len(plans)}")
            state = "draft - held for your review" if clip.status == "review" else "draft"
            self.rep.emit(Event("clip", "editing", f"Clip ready ({state}): {title} (score {clip.final_score}) - {checked}",
                                data=item))
            return item

        workers = render_workers(self.cfg, len(plans))
        if workers > 1:
            self.rep.info("editing", f"Editing {len(plans)} clips, {workers} at a time")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(work, plans))
        else:
            results = [work(p) for p in plans]
        self.rep.progress("editing", 1.0, "Editing complete")
        return [r for r in results if r]


def render_workers(cfg, n_clips: int) -> int:
    """How many clips to edit at once: each edit already keeps several cores busy, and parts of the
    effects chain are single-threaded, so ~1 clip per 4 cores (at most 3) finishes a layer fastest."""
    n = cfg["editing"].get("parallel_renders", "auto")
    if n in (None, "auto"):
        n = min(3, max(1, (os.cpu_count() or 4) // 4))
    return max(1, min(int(n), n_clips))


def list_outputs(output_dir: Path) -> list[dict]:
    import json

    items = []
    files = sorted(output_dir.glob("*/clip_*.json"), key=lambda f: f.name)  # best clip of each video first
    files.sort(key=lambda f: f.parent.name, reverse=True)  # newest video first (stable sort)
    for meta_file in files:
        if meta_file.name.endswith(".package.json"):
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        clip = meta.get("clip", {})
        crew = clip.get("crew") or {}
        items.append({"folder": meta_file.parent.name, "name": meta["name"], "video": meta["video"],
                      "thumbnail": meta.get("thumbnail"), "title": clip.get("title"), "hook": clip.get("hook"),
                      "score": clip.get("final_score"), "judge": clip.get("judge_score"),
                      "duration": meta.get("duration"), "level": meta.get("level"),
                      "style_reason": meta.get("style_reason", ""), "run": meta.get("run", ""),
                      "layer": meta.get("layer"), "review": meta.get("review"),
                      "caption": meta.get("post_caption", ""), "reasons": clip.get("judge_reasons", ""),
                      "source": meta.get("source", {}), "status": crew.get("status") or clip.get("status") or "approved",
                      "confidence": crew.get("confidence"), "support": crew.get("support"),
                      "dissent": crew.get("dissent"), "risks": len(crew.get("risks") or []),
                      "review_because": crew.get("review_because") or [],
                      "package": (meta_file.parent / f"{meta['name']}.package.json").exists()})
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
    for ext in (".mp4", ".jpg", ".txt", ".srt", ".json", ".vtt", ".speakers.srt", ".package.json", ".tiktok.mp4",
                ".instagram.mp4", ".youtube.mp4", ".thumb2.jpg", ".thumb3.jpg", ".thumb4.jpg"):
        f = d / f"{name}{ext}"
        if f.exists():
            f.unlink()
            removed += 1
    left = {f.name for f in d.iterdir()} if d.exists() else set()
    if left and left <= {"crew_audit.json", "coverage.json"}:  # the video's audit goes with its last clip
        for n in left:
            (d / n).unlink()
    if d.exists() and not any(d.iterdir()):
        d.rmdir()
    return removed
