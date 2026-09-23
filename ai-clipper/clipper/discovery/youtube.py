"""Find long-form YouTube videos worth clipping.

Sources, best first:
  * watch-list channels via RSS (free, near real-time new uploads),
  * YouTube Data API: "most popular" chart + searches for long, high-view videos,
  * yt-dlp search as a no-API-key fallback.
"""
from __future__ import annotations

import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import numpy as np
import requests

from ..events import Event
from ..trends.analyzer import pct_rank, trend_fit

API = "https://www.googleapis.com/youtube/v3"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
       "media": "http://search.yahoo.com/mrss/"}


def iso_duration(text: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return 0.0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _iso_ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


class YouTubeAPI:
    def __init__(self, key: str) -> None:
        self.key = key

    def _get(self, endpoint: str, **params) -> dict:
        r = requests.get(f"{API}/{endpoint}", params={**params, "key": self.key}, timeout=30)
        r.raise_for_status()
        return r.json()

    def search(self, query: str, published_after: float, region: str, lang: str,
               max_results: int = 50) -> list[str]:
        data = self._get("search", part="id", type="video", q=query, videoDuration="long",
                         order="viewCount", maxResults=max_results, regionCode=region,
                         relevanceLanguage=lang,
                         publishedAfter=datetime.fromtimestamp(published_after, timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))
        return [i["id"]["videoId"] for i in data.get("items", []) if i["id"].get("videoId")]

    def trending_ids(self, region: str, pages: int = 4) -> list[str]:
        ids, token = [], None
        for _ in range(pages):
            params = {"part": "id", "chart": "mostPopular", "regionCode": region, "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = self._get("videos", **params)
            ids += [i["id"] for i in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
            for it in data.get("items", []):
                sn, st = it["snippet"], it.get("statistics", {})
                out.append({
                    "video_id": it["id"],
                    "title": sn["title"],
                    "description": sn.get("description", ""),
                    "channel": sn["channelTitle"],
                    "channel_id": sn["channelId"],
                    "published": _iso_ts(sn["publishedAt"]),
                    "duration": iso_duration(it["contentDetails"]["duration"]),
                    "views": float(st.get("viewCount", 0)),
                    "likes": float(st.get("likeCount", 0)),
                    "comments": float(st.get("commentCount", 0)),
                    "live": sn.get("liveBroadcastContent", "none") != "none",
                })
        return out

    def subscribers(self, channel_ids: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        uniq = sorted(set(channel_ids))
        for i in range(0, len(uniq), 50):
            data = self._get("channels", part="statistics", id=",".join(uniq[i:i + 50]))
            for it in data.get("items", []):
                out[it["id"]] = float(it.get("statistics", {}).get("subscriberCount", 0))
        return out

    def comments(self, video_id: str, pages: int) -> list[dict]:
        out, token = [], None
        for _ in range(pages):
            params = {"part": "snippet", "videoId": video_id, "order": "relevance",
                      "maxResults": 100, "textFormat": "plainText"}
            if token:
                params["pageToken"] = token
            try:
                data = self._get("commentThreads", **params)
            except requests.HTTPError:  # comments disabled
                break
            for it in data.get("items", []):
                top = it["snippet"]["topLevelComment"]["snippet"]
                out.append({"text": top.get("textDisplay", ""), "likes": float(top.get("likeCount", 0))})
            token = data.get("nextPageToken")
            if not token:
                break
        return out


def poll_rss(channel_id: str) -> list[dict]:
    r = requests.get(RSS.format(channel_id), timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for entry in root.findall("a:entry", _NS):
        stats = entry.find("media:group/media:community/media:statistics", _NS)
        out.append({
            "video_id": entry.findtext("yt:videoId", namespaces=_NS),
            "channel_id": entry.findtext("yt:channelId", namespaces=_NS) or channel_id,
            "channel": entry.findtext("a:author/a:name", namespaces=_NS) or "",
            "title": entry.findtext("a:title", namespaces=_NS) or "",
            "published": _iso_ts(entry.findtext("a:published", namespaces=_NS)),
            "views": float(stats.get("views", 0)) if stats is not None else 0.0,
        })
    return out


def ytdlp_search(query: str, limit: int) -> list[dict]:
    import yt_dlp

    opts = {"quiet": True, "extract_flat": "in_playlist", "skip_download": True, "noprogress": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    out = []
    for e in info.get("entries", []) or []:
        if not e or not e.get("id"):
            continue
        out.append({
            "video_id": e["id"], "title": e.get("title", ""), "description": e.get("description") or "",
            "channel": e.get("channel") or e.get("uploader") or "", "channel_id": e.get("channel_id") or "",
            "published": float(e.get("timestamp") or 0) or time.time() - 7 * 86400,
            "duration": float(e.get("duration") or 0), "views": float(e.get("view_count") or 0),
            "likes": 0.0, "comments": 0.0, "live": e.get("live_status") == "is_live",
        })
    return out


def rank_candidates(cands: list[dict], profile: dict | None, now: float | None = None) -> list[dict]:
    """Adds `rank_score` (0-100) and returns candidates best-first."""
    if not cands:
        return []
    now = now or time.time()
    views = np.array([max(c["views"], 1.0) for c in cands])
    age_h = np.array([max(1.0, (now - c["published"]) / 3600) for c in cands])
    eng = np.array([c["likes"] / max(c["views"], 1.0) for c in cands])
    subs = np.array([c.get("subscribers", 0.0) for c in cands])
    fit = np.array([trend_fit(profile, f"{c['title']} {c.get('description', '')[:600]}", 45) / 100
                    for c in cands])
    fresh = np.array([1.0 if c.get("source") == "watchlist" and now - c["published"] < 48 * 3600 else 0.0
                      for c in cands])
    score = (0.30 * pct_rank(np.log10(views / age_h)) + 0.20 * pct_rank(np.log10(views))
             + 0.10 * pct_rank(eng) + 0.10 * pct_rank(subs) + 0.20 * fit + 0.10 * fresh)
    for c, s in zip(cands, score):
        c["rank_score"] = round(float(s) * 100, 1)
    return sorted(cands, key=lambda c: -c["rank_score"])


def poll_watchlist(cfg, db, rep=None) -> list[dict]:
    """Check every watched channel's RSS feed; returns uploads not seen before."""
    new = []
    for channel_id in cfg["discovery"]["watch_channels"]:
        try:
            for up in poll_rss(channel_id):
                if db.add_upload(up["video_id"], up["channel_id"], up["channel"], up["title"],
                                 up["published"]):
                    new.append(up)
        except Exception as exc:
            if rep:
                rep.error("discovery", f"RSS check failed for {channel_id}: {exc}")
    if rep:
        for up in new:
            rep.emit(Event("upload", "discovery", f"New upload: {up['channel']} - {up['title']}", data=up))
    return new


def discover(cfg, db, rep, profile: dict | None) -> list[dict]:
    d = cfg["discovery"]
    now = time.time()
    after = now - d["published_within_days"] * 86400
    cands: dict[str, dict] = {}

    # 1) watch-list RSS - newest uploads from the channels you care about
    rep.progress("discovery", 0.1, "Checking watched channels for new uploads...")
    poll_watchlist(cfg, db, rep)
    watch_ids = []
    for channel_id in d["watch_channels"]:
        try:
            watch_ids += [u["video_id"] for u in poll_rss(channel_id) if u["published"] >= after]
        except Exception:
            pass

    api = YouTubeAPI(cfg.youtube_key) if cfg.youtube_key else None
    if api:
        ids = set(watch_ids)
        if d["use_trending_chart"]:
            rep.progress("discovery", 0.3, "Reading YouTube's most-popular chart...")
            ids |= set(api.trending_ids(d["region_code"]))
        for q in d["search_queries"]:
            rep.progress("discovery", 0.5, f"Searching YouTube: {q}")
            ids |= set(api.search(q, after, d["region_code"], d["language"]))
        details = api.videos(sorted(ids))
        subs = api.subscribers([v["channel_id"] for v in details])
        for v in details:
            v["subscribers"] = subs.get(v["channel_id"], 0.0)
            v["source"] = "watchlist" if v["video_id"] in watch_ids else "search"
            cands[v["video_id"]] = v
    else:
        rep.info("discovery", "YOUTUBE_API_KEY not set - using yt-dlp search (fewer signals)")
        for q in d["search_queries"]:
            rep.progress("discovery", 0.5, f"Searching YouTube: {q}")
            for v in ytdlp_search(q, 25):
                v["source"] = "search"
                cands.setdefault(v["video_id"], v)
        for vid in watch_ids:
            cands.setdefault(vid, {"video_id": vid, "title": "", "description": "", "channel": "",
                                   "channel_id": "", "published": now, "duration": 0.0, "views": 0.0,
                                   "likes": 0.0, "comments": 0.0, "source": "watchlist", "live": False})

    lo, hi = d["min_duration_minutes"] * 60, d["max_duration_minutes"] * 60
    kept = []
    for c in cands.values():
        if c.get("live") or db.is_processed(c["video_id"]):
            continue
        unknown_len = c["duration"] == 0 and c["source"] == "watchlist"
        if not unknown_len and not lo <= c["duration"] <= hi:
            continue
        if c["source"] != "watchlist" and c["views"] < d["min_views"]:
            continue
        kept.append(c)

    ranked = rank_candidates(kept, profile, now)
    rep.info("discovery", f"{len(cands)} videos found, {len(ranked)} pass the filters")
    for c in ranked[:5]:
        rep.info("discovery", f"  {c['rank_score']:5.1f}  {c['channel']} - {c['title']} "
                              f"({math.floor(c['duration'] / 60)} min, {int(c['views']):,} views)")
    rep.progress("discovery", 1.0, "Discovery complete")
    return ranked
