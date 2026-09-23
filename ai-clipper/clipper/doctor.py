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

    check("ANTHROPIC_API_KEY set", bool(cfg.anthropic_key),
          "needed for accurate clip picking + the strict judge (put it in .env)")
    check("YOUTUBE_API_KEY set", bool(cfg.youtube_key),
          "optional: better discovery, view stats and comment timestamps", required=False)
    check("APIFY_TOKEN set", bool(cfg.apify_token),
          f"needed to pull fresh TikTok/Instagram data (or drop exports in {cfg['trends']['import_dir']})",
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
                           ("anthropic", "pip install anthropic", True)):
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
          have >= cfg["trends"]["min_videos"] or bool(cfg.apify_token),
          "set APIFY_TOKEN or import data", required=False)
    check(f"{len(cfg['discovery']['watch_channels'])} watched channels",
          bool(cfg["discovery"]["watch_channels"]), "add channel IDs for real-time uploads", required=False)

    for key in ("music_dir", "sfx_dir", "broll_dir"):
        folder = cfg.path(f"editing.{key}")
        n = len([p for p in folder.glob("*") if p.is_file() and not p.name.startswith(".")]) if folder.exists() else 0
        check(f"{key}: {n} files", n > 0, "optional assets for professional/extreme levels", required=False)

    print("\nReady." if ok else "\nFix the FAIL items above.")
    return ok
