"""Local web app: one "Get clips" button, live progress, real-time uploads, finished clips."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import ytdl
from ..agents import BOARD, TEAM_SIZE
from ..config import Config
from ..discovery import poll_watchlist
from ..editing import LEVEL_NAMES
from ..events import Reporter
from ..pipeline import LAYER, MAX_CLIPS, Pipeline, delete_clip, list_outputs
from ..publish import PostingService
from ..trends import run_trend_analysis

TEMPLATE = Path(__file__).parent / "templates" / "index.html"

LEVEL_INFO = {
    "simple": ("A clean vertical cut with easy-to-read captions.",
               ["9:16 crop", "Captions", "Loudness fix"]),
    "normal": ("Follows the speaker's face, bold pop-in captions and a hook title.",
               ["Face tracking", "Pop-in captions", "Hook title", "Pause trim"]),
    "hard": ("Tight jump cuts with word-by-word captions and punch-in zooms.",
             ["Jump cuts", "Karaoke captions", "Eased zooms", "Voice cleanup", "Color grade"]),
    "professional": ("Polished like a pro editor: music under the voice, whooshes, progress bar.",
                     ["Smooth tracking", "Pattern zooms", "Music", "Whoosh SFX", "Vignette", "Progress bar"]),
    "extreme": ("Everything, plus a split screen with b-roll, flashes and faster pacing.",
                ["Gameplay split (your b-roll)", "Camera shake", "Flashes", "Push-in", "Faster pacing"]),
}


class RunRequest(BaseModel):
    level: str | None = None
    clips: int | None = None


class ConnectRequest(BaseModel):
    platform: str
    access_token: str
    user_id: str | None = None
    refresh_token: str | None = None
    client_key: str | None = None
    client_secret: str | None = None
    mode: str | None = None


class ScheduleRequest(BaseModel):
    folder: str
    name: str
    platforms: list[str]
    when: str | float = "best"
    caption: str | None = None


class AutoRequest(BaseModel):
    on: bool


class CookiesRequest(BaseModel):
    text: str


class ClipRequest(BaseModel):
    source: str
    level: str | None = None
    clips: int | None = None


def create_app(cfg: Config) -> FastAPI:
    rep = Reporter()
    pipe = Pipeline(cfg, rep)
    stop = threading.Event()

    posting = PostingService(cfg, pipe.db, rep, BOARD)

    def trend_scout() -> None:
        """Trend scouts, 24/7: keep the trend study fresh so a click starts straight at finding videos."""
        stop.wait(20)
        while not stop.is_set():
            t = cfg["trends"]
            fresh = pipe.fresh_trends()
            if fresh:
                age = (time.time() - fresh["created_at"]) / 60
                BOARD.beat("trend", f"Trends fresh ({fresh.get('n_videos', 0)} shorts studied {age:.0f} min ago) - "
                                    f"re-study in {max(0, float(t.get('reuse_hours', 6)) * 60 - age):.0f} min")
            elif not t.get("background_refresh", True):
                BOARD.beat("trend", "", reason="Background refresh is off (trends.background_refresh)")
            elif pipe.running:
                BOARD.beat("trend", "The current run is studying trends")
            else:
                try:
                    with BOARD.work("trend", "Studying what goes viral right now (background)"):
                        run_trend_analysis(cfg, pipe.db, rep)
                except Exception as exc:
                    BOARD.beat("trend", "", reason=f"Trend study failed ({str(exc)[:80]}) - retrying in 10 min")
                    stop.wait(600)
                    continue
            stop.wait(60)

    def video_scout() -> None:
        """Video scouts, 24/7: keep a queue of the most promising long videos ready for the next click."""
        stop.wait(45)
        while not stop.is_set():
            q = pipe.queue_fresh()
            if q is not None:
                BOARD.beat("scout", f"{len(q)} viral candidates queued - next search in "
                                    f"{max(0, int((pipe.queue_at + pipe.queue_hours * 3600 - time.time()) / 60))} min")
            elif pipe.running:
                BOARD.beat("scout", "Searching for the current run")
            else:
                try:
                    pipe.refill_queue()
                except Exception as exc:
                    BOARD.beat("scout", "", reason=f"YouTube search failed ({str(exc)[:80]}) - retrying in 10 min")
                    stop.wait(600)
                    continue
            stop.wait(60)

    def supervisor() -> None:
        """Keeps every agent alive: heartbeats for the agents that wait for work, and restarts any
        background loop that stopped."""
        while not stop.is_set():
            waiting = "Ready for the next video" if not pipe.running else "Ready - takes the next job the moment it comes"
            for role, duty in (("download", waiting), ("listen", waiting), ("audio", waiting), ("judge", waiting),
                               ("hook", "Ready to write hooks, captions and hashtags"),
                               ("director", "Ready to choose each clip's edit"),
                               ("editor", "Ready to edit - clips come in layers of 5" if pipe.running else "Ready to edit the next clips"),
                               ("review", "Ready to watch and fix every finished clip")):
                BOARD.beat(role, duty)
            b = pipe.brain.summary()
            BOARD.beat("brain", f"Memory: {b['made']} clips made, {b['removed']} removed - never repeats a moment")
            for name, (target, thread) in list(loops.items()):
                if not thread.is_alive() and not stop.is_set():
                    rep.info("agents", f"{name} stopped - restarting it")
                    loops[name] = (target, threading.Thread(target=target, daemon=True, name=name))
                    loops[name][1].start()
            stop.wait(15)

    loops: dict = {}
    BOARD.beat("trend", "Starting up - checking how fresh the trend study is")
    BOARD.beat("scout", "Starting up - first search for viral videos in a moment")

    def auto_schedule(ev) -> None:
        p = cfg.get("posting") or {}
        if ev.kind != "clip" or not p.get("auto_schedule"):
            return
        connected = [k for k, v in posting.accounts.status().items() if v["connected"] and k in p.get("platforms", [])]
        if connected:
            try:
                c = ev.data
                caption = (out_dir / c["folder"] / f"{c['name']}.txt").read_text(encoding="utf-8").strip()
                done = posting.schedule(c["folder"], c["name"], connected, "best", caption)
                rep.info("posting", "Scheduled " + ", ".join(
                    f"{d['platform'].title()} {time.strftime('%a %H:%M', time.localtime(d['scheduled_at']))}"
                    for d in done))
            except Exception as exc:
                rep.error("posting", f"Auto-schedule failed: {exc}")

    rep.subscribe(auto_schedule)

    def watcher() -> None:
        interval = max(1, cfg["discovery"]["watch_interval_minutes"]) * 60
        while not stop.is_set():
            if cfg["discovery"]["watch_channels"]:
                poll_watchlist(cfg, pipe.db, rep)
            stop.wait(interval)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for target, name in ((watcher, "upload-watcher"), (trend_scout, "trend-scouts"),
                             (video_scout, "video-scouts"), (lambda: posting.run_forever(stop), "publisher")):
            loops[name] = (target, threading.Thread(target=target, daemon=True, name=name))
            loops[name][1].start()
        threading.Thread(target=supervisor, daemon=True, name="supervisor").start()
        yield
        stop.set()

    app = FastAPI(title="AI Clipper", lifespan=lifespan)
    out_dir = cfg.path("paths.output_dir")
    app.mount("/media", StaticFiles(directory=str(out_dir)), name="media")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict:
        profile = pipe.db.latest_profile()
        return {
            "running": pipe.running,
            "levels": [{"name": n, "info": LEVEL_INFO[n][0], "tags": LEVEL_INFO[n][1]} for n in LEVEL_NAMES],
            "default_level": "auto", "brain": pipe.brain.summary(), "max_clips": MAX_CLIPS, "layer_size": LAYER,
            "default_clips": cfg["editing"].get("clips_per_run", 5),
            "min_videos": cfg["trends"]["min_videos"],
            "output_dir": str(out_dir.resolve()),
            "youtube_login": ytdl.login_status(),
            "keys": {"anthropic": bool(cfg.anthropic_key), "youtube": bool(cfg.youtube_key),
                     "apify": bool(cfg.apify_token)},
            "watch_channels": len(cfg["discovery"]["watch_channels"]),
            "trends": None if not profile else {
                "n_videos": profile["n_videos"], "n_viral": profile["n_viral"],
                "created_at": profile["created_at"], "auc": profile.get("model_auc"),
                "hooks": profile.get("hook_lift", [])[:5], "durations": profile.get("duration_lift", [])[:4],
                "terms": profile.get("top_viral_terms", [])[:15],
                "platforms": {k: v.get("videos", 0) for k, v in profile.get("platforms", {}).items()}},
        }

    @app.post("/api/run")
    def run(req: RunRequest) -> dict:
        if pipe.running:
            raise HTTPException(409, "Already running")
        level = req.level or "auto"  # the app picks the edit each clip needs
        if level not in (*LEVEL_NAMES, "auto"):
            raise HTTPException(400, f"Unknown level {level}")

        def job() -> None:
            try:
                pipe.run(level, req.clips)
            except Exception:
                pass  # already reported as an error event

        threading.Thread(target=job, daemon=True, name="pipeline").start()
        return {"started": True, "level": level}

    @app.post("/api/clip")
    def clip(req: ClipRequest) -> dict:
        if pipe.running:
            raise HTTPException(409, "Already running")
        level = req.level or "auto"  # the app picks the edit each clip needs
        if level not in (*LEVEL_NAMES, "auto"):
            raise HTTPException(400, f"Unknown level {level}")
        if not req.source.strip():
            raise HTTPException(400, "Paste a video link or a file path")

        def job() -> None:
            try:
                pipe.clip_video(req.source, level, req.clips)
            except Exception:
                pass  # already reported as an error event

        threading.Thread(target=job, daemon=True, name="pipeline").start()
        return {"started": True, "level": level}

    @app.get("/api/youtube-login")
    def youtube_login() -> dict:
        return ytdl.login_status()

    @app.post("/api/youtube-login")
    def youtube_login_upload(req: CookiesRequest) -> dict:
        try:
            n = ytdl.save_cookies(req.text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {**ytdl.login_status(), "cookies": n}

    @app.delete("/api/youtube-login")
    def youtube_login_forget() -> dict:
        ytdl.forget_cookies()
        return ytdl.login_status()

    @app.get("/api/events")
    async def events(request: Request, since: int = 0) -> StreamingResponse:
        async def stream():
            last = since
            while not await request.is_disconnected():
                for ev in rep.since(last):
                    last = ev.seq
                    yield f"data: {json.dumps(ev.to_dict())}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/clips")
    def clips() -> list[dict]:
        return list_outputs(out_dir)

    @app.delete("/api/clips/{folder}/{name}")
    def remove_clip(folder: str, name: str) -> dict:
        try:
            removed = delete_clip(out_dir, folder, name, pipe.brain)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not removed:
            raise HTTPException(404, "Clip not found")
        return {"removed": removed}

    @app.post("/api/stop")
    def stop_run() -> dict:
        if not pipe.running:
            return {"stopped": False}
        pipe.stop()
        return {"stopped": True}

    @app.get("/api/agents")
    def agents() -> dict:
        return {"size": TEAM_SIZE, "busy": BOARD.busy(), "agents": BOARD.snapshot()}

    @app.get("/api/posting")
    def posting_status() -> dict:
        posts = pipe.db.posts("scheduled_at > ?", (time.time() - 14 * 86400,))
        return {"accounts": posting.accounts.status(), "posts": posts[-100:],
                "auto_schedule": bool((cfg.get("posting") or {}).get("auto_schedule"))}

    @app.get("/api/posting/besttimes")
    def posting_besttimes(platform: str = "tiktok") -> dict:
        if platform not in ("tiktok", "instagram"):
            raise HTTPException(400, "Unknown platform")
        model = posting.timing(platform)
        grid = [[round(model.score(d, h), 3) for h in range(24)] for d in range(7)]
        nxt = model.best_slots(3, time.time() + 600, [])
        return {"grid": grid, "learned_from": len(model.results), "next": nxt}

    @app.post("/api/posting/connect")
    def posting_connect(req: ConnectRequest) -> dict:
        if req.platform not in ("tiktok", "instagram"):
            raise HTTPException(400, "Unknown platform")
        try:
            return posting.connect(req.platform, req.model_dump(exclude={"platform"}))
        except Exception as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/posting/connect/{platform}")
    def posting_disconnect(platform: str) -> dict:
        posting.accounts.save(platform, None)
        return posting.accounts.status()

    @app.post("/api/posting/schedule")
    def posting_schedule(req: ScheduleRequest) -> list[dict]:
        txt = out_dir / req.folder / f"{req.name}.txt"
        if not re.fullmatch(r"[\w.-]+", req.folder) or not re.fullmatch(r"clip_[\w-]+", req.name) or \
                not (out_dir / req.folder / f"{req.name}.mp4").exists():
            raise HTTPException(404, "Clip not found")
        caption = req.caption if req.caption is not None else \
            (txt.read_text(encoding="utf-8").strip() if txt.exists() else "")
        try:
            return posting.schedule(req.folder, req.name, req.platforms, req.when, caption)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/posting/auto")
    def posting_auto(req: AutoRequest) -> dict:
        cfg.setdefault("posting", {})["auto_schedule"] = bool(req.on)
        return {"auto_schedule": bool(req.on)}

    @app.delete("/api/posting/post/{post_id}")
    def posting_cancel(post_id: int) -> dict:
        pipe.db.update_post(post_id, status="cancelled")
        return {"cancelled": post_id}

    @app.get("/api/uploads")
    def uploads() -> list[dict]:
        return pipe.db.recent_uploads(30)

    return app
