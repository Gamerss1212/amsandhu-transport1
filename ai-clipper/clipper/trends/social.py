"""Live, keyless reads of TikTok and Instagram: an account's recent videos with their real numbers.

TikTok: the account page gives the account's id and follower count; TikTok's own web feed for that account
(the one its website loads) gives every recent video with views, likes, comments, shares and saves.
Instagram: the public profile feed gives an account's 12 latest posts with views, likes and comments.
Instagram limits how often one connection may ask, so its client paces itself and backs off when told to.

Nothing here logs in or needs a key; every request is paced so the platforms keep answering.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time

import requests

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
UA_IG_APP = ("Instagram 337.0.0.35.102 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; "
             "en_US; 614410427)")
MENTION = re.compile(r"(?<![\w.@])@([A-Za-z0-9._]{2,30})")
HASHTAG = re.compile(r"#(\w{2,40})")
_DATA = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


class RateLimited(RuntimeError):
    """The platform asked us to slow down; `until` is when to try again."""

    def __init__(self, message: str, until: float) -> None:
        super().__init__(message)
        self.until = until


class Pacer:
    """At most one request per `interval` seconds (with a little jitter), shared by every thread."""

    def __init__(self, interval: float, jitter: float = 0.35, sleep=time.sleep, clock=time.time) -> None:
        self.interval, self.jitter, self._sleep, self._clock = interval, jitter, sleep, clock
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            at = max(now, self._next)
            self._next = at + self.interval * (1 + random.uniform(-self.jitter, self.jitter))
        if at > now:
            self._sleep(at - now)


def mentions(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in MENTION.findall(text or ""):
        h = m.strip(".").lower()
        if len(h) >= 2:
            seen[h] = None
    return list(seen)


def hashtags(text: str) -> set[str]:
    return {t.lower() for t in HASHTAG.findall(text or "")}


def _num(v) -> float:
    """A count from any shape the platforms send ("1.2M", "12,400", 5, None, garbage): never negative or NaN."""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        t = str(v).strip().replace(",", "").upper()
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(t[-1:], 1.0)
        try:
            x = float(t[:-1] if mult != 1.0 else t) * mult
        except ValueError:
            return 0.0
    return x if x == x and 0 <= x < 1e15 else 0.0


def _int(v) -> int:
    return int(_num(v))


class TikTokWeb:
    PARAMS = dict(aid="1988", app_language="en", app_name="tiktok_web", browser_language="en-US",
                  browser_name="Mozilla", browser_online="true", browser_platform="Win32", channel="tiktok_web",
                  cookie_enabled="true", device_platform="web_pc", focus_state="true", history_len="2",
                  is_fullscreen="false", is_page_visible="true", language="en", os="windows", priority_region="",
                  referer="", region="US", screen_height="1080", screen_width="1920", tz_name="UTC",
                  webcast_language="en")
    PAGE = 15  # TikTok's feed answers at most 15 videos per request

    def __init__(self, interval: float = 0.9, session: requests.Session | None = None, sleep=time.sleep) -> None:
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA_DESKTOP, "Referer": "https://www.tiktok.com/",
                               "Accept-Language": "en-US,en;q=0.9"})
        self.pacer = Pacer(interval, sleep=sleep)
        self.requests = 0
        self.misses = 0            # answers in a row that had no data
        self.blocked: str | None = None

    def _get(self, url: str, **params) -> requests.Response:
        self.pacer.wait()
        self.requests += 1
        return self.s.get(url, params=params or None, timeout=20)

    def _miss(self) -> None:
        self.misses += 1
        if self.misses >= 8:
            self.blocked = "TikTok stopped answering this connection (try again later, or from another network)"

    def profile(self, handle: str) -> dict | None:
        """The account's id and size, or None when it doesn't exist or TikTok didn't answer."""
        try:
            r = self._get(f"https://www.tiktok.com/@{handle}")
            m = _DATA.search(r.text)
            data = json.loads(m.group(1)) if m else {}
            scope = data.get("__DEFAULT_SCOPE__") if isinstance(data, dict) else None
            scope = scope if isinstance(scope, dict) else {}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            self._miss()
            return None
        ui = (scope.get("webapp.user-detail") or {}).get("userInfo") or {}
        user, st = ui.get("user") or {}, ui.get("stats") or {}
        if not user.get("secUid"):
            if not scope:
                self._miss()
            return None
        self.misses = 0
        return {"platform": "tiktok", "handle": (user.get("uniqueId") or handle).lower(),
                "name": user.get("nickname") or handle, "sec_uid": user["secUid"],
                "followers": _int(st.get("followerCount")), "likes": _int(st.get("heartCount") or st.get("heart")),
                "videos": _int(st.get("videoCount")), "verified": bool(user.get("verified")),
                "bio": (user.get("signature") or "")[:300], "private": bool(user.get("privateAccount"))}

    def videos(self, prof: dict, pages: int = 2) -> list[dict]:
        """The account's most recent videos (15 per page), newest first."""
        out: list[dict] = []
        cursor = str(int(time.time() * 1000))
        for _ in range(max(1, pages)):
            try:
                r = self._get("https://www.tiktok.com/api/creator/item_list/", **self.PARAMS, secUid=prof["sec_uid"],
                              count=str(self.PAGE), cursor=cursor, type="1", from_page="user", verifyFp="verify_")
                d = r.json() if r.text.strip() else {}
            except (requests.RequestException, ValueError):
                d = {}
            items = d.get("itemList") or []
            if not items:
                if not out and prof.get("videos"):
                    self._miss()
                break
            self.misses = 0
            out += [v for v in (self.normalize(x, prof) for x in items) if v]
            if not d.get("hasMorePrevious"):
                break
            cursor = str(min(_int(x.get("createTime")) for x in items) * 1000)
        return out

    @staticmethod
    def normalize(item: dict, prof: dict) -> dict | None:
        st = item.get("statsV2") or item.get("stats") or {}
        views = _int(st.get("playCount"))
        if not item.get("id") or views <= 0 or item.get("isAd"):
            return None
        desc = item.get("desc") or ""
        extra = [t for c in (item.get("contents") or []) for t in (c.get("textExtra") or [])]
        extra += item.get("textExtra") or []
        tags = hashtags(desc) | {str(c.get("title", "")).lower() for c in item.get("challenges") or [] if c.get("title")}
        tags |= {str(t.get("hashtagName")).lower() for t in extra if t.get("hashtagName")}
        tags -= {"", "fyp", "foryou", "foryoupage", "viral", "fy", "xyzbca"}
        # TikTok captions show a tagged account by its display name ("@Luke Bryan"); the tag data has the handle
        names = {}
        for t in extra:
            if t.get("userUniqueId"):
                shown = desc[_int(t.get("start")):_int(t.get("end"))].lstrip("@").strip()
                names[str(t["userUniqueId"]).lower()] = shown
        ment = list(names)
        sponsored = any(t.get("isCommerce") for t in extra) or any(re.search(r"partner|sponsor|^ad$|^ads$", t)
                                                                    for t in tags)
        handle = prof["handle"]
        return {
            "platform": "tiktok", "video_id": str(item["id"]),
            "url": f"https://www.tiktok.com/@{handle}/video/{item['id']}",
            "caption": desc[:600], "hashtags": sorted(tags), "views": float(views),
            "likes": float(_int(st.get("diggCount"))), "comments": float(_int(st.get("commentCount"))),
            "shares": float(_int(st.get("shareCount"))), "saves": float(_int(st.get("collectCount"))),
            "duration": _num((item.get("video") or {}).get("duration")), "author": handle,
            "author_name": prof.get("name") or handle, "author_followers": _num(prof.get("followers")) or None,
            "created_at": float(_int(item.get("createTime"))) or None,
            "music": ((item.get("music") or {}).get("title") or "")[:120],
            "mentions": [m for m in dict.fromkeys(ment) if m != handle],
            "mention_names": {m: n for m, n in names.items() if n and m != handle}, "sponsored": sponsored,
        }


class InstagramWeb:
    ENDPOINTS = (  # (url, user agent, app id): the app endpoint answers most often, the website one is the fallback
        ("https://i.instagram.com/api/v1/users/web_profile_info/", UA_IG_APP, "567067343352427"),
        ("https://www.instagram.com/api/v1/users/web_profile_info/", UA_DESKTOP, "936619743392459"),
    )

    def __init__(self, interval: float = 6.0, session: requests.Session | None = None, sleep=time.sleep,
                 clock=time.time) -> None:
        self.s = session or requests.Session()
        self.pacer = Pacer(interval, jitter=0.4, sleep=sleep, clock=clock)
        self.clock = clock
        self.requests = 0
        self.cool_until = 0.0
        self.cooldown = 120.0
        self.limited = 0
        self.blocked: str | None = None

    def profile(self, handle: str) -> tuple[dict | None, list[dict]]:
        """(account, its recent videos). Raises RateLimited when Instagram asks to wait."""
        if self.clock() < self.cool_until:
            raise RateLimited("Instagram asked to wait", self.cool_until)
        why = ""
        for url, ua, app in self.ENDPOINTS:
            self.pacer.wait()
            self.requests += 1
            try:
                r = self.s.get(url, params={"username": handle}, timeout=20,
                               headers={"User-Agent": ua, "x-ig-app-id": app, "Accept": "*/*",
                                        "Referer": f"https://www.instagram.com/{handle}/"})
            except requests.RequestException:
                why = why or "no answer"
                continue
            try:
                j = r.json()
            except ValueError:
                j = {}
            if not isinstance(j, dict):
                j = {}
            user = (j.get("data") or {}).get("user")
            if user:
                self.cooldown, self.limited = 120.0, 0
                return self.parse(user, handle)
            if "data" in j:
                return None, []   # no such account
            if r.status_code in (401, 403, 429) or "wait a few minutes" in str(j.get("message", "")).lower() \
                    or j.get("require_login"):
                why = "limited"
                continue
            if r.status_code >= 500 or not j:
                why = why or "no answer"
                continue
            return None, []       # an account Instagram can't show (e.g. a broken business profile)
        if why == "limited":
            self.limited += 1
            self.cool_until = self.clock() + self.cooldown
            self.cooldown = min(900.0, self.cooldown * 2)
            if self.limited >= 4:
                self.blocked = ("Instagram is limiting this connection - it will answer again in a few minutes "
                                "(TikTok and YouTube keep scanning)")
            raise RateLimited("Instagram asked to wait", self.cool_until)
        return None, []

    @staticmethod
    def parse(user: dict, handle: str) -> tuple[dict, list[dict]]:
        handle = (user.get("username") or handle).lower()
        followers = _int((user.get("edge_followed_by") or {}).get("count"))
        acct = {"platform": "instagram", "handle": handle, "name": user.get("full_name") or handle,
                "followers": followers, "videos": _int((user.get("edge_owner_to_timeline_media") or {}).get("count")),
                "verified": bool(user.get("is_verified")), "bio": (user.get("biography") or "")[:300],
                "private": bool(user.get("is_private"))}
        vids = []
        for e in ((user.get("edge_owner_to_timeline_media") or {}).get("edges") or []):
            n = e.get("node") or {}
            views = _int(n.get("video_view_count") or n.get("video_play_count"))
            if not n.get("is_video") or views <= 0 or not n.get("shortcode"):
                continue
            cap_edges = (n.get("edge_media_to_caption") or {}).get("edges") or []
            caption = ((cap_edges[0].get("node") or {}).get("text") or "") if cap_edges else ""
            likes = (n.get("edge_liked_by") or n.get("edge_media_preview_like") or {}).get("count")
            vids.append({
                "platform": "instagram", "video_id": n["shortcode"],
                "url": f"https://www.instagram.com/reel/{n['shortcode']}/", "caption": caption[:600],
                "hashtags": sorted(hashtags(caption) - {"reels", "viral", "explore", "fyp", "explorepage"}),
                "views": float(views), "likes": float(_int(likes)),
                "comments": float(_int((n.get("edge_media_to_comment") or {}).get("count"))),
                "shares": 0.0, "saves": 0.0, "duration": _num(n.get("video_duration")),
                "author": handle, "author_name": acct["name"], "author_followers": float(followers) or None,
                "created_at": float(_int(n.get("taken_at_timestamp"))) or None, "music": "",
                "mentions": [m for m in mentions(caption) if m != handle],
                "sponsored": bool(n.get("is_paid_partnership")) or "#ad" in caption.lower().split(),
            })
        return acct, vids
