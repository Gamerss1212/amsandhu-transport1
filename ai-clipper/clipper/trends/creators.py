"""The creator scout: which people are worth clipping right now.

Evidence, not guesses:
  * demand   - how many short videos of them the live scan found on TikTok, Instagram and YouTube Shorts,
               how many views those got, how many went viral (clips by other accounts count most),
  * momentum - how fast their clips are gaining views right now (measured minutes apart during the scan),
  * supply   - whether YouTube has fresh long videos of them to clip (and how many views those get),
  * focus    - whether they fit your focus (e.g. motivation from successful people).
People who keep showing up in viral clip captions are found too, even if nobody listed them.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from collections import defaultdict

from . import focus as focus_mod

# first words that make a capitalised phrase a sentence start or a title, not a person
_NOT_NAMES = set("""a about after all an and any are as at be before best big but by can day did do does don dont
each even every everyone first follow for from full get go going god good great guy guys he her here him his how i
if in inside is it its just last let life like link listen live look lord love make man me money more most mr mrs
ms my never new next no not now of oh ok on once one only or our out part people please podcast real remember
she should so some stop success that the their them then there these they this those today top true truth two up
us voice want was watch way we what when where which while who why will with women world yes you your episode
motivation motivational mindset shorts clip clips reels tiktok instagram youtube official channel powerful
speech speeches talk talks interview interviews advice lesson lessons tips secret secrets rule rules rich poor
millionaire millionaires billionaire billionaires entrepreneur entrepreneurs business businesses story stories
daily morning night week year years month video videos highlights reaction reacts react funny moment moments
amazing incredible insane crazy epic ultimate inspirational inspiration discipline hustle grind wealth habits
habit mind brain health fitness gym workout diet life's success's club show news live full episode episodes
season trailer movie music song songs album tour game games match fight final finals world cup league team
king queen prince princess lady sir dr doctor coach mr. mrs. brand partner sponsored thank thanks happy birthday
merry christmas easter halloween summer winter spring fall monday tuesday wednesday thursday friday saturday
sunday january february march april may june july august september october november december""".split())
_NOT_PLACES = {"new york", "los angeles", "united states", "san francisco", "las vegas", "north america",
               "south africa", "hong kong", "wall street", "white house", "silicon valley", "saudi arabia"}
_NAME = re.compile(r"\b([A-Z][a-z]{1,15}(?:[-'][A-Z]?[a-z]+)?(?: [A-Z][a-z]{1,15}(?:[-'][A-Z]?[a-z]+)?){1,2})\b")


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def human(n: float) -> str:
    n = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            v = n / div
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{n:.0f}"


def _alias_patterns(aliases: list[str]) -> list[re.Pattern]:
    pats = []
    for a in aliases:
        a = a.strip()
        if not a:
            continue
        pats.append(re.compile(rf"(?<![\w]){re.escape(a)}(?![\w])", re.I))
        k = key(a)
        if len(k) >= 5:  # hashtag / handle forms: #alexhormozi, @garyvee
            pats.append(re.compile(rf"[#@]{re.escape(k)}\b", re.I))
    return pats


class CreatorScout:
    def __init__(self, focus: dict, famous: list[str], checks: int = 14, search=None, clock=time.time) -> None:
        self.focus, self.checks, self.clock = focus, checks, clock
        self._search = search
        self.catalog: dict[str, dict] = {}
        for name, aliases in focus_mod.people(focus):
            self._add(name, aliases, "your focus", True)
        for entry in famous or []:
            names = [n.strip() for n in str(entry).split("|") if n.strip()]
            if names:
                self._add(names[0], names, "big creator", False)
        self.youtube: dict[str, dict] = {}     # key -> YouTube lookup
        self.last: list[dict] = []

    def _add(self, name: str, aliases: list[str], source: str, focus: bool) -> None:
        k = key(name)
        if k and k not in self.catalog:
            subs = {a.lower() for a in aliases if a.strip()} | {key(a) for a in aliases if len(key(a)) >= 5}
            self.catalog[k] = {"name": name, "aliases": aliases, "source": source, "focus": focus,
                               "patterns": _alias_patterns(aliases), "subs": subs}

    # ------------------------------------------------------------ evidence from the scan (no network)
    def tally(self, videos: list[dict], accounts: list[dict]) -> list[dict]:
        videos = [v for v in videos if v.get("views")]
        if not videos:
            return []
        by_platform = defaultdict(list)
        for v in videos:
            by_platform[v["platform"]].append(v["views"])
        viral_bar = {p: sorted(vs)[int(0.8 * (len(vs) - 1))] for p, vs in by_platform.items()}
        # accounts whose display name is a person's name: their handle maps to that person
        handle_name = {}
        for a in accounts:
            nm = (a.get("name") or "").strip()
            if a.get("handle") and _looks_like_person(nm):
                handle_name[a["handle"].lower()] = nm
        # people found in captions of strong videos (not in the catalog yet)
        found = defaultdict(lambda: {"videos": 0, "authors": set()})
        strong = [v for v in videos if v["views"] >= statistics.median(by_platform[v["platform"]])]
        for v in strong:
            names = {n for n in _NAME.findall(v.get("caption") or "") if _looks_like_person(n)}
            names |= {handle_name[m] for m in v.get("mentions") or [] if m in handle_name}
            names |= {n for n in (v.get("mention_names") or {}).values() if _looks_like_person(n)}
            for n in names:
                f = found[key(n)]
                f["name"] = n
                f["videos"] += 1
                f["authors"].add(v.get("author"))
        for k, f in found.items():
            if k not in self.catalog and f["videos"] >= 3 and len(f["authors"]) >= 2:
                self._add(f["name"], [f["name"]], "found in viral clips", False)

        rows = []
        now = self.clock()
        texts = [(v, " ".join([v.get("caption") or "", " ".join("#" + t for t in v.get("hashtags") or []),
                               " ".join("@" + m for m in v.get("mentions") or [])]),
                  {key(v.get("author_name") or ""), key(v.get("author") or "")}) for v in videos]
        texts = [(v, t, t.lower(), a) for v, t, a in texts]
        for k, c in self.catalog.items():
            clips, own = [], []
            for v, text, low, authors in texts:
                if k in authors:
                    own.append(v)
                elif any(s in low for s in c["subs"]) and any(p.search(text) for p in c["patterns"]):
                    clips.append(v)
            evidence = clips + own
            # clips by other accounts are the proof people want to watch them; their own posts count less
            views = sum(v["views"] for v in clips) + 0.25 * sum(v["views"] for v in own)
            viral = sum(v["views"] >= viral_bar.get(v["platform"], math.inf) for v in evidence)
            rates = [v.get("velocity") or 0.0 for v in evidence]
            per_hour = [v["views"] / max(1.0, (now - v["created_at"]) / 3600) for v in evidence
                        if v.get("created_at") and now - v["created_at"] < 7 * 86400]
            best = max(evidence, key=lambda v: v["views"], default=None)
            fits = [focus_mod.fit(v.get("caption") or "", self.focus) for v in evidence]
            rows.append({
                "key": k, "name": c["name"], "source": c["source"], "focus_listed": c["focus"],
                "clips": len(clips), "own_videos": len(own), "clip_views": views, "viral_clips": int(viral),
                "platforms": sorted({v["platform"] for v in evidence}),
                "clip_accounts": len({v.get("author") for v in clips}),
                "velocity": max(rates, default=0.0), "per_hour": max(per_hour, default=0.0),
                "focus_fit": 1.0 if c["focus"] else (sum(fits) / len(fits) if fits else 0.0),
                "best_clip": None if not best else {
                    "platform": best["platform"], "url": best.get("url"), "caption": (best.get("caption") or "")[:140],
                    "views": best["views"], "author": best.get("author")},
            })
        return rows

    # ------------------------------------------------------------ YouTube supply (network, one person at a time)
    def query(self, name: str) -> str:
        return f"{name} full episode" if re.search(r"podcast|show|theory|lab\b", name, re.I) else f"{name} interview"

    def check(self, row: dict) -> dict:
        name = row["name"]
        search = self._search
        if search is None:
            from ..discovery.youtube import ytdlp_search as search
        found = search(self.query(name), 20, 180, "long") or []
        token = max(re.findall(r"[a-z0-9']{3,}", name.lower()), key=len, default=name.lower())
        rel = [v for v in found if token in (v.get("title", "") + " " + v.get("channel", "")).lower()]
        views = [v["views"] for v in rel if v.get("views")]
        best = max(rel, key=lambda v: v.get("views", 0), default=None)
        own = next((v.get("channel") for v in rel if key(v.get("channel", "")) == row["key"]), None)
        out = {"long_videos": len(rel), "median_views": statistics.median(views) if views else 0,
               "channels": len({v.get("channel") for v in rel}), "own_channel": own, "query": self.query(name),
               "best": None if not best else {"title": best.get("title", "")[:120], "views": best.get("views", 0),
                                              "channel": best.get("channel", ""),
                                              "url": f"https://www.youtube.com/watch?v={best['video_id']}"}}
        self.youtube[row["key"]] = out
        return out

    def next_to_check(self, rows: list[dict]) -> dict | None:
        if len(self.youtube) >= self.checks:
            return None
        ranked = self.rank(rows)
        return next((r for r in ranked if r["key"] not in self.youtube), None)

    # ------------------------------------------------------------ the ranking
    def rank(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        w = float(self.focus.get("weight", 0.6))
        wf = 0.4 * w
        wd, wm, ws = (1 - wf) * 0.45, (1 - wf) * 0.2, (1 - wf) * 0.35

        def pct(vals: list[float]) -> list[float]:
            order = sorted(vals)
            n = len(vals)
            return [0.0 if v <= 0 else (order.index(v) + order.count(v) / 2) / n for v in vals]

        demand_v = pct([math.log10(1 + r["clip_views"]) + 0.3 * r["viral_clips"] + 0.4 * min(r["clip_accounts"], 8)
                        for r in rows])
        moment_v = pct([r["velocity"] * 60 + r["per_hour"] for r in rows])
        out = []
        for r, d, m in zip(rows, demand_v, moment_v):
            yt = self.youtube.get(r["key"])
            if yt is None:
                supply = 0.3  # not looked up yet
            else:
                supply = min(1.0, math.log10(1 + yt["median_views"]) / 6.3) * min(1.0, yt["long_videos"] / 8)
            score = 100 * (wd * d + wm * m + ws * supply + wf * r["focus_fit"])
            out.append({**r, "score": round(score, 1), "youtube": yt, "reasons": self.reasons(r, yt)})
        out.sort(key=lambda r: -r["score"])
        self.last = out
        return out

    @staticmethod
    def reasons(r: dict, yt: dict | None) -> list[str]:
        why = []
        if r["clips"] or r["own_videos"]:
            where = ", ".join({"tiktok": "TikTok", "instagram": "Instagram", "youtube_shorts": "YouTube Shorts"}
                              .get(p, p) for p in r["platforms"])
            if r["clips"]:
                why.append(f"{r['clips']} clips of them by {r['clip_accounts']} other account(s)"
                           + (f" + {r['own_videos']} of their own videos" if r["own_videos"] else "")
                           + f" on {where}: {human(r['clip_views'])} views, {r['viral_clips']} went viral")
            else:
                why.append(f"{r['own_videos']} of their own videos on {where}, {r['viral_clips']} went viral "
                           "(no clip pages posting them yet - less competition)")
        else:
            why.append("No clips of them in this scan yet")
        if r["velocity"] >= 1:
            why.append(f"A video of them is gaining {human(r['velocity'])} views per minute right now")
        elif r["per_hour"] >= 1000:
            why.append(f"Their newest videos average up to {human(r['per_hour'])} views per hour")
        if yt:
            if yt["long_videos"]:
                why.append(f"YouTube: {yt['long_videos']} long videos in the last 6 months on {yt['channels']} channel(s), "
                           f"typically {human(yt['median_views'])} views" + (f" (own channel: {yt['own_channel']})"
                                                                            if yt.get("own_channel") else ""))
            else:
                why.append("YouTube: no recent long videos found to clip")
        if r["focus_listed"]:
            why.append("On your focus list")
        elif r["focus_fit"] >= 0.5:
            why.append("Their clips match your focus")
        if r["source"] == "found in viral clips":
            why.append("Found by the scan: keeps showing up in viral clip captions")
        return why


def _looks_like_person(name: str) -> bool:
    parts = name.split()
    if not 2 <= len(parts) <= 3:
        return False
    low = name.lower()
    if low in _NOT_PLACES or any(p.lower().strip("'") in _NOT_NAMES for p in parts):
        return False
    return all(p[0].isupper() and p[1:].replace("-", "").replace("'", "").islower() for p in parts)
