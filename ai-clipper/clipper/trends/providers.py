"""Collect short-form videos (TikTok / Instagram Reels) with their performance stats.

Sources:
  * Apify actors (default) - hosted scrapers with a REST API.
  * Local .json / .csv exports in `trends.import_dir` - lets you plug in any other
    source (TikTok Research API, a data vendor, your own analytics exports...).
"""
from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

APIFY = "https://api.apify.com/v2"


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    """First present, non-empty value among dotted keys."""
    for key in keys:
        node: Any = d
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if node not in (None, "", []):
            return node
    return default


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").upper()
    mult = 1.0
    if text.endswith("K"):
        mult, text = 1e3, text[:-1]
    elif text.endswith("M"):
        mult, text = 1e6, text[:-1]
    elif text.endswith("B"):
        mult, text = 1e9, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return 0.0


def _timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    text = str(value)
    if text.isdigit():
        return _timestamp(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _hashtags(item: dict, caption: str) -> list[str]:
    tags = _first(item, "hashtags", default=[]) or []
    names = []
    for t in tags:
        name = t.get("name") if isinstance(t, dict) else str(t)
        if name:
            names.append(name.lstrip("#").lower())
    if not names:
        names = [m.lower() for m in re.findall(r"#(\w+)", caption)]
    return sorted(set(names))


def normalize(item: dict, platform: str) -> dict | None:
    """Map one raw record (any of the common scraper/export shapes) to our schema."""
    vid = _first(item, "id", "video_id", "shortCode", "shortcode", "aweme_id", "pk")
    views = _num(_first(item, "playCount", "videoPlayCount", "videoViewCount", "views",
                        "view_count", "stats.playCount", "play_count"))
    if not vid or views <= 0:
        return None
    caption = str(_first(item, "text", "caption", "desc", "description", "title", default=""))
    return {
        "platform": platform,
        "video_id": str(vid),
        "url": _first(item, "webVideoUrl", "url", "postUrl", "video_url", default=""),
        "caption": caption,
        "hashtags": _hashtags(item, caption),
        "views": views,
        "likes": _num(_first(item, "diggCount", "likesCount", "likes", "like_count", "stats.diggCount")),
        "comments": _num(_first(item, "commentCount", "commentsCount", "comments", "comment_count",
                                "stats.commentCount")),
        "shares": _num(_first(item, "shareCount", "sharesCount", "shares", "share_count",
                              "stats.shareCount")),
        "saves": _num(_first(item, "collectCount", "savesCount", "saves", "save_count")),
        "duration": _num(_first(item, "videoMeta.duration", "videoDuration", "duration",
                                "video_duration")),
        "author": str(_first(item, "authorMeta.name", "ownerUsername", "author", "username",
                             "author.uniqueId", default="")),
        "author_followers": _num(_first(item, "authorMeta.fans", "followersCount", "author_followers",
                                        "ownerFollowersCount", "authorStats.followerCount")),
        "created_at": _timestamp(_first(item, "createTimeISOString", "createTime", "timestamp",
                                        "taken_at", "created_at", "create_time")),
        "music": str(_first(item, "musicMeta.musicName", "musicInfo.song_name", "music", default="")),
    }


class ApifyProvider:
    """Runs an Apify actor and returns its dataset items."""

    def __init__(self, token: str, timeout_s: int = 1800) -> None:
        self.token = token
        self.timeout_s = timeout_s

    def run_actor(self, actor: str, actor_input: dict) -> list[dict]:
        params = {"token": self.token}
        r = requests.post(f"{APIFY}/acts/{actor}/runs", params=params, json=actor_input, timeout=60)
        r.raise_for_status()
        run = r.json()["data"]
        deadline = time.time() + self.timeout_s
        while run["status"] in ("READY", "RUNNING"):
            if time.time() > deadline:
                raise TimeoutError(f"Apify actor {actor} did not finish in time")
            time.sleep(10)
            r = requests.get(f"{APIFY}/actor-runs/{run['id']}", params=params, timeout=60)
            r.raise_for_status()
            run = r.json()["data"]
        if run["status"] != "SUCCEEDED":
            raise RuntimeError(f"Apify actor {actor} ended with status {run['status']}")
        r = requests.get(f"{APIFY}/datasets/{run['defaultDatasetId']}/items",
                         params={**params, "clean": "true", "format": "json"}, timeout=300)
        r.raise_for_status()
        return r.json()

    def tiktok(self, cfg: dict, limit: int) -> list[dict]:
        tags = cfg.get("hashtags", [])
        queries = cfg.get("search_queries", [])
        per_source = max(10, limit // max(1, len(tags) + len(queries)))
        actor_input = {
            "hashtags": tags,
            "searchQueries": queries,
            "resultsPerPage": per_source,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            **cfg.get("actor_input", {}),
        }
        return [v for v in (normalize(i, "tiktok") for i in self.run_actor(cfg["actor"], actor_input)) if v]

    def instagram(self, cfg: dict, limit: int) -> list[dict]:
        tags = cfg.get("hashtags", [])
        actor_input = {
            "hashtags": tags,
            "resultsLimit": max(10, limit // max(1, len(tags))),
            "resultsType": "reels",
            **cfg.get("actor_input", {}),
        }
        items = self.run_actor(cfg["actor"], actor_input)
        return [v for v in (normalize(i, "instagram") for i in items) if v]


def _guess_platform(path: Path, item: dict) -> str:
    text = (path.name + " " + str(item.get("url", "")) + " " + str(item.get("platform", ""))).lower()
    return "instagram" if "insta" in text else "tiktok"


def load_imports(folder: Path) -> list[dict]:
    """Read every .json / .jsonl / .csv file in `folder`."""
    out: list[dict] = []
    if not folder.exists():
        return out
    for path in sorted(folder.iterdir()):
        items: Iterable[dict] = []
        if path.suffix == ".json":
            data = json.loads(path.read_text())
            items = data if isinstance(data, list) else data.get("items", [])
        elif path.suffix == ".jsonl":
            items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        elif path.suffix == ".csv":
            with path.open(newline="", encoding="utf-8") as f:
                items = list(csv.DictReader(f))
        for item in items:
            norm = normalize(item, item.get("platform") or _guess_platform(path, item))
            if norm:
                out.append(norm)
    return out
