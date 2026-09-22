"""Samples you supply yourself - the most reliable grounding available.

Accepts a JSON array, JSONL, or CSV.  Column/key names are matched loosely so
a raw TikTok Creator Center export, an Instagram Insights CSV, or a scrape you
did by hand all work without reshaping.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..profile import PostSample

# Map the many names these exports use onto PostSample fields.
ALIASES = {
    "platform": ["platform", "network", "source"],
    "url": ["url", "link", "post_url", "permalink", "webvideourl", "videourl"],
    "caption": ["caption", "text", "description", "title", "desc", "post_caption"],
    "hashtags": ["hashtags", "tags", "hashtag"],
    "duration": ["duration", "length", "video_duration", "videolength", "seconds"],
    "views": ["views", "play_count", "playcount", "plays", "video_views", "impressions", "reach"],
    "likes": ["likes", "like_count", "diggcount", "digg_count", "favorites", "heart_count"],
    "comments": ["comments", "comment_count", "commentcount", "replies"],
    "shares": ["shares", "share_count", "sharecount", "sends", "reposts"],
    "saves": ["saves", "save_count", "collect_count", "collectcount", "bookmarks"],
    "followers": ["followers", "follower_count", "fans", "author_followers"],
    "posted_at": ["posted_at", "create_time", "createtime", "timestamp", "date", "published"],
    "author": ["author", "username", "handle", "creator", "account", "uniqueid"],
}
_LOOKUP = {alias: field for field, aliases in ALIASES.items() for alias in aliases}


def _normalise_key(key: str) -> str:
    return "".join(ch for ch in str(key).lower().strip() if ch.isalnum() or ch == "_")


def _remap(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for raw_key, value in row.items():
        field = _LOOKUP.get(_normalise_key(raw_key))
        if field and (field not in out or out[field] in ("", 0, 0.0, None)):
            out[field] = value
    if isinstance(out.get("hashtags"), str):
        text = out["hashtags"]
        out["hashtags"] = [t.strip().lstrip("#") for t in text.replace(",", " ").split()
                           if t.strip()]
    if not out.get("hashtags") and isinstance(out.get("caption"), str):
        out["hashtags"] = [w.lstrip("#") for w in out["caption"].split() if w.startswith("#")]
    return out


def _rows_from(path: Path) -> Iterable[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    if path.suffix.lower() == ".csv" or (text[0] not in "[{"):
        reader = csv.DictReader(io.StringIO(text))
        return [dict(r) for r in reader]
    if text[0] == "[":
        data = json.loads(text)
        return data if isinstance(data, list) else []
    # JSONL, or a single object wrapping a list
    rows: List[Dict[str, Any]] = []
    try:
        obj = json.loads(text)
        for key in ("items", "data", "results", "posts", "videos"):
            if isinstance(obj.get(key), list):
                return obj[key]
        return [obj]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


class FileProvider:
    name = "file"

    def __init__(self, path: str):
        self.path = Path(path).expanduser()

    def collect(self, niche: str, platforms: List[str], limit: int) -> List[PostSample]:
        if not self.path.is_file():
            raise FileNotFoundError(
                f"trends.samples_path points at {self.path}, which does not exist."
            )
        samples: List[PostSample] = []
        for row in _rows_from(self.path):
            if not isinstance(row, dict):
                continue
            mapped = _remap(row)
            if not mapped:
                continue
            mapped.setdefault("platform", platforms[0] if platforms else "unknown")
            sample = PostSample.from_dict(mapped)
            if sample.views or sample.likes:
                samples.append(sample)
        samples.sort(key=lambda s: s.velocity, reverse=True)
        return samples[:limit]
