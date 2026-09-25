"""Find long-form YouTube videos worth clipping.

Sources, best first:
  * watch-list channels via RSS (free, near real-time new uploads),
  * YouTube Data API: "most popular" chart + searches for long, high-view videos,
  * yt-dlp search as a no-API-key fallback.
"""
from __future__ import annotations

import base64
import math
import random
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus

import numpy as np
import requests

from .. import ytdl
from ..events import Event
from ..trends import focus as focus_mod
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


def mostly_other_script(title: str) -> bool:
    """True when many letters in the title are not Latin (e.g. Devanagari, Cyrillic, Arabic)."""
    letters = [ch for ch in title if ch.isalpha()]
    other = sum(not ch.isascii() and "LATIN" not in unicodedata.name(ch, "") for ch in letters)
    return bool(letters) and other / len(letters) > 0.3


LENGTH_CODE = {"long": 0x02, "medium": 0x03}  # YouTube's length filter: long > 20 min, medium 4-20 min


def search_filter(within_days: float, length: str | bool | None = None) -> str:
    """YouTube's `sp` search filter: uploaded within a period, videos only, optionally by length."""
    if length is True:
        length = "long"
    period = 2 if within_days <= 1 else 3 if within_days <= 7 else 4 if within_days <= 31 else 5
    raw = bytes([0x08, period, 0x10, 0x01] + ([0x18, LENGTH_CODE[length]] if length else []))
    return quote(base64.b64encode(bytes([0x12, len(raw)]) + raw).decode())


def ytdlp_search(query: str, limit: int, within_days: float | None = None,
                 length: str | bool | None = None) -> list[dict]:
    """Search without an API key. `published` is 0 when YouTube's listing doesn't say.
    length: None, "medium" (4-20 min) or "long" (over 20 min)."""
    if within_days is None and not length:
        url = f"ytsearch{limit}:{query}"
    else:
        url = (f"https://www.youtube.com/results?search_query={quote_plus(query)}"
               f"&sp={search_filter(within_days or 3650, length)}")
    with ytdl.ydl(extract_flat="in_playlist", skip_download=True, playlistend=limit, ignoreerrors=True) as y:
        info = y.extract_info(url, download=False) or {}
    out = []
    for e in info.get("entries", []) or []:
        if not e or not e.get("id") or e.get("ie_key", "Youtube") != "Youtube":
            continue
        out.append({
            "video_id": e["id"], "title": e.get("title", ""), "description": e.get("description") or "",
            "channel": e.get("channel") or e.get("uploader") or "", "channel_id": e.get("channel_id") or "",
            "published": float(e.get("timestamp") or 0),
            "duration": float(e.get("duration") or 0), "views": float(e.get("view_count") or 0),
            "likes": 0.0, "comments": 0.0, "live": e.get("live_status") == "is_live",
        })
    return out


def ytdlp_enrich(cands: list[dict], workers: int = 2) -> None:
    """Fills real upload date, likes, comments and subscriber count (in place) without an API key."""

    def one(c: dict) -> None:
        try:
            with ytdl.ydl(skip_download=True, extractor_args={"youtube": {"skip": ["dash", "hls"]}}) as y:
                info = y.extract_info(f"https://www.youtube.com/watch?v={c['video_id']}", download=False)
        except Exception:
            return
        if not info:
            return
        d = info.get("upload_date")
        if info.get("timestamp"):
            c["published"] = float(info["timestamp"])
        elif d and re.fullmatch(r"\d{8}", str(d)):
            c["published"] = datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        c["likes"] = float(info.get("like_count") or 0)
        c["comments"] = float(info.get("comment_count") or 0)
        c["subscribers"] = float(info.get("channel_follower_count") or 0)
        c["views"] = float(info.get("view_count") or c["views"])
        c["duration"] = float(info.get("duration") or c["duration"])
        c["description"] = info.get("description") or c.get("description", "")
        c["channel"] = info.get("channel") or c.get("channel", "")
        c["channel_id"] = info.get("channel_id") or c.get("channel_id", "")
        c["has_heatmap"] = bool(info.get("heatmap"))
        c["enriched"] = True

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, cands))


def is_famous(channel: str, famous: list[str]) -> bool:
    key = re.sub(r"[^a-z0-9]", "", channel.lower())
    return bool(key) and any(re.sub(r"[^a-z0-9]", "", f.lower()) in key or key in re.sub(r"[^a-z0-9]", "", f.lower())
                             for f in famous if len(f) > 3)


def creator_queries(profile: dict | None, focus: dict, famous: list[str], n: int = 10,
                    rng: random.Random | None = None) -> tuple[list[str], list[str]]:
    """YouTube searches for the people worth clipping: the creator scout's best-ranked people first (the ones
    whose clips go viral now and who have long videos), then your focus list, then other big creators."""
    rng = rng or random.Random()
    w = focus.get("weight", 0.6)
    ranked = [c for c in ((profile or {}).get("live") or {}).get("creators", [])
              if (c.get("youtube") or {}).get("long_videos", 1)]
    names = [c["name"] for c in ranked[:6]]
    fp = [nm for nm, _ in focus_mod.people(focus) if nm not in names]
    names += rng.sample(fp, min(len(fp), round(4 * w)))
    rest = [f for f in famous if f not in names]
    names += rng.sample(rest, max(0, min(len(rest), n - len(names))))
    queries = [f"{nm} full episode" if nm in famous or re.search(r"podcast|show|theory|\blab\b", nm, re.I)
               else f"{nm} interview" for nm in names[:n]]
    return queries, names[:n]


def rank_candidates(cands: list[dict], profile: dict | None, now: float | None = None,
                    famous: list[str] | None = None, focus: dict | None = None) -> list[dict]:
    """Adds `rank_score` (0-100) and returns candidates best-first."""
    if not cands:
        return []
    now = now or time.time()
    famous = famous or []
    focus = focus or {"weight": 0.0, "keywords": [], "creators": []}
    w = float(focus.get("weight", 0.0))
    people = [a for _, aliases in focus_mod.people(focus) for a in aliases if len(a) > 3]
    views = np.array([max(c["views"], 1.0) for c in cands])
    age_h = np.array([max(1.0, (now - c["published"]) / 3600) for c in cands])
    eng = np.array([c["likes"] / max(c["views"], 1.0) for c in cands])
    subs = np.array([c.get("subscribers", 0.0) for c in cands])
    fit = np.array([trend_fit(profile, f"{c['title']} {c.get('description', '')[:600]}", 45) / 100
                    for c in cands])
    fresh = np.array([1.0 if c.get("source") == "watchlist" and now - c["published"] < 48 * 3600 else 0.0
                      for c in cands])
    heat = np.array([1.0 if c.get("has_heatmap") else 0.0 for c in cands])  # "most replayed" data helps
    star = np.array([1.0 if is_famous(c.get("channel", ""), famous) or any(
        re.search(rf"\b{re.escape(f)}\b", c.get("title", ""), re.I) for f in famous if len(f) > 3) else 0.0
        for c in cands])

    def on_focus(c: dict) -> float:
        text = f"{c.get('title', '')} {c.get('channel', '')} {c.get('description', '')[:300]}"
        if any(re.search(rf"\b{re.escape(p)}\b", text, re.I) for p in people):
            return 1.0
        return focus_mod.fit(text, focus)
    foc = np.array([on_focus(c) for c in cands])
    score = (0.30 * pct_rank(np.log10(views / age_h)) + 0.20 * pct_rank(np.log10(views))
             + 0.10 * pct_rank(eng) + 0.20 * pct_rank(subs) + 0.20 * fit + 0.10 * fresh + 0.05 * heat
             + 0.30 * star + 0.5 * w * foc) / (1.45 + 0.5 * w)
    for c, s, f in zip(cands, score, foc):
        c["rank_score"] = round(float(s) * 100, 1)
        c["focus_fit"] = round(float(f), 2)
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


def discover(cfg, db, rep, profile: dict | None, max_videos: int | None = None, board=None) -> list[dict]:
    d = cfg["discovery"]
    focus = focus_mod.load(cfg)
    max_videos = max_videos or d.get("max_videos_per_run", 8)
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
        people, _ = creator_queries(profile, focus, list(d.get("famous_creators") or []), n=6)
        fs = list(focus.get("searches") or [])
        api_queries = people + random.sample(fs, min(len(fs), round(2 * focus.get("weight", 0.6)))) + \
            random.sample(d["search_queries"], len(d["search_queries"]))  # a different order every run
        for q in api_queries:
            rep.progress("discovery", 0.5, f"Searching YouTube: {q}")
            ids |= set(api.search(q, after, d["region_code"], d["language"]))
        details = api.videos(sorted(ids))
        subs = api.subscribers([v["channel_id"] for v in details])
        for v in details:
            v["subscribers"] = subs.get(v["channel_id"], 0.0)
            v["source"] = "watchlist" if v["video_id"] in watch_ids else "search"
            cands[v["video_id"]] = v
    else:
        rep.info("discovery", "Free YouTube search (no key needed)")
        # 10-20 minute videos come from the "medium" filter, everything longer from "long"
        lengths = ["long"] if d["min_duration_minutes"] >= 20 else ["long", "medium"]
        def search_one(q: str) -> list[dict]:
            found = []
            rep.emit(Event("watch", "discovery", data={"kind": "search", "query": q, "state": "start"}))
            with (board.work("scout", f"Searching YouTube: {q}") if board else nullcontext()):
                for length in lengths:
                    try:
                        found += ytdlp_search(q, 40 if length == "long" else 20, d["published_within_days"], length)
                    except Exception as exc:
                        rep.info("discovery", f"Search '{q}' ({length}) failed: {exc}")
            rep.emit(Event("watch", "discovery", data={"kind": "search", "query": q, "state": "done",
                                                       "found": len(found)}))
            return found

        # the video scouts search in parallel (3 at a time - polite to YouTube, several times faster)
        # the people worth clipping first (creator scout's ranking, your focus, big creators), then your
        # focus searches, then the general searches
        famous = list(d.get("famous_creators") or [])
        queries, _ = creator_queries(profile, focus, famous)
        fs = list(focus.get("searches") or [])
        queries += random.sample(fs, min(len(fs), round(3 * focus.get("weight", 0.6))))
        queries += random.sample(d["search_queries"], len(d["search_queries"]))
        rep.progress("discovery", 0.4, f"Video scouts searching YouTube ({len(queries)} searches)...")
        with ThreadPoolExecutor(max_workers=3) as ex:
            for found in ex.map(search_one, queries):
                for v in found:
                    v["source"] = "search"
                    cands.setdefault(v["video_id"], v)
        # the listing has no dates/likes: read them for the most promising videos
        lo_s = d["min_duration_minutes"] * 60
        pool = sorted((c for c in cands.values() if c["duration"] >= lo_s and not db.is_processed(c["video_id"])),
                      key=lambda c: -c["views"])[: max(12, 2 * max_videos)]
        rep.progress("discovery", 0.7, f"Checking upload dates and engagement for {len(pool)} videos...")
        ytdlp_enrich(pool)
        for c in cands.values():
            if not c["published"]:  # search was limited to the time window, so assume mid-window
                c["published"] = now - d["published_within_days"] * 86400 / 2
        for vid in watch_ids:
            cands.setdefault(vid, {"video_id": vid, "title": "", "description": "", "channel": "",
                                   "channel_id": "", "published": now, "duration": 0.0, "views": 0.0,
                                   "likes": 0.0, "comments": 0.0, "source": "watchlist", "live": False})

    lo = d["min_duration_minutes"] * 60
    hi = d["max_duration_minutes"] * 60 if d.get("max_duration_minutes") else float("inf")  # 0 = no limit
    kept = []
    for c in cands.values():
        if c.get("live") or db.is_processed(c["video_id"]):
            continue
        unknown_len = c["duration"] == 0 and c["source"] == "watchlist"
        if not unknown_len and not lo <= c["duration"] <= hi:
            continue
        if c["source"] != "watchlist" and c["views"] < d["min_views"]:
            continue
        if c["source"] != "watchlist" and c["published"] < after:
            continue
        if c["source"] != "watchlist" and d.get("language") == "en" and mostly_other_script(c.get("title", "")):
            continue
        title = c.get("title", "").lower()
        if c["source"] != "watchlist" and any(re.search(rf"\b{re.escape(w.lower())}\b", title)
                                              for w in d.get("exclude_title_words", [])):
            continue
        kept.append(c)

    stars = list(d.get("famous_creators") or []) + [c["name"] for c in ((profile or {}).get("live") or {})
                                                     .get("creators", [])[:12]]
    ranked = rank_candidates(kept, profile, now, stars, focus)
    # spread over creators: each channel's best video first, then second-best videos, and so on
    seen: dict[str, int] = {}
    for c in ranked:
        key = (c.get("channel") or c["video_id"]).lower()
        c["_round"] = seen.get(key, 0)
        seen[key] = c["_round"] + 1
    ranked.sort(key=lambda c: (c.pop("_round"), -c["rank_score"]))
    rep.info("discovery", f"{len(cands)} videos found, {len(ranked)} pass the filters")
    for c in ranked[:5]:
        rep.info("discovery", f"  {c['rank_score']:5.1f}  {c['channel']} - {c['title']} "
                              f"({math.floor(c['duration'] / 60)} min, {int(c['views']):,} views)")
    rep.progress("discovery", 1.0, "Discovery complete")
    return ranked
