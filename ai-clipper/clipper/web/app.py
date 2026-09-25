"""Local web app: one "Get clips" button, live progress, real-time uploads, finished clips."""
from __future__ import annotations

import asyncio
import os
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

from .. import APP_VERSION, ytdl
from ..agents import BOARD, DIVISIONS, TEAM_SIZE
from ..config import Config
from ..crew import CREW
from ..crew.package import publish_check
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
            crew_idle = "Ready - takes the next assignment from the registry the moment one is queued"
            for role, duty in (("download", waiting), ("listen", waiting),
                               ("hookwriter", "Ready to write hook options in the speaker's own words"),
                               ("titles", "Ready to write title and description options"),
                               ("captions", "Ready to write captions for each platform"),
                               ("hashtags", "Ready to build hashtag sets"), ("cta", "Ready to write calls to action"),
                               ("thumbs", "Ready to pick cover frames"),
                               ("subtitles", "Ready to write SRT / VTT subtitles with speaker labels"),
                               ("director", "Ready to choose each clip's edit"),
                               ("editor", "Ready to edit - clips come in layers of 5" if pipe.running else "Ready to edit the next clips"),
                               ("inspector", "Ready to watch and fix every finished clip"),
                               ("tiktok", "Ready to make and check TikTok versions"),
                               ("reels", "Ready to make and check Reels versions"),
                               ("shorts", "Ready to make and check Shorts versions"),
                               ("strategist", "Ready to pick each clip's best platform and audience"),
                               ("order", "Ready to plan the posting order"),
                               ("compliance", "Auto-posting is " + ("on: only fully cleared clips go out" if
                                              (cfg.get("posting") or {}).get("auto_schedule") else
                                              "off: every clip stays a draft for your approval"))):
                BOARD.beat(role, duty)
            for role, *_ in (r for r in __import__("clipper.agents", fromlist=["ROLES"]).ROLES
                             if r[1] in ("command", "full", "section", "gate", "verify")):
                BOARD.beat(role, crew_idle)
            CREW.start()  # restarts any crew agent whose thread died
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
        """Clips are drafts. Only when automatic posting is on, the accounts are connected and the compliance
        officer clears every check does a clip get scheduled - anything else waits for a person."""
        p = cfg.get("posting") or {}
        if ev.kind != "clip" or not p.get("auto_schedule"):
            return
        c = ev.data
        ok, why = publish_check(c, cfg)
        if not ok:
            rep.info("posting", f"Kept as a draft for your review: {c.get('title', c['name'])} - {why[0]}")
            return
        connected = [k for k, v in posting.accounts.status().items() if v["connected"] and k in p.get("platforms", [])]
        if not connected:
            rep.info("posting", f"{c.get('title', c['name'])} is cleared to post, but no account is connected")
            return
        try:
            credit = (out_dir / c["folder"] / f"{c['name']}.txt").read_text(encoding="utf-8").strip()
            caps = ((c.get("package") or {}).get("metadata") or {}).get("captions") or {}
            done = []
            for plat in connected:  # each platform gets its own caption
                cap = caps.get(plat) or credit
                if "Credit:" in credit and "Credit:" not in cap:
                    cap += "\n\n" + credit[credit.index("Credit:"):]
                done += posting.schedule(c["folder"], c["name"], [plat], "best", cap)
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
        CREW.start()  # the crew's agents run for as long as the app is open
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
    def index() -> HTMLResponse:
        # never let the browser show a page cached from an older version of the app
        return HTMLResponse(TEMPLATE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/api/version")
    def version() -> dict:
        return {"app": "ai-clipper", "version": APP_VERSION, "agents": TEAM_SIZE}

    @app.post("/api/shutdown")
    def shutdown(request: Request) -> dict:
        """Lets a newer copy of the app close this one (only from this computer)."""
        if request.client is None or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "Only from this computer")
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"closing": True}

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

    @app.get("/api/agents/{agent_id}")
    def agent_detail(agent_id: str) -> dict:
        a = next((x for x in BOARD.snapshot() if x["id"] == agent_id), None)
        if a is None:
            raise HTTPException(404, "No such agent")
        return {**a, "history": BOARD.history(agent_id)}

    @app.get("/api/agents")
    def agents(after: int = 0) -> dict:
        return {"feed": BOARD.feed(after), "size": TEAM_SIZE, "busy": BOARD.busy(), "agents": BOARD.snapshot(),
                "divisions": [{"key": k, "name": n} for k, n in DIVISIONS], "registry": CREW.registry.stats(),
                "restarts": CREW.restarts}

    @app.get("/api/clips/{folder}/{name}/package")
    def clip_package(folder: str, name: str) -> dict:
        f = out_dir / folder / f"{name}.package.json"
        if not re.fullmatch(r"[\w.-]+", folder) or not re.fullmatch(r"clip_[\w-]+", name) or not f.exists():
            raise HTTPException(404, "No package for this clip")
        pkg = json.loads(f.read_text(encoding="utf-8"))
        ok, why = publish_check({"name": name, "package": pkg, "review": pkg.get("export_review")}, cfg)
        return {**pkg, "publish_ready": ok, "publish_blockers": why}

    @app.get("/api/package/latest")
    def latest_package() -> dict:
        root = out_dir / "_packages"
        runs = sorted(root.glob("*/package.json"), key=lambda f: f.stat().st_mtime) if root.exists() else []
        if not runs:
            raise HTTPException(404, "No package yet - press Get clips")
        return {"folder": runs[-1].parent.name, **json.loads(runs[-1].read_text(encoding="utf-8"))}

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
