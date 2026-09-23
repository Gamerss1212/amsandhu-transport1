"""Keyless short-video collection with yt-dlp.

YouTube Shorts is the reliable free source (Shorts tabs of big creators, hashtag pages and
short-video searches). TikTok creator pages are tried too; TikTok often blocks these requests,
so they only add to the pool when they work. Instagram needs a login, so it is not covered here.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote_plus

from .. import ytdl

SHORT_FILTER = "EgIYAQ%3D%3D"  # YouTube search filter: videos under 4 minutes


def _ydl(extra: dict | None = None):
    return ytdl.ydl(skip_download=True, ignoreerrors=True,
                    extractor_args={"youtube": {"skip": ["dash", "hls"]}}, **(extra or {}))


def sources(fcfg: dict) -> list[tuple[str, str, int]]:
    """(platform, url, max items) for every configured free source."""
    per = int(fcfg.get("per_source", 120))
    out = [("youtube_shorts", f"https://www.youtube.com/@{c.lstrip('@')}/shorts", per)
           for c in fcfg.get("youtube_channels", [])]
    out += [("youtube_shorts", f"https://www.youtube.com/hashtag/{t.lstrip('#')}/shorts", per)
            for t in fcfg.get("youtube_hashtags", [])]
    out += [("youtube_shorts", f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={SHORT_FILTER}", per)
            for q in fcfg.get("youtube_searches", [])]
    out += [("tiktok", f"https://www.tiktok.com/@{c.lstrip('@')}", per) for c in fcfg.get("tiktok_creators", [])]
    return out


def list_source(url: str, limit: int) -> list[dict]:
    with _ydl({"extract_flat": "in_playlist", "playlistend": limit}) as y:
        info = y.extract_info(url, download=False) or {}
    return [e for e in (info.get("entries") or []) if e and e.get("id")]


def details(url: str) -> dict | None:
    time.sleep(0.4)  # a gentle, steady rate instead of a burst of requests
    with _ydl() as y:
        return y.extract_info(url, download=False)


def _created(info: dict) -> float | None:
    if info.get("timestamp"):
        return float(info["timestamp"])
    d = info.get("upload_date")
    if d and re.fullmatch(r"\d{8}", str(d)):
        return datetime.strptime(str(d), "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    return None


def to_short(info: dict, platform: str) -> dict | None:
    views = float(info.get("view_count") or 0)
    if not info.get("id") or views <= 0:
        return None
    caption = " ".join(x for x in (info.get("title") or "", info.get("description") or "") if x).strip()
    tags = {t.lstrip("#").lower() for t in (info.get("tags") or []) if t}
    tags |= {m.lower() for m in re.findall(r"#(\w+)", caption)}
    tags.discard("shorts")
    return {
        "platform": platform,
        "video_id": str(info["id"]),
        "url": info.get("webpage_url") or info.get("url") or "",
        "caption": caption[:600],
        "hashtags": sorted(tags),
        "views": views,
        "likes": float(info.get("like_count") or 0),
        "comments": float(info.get("comment_count") or 0),
        "shares": float(info.get("repost_count") or 0),
        "saves": 0.0,
        "duration": float(info.get("duration") or 0),
        "author": info.get("channel") or info.get("uploader") or "",
        "author_followers": float(info.get("channel_follower_count") or 0) or None,
        "created_at": _created(info),
        "music": info.get("track") or "",
    }


def collect_free(fcfg: dict, known: set[str], progress=None, log=None) -> list[dict]:
    """Returns normalized short videos. Videos already in `known` are not re-fetched."""
    progress = progress or (lambda f, m="": None)
    log = log or (lambda m: None)
    workers = int(fcfg.get("workers", 3))
    srcs = sources(fcfg)
    listed: dict[tuple[str, str], dict] = {}
    failed_platforms: dict[str, int] = {}

    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(list_source, url, limit): (platform, url) for platform, url, limit in srcs}
        for i, fut in enumerate(as_completed(futures)):
            platform, url = futures[fut]
            try:
                entries = fut.result()
            except Exception:
                entries = []
            if not entries:
                failed_platforms[platform] = failed_platforms.get(platform, 0) + 1
            for e in entries:
                listed.setdefault((platform, str(e["id"])), e)
            progress(0.3 * (i + 1) / max(1, len(futures)), f"Listing short videos ({len(listed)} found)...")
    for platform, n in failed_platforms.items():
        log(f"{n} {platform} source(s) returned nothing" +
            (" - TikTok often blocks free access; YouTube Shorts is used instead" if platform == "tiktok" else ""))

    new = [(p, vid, e) for (p, vid), e in listed.items() if vid not in known]
    # only a sample gets the (one request per video) detail check - hammering YouTube with hundreds of page
    # loads makes it block this connection, and then the long-video downloads fail too
    detail_n = int(fcfg.get("max_details", 120))
    rest = [(p, vid, e) for p, vid, e in new[detail_n:]]
    new = new[:detail_n]
    log(f"{len(listed)} short videos listed, {len(new)} new to check in detail")
    out: list[dict] = []
    deadline = time.time() + float(fcfg.get("max_detail_minutes", 8)) * 60
    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(details, e.get("url") or e.get("webpage_url") or
                             f"https://www.youtube.com/shorts/{vid}"): (p, vid, e) for p, vid, e in new}
        done = misses = 0
        for fut in as_completed(futures):
            p, vid, e = futures[fut]
            try:
                info = fut.result()
            except Exception:
                info = None
            misses = 0 if info else misses + 1
            if info and (v := to_short(info, p)):
                out.append(v)
            done += 1
            if done % 25 == 0:
                progress(0.3 + 0.5 * done / max(1, len(new)), f"Checked {done}/{len(new)} videos in detail...")
            stop = None
            if time.time() > deadline:
                stop = f"Detail time limit reached after {done} videos"
            elif misses >= 6:
                stop = ("YouTube is limiting detail requests (set analysis.cookies_from_browser in "
                        "config.yaml to avoid this)")
            if stop:
                log(f"{stop}; using basic listing data (views) for the remaining videos")
                ex.shutdown(wait=False, cancel_futures=True)
                break
        checked = {v["video_id"] for v in out}
        out += [v for p, vid, e in new + rest if vid not in checked and (v := to_short(e, p))]
    return out
