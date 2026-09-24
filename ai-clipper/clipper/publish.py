"""Posting to TikTok and Instagram through their official APIs, at the best time for your audience.

Instagram: Instagram API (Instagram Login) - Reels are sent straight from this PC with a resumable
           upload, then published. Needs a Business/Creator account token with
           instagram_business_content_publish (and instagram_business_manage_insights for stats).
TikTok:    Content Posting API - FILE_UPLOAD in chunks. "direct" posts to the profile (apps TikTok
           has not audited yet may only post privately); "inbox" drops the video in your TikTok
           inbox to finish posting in the app. Needs video.publish / video.upload (+ video.list for stats).

Neither platform lets an app schedule a post on their side, so the app keeps the schedule and posts
when the time comes (keep AI Clipper open). The timing analyst learns the best hours from your own
posts' results, starting from typical short-video peak hours.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

IG_API = "https://graph.instagram.com/v21.0"
TT_API = "https://open.tiktokapis.com/v2"
TIMEOUT = 60


class PublishError(RuntimeError):
    pass


# ---------------------------------------------------------------- accounts
class Accounts:
    """Connected accounts, kept on this PC only (data/social_accounts.json)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, platform: str, data: dict | None) -> None:
        with self._lock:
            all_ = self.load()
            if data:
                all_[platform] = {**all_.get(platform, {}), **data}
            else:
                all_.pop(platform, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(all_, indent=1), encoding="utf-8")

    def status(self) -> dict:
        a = self.load()
        return {p: {"connected": bool(a.get(p, {}).get("access_token")), "name": a.get(p, {}).get("username", ""),
                    "mode": a.get(p, {}).get("mode", "")} for p in ("tiktok", "instagram")}


def _check(resp: requests.Response, what: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    err = data.get("error")
    if resp.status_code >= 400 or (isinstance(err, dict) and err.get("code") not in (None, "ok")):
        msg = (err or {}).get("message") if isinstance(err, dict) else err
        raise PublishError(f"{what} failed ({resp.status_code}): {msg or resp.text[:200]}")
    return data


# ---------------------------------------------------------------- Instagram
class Instagram:
    def __init__(self, account: dict, session: requests.Session | None = None) -> None:
        self.token = account["access_token"]
        self.user = account.get("user_id") or "me"
        self.http = session or requests.Session()

    def whoami(self) -> dict:
        data = _check(self.http.get(f"{IG_API}/me", params={"fields": "user_id,username",
                                                            "access_token": self.token}, timeout=TIMEOUT), "Instagram login")
        return {"user_id": data.get("user_id") or data.get("id"), "username": data.get("username", "")}

    def refresh(self) -> dict:
        data = _check(self.http.get("https://graph.instagram.com/refresh_access_token",
                                    params={"grant_type": "ig_refresh_token", "access_token": self.token},
                                    timeout=TIMEOUT), "Instagram token refresh")
        self.token = data["access_token"]
        return {"access_token": self.token, "expires_at": time.time() + float(data.get("expires_in", 5184000))}

    def publish(self, video: Path, caption: str, poll_s: float = 5.0, max_wait: float = 900) -> str:
        size = video.stat().st_size
        container = _check(self.http.post(f"{IG_API}/{self.user}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200], "share_to_feed": "true",
            "access_token": self.token}, timeout=TIMEOUT), "Instagram upload setup")
        cid, uri = container["id"], container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v21.0/{container['id']}"
        with open(video, "rb") as fh:
            _check(self.http.post(uri, data=fh, headers={"Authorization": f"OAuth {self.token}", "offset": "0",
                                                          "file_size": str(size)}, timeout=600), "Instagram upload")
        waited = 0.0
        while True:  # Instagram processes the video before it can be published
            st = _check(self.http.get(f"{IG_API}/{cid}", params={"fields": "status_code,status",
                                                                 "access_token": self.token}, timeout=TIMEOUT),
                        "Instagram processing check")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the video: {st.get('status') or code}")
            if waited >= max_wait:
                raise PublishError("Instagram is still processing the video - try again later")
            time.sleep(poll_s)
            waited += poll_s
        done = _check(self.http.post(f"{IG_API}/{self.user}/media_publish", data={
            "creation_id": cid, "access_token": self.token}, timeout=TIMEOUT), "Instagram publish")
        return str(done["id"])

    def stats(self, media_id: str) -> dict:
        data = _check(self.http.get(f"{IG_API}/{media_id}/insights", params={
            "metric": "views,likes,comments,shares,saved", "access_token": self.token}, timeout=TIMEOUT),
            "Instagram insights")
        return {m["name"]: (m.get("values") or [{}])[0].get("value", 0) for m in data.get("data", [])}

    def online_hours(self) -> dict[int, float]:
        """When your followers are online (hour of day -> share), if Instagram provides it."""
        try:
            data = _check(self.http.get(f"{IG_API}/{self.user}/insights", params={
                "metric": "online_followers", "period": "lifetime", "access_token": self.token},
                timeout=TIMEOUT), "Instagram audience hours")
            values = data["data"][0]["values"]
            totals: dict[int, float] = {}
            for day in values:
                for h, n in (day.get("value") or {}).items():
                    totals[int(h)] = totals.get(int(h), 0) + float(n)
            peak = max(totals.values(), default=0) or 1
            return {h: v / peak for h, v in totals.items()}
        except Exception:
            return {}


# ---------------------------------------------------------------- TikTok
class TikTok:
    def __init__(self, account: dict, session: requests.Session | None = None) -> None:
        self.account = account
        self.token = account["access_token"]
        self.mode = account.get("mode", "direct")
        self.http = session or requests.Session()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=UTF-8"}

    def refresh(self) -> dict:
        a = self.account
        if not (a.get("refresh_token") and a.get("client_key") and a.get("client_secret")):
            raise PublishError("TikTok token expired - paste a new one (or add client key/secret + refresh token)")
        data = _check(self.http.post(f"{TT_API}/oauth/token/", data={
            "client_key": a["client_key"], "client_secret": a["client_secret"], "grant_type": "refresh_token",
            "refresh_token": a["refresh_token"]}, timeout=TIMEOUT), "TikTok token refresh")
        self.token = data["access_token"]
        return {"access_token": self.token, "refresh_token": data.get("refresh_token", a["refresh_token"]),
                "expires_at": time.time() + float(data.get("expires_in", 86400))}

    def whoami(self) -> dict:
        data = _check(self.http.get(f"{TT_API}/user/info/", params={"fields": "open_id,display_name"},
                                    headers=self._h(), timeout=TIMEOUT), "TikTok login")
        u = data.get("data", {}).get("user", {})
        return {"open_id": u.get("open_id", ""), "username": u.get("display_name", "")}

    def publish(self, video: Path, caption: str, poll_s: float = 5.0, max_wait: float = 900) -> str:
        size = video.stat().st_size
        chunk = size if size <= 64 * 1024 * 1024 else 10 * 1024 * 1024
        count = max(1, size // chunk)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        if self.mode == "inbox":  # lands in your TikTok inbox to post from the app (works before app audit)
            init = _check(self.http.post(f"{TT_API}/post/publish/inbox/video/init/", headers=self._h(),
                                         json={"source_info": source}, timeout=TIMEOUT), "TikTok upload setup")
        else:
            info = _check(self.http.post(f"{TT_API}/post/publish/creator_info/query/", headers=self._h(),
                                         json={}, timeout=TIMEOUT), "TikTok account check")
            options = info.get("data", {}).get("privacy_level_options") or ["SELF_ONLY"]
            privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[0]
            init = _check(self.http.post(f"{TT_API}/post/publish/video/init/", headers=self._h(), json={
                "post_info": {"title": caption[:2200], "privacy_level": privacy, "disable_duet": False,
                              "disable_comment": False, "disable_stitch": False},
                "source_info": source}, timeout=TIMEOUT), "TikTok upload setup")
        publish_id, url = init["data"]["publish_id"], init["data"]["upload_url"]
        with open(video, "rb") as fh:
            for k in range(count):
                start = k * chunk
                end = size - 1 if k == count - 1 else start + chunk - 1
                fh.seek(start)
                body = fh.read(end - start + 1)
                resp = self.http.put(url, data=body, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}"})
                if resp.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok upload failed at chunk {k + 1}/{count} ({resp.status_code})")
        waited = 0.0
        while True:
            st = _check(self.http.post(f"{TT_API}/post/publish/status/fetch/", headers=self._h(),
                                       json={"publish_id": publish_id}, timeout=TIMEOUT), "TikTok status check")
            status = st.get("data", {}).get("status", "")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                ids = st["data"].get("publicaly_available_post_id") or st["data"].get("publicly_available_post_id") or []
                return str(ids[0]) if ids else publish_id
            if status == "FAILED":
                raise PublishError(f"TikTok rejected the video: {st['data'].get('fail_reason', 'unknown reason')}")
            if waited >= max_wait:
                return publish_id  # still processing on TikTok's side; it finishes there
            time.sleep(poll_s)
            waited += poll_s

    def stats(self, video_id: str) -> dict:
        data = _check(self.http.post(f"{TT_API}/video/query/", headers=self._h(),
                                     params={"fields": "id,view_count,like_count,comment_count,share_count"},
                                     json={"filters": {"video_ids": [video_id]}}, timeout=TIMEOUT), "TikTok stats")
        v = (data.get("data", {}).get("videos") or [{}])[0]
        return {"views": v.get("view_count", 0), "likes": v.get("like_count", 0),
                "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)}


CLIENTS = {"instagram": Instagram, "tiktok": TikTok}


# ---------------------------------------------------------------- best time to post
def _prior(weekday: int, hour: int) -> float:
    """Typical short-video engagement by local time (0-1): lunch and evening peaks, later on weekends."""
    weekend = weekday >= 5
    peaks = [(12.5, 1.6, 0.75), (19.5, 2.2, 1.0)] if not weekend else [(11.5, 2.0, 0.85), (20.0, 2.4, 1.0)]
    v = sum(h * math.exp(-0.5 * ((hour - c) / w) ** 2) for c, w, h in peaks)
    return 0.15 + 0.85 * min(1.0, v) if 7 <= hour <= 23 else 0.05


@dataclass
class TimingModel:
    """Best posting hour of the week: typical peaks, then your own results take over as they come in."""
    results: list[dict]            # {"weekday", "hour", "views"} of your past posts
    audience: dict[int, float]     # hour -> share of your followers online (Instagram), may be empty

    def score(self, weekday: int, hour: int) -> float:
        base = _prior(weekday, hour)
        if self.audience:
            base = 0.5 * base + 0.5 * self.audience.get(hour, 0.0)
        mine = [r for r in self.results if abs(r["hour"] - hour) <= 1 and (r["weekday"] >= 5) == (weekday >= 5)]
        if not self.results or not mine:
            return base
        overall = sum(math.log1p(r["views"]) for r in self.results) / len(self.results)
        here = sum(math.log1p(r["views"]) for r in mine) / len(mine)
        # difference of mean log-views = how many times better this hour did than your average
        lift = max(-3.0, min(2.0, here - overall))
        trust = len(mine) / (len(mine) + 3)  # a few posts nudge it, many posts decide it
        return max(0.001, base * math.exp(trust * lift))

    def best_slots(self, n: int, after: float, taken: list[float], max_per_day: int = 3,
                   gap_hours: float = 3.0, horizon_days: int = 7) -> list[float]:
        """The n best upcoming times (epoch seconds), spread out: at most max_per_day per day, gap_hours apart."""
        start = int(after // 3600 + 1) * 3600
        hours = [start + k * 3600 for k in range(horizon_days * 24)]
        ranked = sorted(hours, key=lambda t: -self.score(time.localtime(t).tm_wday, time.localtime(t).tm_hour))
        chosen = list(taken)
        out: list[float] = []
        for t in ranked:
            day = time.localtime(t)[:3]
            if sum(1 for c in chosen if time.localtime(c)[:3] == day) >= max_per_day:
                continue
            if any(abs(t - c) < gap_hours * 3600 for c in chosen):
                continue
            chosen.append(t)
            out.append(float(t))
            if len(out) >= n:
                break
        return sorted(out)


# ---------------------------------------------------------------- the posting service
class PostingService:
    """Keeps the schedule: the publisher agent posts what is due, the timing analyst collects each post's
    results a day later so the best-time model keeps learning."""

    def __init__(self, cfg, db, rep, board=None) -> None:
        self.cfg, self.db, self.rep, self.board = cfg, db, rep, board
        self.accounts = Accounts(cfg.path("paths.db").parent / "social_accounts.json")
        self._audience: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _work(self, role: str, task: str):
        from contextlib import nullcontext
        return self.board.work(role, task) if self.board else nullcontext()

    def client(self, platform: str):
        acct = self.accounts.load().get(platform)
        if not acct or not acct.get("access_token"):
            raise PublishError(f"{platform.title()} is not connected - add it under Posting")
        c = CLIENTS[platform](acct)
        if acct.get("expires_at") and acct["expires_at"] - time.time() < 3600:  # refresh ahead of expiry
            try:
                self.accounts.save(platform, c.refresh())
            except Exception as exc:
                self.rep.info("posting", f"{platform.title()} token refresh failed: {exc}")
        return c

    def connect(self, platform: str, data: dict) -> dict:
        data = {k: v.strip() for k, v in data.items() if isinstance(v, str) and v.strip()}
        if not data.get("access_token"):
            raise PublishError("Paste the access token")
        who = CLIENTS[platform]({**data}).whoami()
        self.accounts.save(platform, {**data, **who})
        return self.accounts.status()[platform]

    def timing(self, platform: str) -> TimingModel:
        results = []
        for p in self.db.posts("platform = ? AND status = 'posted' AND stats IS NOT NULL", (platform,)):
            lt = time.localtime(p["posted_at"] or p["scheduled_at"])
            results.append({"weekday": lt.tm_wday, "hour": lt.tm_hour,
                            "views": float((p["stats"] or {}).get("views", 0) or 0)})
        audience = {}
        if platform == "instagram" and self.accounts.status()["instagram"]["connected"]:
            at, cached = self._audience.get(platform, (0.0, {}))
            if time.time() - at > 86400:
                try:
                    cached = self.client("instagram").online_hours()
                except Exception:
                    cached = {}
                self._audience[platform] = (time.time(), cached)
            audience = cached
        return TimingModel(results, audience)

    def schedule(self, folder: str, name: str, platforms: list[str], when: str | float, caption: str) -> list[dict]:
        """when: "now", "best" (the timing analyst picks), or a time (epoch seconds)."""
        p = self.cfg.get("posting", {}) or {}
        out = []
        for platform in platforms:
            if not self.accounts.status()[platform]["connected"]:
                raise PublishError(f"{platform.title()} is not connected - add it under Posting")
            if when == "now":
                at = time.time()
            elif when == "best":
                with self._work("timing", f"Finding the best time to post on {platform.title()}"), self._lock:
                    taken = [x["scheduled_at"] for x in self.db.posts(
                        "platform = ? AND status IN ('scheduled', 'posting', 'posted') AND scheduled_at > ?",
                        (platform, time.time() - 86400))]
                    at = self.timing(platform).best_slots(1, time.time() + 600, taken,
                                                          int(p.get("max_per_day", 3)),
                                                          float(p.get("gap_hours", 3)))[0]
            else:
                at = float(when)
            pid = self.db.add_post(folder, name, platform, at, caption)
            out.append({"id": pid, "platform": platform, "scheduled_at": at})
        return out

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        out_dir = self.cfg.path("paths.output_dir")
        for post in self.db.posts("status = 'scheduled' AND scheduled_at <= ?", (now,)):
            video = out_dir / post["folder"] / f"{post['name']}.mp4"
            self.db.update_post(post["id"], status="posting", attempts=post["attempts"] + 1)
            try:
                if not video.exists():
                    raise PublishError("the clip file was removed")
                with self._work("publish", f"Posting {post['name']} to {post['platform'].title()}"):
                    remote = self.client(post["platform"]).publish(video, post["caption"] or "")
                self.db.update_post(post["id"], status="posted", remote_id=remote, posted_at=time.time(), error=None)
                self.rep.info("posting", f"Posted {post['name']} to {post['platform'].title()}")
            except Exception as exc:
                retry = post["attempts"] + 1 < 3 and not isinstance(exc, FileNotFoundError)
                self.db.update_post(post["id"], status="scheduled" if retry else "failed", error=str(exc)[:300],
                                    scheduled_at=now + 900 if retry else post["scheduled_at"])
                self.rep.error("posting", f"Posting {post['name']} to {post['platform'].title()} failed: {exc}"
                                          + (" - will retry in 15 min" if retry else ""))
        # a day after posting (and again at 3 and 7 days) the timing analyst reads the results
        for post in self.db.posts("status = 'posted' AND posted_at < ? AND posted_at > ?", (now - 86400, now - 8 * 86400)):
            age = now - post["posted_at"]
            due = post["stats_at"] is None or (age > 3 * 86400 and post["stats_at"] - post["posted_at"] < 3 * 86400) \
                or (age > 7 * 86400 and post["stats_at"] - post["posted_at"] < 7 * 86400)
            if not due or not post["remote_id"]:
                continue
            try:
                with self._work("timing", f"Reading results of {post['name']} on {post['platform'].title()}"):
                    stats = self.client(post["platform"]).stats(post["remote_id"])
                self.db.update_post(post["id"], stats=stats, stats_at=now)
            except Exception as exc:
                self.db.update_post(post["id"], stats_at=now)
                self.rep.info("posting", f"Could not read results for {post['name']}: {exc}")

    def run_forever(self, stop: threading.Event, every: float = 30.0) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # the scheduler must never die
                self.rep.error("posting", f"Scheduler error: {exc}")
            stop.wait(every)
