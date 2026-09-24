import time

from fastapi.testclient import TestClient

from clipper.db import Database
from clipper.discovery import youtube
from clipper.discovery.youtube import iso_duration, poll_watchlist, rank_candidates
from clipper.events import Reporter
from clipper.web.app import create_app

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <yt:videoId>abc123</yt:videoId><yt:channelId>UC1</yt:channelId>
  <title>Huge new episode</title><author><name>Big Pod</name></author>
  <published>2026-09-20T12:00:00+00:00</published>
  <media:group><media:community><media:statistics views="54321"/></media:community></media:group>
 </entry>
</feed>"""


def test_iso_duration():
    assert iso_duration("PT1H2M3S") == 3723
    assert iso_duration("PT45M") == 2700
    assert iso_duration("P1DT1S") == 86401


def test_rank_prefers_fast_growing_videos():
    now = time.time()
    base = {"description": "", "likes": 0, "comments": 0, "subscribers": 1e6, "source": "search"}
    slow = {**base, "video_id": "slow", "title": "a", "views": 1e6, "published": now - 30 * 86400}
    fast = {**base, "video_id": "fast", "title": "b", "views": 1e6, "published": now - 1 * 86400}
    assert [c["video_id"] for c in rank_candidates([slow, fast], None, now)] == ["fast", "slow"]


class FakeResponse:
    content = RSS

    def raise_for_status(self):
        pass


def test_watchlist_reports_new_uploads_once(cfg, monkeypatch):
    monkeypatch.setattr(youtube.requests, "get", lambda *a, **k: FakeResponse())
    cfg["discovery"]["watch_channels"] = ["UC1"]
    db, rep = Database(cfg.path("paths.db")), Reporter()
    new = poll_watchlist(cfg, db, rep)
    assert [u["video_id"] for u in new] == ["abc123"] and new[0]["views"] == 54321
    assert poll_watchlist(cfg, db, rep) == []
    assert any(e.kind == "upload" for e in rep.history)
    assert db.recent_uploads()[0]["title"] == "Huge new episode"


def test_web_app_endpoints(cfg):
    client = TestClient(create_app(cfg))
    page = client.get("/")
    assert page.status_code == 200 and "AI Clipper" in page.text and "How many clips" in page.text and "Editing style" not in page.text
    st = client.get("/api/status").json()
    assert [l["name"] for l in st["levels"]] == ["simple", "normal", "hard", "professional", "extreme"]
    assert st["running"] is False
    assert client.get("/api/clips").json() == []
    assert client.post("/api/run", json={"level": "ultra"}).status_code == 400
