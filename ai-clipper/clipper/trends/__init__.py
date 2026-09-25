"""Step 1: learn what goes viral in short-form video (YouTube Shorts, TikTok, Instagram)."""
from __future__ import annotations

import threading
import time

from ..config import Config
from ..db import Database
from ..events import Reporter
from .analyzer import build_profile, playbook_text
from .free import collect_free
from .providers import ApifyProvider, load_imports


class NotEnoughTrendData(RuntimeError):
    pass


SCAN_LOCK = threading.Lock()   # one live scan at a time (a click waits for the background scan to finish)
CURRENT: dict = {"scan": None}  # the scan running right now, for the live view


def run_trend_analysis(cfg: Config, db: Database, rep: Reporter, board=None) -> dict:
    if not SCAN_LOCK.acquire(blocking=False):
        rep.info("trends", "A live scan is already running - waiting for it to finish and using its results")
        with SCAN_LOCK:
            pass
        latest = db.latest_profile()
        if latest and time.time() - latest.get("created_at", 0) < 900:
            return latest
        SCAN_LOCK.acquire()
    try:
        return _run(cfg, db, rep, board)
    finally:
        CURRENT["scan"] = None
        SCAN_LOCK.release()


def _run(cfg: Config, db: Database, rep: Reporter, board=None) -> dict:
    tcfg = cfg["trends"]
    fresh: list[dict] = []
    since = time.time() - tcfg["lookback_days"] * 86400
    live = None

    if tcfg.get("live", {}).get("enabled"):
        from ..agents import BOARD
        from .live import LiveScan

        known = {v["video_id"] for v in db.load_short_videos(since)}
        scan = LiveScan(cfg, rep=rep, board=board or BOARD, known=known)
        CURRENT["scan"] = scan
        live = scan.run()
        st = live["stats"]
        per = ", ".join(f"{v['videos']} {k.replace('_', ' ')}" for k, v in st["platforms"].items() if v["videos"])
        rep.info("trends", f"Live scan done in {st['elapsed'] // 60}:{st['elapsed'] % 60:02d}: {st['videos']:,} videos "
                           f"from {st['accounts']} accounts ({per or 'nothing answered'}), {st['measured']} re-measured "
                           "for views per minute")
        fresh += live["videos"]
    elif tcfg["free"]["enabled"]:
        known = {v["video_id"] for v in db.load_short_videos(since)}
        got = collect_free(tcfg["free"], known,
                           progress=lambda f, m="": rep.progress("trends", 0.8 * f, m),
                           log=lambda m: rep.info("trends", m))
        rep.info("trends", f"Collected {len(got)} new short videos from free sources "
                           f"({len(known)} already stored from earlier runs)")
        fresh += got

    if cfg.apify_token:
        apify = ApifyProvider(cfg.apify_token)
        for i, platform in enumerate(tcfg["platforms"]):
            rep.progress("trends", i / (len(tcfg["platforms"]) + 1), f"Collecting {platform} videos...")
            try:
                got = getattr(apify, platform)(tcfg[platform], tcfg["fetch_per_platform"])
                rep.info("trends", f"Collected {len(got)} {platform} videos")
                fresh += got
            except Exception as exc:  # one platform failing shouldn't kill the run
                rep.error("trends", f"{platform} collection failed: {exc}")
    else:
        rep.info("trends", "No APIFY_TOKEN - skipping paid TikTok/Instagram collection (free sources used)")

    imported = load_imports(cfg.path("trends.import_dir"))
    if imported:
        rep.info("trends", f"Loaded {len(imported)} videos from {tcfg['import_dir']}")
    db.save_short_videos(fresh + imported)

    videos = db.load_short_videos(since)
    rep.info("trends", f"{len(videos)} videos available for analysis "
                       f"(last {tcfg['lookback_days']} days, minimum {tcfg['min_videos']})")
    if len(videos) < tcfg["min_videos"]:
        raise NotEnoughTrendData(
            f"Only {len(videos)} short videos available, need {tcfg['min_videos']}. Check your internet "
            "connection, add more channels under trends.free.youtube_channels in config.yaml, or run "
            "again (videos from earlier runs are kept and add up).")

    rep.progress("trends", 0.9, "Learning what makes videos go viral...")
    profile = build_profile(videos, tcfg["viral_top_fraction"], tcfg["flop_bottom_fraction"])
    if live:
        profile["live"] = {"viral_now": live["viral_now"], "creators": live["creators"],
                           "accounts": live["accounts"][:60], "stats": live["stats"], "notes": live["notes"],
                           "focus": live["focus"]}
    profile["playbook"] = playbook_text(profile)
    db.save_profile(profile)

    top_hooks = ", ".join(r["feature"] for r in profile["hook_lift"][:3]) or "n/a"
    rep.info("trends", f"Trend analysis done: {profile['n_viral']} viral of {profile['n_videos']}; "
                       f"strongest hooks: {top_hooks}; model AUC {profile['model_auc']}",
             profile_summary=profile["playbook"])
    rep.progress("trends", 1.0, "Trend analysis complete")
    return profile
