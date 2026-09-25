"""System backtest: 100+ scenarios for the parts the render stress test does not cover.

  social    - TikTok / Instagram payloads with missing, wrong-typed, huge or hostile fields
  livescan  - whole live scans against simulated platforms: failures, rate limits, blocks, mention loops,
              huge account graphs, slow answers, crashing clients, old-only videos
  creators  - the creator scout on random captions, odd names, regex characters, failing YouTube lookups
  discovery - ranking and search planning on missing / zero / odd values
  focus     - corrupt or wrong-typed focus files and payloads
  api       - every web endpoint with bad input (nothing may answer 500)
  pipeline  - the 150-agent crew picking and editing clips from awkward generated videos

Every scenario checks invariants, not just "did not crash".
usage: python tools/system_backtest.py [--only group,group] [--seed S] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipper.analysis.transcribe import group_segments  # noqa: E402
from clipper.config import load_config  # noqa: E402
from clipper.media import ffmpeg_exe  # noqa: E402
from clipper.trends import focus as focus_mod  # noqa: E402
from clipper.trends.creators import CreatorScout  # noqa: E402
from clipper.trends.live import LiveScan  # noqa: E402
from clipper.trends.social import InstagramWeb, RateLimited, TikTokWeb  # noqa: E402

CASES: list[tuple[str, str, object]] = []


def case(group: str, name: str):
    def reg(fn):
        CASES.append((group, name, fn))
        return fn
    return reg


def fresh_cfg(root: Path):
    c = load_config(root / "missing.yaml")
    c["paths"] = {"work_dir": str(root / "work"), "output_dir": str(root / "out"), "db": str(root / "db.sqlite")}
    for d in ("work", "out"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for key in ("music_dir", "sfx_dir", "broll_dir", "fonts_dir"):
        c["editing"][key] = str(root / key)
    c["trends"]["import_dir"] = str(root / "imports")
    c["trends"]["free"]["enabled"] = False
    c["trends"]["live"]["enabled"] = False
    return c


# ================================================================ social payloads
JUNK = [None, "", "abc", -5, 0, 1e30, "1.2M", [], {}, "999999999999999999999", float("nan"), True]


def check_video(v, platform):
    if v is None:
        return []
    bad = []
    for k in ("views", "likes", "comments", "shares", "saves", "duration"):
        x = v.get(k)
        if not isinstance(x, float) or x != x or x < 0:
            bad.append(f"{k}={x!r}")
    if v["platform"] != platform or not v["url"].startswith("https://"):
        bad.append("bad url/platform")
    if not all(isinstance(m, str) and m for m in v.get("mentions", [])):
        bad.append("bad mentions")
    if not isinstance(v.get("caption"), str) or not isinstance(v.get("hashtags"), list):
        bad.append("bad caption/hashtags")
    return bad


def _tt_item(rng):
    desc = rng.choice(["", "hi @Some Body #fyp", "🔥" * 300, None, "@x", "#a#b#c", "{\\an8} \\N", "a" * 5000])
    item = {"id": rng.choice(["7", 7, "", None, "123456789012345678901"]), "desc": desc,
            "createTime": rng.choice(JUNK + [1790000000]),
            "stats": rng.choice([None, {}, {"playCount": rng.choice(JUNK)}]),
            "statsV2": rng.choice([None, {}, {k: rng.choice(JUNK + ["100"]) for k in
                                              ("playCount", "diggCount", "commentCount", "shareCount", "collectCount")}]),
            "video": rng.choice([None, {}, {"duration": rng.choice(JUNK)}]),
            "music": rng.choice([None, {}, {"title": rng.choice([None, "x" * 500])}]),
            "challenges": rng.choice([None, [], [{"title": None}, {"title": "Tag"}, {}]]),
            "contents": rng.choice([None, [], [{"textExtra": None}],
                                    [{"textExtra": [{"start": rng.choice(JUNK), "end": rng.choice(JUNK),
                                                     "userUniqueId": rng.choice([None, "", "UPPER.Case", 5])},
                                                    {"hashtagName": rng.choice([None, "", "partner"]),
                                                     "isCommerce": rng.choice([None, True])}]}]]),
            "isAd": rng.choice([None, False, True])}
    return item


for k in range(20):
    @case("social", f"tiktok_junk_{k}")
    def _(ctx, k=k):
        rng = random.Random(k)
        prof = {"handle": rng.choice(["h", "a.b_c"]), "name": rng.choice([None, "N"]), "followers": rng.choice(JUNK)}
        problems = []
        for _ in range(40):
            v = TikTokWeb.normalize(_tt_item(rng), prof)
            problems += check_video(v, "tiktok")
        return problems


def _ig_user(rng):
    def node():
        return {"is_video": rng.choice([True, True, False, None]), "shortcode": rng.choice(["Dd1", None, "", "x" * 60]),
                "video_view_count": rng.choice(JUNK), "video_play_count": rng.choice(JUNK),
                "taken_at_timestamp": rng.choice(JUNK), "video_duration": rng.choice(JUNK),
                "edge_liked_by": rng.choice([None, {}, {"count": rng.choice(JUNK)}]),
                "edge_media_to_comment": rng.choice([None, {}, {"count": rng.choice(JUNK)}]),
                "edge_media_to_caption": rng.choice([None, {}, {"edges": []}, {"edges": [{}]},
                                                     {"edges": [{"node": {"text": rng.choice([None, "@a @b #c", "é"])}}]}]),
                "is_paid_partnership": rng.choice([None, True])}
    return {"username": rng.choice([None, "User.Name", ""]), "full_name": rng.choice([None, "Full"]),
            "edge_followed_by": rng.choice([None, {}, {"count": rng.choice(JUNK)}]),
            "edge_owner_to_timeline_media": rng.choice([None, {}, {"edges": None},
                                                        {"count": rng.choice(JUNK), "edges": [{"node": node()} for _ in range(12)] + [{}]}]),
            "is_private": rng.choice([None, True, False]), "biography": rng.choice([None, "bio"])}


for k in range(12):
    @case("social", f"instagram_junk_{k}")
    def _(ctx, k=k):
        rng = random.Random(100 + k)
        problems = []
        for _ in range(20):
            acct, vids = InstagramWeb.parse(_ig_user(rng), "fallback")
            if not isinstance(acct.get("followers"), int) or acct["followers"] < 0:
                problems.append(f"followers={acct.get('followers')!r}")
            for v in vids:
                problems += check_video(v, "instagram")
        return problems


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class _Sess:
    def __init__(self, answers):
        self.answers, self.headers = list(answers), {}

    def get(self, *a, **k):
        x = self.answers.pop(0) if self.answers else _Resp(500, "")
        if isinstance(x, Exception):
            raise x
        return x


IG_ANSWERS = {
    "html_page": [_Resp(200, "<html>login</html>")] * 2,
    "list_body": [_Resp(200, [1, 2])] * 2,
    "server_errors": [_Resp(502, "")] * 2,
    "timeouts": [__import__("requests").Timeout("slow")] * 2,
    "not_found": [_Resp(200, {"data": {"user": None}, "status": "ok"})],
    "wait_then_ok": [_Resp(200, {"message": "Please wait a few minutes", "status": "fail"}),
                     _Resp(200, {"data": {"user": {"username": "ok", "edge_followed_by": {"count": 5}}}})],
    "forbidden": [_Resp(403, {"message": "login required", "require_login": True})] * 2,
}
for label, answers in IG_ANSWERS.items():
    @case("social", f"instagram_http_{label}")
    def _(ctx, answers=answers, label=label):
        ig = InstagramWeb(interval=0, session=_Sess(answers), sleep=lambda s: None)
        try:
            acct, vids = ig.profile("someone")
        except RateLimited as e:
            return [] if label in ("forbidden",) and e.until > time.time() else [f"unexpected RateLimited ({label})"]
        except Exception as exc:
            return [f"CRASH {type(exc).__name__}: {exc}"]
        want_acct = label == "wait_then_ok"
        return [] if bool(acct) == want_acct else [f"{label}: account={acct}"]


TT_PAGES = {
    "no_script": _Resp(200, "<html>captcha</html>"),
    "broken_json": _Resp(200, '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="x">{broken</script>'),
    "no_user": _Resp(200, '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="x">{"__DEFAULT_SCOPE__":{}}</script>'),
    "list_scope": _Resp(200, '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="x">[1]</script>'),
    "stats_none": _Resp(200, '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="x">{"__DEFAULT_SCOPE__":'
                             '{"webapp.user-detail":{"userInfo":{"user":{"secUid":"S","uniqueId":"U"},"stats":null}}}}</script>'),
}
for label, resp in TT_PAGES.items():
    @case("social", f"tiktok_page_{label}")
    def _(ctx, resp=resp, label=label):
        tt = TikTokWeb(interval=0, session=_Sess([resp, _Resp(200, ""), _Resp(200, {"itemList": None})]),
                       sleep=lambda s: None)
        try:
            p = tt.profile("u")
            vids = tt.videos(p, 2) if p else []
        except Exception as exc:
            return [f"CRASH {type(exc).__name__}: {exc}"]
        if label == "stats_none":
            return [] if p and p["followers"] == 0 and vids == [] else [f"stats_none: {p}"]
        return [] if p is None else [f"{label}: expected None, got {p}"]


# ================================================================ live scans on simulated platforms
class SimPlatform:
    """A random world of accounts that tag each other, with failures injected."""

    def __init__(self, rng, platform, n_accounts=60, fail=0.0, limit_every=0, crash=0.0, slow=0.0,
                 old_only=False, dead=False, loops=False, weird_handles=False):
        self.rng, self.platform, self.fail, self.limit_every, self.crash, self.slow = rng, platform, fail, limit_every, crash, slow
        self.old_only, self.dead, self.requests, self.blocked, self.cool_until = old_only, dead, 0, None, 0.0
        self.handles = [f"acct{i}" for i in range(n_accounts)]
        if weird_handles:
            self.handles += ["UPPER", "with space", "x" * 40, "émoji🔥", "..", "a", "dot.ted_", "@at"]
        self.tags = {h: ([self.handles[(i + 1) % len(self.handles)]] if loops else
                         rng.sample(self.handles, min(3, len(self.handles)))) for i, h in enumerate(self.handles)}
        self.reads = Counter()

    def _maybe_fail(self):
        self.requests += 1
        if self.slow and self.rng.random() < self.slow:
            time.sleep(0.2)
        if self.crash and self.rng.random() < self.crash:
            raise ValueError("simulated crash")
        if self.limit_every and self.requests % self.limit_every == 0:
            raise RateLimited("sim limit", time.time() + 0.3)

    def _prof(self, h):
        if self.dead or (self.fail and self.rng.random() < self.fail):
            return None
        return {"platform": self.platform, "handle": h.lower(), "name": h.title(), "sec_uid": h,
                "followers": self.rng.choice([10, 50_000, 2_000_000]), "likes": 1e6, "videos": 10,
                "bio": self.rng.choice(["", "Breaking news network"])}

    def _vids(self, h):
        self.reads[h] += 1
        now = time.time()
        out = []
        for k in range(self.rng.randint(0, 8)):
            age = (90 if self.old_only else self.rng.uniform(0.2, 200)) * 3600
            views = self.rng.choice([0, 100, 60_000, 3_000_000]) * (1 + 0.3 * self.reads[h])
            out.append({"platform": self.platform, "video_id": f"{h}-{k}", "url": f"https://sim/{h}/{k}",
                        "caption": self.rng.choice(["Alex Hormozi on money and discipline", "lol", "Logan Paul reacts",
                                                    "billionaire mindset success", "", "🔥"]),
                        "hashtags": [], "views": float(views), "likes": 1.0, "comments": 1.0, "shares": 1.0,
                        "saves": 1.0, "duration": 30.0, "author": h.lower(), "author_name": h.title(),
                        "author_followers": 1e6, "created_at": now - (age if not self.old_only else 90 * 86400),
                        "music": "", "mentions": list(self.tags.get(h, [])) if views else [],
                        "sponsored": self.rng.random() < 0.1})
        return out


class SimTikTok(SimPlatform):
    def profile(self, h):
        self._maybe_fail()
        return self._prof(h)

    def videos(self, prof, pages=2):
        self._maybe_fail()
        return self._vids(prof["handle"])


class SimInstagram(SimPlatform):
    def profile(self, h):
        self._maybe_fail()
        p = self._prof(h)
        return (p, self._vids(h) if p else [])


def sim_shorts(rng, fail=False):
    def run(fcfg, known, progress=None, log=None):
        if fail:
            raise RuntimeError("youtube down")
        now = time.time()
        return [{"platform": "youtube_shorts", "video_id": f"s{k}", "url": f"https://yt/{k}", "caption": rng.choice(
            ["Grant Cardone motivation speech", "cat", "Elon Musk interview clip"]), "hashtags": [],
                 "views": float(rng.randint(0, 5_000_000)), "likes": 0.0, "comments": 0.0, "shares": 0.0, "saves": 0.0,
                 "duration": 40.0, "author": rng.choice(["Clips Hub", "News 24"]), "author_handle": rng.choice(
                ["acct1", "cnnnews", "", "clipshub"]), "author_followers": None,
                 "created_at": rng.choice([None, now - 3600, now - 90 * 86400]), "music": ""} for k in range(30)]
    return run


def sim_search(rng, fail=0.0):
    def run(q, limit, within_days=None, length=None):
        if rng.random() < fail:
            raise RuntimeError("search failed")
        return [{"video_id": f"v{k}", "title": rng.choice([q, "unrelated", ""]), "channel": rng.choice(["Chan", q]),
                 "views": float(rng.choice([0, 10_000, 2_000_000])), "duration": 3600.0} for k in range(rng.randint(0, 10))]
    return run


SCAN_SCENARIOS = {
    "healthy": {}, "flaky_20pct": {"fail": 0.2}, "flaky_80pct": {"fail": 0.8}, "rate_limit_every_3": {"limit_every": 3},
    "rate_limit_every_1": {"limit_every": 1}, "client_crashes_30pct": {"crash": 0.3}, "slow": {"slow": 0.5},
    "old_videos_only": {"old_only": True}, "everything_dead": {"dead": True}, "mention_loop": {"loops": True},
    "huge_graph": {"n_accounts": 900}, "tiny_graph": {"n_accounts": 1}, "weird_handles": {"weird_handles": True},
}
for label, opts in SCAN_SCENARIOS.items():
    for variant in ("both", "tiktok_only", "shorts_fail"):
        @case("livescan", f"{label}_{variant}")
        def _(ctx, label=label, opts=opts, variant=variant):
            rng = random.Random(hash((label, variant)) % 10_000)
            cfg = fresh_cfg(ctx["tmp"] / f"scan_{label}_{variant}")
            lc = cfg["trends"]["live"]
            lc.update(enabled=True, min_minutes=0.02, max_minutes=0.08, recheck_after_minutes=0.005,
                      velocity_min_seconds=0.2, tiktok_accounts=["acct0", "acct1", "Acct0", "bad handle!"],
                      instagram_accounts=["acct2"], max_accounts=150, creator_checks=4,
                      instagram=variant != "tiktok_only")
            cfg["focus"].update(tiktok_accounts=["acct3"], instagram_accounts=["acct3"])
            tt = SimTikTok(rng, "tiktok", **opts)
            ig = SimInstagram(rng, "instagram", **opts)
            scan = LiveScan(cfg, tiktok=tt, instagram=ig, shorts=sim_shorts(rng, variant == "shorts_fail"),
                            creator_search=sim_search(rng, 0.3))
            t = time.time()
            res = scan.run()
            took = time.time() - t
            p = []
            if took > 0.08 * 60 + 8:
                p.append(f"overran: {took:.1f}s")
            if took < 0.02 * 60 - 0.2 and label != "everything_dead":
                p.append(f"stopped before the minimum: {took:.1f}s")
            keys = [(a["platform"], a["handle"]) for a in res["accounts"]]
            if len(keys) != len(set(keys)):
                p.append("duplicate accounts")
            if any(n > 150 for n in Counter(k[0] for k in keys).values()):
                p.append("max_accounts exceeded")
            vids = res["videos"]
            if any((v.get("velocity") or 0) < 0 for v in vids):
                p.append("negative velocity")
            urls = [r["url"] for r in res["viral_now"]]
            if len(urls) != len(set(urls)):
                p.append("duplicate viral_now rows")
            sc = [c["score"] for c in res["creators"]]
            if sc != sorted(sc, reverse=True):
                p.append("creators not sorted")
            st = res["stats"]
            if sum(x["videos"] for x in st["platforms"].values()) != st["videos"]:
                p.append("stats don't add up")
            if any("news" in a["handle"] for a in res["accounts"] if a.get("depth")):
                p.append("news account scanned")
            if any(v.get("created_at") and time.time() - v["created_at"] > 31 * 86400 for v in vids):
                p.append("old video kept")
            json.dumps(res)  # everything must be storable
            return p


# ================================================================ creator scout
for k in range(12):
    @case("creators", f"scout_fuzz_{k}")
    def _(ctx, k=k):
        rng = random.Random(500 + k)
        people = rng.choice([[], ["C++ Guy|C++", "A.I. Expert", "(weird)", "Élodie Dupont", "Mr. T"],
                             [f"Person {i}|P{i}" for i in range(200)], ["Alex Hormozi|Hormozi"]])
        f = {"weight": rng.choice([0, 0.6, 1]), "creators": people, "keywords": rng.choice([[], ["money", "(", "["]])}
        scout = CreatorScout(f, rng.choice([[], ["Theo Von", "?", ""]]), checks=3, search=sim_search(rng, 0.5))
        now = time.time()
        vids = [{"platform": rng.choice(["tiktok", "instagram", "youtube_shorts"]), "views": float(rng.choice([0, 5, 1e6])),
                 "caption": rng.choice(["Logan Paul Logan Paul", "C++ Guy talks", "A.I. Expert (weird) Élodie Dupont",
                                        "", None, "New York City Mayor", "Jake Paul vs Tommy Fury"]),
                 "hashtags": rng.choice([[], None, ["alexhormozi"]]), "mentions": rng.choice([[], None, ["hormozi"]]),
                 "mention_names": rng.choice([None, {"x": "Jake Paul"}]), "author": rng.choice(["a", "b", "c", None]),
                 "author_name": rng.choice(["Alex Hormozi", None, ""]), "created_at": rng.choice([None, now - 3600]),
                 "velocity": rng.choice([None, 3.0]), "url": "https://x"} for _ in range(rng.randint(0, 300))]
        rows = scout.tally(vids, [{"handle": "hormozi", "name": "Alex Hormozi"}, {"handle": None, "name": None}])
        p = []
        for _ in range(4):
            nxt = scout.next_to_check(rows)
            if nxt is None:
                break
            try:
                scout.check(nxt)
            except RuntimeError:
                scout.youtube[nxt["key"]] = {"long_videos": 0, "median_views": 0, "channels": 0, "own_channel": None,
                                             "best": None, "query": ""}
        ranked = scout.rank(rows)
        if any(not (0 <= r["score"] <= 100) for r in ranked):
            p.append("score out of range")
        if any(not r["reasons"] for r in ranked):
            p.append("missing reasons")
        json.dumps(ranked)
        return p


# ================================================================ discovery ranking
for k in range(12):
    @case("discovery", f"rank_fuzz_{k}")
    def _(ctx, k=k):
        from clipper.discovery.youtube import creator_queries, rank_candidates

        rng = random.Random(900 + k)
        cfg = fresh_cfg(ctx["tmp"] / f"disc{k}")
        f = focus_mod.load(cfg)
        f["weight"] = rng.choice([0.0, 0.6, 1.0])
        now = time.time()
        cands = [{"video_id": f"v{i}", "title": rng.choice(["", "Alex Hormozi on money", "cats", "(unbalanced [regex"]),
                  "description": rng.choice(["", None and "", "x" * 3000]), "channel": rng.choice(["", "Theo Von", "?"]),
                  "published": rng.choice([0.0, now, now + 86400, now - 400 * 86400]),
                  "duration": rng.choice([0.0, 600.0, 36000.0]), "views": rng.choice([0.0, 1.0, 1e9]),
                  "likes": rng.choice([0.0, 1e9]), "comments": 0.0, "subscribers": rng.choice([0.0, 1e7]),
                  "source": rng.choice(["search", "watchlist"]), "has_heatmap": rng.random() < 0.5}
                 for i in range(rng.randint(0, 60))]
        profile = rng.choice([None, {}, {"live": {"creators": [{"name": "(x"}, {"name": "Logan Paul", "youtube": None}]}},
                              {"term_weights": {"money": 2.0}, "hook_lift": [], "duration_lift": []}])
        ranked = rank_candidates(cands, profile, now, rng.choice([[], ["Theo Von", "(x"]]), f)
        p = []
        if len(ranked) != len(cands):
            p.append("lost candidates")
        if any(not (0 <= c["rank_score"] <= 100) or c["rank_score"] != c["rank_score"] for c in ranked):
            p.append(f"rank_score out of range: {[c['rank_score'] for c in ranked][:5]}")
        q, names = creator_queries(profile, f, rng.choice([[], ["Theo Von"] * 3]), n=rng.choice([0, 1, 10]),
                                   rng=random.Random(k))
        if len(q) != len(names) or len(set(names)) != len(names):
            p.append(f"bad queries: {names}")
        return p


# ================================================================ focus files and payloads
FOCUS_FILES = ["", "{", "[]", "null", '{"weight": "abc"}', '{"weight": 5, "creators": "Alex"}',
               '{"creators": [1, null, "Ok"], "keywords": {"a": 1}}', '{"name": 12345}', '{"weight": null}',
               json.dumps({"creators": ["x"] * 5000})]
for k, text in enumerate(FOCUS_FILES):
    @case("focus", f"focus_file_{k}")
    def _(ctx, k=k, text=text):
        cfg = fresh_cfg(ctx["tmp"] / f"focus{k}")
        (Path(cfg["paths"]["db"]).parent / "focus.json").write_text(text, encoding="utf-8")
        try:
            f = focus_mod.load(cfg)
        except Exception as exc:
            return [f"CRASH {type(exc).__name__}: {exc}"]
        p = []
        if not 0 <= f["weight"] <= 1:
            p.append(f"weight {f['weight']}")
        for key in ("keywords", "creators", "tiktok_accounts"):
            if not isinstance(f[key], list) or not all(isinstance(x, str) for x in f[key]):
                p.append(f"{key} not a list of text")
        if not isinstance(f["name"], str):
            p.append("name not text")
        return p


# ================================================================ web API
API_CALLS = [
    ("post", "/api/focus", {"weight": "high"}), ("post", "/api/focus", {"creators": "Alex"}),
    ("post", "/api/focus", {"weight": -3, "name": "x" * 500}), ("post", "/api/focus", [1, 2]),
    ("post", "/api/focus", {"keywords": [None, 5, "ok"]}), ("get", "/api/scan", None), ("post", "/api/scan", None),
    ("get", "/api/agents/%2e%2e", None), ("get", "/api/agents/" + "x" * 3000, None), ("get", "/api/agents?after=-5", None),
    ("get", "/api/agents?after=abc", None), ("post", "/api/run", {"level": "nope"}), ("post", "/api/run", {"clips": -1}),
    ("post", "/api/run", {"clips": 10**9}), ("post", "/api/clip", {"source": ""}), ("post", "/api/clip", {"source": "   \n "}),
    ("delete", "/api/clips/..%2F..%2Fetc/clip_x", None), ("get", "/api/clips/../x/package", None),
    ("get", "/api/package/latest", None), ("get", "/api/status", None), ("post", "/api/stop", None),
    ("get", "/api/version", None), ("post", "/api/schedule", {"folder": "../", "name": "x", "platforms": ["myspace"]}),
    ("post", "/api/youtube-login", {"text": "garbage"}), ("get", "/api/uploads", None),
]
for k, (method, url, body) in enumerate(API_CALLS):
    @case("api", f"api_{k}_{method}_{url.split('?')[0].strip('/').replace('/', '_')[:40]}")
    def _(ctx, method=method, url=url, body=body):
        client = ctx["client"]()
        try:
            r = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        except Exception as exc:
            return [f"CRASH {type(exc).__name__}: {exc}"]
        p = [] if r.status_code < 500 else [f"{method.upper()} {url[:60]} -> {r.status_code}: {r.text[:120]}"]
        if url == "/api/run" and r.status_code == 200:
            ctx["stop_run"]()
        return p


# ================================================================ full pipeline on awkward videos
DULL = ("We went over the schedule for next week. The meeting room was booked on Tuesday. "
        "I think the numbers were fine overall. The coffee machine on the third floor works now. ")
STRONG = ("Nobody talks about the day I lost everything. I was twenty two and I had one million dollars "
          "in the bank. Six months later I was broke and homeless. My own brother lied to me and stole "
          "all of it. I never told anyone this story until today. That's why I trust nobody with money now. ")
VIDEOS = {
    "normal_3min": DULL * 3 + STRONG + DULL * 3,
    "strong_only": STRONG * 3,
    "dull_only": DULL * 8,
    "one_word_repeated": "yes " * 400,
    "no_punctuation": (DULL + STRONG).replace(".", "") * 3,
    "very_short_40s": STRONG,
    "unicode_mix": (DULL + "Café 日本語 🔥 ¿qué? " + STRONG) * 2,
    "long_12min": (DULL * 4 + STRONG) * 6,
    "questions": ("Why do people fail? Because they quit too early. What should you do? Keep going. " * 12),
    "profanity": ("This is fucking insane and I swear to god it's the craziest shit. " * 6 + STRONG) * 2,
}


def make_words(text, step=0.4):
    return [{"w": w, "s": round(i * step, 3), "e": round(i * step + step * 0.9, 3)} for i, w in enumerate(text.split())]


def make_video(cfg, root: Path, name: str, text: str, kind: str) -> Path:
    from clipper.analysis.download import source_key

    words = make_words(text)
    duration = words[-1]["e"] + 2
    src = root / f"{name}.mp4"
    vsrc = {"normal": f"testsrc2=s=640x360:r=30:d={duration}", "vertical": f"testsrc2=s=360x640:r=30:d={duration}",
            "black": f"color=c=black:s=640x360:r=30:d={duration}"}[kind]
    audio = ["-f", "lavfi", "-i", f"sine=f=200:d={duration}"] if kind != "black" else \
        ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}"]
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", vsrc, *audio, "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", str(duration), str(src)], check=True)
    work = cfg.path("paths.work_dir") / source_key(str(src))
    work.mkdir(parents=True, exist_ok=True)
    (work / "transcript.json").write_text(json.dumps({"source": "test", "words": words,
                                                      "segments": group_segments(words)}))
    return src


for name, text in VIDEOS.items():
    for kind in (("normal", "vertical") if name in ("normal_3min", "strong_only") else ("normal",)):
        @case("pipeline", f"{name}_{kind}")
        def _(ctx, name=name, text=text, kind=kind):
            from clipper.pipeline import Pipeline

            root = ctx["tmp"] / f"pipe_{name}_{kind}"
            cfg = fresh_cfg(root)
            cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60, use_comments=False)
            cfg["editing"]["x264_preset"] = "ultrafast"
            src = make_video(cfg, root, name, text, kind)
            try:
                clips = Pipeline(cfg).clip_video(str(src), None, clips=2)
            except RuntimeError as exc:
                msg = str(exc)
                ok = name in ("dull_only", "one_word_repeated") and ("strict review" in msg or "No clip" in msg)
                return [] if ok else [f"RuntimeError: {msg[:200]}"]
            p = []
            if name in ("dull_only", "one_word_repeated") and clips:
                p.append(f"made {len(clips)} clip(s) from a video with nothing worth clipping")
            spans = sorted((c["start"], c["end"]) for c in clips if "start" in c)
            for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
                if b0 < a1 - 0.5:
                    p.append("overlapping clips")
            words = make_words(text)
            story = [(words[k]["s"], words[k + len(STRONG.split()) - 1]["e"]) for k in range(len(words))
                     if " ".join(w["w"] for w in words[k:k + 5]) == " ".join(STRONG.split()[:5])]
            if story and clips:  # the strong story must be found, and found whole
                cov = max(max(0.0, min(c["end"], b) - max(c["start"], a)) / (b - a) for a, b in story for c in clips)
                if cov < 0.8:
                    p.append(f"best clip covers only {cov:.0%} of the story")
            if "." in text:
                for c in clips:
                    ins = [w for w in words if w["s"] >= c["start"] - 0.05 and w["s"] < c["end"] - 0.02]
                    before = [w for w in words if w["e"] <= c["start"] + 0.01]
                    if ins and not ins[-1]["w"].endswith((".", "?", "!")):
                        p.append(f"ends mid-sentence at '{ins[-1]['w']}'")
                    if ins and before and not before[-1]["w"].endswith((".", "?", "!")):
                        p.append(f"starts mid-sentence at '{ins[0]['w']}'")
            out = cfg.path("paths.output_dir")
            for c in clips:
                d = c.get("duration") or (c.get("end", 0) - c.get("start", 0))
                if d and not 10 <= d <= 75:
                    p.append(f"clip length {d:.1f}s")
                if not (out / c["folder"] / f"{c['name']}.mp4").exists():
                    p.append("mp4 missing")
                if not (out / c["folder"] / f"{c['name']}.srt").exists():
                    p.append("srt missing")
            return p


# ================================================================ runner
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="system_backtest_out")
    args = ap.parse_args()
    only = {g for g in args.only.split(",") if g}
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sysbt_", dir=out))
    started = time.time()
    app_cfg = fresh_cfg(tmp / "app")
    state = {}

    def client():
        if "client" not in state:
            from fastapi.testclient import TestClient

            from clipper.web.app import create_app
            state["client"] = TestClient(create_app(app_cfg))
        return state["client"]

    def stop_run():
        from clipper.media import CANCEL
        client().post("/api/stop")
        CANCEL.set()
        time.sleep(1.5)

    ctx = {"tmp": tmp, "client": client, "stop_run": stop_run}
    results = []
    todo = [c for c in CASES if not only or c[0] in only]
    for i, (group, name, fn) in enumerate(todo):
        t0 = time.time()
        try:
            problems = fn(ctx) or []
            trace = None
        except Exception as exc:
            problems = [f"CRASH {type(exc).__name__}: {str(exc)[-300:]}"]
            trace = traceback.format_exc()[-2000:]
        row = {"i": i, "group": group, "case": name, "seconds": round(time.time() - t0, 1),
               "problems": sorted(set(problems))[:10]}
        if trace:
            row["trace"] = trace
        results.append(row)
        print(f"[{time.time() - started:7.1f}s] #{i:03d} {group:9} {name:40} "
              f"{'OK' if not problems else 'FAIL: ' + '; '.join(row['problems'])[:300]}", flush=True)
        (out / "report.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    fails = [r for r in results if r["problems"]]
    print(f"DONE {len(results) - len(fails)}/{len(results)} passed in {(time.time() - started) / 60:.1f} min")
    for g, n in Counter(r["group"] for r in fails).most_common():
        print(f"  {g}: {n} failed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
