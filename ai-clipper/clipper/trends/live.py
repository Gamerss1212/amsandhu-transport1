"""The live internet scan: 5-10 minutes of reading TikTok, Instagram and YouTube Shorts right now.

  * TikTok scanners read accounts (big creators, your focus, and every account a strong video tags) with the
    real numbers for each recent video: views, likes, comments, shares, saves.
  * The Instagram scanner does the same for Reels, pacing itself so Instagram keeps answering.
  * The trend analyst scans YouTube Shorts (creators, hashtags, searches incl. your focus) and hands the clip
    channels it finds to the TikTok and Instagram scanners (clip pages use the same name everywhere).
  * Hot videos are read again a few minutes later: the difference is how many views they gain per minute
    right now - what is going viral this moment, not last month.
  * The creator scout ranks the people whose clips go viral and checks who has fresh long videos on YouTube.

The scan runs at least `min_minutes` and stops by `max_minutes`. Every step shows on the agent board.
"""
from __future__ import annotations

import heapq
import itertools
import math
import re
import statistics
import threading
import time
from contextlib import contextmanager

from ..events import Event
from . import focus as focus_mod
from .creators import CreatorScout, human
from .free import collect_free
from .social import InstagramWeb, RateLimited, TikTokWeb

PLATFORM_NAME = {"tiktok": "TikTok", "instagram": "Instagram", "youtube_shorts": "YouTube Shorts"}
_NEWSY = re.compile(r"\b(news|breaking news|journalis\w*|newsroom|network|broadcast\w*|late night|tonight show|"
                    r"sports? (?:network|news|center)|official (?:store|shop))\b", re.I)
_TALKY = re.compile(r"clip|podcast|interview|motivat|mindset|speech|success|entrepreneur|millionaire|billionaire|"
                    r"business|money|wisdom|advice|lesson|talks?\b|\bshow\b|stor(y|ies)|comedy|standup|stand-up|"
                    r"reaction|debate|mentor|coach|grind|hustle|discipline", re.I)
SEEDS = ("your focus", "big creator")


class _NoBoard:
    @contextmanager
    def work(self, role, task):
        yield None

    def report(self, *a, **k):
        pass

    def beat(self, *a, **k):
        pass


def ago(seconds: float) -> str:
    s = max(0.0, seconds)
    return f"{s / 60:.0f} min" if s < 3600 else f"{s / 3600:.0f} h" if s < 172800 else f"{s / 86400:.0f} days"


def _short(text: str, n: int = 60) -> str:
    t = " ".join((text or "").split())
    return t[:n].rstrip() + ("..." if len(t) > n else "")


class LiveScan:
    def __init__(self, cfg, focus: dict | None = None, rep=None, board=None, known: set | None = None,
                 tiktok=None, instagram=None, shorts=None, creator_search=None, clock=time.time,
                 sleep=time.sleep) -> None:
        self.lc = cfg["trends"]["live"]
        self.fcfg = cfg["trends"]["free"]
        self.focus = focus if focus is not None else focus_mod.load(cfg)
        self.rep, self.board = rep, board or _NoBoard()
        self.known = set(known or ())
        self.clock, self.sleep = clock, sleep
        self.min_s = max(0.0, float(self.lc.get("min_minutes", 5)) * 60)
        self.max_s = max(self.min_s, float(self.lc.get("max_minutes", 10)) * 60)
        self.clients = {}
        if self.lc.get("tiktok", True):
            self.clients["tiktok"] = tiktok or TikTokWeb(float(self.lc.get("tiktok_interval", 0.9)))
        if self.lc.get("instagram", True):
            self.clients["instagram"] = instagram or InstagramWeb(float(self.lc.get("instagram_interval", 7)))
        self.shorts = shorts or collect_free
        self.scout = CreatorScout(self.focus, list(cfg["discovery"].get("famous_creators") or []),
                                  int(self.lc.get("creator_checks", 24)), search=creator_search, clock=clock)
        self.lock = threading.RLock()
        self.queues = {p: [] for p in self.clients}
        self.queued = {p: set() for p in self.clients}
        self.seq = itertools.count()
        self.accounts: dict[tuple[str, str], dict] = {}
        self.profiles: dict[tuple[str, str], dict] = {}
        self.videos: dict[tuple[str, str], dict] = {}
        self.snaps: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self.heat: dict[tuple[str, str], float] = {}
        self.reads: dict[tuple[str, str], int] = {}
        self.last_read: dict[tuple[str, str], float] = {}
        self.turn = {p: 0 for p in self.clients}
        self.mentions_followed = 0
        self.skipped = 0
        self.notes: list[str] = []
        self.now_doing: dict[str, str] = {}
        self.shorts_state, self.shorts_count = ("waiting" if self.lc.get("youtube_shorts", True) else "off"), 0
        self.shorts_done = not self.lc.get("youtube_shorts", True)
        self.creators: list[dict] = []
        self.t0 = 0.0
        self.running = False

    # ------------------------------------------------------------------ bookkeeping
    def elapsed(self) -> float:
        return self.clock() - self.t0

    def note(self, text: str) -> None:
        with self.lock:
            if text not in self.notes:
                self.notes.append(text)
        if self.rep:
            self.rep.info("trends", text)

    def skip_account(self, handle: str, name: str = "", bio: str = "") -> bool:
        """News, TV and sports networks go viral for reasons a clipper can't use."""
        words = [w.lower() for w in self.lc.get("skip_account_words", []) if w]
        return any(w in handle for w in words) or bool(_NEWSY.search(f"{name} {bio}"))

    def enqueue(self, platform: str, handle: str, source: str, priority: float, focus: bool = False,
                depth: int = 0) -> bool:
        if platform not in self.queues:
            return False
        h = (handle or "").lower().strip("@. ")
        if not re.fullmatch(r"[a-z0-9._]{2,30}", h) or depth > 2 or (depth and self.skip_account(h)):
            return False
        with self.lock:
            if h in self.queued[platform] or len(self.queued[platform]) >= int(self.lc.get("max_accounts", 300)):
                return False
            self.queued[platform].add(h)
            heapq.heappush(self.queues[platform], (-priority, next(self.seq), {"handle": h, "source": source,
                                                                               "focus": focus, "depth": depth}))
        return True

    def seed(self) -> None:
        w = self.focus.get("weight", 0.6)
        for p, k in (("tiktok", "tiktok_accounts"), ("instagram", "instagram_accounts")):
            for h in self.focus.get(k, []):
                self.enqueue(p, h, "your focus", 60 + 40 * w, True)
            for h in self.lc.get(k, []):
                self.enqueue(p, h, "big creator", 60)

    # ------------------------------------------------------------------ the scan
    def run(self) -> dict:
        self.t0 = self.clock()
        self.running = True
        self.seed()
        threads = []
        for p in self.clients:
            n = int(self.lc.get("tiktok_workers", 2)) if p == "tiktok" else 1
            role = "tt_scan" if p == "tiktok" else "ig_scan"
            threads += [threading.Thread(target=self._platform_worker, args=(p, role, i), daemon=True,
                                         name=f"scan-{p}-{i}") for i in range(max(1, n))]
        if not self.shorts_done:
            threads.append(threading.Thread(target=self._shorts_worker, daemon=True, name="scan-shorts"))
        threads.append(threading.Thread(target=self._creator_worker, daemon=True, name="scan-creators"))
        for t in threads:
            t.start()
        self.note(f"Live scan started: at least {self.min_s / 60:g} and at most {self.max_s / 60:g} minutes "
                  f"on {', '.join(PLATFORM_NAME[p] for p in (*self.clients, *(['youtube_shorts'] if not self.shorts_done else [])))}")
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=2.0 / max(1, len(threads)))
            self._status()
        self.running = False
        self.creators = self.scout.rank(self.scout.tally(list(self.videos.values()), self._account_list()))
        self._status(final=True)
        return self.result()

    def _dead(self) -> bool:
        """Nothing can answer (offline, or every platform refusing): stop instead of waiting out the clock."""
        if self.videos:
            return False
        blocked = all(getattr(c, "blocked", None) for c in self.clients.values())
        return blocked and self.shorts_done

    def _platform_idle_done(self, platform: str) -> bool:
        with self.lock:
            return (self.elapsed() >= self.min_s and not self.queues[platform] and self.shorts_done
                    and self._recheck_candidate(platform, reserve=False) is None)

    def _platform_worker(self, platform: str, role: str, idx: int) -> None:
        client = self.clients[platform]
        while True:
            el = self.elapsed()
            if el >= self.max_s or self._dead():
                break
            if client.blocked and platform == "tiktok":
                self.note(client.blocked)
                break
            job = self._next(platform)
            if job is None:
                if self._platform_idle_done(platform):
                    break
                self.sleep(1.0)
                continue
            kind, item = job
            try:
                if kind == "scan":
                    self._scan(platform, role, item)
                else:
                    self._recheck(platform, role, item)
            except RateLimited as exc:
                if kind == "scan":
                    with self.lock:
                        heapq.heappush(self.queues[platform], (-50, next(self.seq), item))
                wait_until = min(exc.until, self.t0 + self.max_s)
                self.board.beat(role, "", reason=f"Instagram asked to slow down - reading again in "
                                                 f"{max(0, wait_until - self.clock()):.0f}s")
                while self.clock() < wait_until and not self._platform_idle_done(platform):
                    self.sleep(1.0)
                if client.blocked:
                    self.note(client.blocked)
            except Exception as exc:  # one bad account never stops the scan
                self.note(f"{PLATFORM_NAME[platform]}: skipped one account ({type(exc).__name__})")

    def _next(self, platform: str):
        with self.lock:
            self.turn[platform] += 1
            due = self._recheck_candidate(platform, reserve=False)
            # every third job is a re-check when one is due, so views-per-minute gets measured during the scan
            if due is not None and (not self.queues[platform] or self.turn[platform] % 3 == 0):
                return "recheck", self._recheck_candidate(platform, reserve=True)
            if self.queues[platform]:
                return "scan", heapq.heappop(self.queues[platform])[2]
            return None

    def _recheck_candidate(self, platform: str, reserve: bool):
        now = self.clock()
        gap = float(self.lc.get("recheck_after_minutes", 3)) * 60
        top = 25 if platform == "tiktok" else 6
        limit = 4 if platform == "tiktok" else 2
        hot = sorted(((h, k) for k, h in self.heat.items() if k[0] == platform and h > 0), reverse=True)[:top]
        for _, k in hot:
            if self.reads.get(k, 0) < limit and now - self.last_read.get(k, now) >= gap:
                if reserve:
                    self.last_read[k] = now
                return k
        return None

    # ------------------------------------------------------------------ one account
    def _scan(self, platform: str, role: str, acct: dict) -> None:
        limited = self._scan_one(platform, role, acct)
        if limited is not None:  # raised outside the agent's work, so waiting is not counted as a failure
            raise limited

    def _scan_one(self, platform: str, role: str, acct: dict) -> RateLimited | None:
        h, name = acct["handle"], PLATFORM_NAME[platform]
        with self.board.work(role, f"Reading @{h} on {name} ({acct['source']})") as me:
            with self.lock:
                self.now_doing[me or f"{role}-{h}"] = f"{name}: reading @{h}"
            try:
                if platform == "tiktok":
                    prof = self.clients[platform].profile(h)
                    vids = []
                    if prof and self._big_enough(prof):
                        vids = self.clients[platform].videos(prof, pages=2 if acct["source"] in SEEDS else 1)
                else:
                    try:
                        prof, vids = self.clients[platform].profile(h)
                    except RateLimited as exc:
                        self.board.report(me, f"Instagram asked to slow down - coming back to @{h} in "
                                              f"{max(0, exc.until - self.clock()):.0f}s", "wait")
                        return exc
                if not prof:
                    self.board.report(me, f"@{h}: not found on {name} - skipped")
                    self.skipped += 1
                    return None
                if not self._big_enough(prof):
                    self.board.report(me, f"@{h}: only {human(prof['followers'])} followers"
                                          f"{' (private)' if prof.get('private') else ''} - skipped")
                    self.skipped += 1
                    return None
                if acct.get("depth", 0) and self.skip_account(h, prof.get("name", ""), prof.get("bio", "")):
                    self.board.report(me, f"@{h}: a news/TV/sports account - not clippable, skipped")
                    self.skipped += 1
                    return None
                n, new, best = self._add(platform, prof, vids, acct)
                msg = f"@{h} ({human(prof['followers'])} followers): {n} recent videos"
                if best:
                    msg += (f"; best \"{_short(best['caption'], 50)}\" {human(best['views'])} views in "
                            f"{ago(self.clock() - best['created_at']) if best.get('created_at') else '?'}")
                if new:
                    msg += f"; followed {new} tagged account(s)"
                self.board.report(me, msg)
                return None
            finally:
                with self.lock:
                    self.now_doing.pop(me or f"{role}-{h}", None)

    def _big_enough(self, prof: dict) -> bool:
        reach = max(prof.get("followers") or 0, (prof.get("likes") or 0) / 25)
        return reach >= float(self.lc.get("min_followers", 20000)) and not prof.get("private")

    def _add(self, platform: str, prof: dict, vids: list[dict], acct: dict) -> tuple[int, int, dict | None]:
        now = self.clock()
        max_age = float(self.lc.get("max_video_age_days", 30)) * 86400
        fresh = [v for v in vids if not v.get("created_at") or now - v["created_at"] <= max_age]
        k = (platform, prof["handle"])
        new = 0
        with self.lock:
            self.profiles[k] = prof
            self.reads[k] = self.reads.get(k, 0) + 1
            self.last_read[k] = now
            for v in fresh:
                v["depth"] = acct.get("depth", 0)
                self._keep(v, now)
            views = [v["views"] for v in fresh]
            heat = max((v["views"] / max(0.25, (now - v["created_at"]) / 3600) for v in fresh
                        if v.get("created_at") and now - v["created_at"] < 72 * 3600), default=0.0)
            self.heat[k] = heat
            best = max(fresh, key=lambda v: v["views"], default=None)
            self.accounts[k] = {
                "platform": platform, "handle": prof["handle"], "name": prof.get("name") or prof["handle"],
                "followers": prof.get("followers", 0), "source": acct["source"], "focus": acct.get("focus", False),
                "depth": acct.get("depth", 0),
                "videos": len(fresh), "median_views": statistics.median(views) if views else 0,
                "best": None if not best else {"caption": _short(best["caption"], 120), "views": best["views"],
                                               "url": best.get("url")},
                "url": (f"https://www.tiktok.com/@{prof['handle']}" if platform == "tiktok"
                        else f"https://www.instagram.com/{prof['handle']}/"),
            }
        if self.lc.get("follow_mentions", True):
            # only strong, unsponsored videos lead to new accounts, and ones that fit your focus go first
            # accounts found by the scan only lead further through videos that fit your focus (no drifting off)
            bar = max(float(self.lc.get("mention_min_views", 50000)), statistics.median(views) if views else 0)
            w = self.focus.get("weight", 0.6)
            depth = acct.get("depth", 0)
            for v in fresh:
                fit = v.get("focus_fit", 0)
                if v["views"] < bar or v.get("sponsored") or (depth >= 1 and fit <= 0):
                    continue
                for m in (v.get("mentions") or [])[:3]:
                    pr = 10 * math.log10(v["views"]) + 40 * w * fit - (0 if fit or acct.get("focus") else 15)
                    if self.enqueue(platform, m, f"tagged by @{prof['handle']}", pr, fit >= 0.5, depth + 1):
                        new += 1
            with self.lock:
                self.mentions_followed += new
        return len(fresh), new, best

    def _keep(self, v: dict, now: float) -> None:
        key = (v["platform"], v["video_id"])
        v["focus_fit"] = focus_mod.fit(v.get("caption", ""), self.focus)
        v["scanned_at"] = now
        snaps = self.snaps.setdefault(key, [])
        if not snaps or snaps[-1][1] != v["views"] or now - snaps[-1][0] > 1:
            snaps.append((now, v["views"]))
        t0, v0 = snaps[0]
        if now - t0 >= float(self.lc.get("velocity_min_seconds", 60)) and v["views"] >= v0:
            v["velocity"] = (v["views"] - v0) / ((now - t0) / 60)
            v["velocity_window"] = round(now - t0)
        elif key in self.videos and self.videos[key].get("velocity") is not None:
            v["velocity"] = self.videos[key]["velocity"]
            v["velocity_window"] = self.videos[key].get("velocity_window")
        self.videos[key] = v

    def _recheck(self, platform: str, role: str, k: tuple[str, str]) -> None:
        h = k[1]
        prof = self.profiles.get(k)
        if prof is None:
            return
        before = {vid: s[0] for (p, vid), s in self.snaps.items() if p == platform}
        limited = None
        with self.board.work(role, f"Re-reading @{h} on {PLATFORM_NAME[platform]} to measure views per minute") as me:
            if platform == "tiktok":
                vids = self.clients[platform].videos(prof, pages=1)
            else:
                try:
                    prof2, vids = self.clients[platform].profile(h)
                except RateLimited as exc:
                    self.board.report(me, f"Instagram asked to slow down - re-reading @{h} later", "wait")
                    limited = exc
                    prof2, vids = None, []
                prof = prof2 or prof
            prev = self.accounts.get(k, {})
            if vids:
                self._add(platform, prof, vids, {"source": prev.get("source", "re-check"),
                                                 "focus": prev.get("focus", False), "depth": prev.get("depth", 0)})
            else:
                with self.lock:
                    self.reads[k] = self.reads.get(k, 0) + 1
            gains = []
            for v in vids:
                first = before.get(v["video_id"])
                if first and v.get("velocity") is not None:
                    gains.append(v)
            if gains:
                g = max(gains, key=lambda v: v["velocity"])
                mins = (g.get("velocity_window") or 60) / 60
                self.board.report(me, f"@{h} again after {mins:.1f} min: \"{_short(g['caption'], 45)}\" "
                                      f"+{human(g['velocity'] * mins)} views = {human(g['velocity'])}/min right now")
            elif limited is None:
                self.board.report(me, f"@{h} again: no new views to measure yet")
        if limited is not None:
            raise limited

    # ------------------------------------------------------------------ YouTube Shorts
    def _shorts_worker(self) -> None:
        fcfg = dict(self.fcfg)
        fcfg["youtube_searches"] = list(fcfg.get("youtube_searches") or []) + list(self.focus.get("short_searches") or [])
        budget = max(1.0, (self.max_s - 90) / 60)
        fcfg["max_detail_minutes"] = min(float(fcfg.get("max_detail_minutes", 4)), budget)
        self.shorts_state = "scanning"
        try:
            with self.board.work("trend", "Scanning YouTube Shorts: big creators, hashtags and searches") as me:
                got = self.shorts(fcfg, self.known, progress=lambda f, m="": setattr(self, "shorts_state", m or "scanning"),
                                  log=lambda m: None) or []
                now = self.clock()
                max_age = float(self.lc.get("max_video_age_days", 30)) * 86400
                handles = {}
                with self.lock:
                    for v in got:
                        if v.get("created_at") and now - v["created_at"] > max_age:
                            continue
                        self._keep(v, now)
                        v["depth"] = 1
                        h = v.get("author_handle")
                        talky = _TALKY.search(f"{v.get('author', '')} {h} {v.get('caption', '')}")
                        if h and (talky or v.get("focus_fit", 0) >= 0.5) and not self.skip_account(h, v.get("author", "")):
                            handles[h] = max(handles.get(h, 0.0), v["views"] * (1 + v.get("focus_fit", 0)))
                    self.shorts_count = sum(1 for k in self.videos if k[0] == "youtube_shorts")
                best = max(got, key=lambda v: v["views"], default=None)
                sent = 0
                for h, score in sorted(handles.items(), key=lambda x: -x[1])[:60]:
                    for p in self.clients:  # clip pages usually use the same name on every platform
                        sent += self.enqueue(p, h, "YouTube Shorts clip channel", 5 * math.log10(1 + score), depth=1)
                self.board.report(me, f"YouTube Shorts: {len(got)} new videos"
                                      + (f"; best \"{_short(best['caption'], 45)}\" {human(best['views'])} views"
                                         if best else "") + (f"; sent {sent} clip channels to the TikTok/Instagram "
                                                             f"scanners" if sent else ""))
                self.shorts_state = "done"
        except Exception as exc:
            self.shorts_state = "failed"
            self.note(f"YouTube Shorts scan failed ({type(exc).__name__}) - TikTok and Instagram keep going")
        finally:
            self.shorts_done = True

    # ------------------------------------------------------------------ creator scout
    def _creator_worker(self) -> None:
        start_after = min(90.0, self.min_s / 3)
        while self.elapsed() < start_after and not self._dead():
            self.sleep(1.0)
        checked = 0
        while self.elapsed() < self.max_s and not self._dead():
            with self.lock:
                vids, accts = list(self.videos.values()), self._account_list()
            rows = self.scout.tally(vids, accts)
            self.creators = self.scout.rank(rows)
            nxt = self.scout.next_to_check(rows)
            if nxt is None:
                if all(not t for t in self.queues.values()) and self.shorts_done and self.elapsed() >= self.min_s:
                    break
                self.sleep(10.0 if len(self.scout.youtube) >= self.scout.checks else 3.0)
                continue
            with self.board.work("creators", f"Checking YouTube for long videos of {nxt['name']}") as me:
                try:
                    yt = self.scout.check(nxt)
                    checked += 1
                    self.board.report(me, f"{nxt['name']}: {nxt['clips']} clips in the scan "
                                          f"({human(nxt['clip_views'])} views); YouTube has {yt['long_videos']} long "
                                          f"videos, typically {human(yt['median_views'])} views")
                except Exception as exc:
                    self.scout.youtube[nxt["key"]] = {"long_videos": 0, "median_views": 0, "channels": 0,
                                                      "own_channel": None, "best": None, "query": "",
                                                      "error": type(exc).__name__}
                    self.board.report(me, f"{nxt['name']}: YouTube lookup failed - ranked on clips only", "fail")

    # ------------------------------------------------------------------ results
    def _account_list(self) -> list[dict]:
        return list(self.accounts.values())

    def viral_now(self, n: int = 30) -> list[dict]:
        now = self.clock()
        rows = []
        with self.lock:
            vids = list(self.videos.values())
        for v in vids:
            if not v.get("created_at") or now - v["created_at"] > 7 * 86400:
                continue
            age_h = max(0.25, (now - v["created_at"]) / 3600)
            measured = v.get("velocity") is not None
            rate = v["velocity"] if measured else v["views"] / age_h / 60
            fit = v.get("focus_fit", 0)
            # for you: your focus, the creators you seeded, and talk/clip content - not random viral noise
            rel = 1.0 if fit >= 0.5 or v.get("depth", 0) == 0 else 0.8 if _TALKY.search(
                f"{v.get('author', '')} {v.get('caption', '')}") else 0.3
            rows.append({"platform": v["platform"], "url": v.get("url"), "caption": _short(v.get("caption"), 140),
                         "author": v.get("author"), "views": v["views"], "age_hours": round(age_h, 1),
                         "per_min": round(rate, 1), "measured": measured, "focus_fit": fit, "for_you": rel >= 0.8,
                         "likes": v.get("likes", 0), "shares": v.get("shares", 0)})
        rows.sort(key=lambda r: -(r["per_min"] * (1.5 if r["measured"] else 1) * (1.0 if r["for_you"] else 0.4)))
        def pick(candidates: list[dict], k: int, taken: set) -> list[dict]:
            got, per_author = [], {}
            for r in candidates:
                if len(got) >= k:
                    break
                if per_author.get(r["author"], 0) >= 2 or r["url"] in taken:
                    continue
                per_author[r["author"]] = per_author.get(r["author"], 0) + 1
                got.append(r)
                taken.add(r["url"])
            return got
        taken: set = set()
        out = pick(rows, n, taken)
        # your niche's fastest-growing videos are always in the list, even when bigger niches outpace them
        out += pick([r for r in rows if r["focus_fit"] >= 0.5], max(5, n // 2), taken)
        return out

    def stats(self) -> dict:
        with self.lock:
            per = {}
            for p in (*self.clients, "youtube_shorts"):
                c = self.clients.get(p)
                per[p] = {"accounts": sum(1 for k in self.accounts if k[0] == p),
                          "queued": len(self.queues.get(p, [])),
                          "videos": sum(1 for k in self.videos if k[0] == p),
                          "requests": getattr(c, "requests", 0), "blocked": getattr(c, "blocked", None),
                          "waiting": max(0, round(getattr(c, "cool_until", 0) - self.clock()))}
            per["youtube_shorts"]["state"] = self.shorts_state
            measured = sum(1 for v in self.videos.values() if v.get("velocity") is not None)
            return {"elapsed": round(self.elapsed()), "min": self.min_s, "max": self.max_s, "running": self.running,
                    "videos": len(self.videos), "accounts": len(self.accounts), "measured": measured,
                    "mentions_followed": self.mentions_followed, "skipped": self.skipped, "platforms": per,
                    "now": list(self.now_doing.values())[:8], "notes": self.notes[-6:]}

    def _status(self, final: bool = False) -> None:
        if not self.rep:
            return
        st = self.stats()
        frac = min(1.0, st["elapsed"] / max(1.0, self.min_s)) if not final else 1.0
        mins = f"{st['elapsed'] // 60}:{st['elapsed'] % 60:02d}"
        msg = (f"Live scan {mins} (runs {self.min_s / 60:.0f}-{self.max_s / 60:.0f} min): {st['videos']:,} videos from "
               f"{st['accounts']} accounts" + (f", {st['measured']} re-measured" if st["measured"] else ""))
        self.rep.progress("trends", 0.85 * frac, msg)
        top = [{k: c[k] for k in ("name", "score", "clips", "clip_views", "focus_listed", "source")}
               for c in (self.creators or [])[:8]]
        self.rep.emit(Event("scan", "trends", msg, data={**st, "final": final, "hot": self.viral_now(8),
                                                         "creators": top}))

    def result(self) -> dict:
        st = self.stats()
        return {"videos": list(self.videos.values()), "accounts": sorted(self._account_list(),
                                                                         key=lambda a: -a["median_views"]),
                "viral_now": self.viral_now(30), "creators": [{k: v for k, v in c.items() if k != "key"}
                                                              for c in self.creators[:30]],
                "stats": st, "seconds": st["elapsed"], "notes": list(self.notes), "focus": self.focus.get("name", "")}
