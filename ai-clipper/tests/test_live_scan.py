import time

from clipper.db import Database
from clipper.events import Reporter
from clipper.trends import focus as focus_mod
from clipper.trends.creators import CreatorScout, _looks_like_person
from clipper.trends.live import LiveScan
from clipper.trends.social import InstagramWeb, RateLimited, TikTokWeb

NAMES = {"garyvee": "GaryVee", "clipsdaily": "Clips Daily", "motivationhub": "Motivation Hub"}


def _vid(platform, handle, i, views, caption, mentions=(), age_h=3.0, sponsored=False):
    return {"platform": platform, "video_id": f"{handle}-{i}", "url": f"https://x/{handle}/{i}", "caption": caption,
            "hashtags": [], "views": float(views), "likes": views * 0.05, "comments": views * 0.002,
            "shares": views * 0.004, "saves": views * 0.003, "duration": 30.0, "author": handle,
            "author_name": NAMES.get(handle, handle), "author_followers": 1e6, "created_at": time.time() - age_h * 3600,
            "music": "", "mentions": list(mentions), "sponsored": sponsored}


class FakeTikTok:
    blocked = None
    requests = 0

    def __init__(self):
        self.reads = {}

    def profile(self, h):
        self.requests += 1
        if h == "ghost":
            return None
        big = h != "tiny"
        return {"platform": "tiktok", "handle": h, "name": NAMES.get(h, h), "sec_uid": h,
                "followers": 2_000_000 if big else 40, "likes": 9e7 if big else 10, "videos": 50}

    def videos(self, prof, pages=2):
        h = prof["handle"]
        n = self.reads[h] = self.reads.get(h, 0) + 1
        grow = 1 + 0.2 * (n - 1)  # every re-read shows more views: the scan measures views per minute
        if h == "garyvee":
            return [_vid("tiktok", h, 1, 900_000 * grow, "Discipline beats motivation. Success is a habit",
                         mentions=["clipsdaily"]),
                    _vid("tiktok", h, 2, 800_000 * grow, "new drink #ad", mentions=["somebrand"], sponsored=True),
                    _vid("tiktok", h, 3, 5_000, "old one", age_h=24 * 90)]
        return [_vid("tiktok", h, k, (400_000 - 50_000 * k) * grow,
                     f"Alex Hormozi on how millionaires think about money part {k}") for k in range(4)] + [
            _vid("tiktok", h, 9, 900_000 * grow, "lol my cat", mentions=["randomcat"])]  # off-focus: not followed


class FakeInstagram:
    blocked = None
    requests = 0
    cool_until = 0.0

    def __init__(self):
        self.calls = 0

    def profile(self, h):
        self.calls += 1
        if self.calls == 1:  # Instagram asks to slow down once
            raise RateLimited("wait", time.time() + 0.3)
        return ({"platform": "instagram", "handle": h, "name": NAMES.get(h, h), "followers": 3_000_000},
                [_vid("instagram", h, 1, 2_000_000, "Grant Cardone: the rich mindset nobody tells you about")])


def fake_shorts(fcfg, known, progress=None, log=None):
    assert "billionaire motivation" in fcfg["youtube_searches"]  # your focus's short searches are added
    return [{**_vid("youtube_shorts", "Motivation Hub", 1, 3_000_000, "Alex Hormozi billionaire mindset speech"),
             "author_handle": "motivationhub"},
            {**_vid("youtube_shorts", "BBC News", 2, 9_000_000, "Breaking: storm hits the coast"),
             "author_handle": "bbcnews"}]


def fake_search(query, limit, within_days=None, length=None):
    return [{"video_id": f"v{k}", "title": f"{query.split(' interview')[0]} full talk {k}", "channel": "Some Podcast",
             "views": 500_000.0 + k, "duration": 3600.0} for k in range(6)]


def _cfg(cfg):
    cfg["trends"]["live"].update(enabled=True, min_minutes=0.03, max_minutes=0.12, recheck_after_minutes=0.01,
                                 velocity_min_seconds=0.3, tiktok_accounts=["garyvee", "tiny", "ghost"],
                                 instagram_accounts=["grantcardone"], mention_min_views=50_000, creator_checks=3)
    cfg["focus"].update(tiktok_accounts=[], instagram_accounts=[], short_searches=["billionaire motivation"])
    return cfg


def test_live_scan_reads_follows_measures_and_ranks(cfg):
    from clipper.agents import AgentBoard

    board = AgentBoard()
    rep = Reporter()
    scan = LiveScan(_cfg(cfg), rep=rep, board=board, tiktok=FakeTikTok(), instagram=FakeInstagram(),
                    shorts=fake_shorts, creator_search=fake_search)
    t = time.time()
    res = scan.run()
    took = time.time() - t
    assert 0.03 * 60 <= took <= 0.12 * 60 + 5   # at least the minimum, never much past the maximum
    accts = {(a["platform"], a["handle"]): a for a in res["accounts"]}
    assert ("tiktok", "garyvee") in accts and ("instagram", "grantcardone") in accts
    assert accts[("tiktok", "clipsdaily")]["source"] == "tagged by @garyvee"          # followed an @mention
    assert ("tiktok", "somebrand") not in accts                                        # ...not from a paid post
    assert accts[("tiktok", "motivationhub")]["source"] == "YouTube Shorts clip channel"  # same name elsewhere
    assert ("tiktok", "tiny") not in accts and res["stats"]["skipped"] >= 2             # too small / not found
    assert ("tiktok", "bbcnews") not in accts                                          # news: not clippable
    assert ("tiktok", "randomcat") not in accts             # found accounts only lead on through on-focus videos
    assert all(r["for_you"] for r in res["viral_now"] if r["author"] in ("garyvee", "grantcardone"))
    assert not any(v["video_id"] == "garyvee-3" for v in res["videos"])                # 90 days old: dropped
    plats = {v["platform"] for v in res["videos"]}
    assert plats == {"tiktok", "instagram", "youtube_shorts"}
    measured = [r for r in res["viral_now"] if r["measured"]]
    assert measured and measured[0]["per_min"] > 0                                     # views per minute, measured
    top = {c["name"]: c for c in res["creators"]}
    hz = top["Alex Hormozi"]
    assert hz["clips"] >= 5 and hz["youtube"]["long_videos"] == 6 and hz["focus_listed"]
    assert any("clips of them by" in r for r in hz["reasons"]) and any("YouTube:" in r for r in hz["reasons"])
    assert res["creators"][0]["score"] >= res["creators"][-1]["score"]
    feed = " | ".join(e["text"] for e in board.feed())
    assert "@garyvee" in feed and "views" in feed and "Instagram asked to slow down" in feed
    assert any(e.kind == "scan" and e.data.get("final") for e in rep.history)


def test_scan_stops_early_when_nothing_answers(cfg):
    class Dead(FakeTikTok):
        blocked = "TikTok stopped answering"

        def profile(self, h):
            return None

    c = _cfg(cfg)
    c["trends"]["live"].update(min_minutes=0.5, max_minutes=1, instagram=False)
    t = time.time()
    res = LiveScan(c, tiktok=Dead(), shorts=lambda *a, **k: [], creator_search=fake_search).run()
    assert time.time() - t < 15 and res["videos"] == []


def test_tiktok_and_instagram_payloads_are_normalized():
    prof = {"handle": "theovon", "name": "Theo Von", "followers": 8_100_000}
    desc = "It's good @Luke Bryan @CelsiusOfficial #CELSIUSBrandPartner"
    item = {"id": "7", "desc": desc, "createTime": 1790000000, "stats": {"playCount": 1200, "diggCount": 5},
            "statsV2": {"playCount": "1800000", "diggCount": "9000", "commentCount": "10", "shareCount": "40",
                        "collectCount": "7"}, "video": {"duration": 14}, "music": {"title": "og"},
            "contents": [{"textExtra": [{"start": 10, "end": 21, "userUniqueId": "lukebryan"},
                                        {"start": 22, "end": 38, "userUniqueId": "celsiusofficial"},
                                        {"hashtagName": "celsiusbrandpartner", "isCommerce": True}]}]}
    v = TikTokWeb.normalize(item, prof)
    assert v["views"] == 1_800_000 and v["shares"] == 40 and v["saves"] == 7
    assert v["mentions"] == ["lukebryan", "celsiusofficial"] and v["mention_names"]["lukebryan"] == "Luke Bryan"
    assert v["sponsored"] and v["url"].endswith("/video/7")
    assert TikTokWeb.normalize({**item, "isAd": True}, prof) is None

    user = {"username": "GaryVee", "full_name": "Gary Vaynerchuk", "edge_followed_by": {"count": 12_000_000},
            "edge_owner_to_timeline_media": {"count": 9, "edges": [
                {"node": {"is_video": True, "video_view_count": 55_000, "shortcode": "Dd1", "taken_at_timestamp": 1790000000,
                          "edge_liked_by": {"count": 900}, "edge_media_to_comment": {"count": 30},
                          "edge_media_to_caption": {"edges": [{"node": {"text": "talking with @stevenbartlett #mindset"}}]}}},
                {"node": {"is_video": False, "shortcode": "Dd2"}}]}}
    acct, vids = InstagramWeb.parse(user, "garyvee")
    assert acct["followers"] == 12_000_000 and len(vids) == 1
    assert vids[0]["mentions"] == ["stevenbartlett"] and vids[0]["hashtags"] == ["mindset"]


def test_instagram_backs_off_when_limited():
    class R:
        status_code = 429
        text = ""

        def json(self):
            raise ValueError

    class S:
        def get(self, *a, **k):
            return R()

    now = [1000.0]
    ig = InstagramWeb(interval=0, session=S(), sleep=lambda s: None, clock=lambda: now[0])
    try:
        ig.profile("x")
    except RateLimited as e:
        assert e.until == 1120.0 and ig.cooldown == 240.0
    else:
        raise AssertionError("expected RateLimited")
    try:
        ig.profile("x")  # still cooling down: no request is made
    except RateLimited:
        pass
    assert ig.requests == 2


def test_person_names_and_focus(cfg):
    assert _looks_like_person("Logan Paul") and _looks_like_person("Alex Hormozi")
    assert not _looks_like_person("Powerful Motivational Speech") and not _looks_like_person("New York")
    f = focus_mod.load(cfg)
    assert f["name"] and 0 <= f["weight"] <= 1 and focus_mod.people(f)[0][0] == "Alex Hormozi"
    assert focus_mod.fit("Billionaire mindset: discipline and success", f) == 1.0
    assert focus_mod.fit("my cat video", f) == 0.0
    saved = focus_mod.save(cfg, {"name": "Comedy", "weight": 0.2, "creators": ["Theo Von"]})
    assert saved["name"] == "Comedy" and saved["weight"] == 0.2 and focus_mod.load(cfg)["creators"] == ["Theo Von"]


def test_creator_scout_finds_new_people_from_viral_captions():
    scout = CreatorScout({"weight": 0.6, "creators": [], "keywords": ["money"]}, [], checks=2, search=fake_search)
    vids = []
    for i, author in enumerate(["a", "b", "c", "d"]):
        vids.append(_vid("tiktok", author, i, 2_000_000, "Logan Paul reacts to the craziest fight"))
        vids.append(_vid("tiktok", author, i + 10, 10_000, "boring"))
    rows = scout.tally(vids, [])
    lp = next(r for r in rows if r["name"] == "Logan Paul")
    assert lp["source"] == "found in viral clips" and lp["clips"] == 4 and lp["clip_accounts"] == 4
    assert scout.next_to_check(rows)["name"] == "Logan Paul"


def test_discovery_searches_ranked_creators_and_focus_first(cfg):
    import random

    from clipper.discovery.youtube import creator_queries, rank_candidates

    f = focus_mod.load(cfg)
    profile = {"live": {"creators": [{"name": "Logan Paul", "youtube": {"long_videos": 5}},
                                     {"name": "Nobody", "youtube": {"long_videos": 0}}]}}
    q, names = creator_queries(profile, f, ["Theo Von"], n=8, rng=random.Random(1))
    assert names[0] == "Logan Paul" and "Nobody" not in names and q[0] == "Logan Paul interview"
    assert sum(n in [p for p, _ in focus_mod.people(f)] for n in names) >= 2
    now = time.time()
    base = {"description": "", "channel": "X", "published": now - 86400, "duration": 3600, "views": 1e6,
            "likes": 1e4, "comments": 1e3}
    ranked = rank_candidates([{**base, "video_id": "a", "title": "Cooking pasta at home"},
                              {**base, "video_id": "b", "title": "Alex Hormozi on getting rich"}], None, now, [], f)
    assert ranked[0]["video_id"] == "b" and ranked[0]["focus_fit"] == 1.0


def test_trend_analysis_keeps_live_results(cfg, monkeypatch):
    from .conftest import synthetic_short_videos

    import clipper.trends.live as live_mod
    from clipper.trends import run_trend_analysis

    class Scan:
        def __init__(self, *a, **k):
            pass

        def run(self):
            st = {"elapsed": 301, "videos": 1200, "accounts": 80, "measured": 40, "platforms": {}, "notes": []}
            return {"videos": synthetic_short_videos(), "viral_now": [{"platform": "tiktok", "per_min": 900.0,
                                                                       "caption": "x", "measured": True}],
                    "creators": [{"name": "Alex Hormozi", "score": 90}], "accounts": [], "stats": st, "notes": [],
                    "focus": "Motivation"}

    monkeypatch.setattr(live_mod, "LiveScan", Scan)
    cfg["trends"]["live"]["enabled"] = True
    profile = run_trend_analysis(cfg, Database(cfg.path("paths.db")), Reporter())
    assert profile["live"]["creators"][0]["name"] == "Alex Hormozi"
    assert "900 views/min" in profile["playbook"] and "Alex Hormozi" in profile["playbook"]


def test_scan_and_focus_endpoints(cfg):
    from fastapi.testclient import TestClient

    from clipper.web.app import create_app

    client = TestClient(create_app(cfg))
    d = client.get("/api/scan").json()
    assert d["running"] is False and d["last"] is None and d["min_minutes"] == 5 and d["max_minutes"] == 10
    f = client.post("/api/focus", json={"name": "Rich people motivation", "weight": 3,
                                        "creators": ["Grant Cardone", " "], "tiktok_accounts": ["grantcardone"]}).json()
    assert f["name"] == "Rich people motivation" and f["weight"] == 1.0 and f["creators"] == ["Grant Cardone"]
    assert client.get("/api/focus").json()["tiktok_accounts"] == ["grantcardone"]
