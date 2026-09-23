"""Local web app: one "Get clips" button, live progress, real-time uploads, finished clips."""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import ytdl
from ..config import Config
from ..discovery import poll_watchlist
from ..editing import LEVEL_NAMES
from ..events import Reporter
from ..pipeline import Pipeline, list_outputs

TEMPLATE = Path(__file__).parent / "templates" / "index.html"

LEVEL_INFO = {
    "simple": ("A clean vertical cut with easy-to-read captions.",
               ["9:16 crop", "Captions", "Loudness fix"]),
    "normal": ("Follows the speaker's face, bold pop-in captions and a hook title.",
               ["Face tracking", "Pop-in captions", "Hook title", "Pause trim"]),
    "hard": ("Tight jump cuts with word-by-word captions and punch-in zooms.",
             ["Jump cuts", "Karaoke captions", "Punch-in zooms", "Color grade"]),
    "professional": ("Polished like a pro editor: music under the voice, whooshes, progress bar.",
                     ["Smooth tracking", "Pattern zooms", "Music", "Whoosh SFX", "Progress bar"]),
    "extreme": ("Everything, plus a split screen with b-roll, flashes and faster pacing.",
                ["B-roll split", "Flashes", "Push-in", "Faster pacing"]),
}


class RunRequest(BaseModel):
    level: str | None = None
    clips: int | None = None


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

    def watcher() -> None:
        interval = max(1, cfg["discovery"]["watch_interval_minutes"]) * 60
        while not stop.is_set():
            if cfg["discovery"]["watch_channels"]:
                poll_watchlist(cfg, pipe.db, rep)
            stop.wait(interval)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        thread = threading.Thread(target=watcher, daemon=True, name="upload-watcher")
        thread.start()
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
            "default_level": cfg["editing"]["default_level"],
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
        level = req.level or cfg["editing"]["default_level"]
        if level not in LEVEL_NAMES:
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
        level = req.level or cfg["editing"]["default_level"]
        if level not in LEVEL_NAMES:
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

    @app.get("/api/uploads")
    def uploads() -> list[dict]:
        return pipe.db.recent_uploads(30)

    return app
