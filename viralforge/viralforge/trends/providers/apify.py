"""Pull top-performing posts from TikTok / Instagram through Apify actors.

Neither platform exposes an official "what is trending" API to third parties,
so a scraping service is the practical route to real numbers.  You bring your
own Apify account and token; ViralForge just runs the actor and normalises the
result.  Respect each platform's terms and your local law when you use it.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from ..profile import PostSample

API_ROOT = "https://api.apify.com/v2"


class ApifyError(RuntimeError):
    pass


class ApifyProvider:
    name = "apify"

    def __init__(self, token_env: str = "APIFY_TOKEN",
                 tiktok_actor: str = "clockworks~tiktok-scraper",
                 instagram_actor: str = "apify~instagram-scraper",
                 timeout: float = 600.0):
        self.token = os.environ.get(token_env, "").strip()
        self.token_env = token_env
        self.tiktok_actor = tiktok_actor
        self.instagram_actor = instagram_actor
        self.timeout = timeout

    def collect(self, niche: str, platforms: List[str], limit: int) -> List[PostSample]:
        if not self.token:
            raise ApifyError(
                f"No Apify token found in ${self.token_env}. Set it, or switch to "
                "trends.provider: file (your own export) or local (the baseline)."
            )
        terms = _search_terms(niche)
        per_platform = max(10, limit // max(len(platforms), 1))
        out: List[PostSample] = []
        for platform in platforms:
            try:
                if platform == "tiktok":
                    out += self._tiktok(terms, per_platform)
                elif platform == "instagram":
                    out += self._instagram(terms, per_platform)
            except ApifyError:
                raise
            except Exception as exc:  # network, schema drift in a third-party actor
                raise ApifyError(f"Apify {platform} run failed: {exc}") from exc
        out.sort(key=lambda s: s.velocity, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------ #

    def _run_actor(self, actor: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        import requests

        start = requests.post(
            f"{API_ROOT}/acts/{actor}/runs",
            params={"token": self.token},
            json=payload,
            timeout=60,
        )
        if start.status_code == 401:
            raise ApifyError("Apify rejected the token (401). Check ${}.".format(self.token_env))
        if start.status_code == 404:
            raise ApifyError(f"Apify actor '{actor}' not found - check trends.apify_*_actor.")
        start.raise_for_status()
        run = start.json()["data"]
        run_id, dataset_id = run["id"], run["defaultDatasetId"]

        deadline = time.time() + self.timeout
        delay = 3.0
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.4, 20.0)
            status = requests.get(f"{API_ROOT}/actor-runs/{run_id}",
                                  params={"token": self.token}, timeout=30)
            status.raise_for_status()
            state = status.json()["data"]["status"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise ApifyError(f"Apify run {run_id} ended as {state}.")
        else:
            raise ApifyError(f"Apify run {run_id} did not finish within {self.timeout:.0f}s.")

        items = requests.get(f"{API_ROOT}/datasets/{dataset_id}/items",
                             params={"token": self.token, "format": "json", "clean": "true"},
                             timeout=120)
        items.raise_for_status()
        data = items.json()
        return data if isinstance(data, list) else []

    def _tiktok(self, terms: List[str], limit: int) -> List[PostSample]:
        rows = self._run_actor(self.tiktok_actor, {
            "searchQueries": terms,
            "resultsPerPage": limit,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        })
        return [s for s in (_tiktok_sample(r) for r in rows) if s]

    def _instagram(self, terms: List[str], limit: int) -> List[PostSample]:
        rows = self._run_actor(self.instagram_actor, {
            "search": terms[0] if terms else "",
            "searchType": "hashtag",
            "resultsType": "posts",
            "resultsLimit": limit,
            "searchLimit": max(1, len(terms)),
        })
        return [s for s in (_instagram_sample(r) for r in rows) if s]


def _search_terms(niche: str) -> List[str]:
    words = [w.strip() for w in (niche or "").replace(",", " ").split() if len(w.strip()) > 2]
    return words[:4] or ["fyp"]


def _tiktok_sample(row: Dict[str, Any]) -> Optional[PostSample]:
    if not isinstance(row, dict):
        return None
    stats = row.get("stats") or {}
    author = row.get("authorMeta") or {}
    video = row.get("videoMeta") or {}
    views = _num(row.get("playCount"), stats.get("playCount"))
    if not views:
        return None
    return PostSample(
        platform="tiktok",
        url=str(row.get("webVideoUrl") or ""),
        caption=str(row.get("text") or ""),
        hashtags=[str(h.get("name", "")).lstrip("#")
                  for h in (row.get("hashtags") or []) if isinstance(h, dict)],
        duration=float(_num(video.get("duration"), row.get("duration")) or 0),
        views=views,
        likes=_num(row.get("diggCount"), stats.get("diggCount")),
        comments=_num(row.get("commentCount"), stats.get("commentCount")),
        shares=_num(row.get("shareCount"), stats.get("shareCount")),
        saves=_num(row.get("collectCount"), stats.get("collectCount")),
        followers=_num(author.get("fans"), author.get("followers")),
        posted_at=str(row.get("createTimeISO") or row.get("createTime") or ""),
        author=str(author.get("name") or author.get("nickName") or ""),
    )


def _instagram_sample(row: Dict[str, Any]) -> Optional[PostSample]:
    if not isinstance(row, dict):
        return None
    views = _num(row.get("videoPlayCount"), row.get("videoViewCount"), row.get("playCount"))
    if not views:
        return None
    return PostSample(
        platform="instagram",
        url=str(row.get("url") or ""),
        caption=str(row.get("caption") or ""),
        hashtags=[str(h).lstrip("#") for h in (row.get("hashtags") or [])],
        duration=float(_num(row.get("videoDuration")) or 0),
        views=views,
        likes=_num(row.get("likesCount")),
        comments=_num(row.get("commentsCount")),
        followers=_num(row.get("ownerFollowersCount")),
        posted_at=str(row.get("timestamp") or ""),
        author=str(row.get("ownerUsername") or ""),
    )


def _num(*values: Any) -> int:
    for v in values:
        if v in (None, "", False):
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return 0
