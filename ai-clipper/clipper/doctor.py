"""`python -m clipper doctor` - check that everything the pipeline needs is in place."""
from __future__ import annotations

import importlib
import subprocess
import time

from .config import Config
from .db import Database
from .media import ffmpeg_exe


def doctor(cfg: Config) -> bool:
    ok = True

    def check(label: str, passed: bool, hint: str = "", required: bool = True) -> None:
        nonlocal ok
        mark = "OK  " if passed else ("FAIL" if required else "WARN")
        print(f"[{mark}] {label}" + (f"  -> {hint}" if hint and not passed else ""))
        if required and not passed:
            ok = False

    print("No keys are needed - everything below marked (optional) is an extra.")
    check("ANTHROPIC_API_KEY (optional)", bool(cfg.anthropic_key),
          "not set: the built-in judge picks clips (free); a key adds Claude as an extra AI judge",
          required=False)
    check("YOUTUBE_API_KEY (optional)", bool(cfg.youtube_key),
          "not set: free YouTube search, RSS and yt-dlp are used", required=False)
    check("APIFY_TOKEN (optional)", bool(cfg.apify_token),
          "not set: trends come from free YouTube Shorts collection instead of paid TikTok/Instagram data",
          required=False)

    try:
        exe = ffmpeg_exe()
        filters = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        missing = [f for f in ("subtitles", "zoompan", "sidechaincompress", "loudnorm") if f" {f} " not in filters]
        check(f"ffmpeg ({exe})", not missing, f"missing filters: {', '.join(missing)}")
    except Exception as exc:
        check("ffmpeg", False, str(exc))

    for mod, hint, req in (("yt_dlp", "pip install yt-dlp", True),
                           ("faster_whisper", "pip install faster-whisper (falls back to YouTube captions)", False),
                           ("cv2", "pip install opencv-python-headless", True),
                           ("sklearn", "pip install scikit-learn", True),
                           ("anthropic", "pip install anthropic (only for the optional Claude judge)", False)):
        try:
            importlib.import_module(mod)
            check(f"python package {mod}", True)
        except ImportError:
            check(f"python package {mod}", False, hint, required=req)

    from .editing.reframe import _yunet_model

    check("face detection model", _yunet_model() is not None,
          "could not download YuNet; face tracking falls back to centre crop", required=False)

    db = Database(cfg.path("paths.db"))
    have = len(db.load_short_videos(time.time() - cfg["trends"]["lookback_days"] * 86400))
    check(f"trend history: {have} videos stored (need {cfg['trends']['min_videos']} per run)",
          have >= cfg["trends"]["min_videos"] or cfg["trends"]["free"]["enabled"] or bool(cfg.apify_token),
          "turn on trends.free in config.yaml or import data", required=False)
    check(f"{len(cfg['discovery']['watch_channels'])} watched channels",
          bool(cfg["discovery"]["watch_channels"]), "add channel IDs for real-time uploads", required=False)

    for key in ("music_dir", "sfx_dir", "broll_dir"):
        folder = cfg.path(f"editing.{key}")
        n = len([p for p in folder.glob("*") if p.is_file() and not p.name.startswith(".")]) if folder.exists() else 0
        check(f"{key}: {n} files", n > 0, "optional assets for professional/extreme levels", required=False)

    print("\nReady." if ok else "\nFix the FAIL items above.")
    return ok
