"""Step 1: learn what goes viral on TikTok and Instagram."""
from __future__ import annotations

import time

from ..config import Config
from ..db import Database
from ..events import Reporter
from .analyzer import build_profile, playbook_text
from .providers import ApifyProvider, load_imports


class NotEnoughTrendData(RuntimeError):
    pass


def run_trend_analysis(cfg: Config, db: Database, rep: Reporter) -> dict:
    tcfg = cfg["trends"]
    fresh: list[dict] = []

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
        rep.info("trends", "APIFY_TOKEN not set - using imported files and stored history only")

    imported = load_imports(cfg.path("trends.import_dir"))
    if imported:
        rep.info("trends", f"Loaded {len(imported)} videos from {tcfg['import_dir']}")
    db.save_short_videos(fresh + imported)

    since = time.time() - tcfg["lookback_days"] * 86400
    videos = db.load_short_videos(since)
    rep.info("trends", f"{len(videos)} videos available for analysis "
                       f"(last {tcfg['lookback_days']} days, minimum {tcfg['min_videos']})")
    if len(videos) < tcfg["min_videos"]:
        raise NotEnoughTrendData(
            f"Only {len(videos)} TikTok/Instagram videos available, need {tcfg['min_videos']}. "
            "Set APIFY_TOKEN, raise trends.fetch_per_platform, or add exports to "
            f"{tcfg['import_dir']}.")

    rep.progress("trends", 0.8, "Learning what makes videos go viral...")
    profile = build_profile(videos, tcfg["viral_top_fraction"], tcfg["flop_bottom_fraction"])
    profile["playbook"] = playbook_text(profile)
    db.save_profile(profile)

    top_hooks = ", ".join(r["feature"] for r in profile["hook_lift"][:3]) or "n/a"
    rep.info("trends", f"Trend analysis done: {profile['n_viral']} viral of {profile['n_videos']}; "
                       f"strongest hooks: {top_hooks}; model AUC {profile['model_auc']}",
             profile_summary=profile["playbook"])
    rep.progress("trends", 1.0, "Trend analysis complete")
    return profile
