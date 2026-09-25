import json
import time

import pytest

from clipper import publish
from clipper.agents import BOARD, ROLES, TEAM_SIZE, AgentBoard
from clipper.db import Database
from clipper.events import Reporter


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._data


class FakeHTTP:
    """Records calls and answers like the TikTok / Instagram APIs do."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _go(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if kw.get("data") is not None and hasattr(kw["data"], "read"):
            kw["data"].read()
        for key, answer in self.routes.items():
            if key in url:
                return answer(method, url, kw) if callable(answer) else FakeResp(answer)
        return FakeResp({"error": {"message": f"unexpected {url}"}}, 404)

    def get(self, url, **kw):
        return self._go("GET", url, **kw)

    def post(self, url, **kw):
        return self._go("POST", url, **kw)

    def put(self, url, **kw):
        return self._go("PUT", url, **kw)


def test_team_is_150_agents_and_the_board_tracks_work():
    from collections import Counter

    assert TEAM_SIZE == 150 and len({r for r, *_ in ROLES}) == len(ROLES)  # every role distinct
    per_division = Counter()
    for _, division, _, n, _ in ROLES:
        per_division[division] += n
    assert per_division == {"command": 6, "intake": 11, "full": 10, "section": 60, "gate": 32, "verify": 10,
                            "production": 15, "publishing": 6}
    board = AgentBoard()
    assert len({a["id"] for a in board.agents}) == 150
    with board.work("listen", "Listening to a podcast"):
        snap = board.snapshot()
        busy = [a for a in snap if a["busy"]]
        assert len(snap) == 150 and busy[0]["name"] == "Listener 1" and "podcast" in busy[0]["task"]
        with board.work("listen", "second video"):
            assert board.busy() == 2
    assert board.busy() == 0 and [a["done"] for a in board.snapshot() if a["role"] == "listen"][:2] == [1, 1]


def test_instagram_reel_upload_flow(tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x" * 1000)
    http = FakeHTTP({
        "/media_publish": {"id": "1799"},
        "/me/media": {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v21.0/C1"},
        "rupload": {"success": True},
        "/C1": {"status_code": "FINISHED"},
    })
    media_id = publish.Instagram({"access_token": "T", "user_id": "me"}, http).publish(video, "My caption #fyp", poll_s=0)
    assert media_id == "1799"
    setup = http.calls[0]
    assert setup[2]["data"]["media_type"] == "REELS" and setup[2]["data"]["upload_type"] == "resumable"
    upload = http.calls[1]
    assert upload[2]["headers"]["file_size"] == "1000" and upload[2]["headers"]["Authorization"] == "OAuth T"


def test_tiktok_upload_flow_and_privacy(tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"y" * 3000)
    http = FakeHTTP({
        "creator_info": {"data": {"privacy_level_options": ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS"]}, "error": {"code": "ok"}},
        "video/init": {"data": {"publish_id": "P1", "upload_url": "https://upload.tiktokapis.com/x"}, "error": {"code": "ok"}},
        "upload.tiktokapis.com": {},
        "status/fetch": {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300]},
                         "error": {"code": "ok"}},
    })
    post_id = publish.TikTok({"access_token": "A", "mode": "direct"}, http).publish(video, "caption", poll_s=0)
    assert post_id == "7300"
    init = next(c for c in http.calls if "video/init" in c[1])
    assert init[2]["json"]["post_info"]["privacy_level"] == "SELF_ONLY"  # unaudited app: only what TikTok allows
    assert init[2]["json"]["source_info"] == {"source": "FILE_UPLOAD", "video_size": 3000, "chunk_size": 3000,
                                             "total_chunk_count": 1}
    put = next(c for c in http.calls if c[0] == "PUT")
    assert put[2]["headers"]["Content-Range"] == "bytes 0-2999/3000"


def test_api_errors_are_clear():
    http = FakeHTTP({"user/info": lambda *a: FakeResp({"error": {"code": "access_token_invalid",
                                                                 "message": "token expired"}}, 401)})
    with pytest.raises(publish.PublishError, match="token expired"):
        publish.TikTok({"access_token": "bad"}, http).whoami()


def test_best_time_model_learns_and_spreads_posts():
    model = publish.TimingModel([], {})
    # typical peaks: evening beats 4 am, weekday lunch beats mid-morning
    assert model.score(1, 19) > model.score(1, 4) and model.score(1, 12) > model.score(1, 9)
    # your own results win once there are enough of them: here mornings did far better
    results = [{"weekday": 2, "hour": 9, "views": 90000}] * 8 + [{"weekday": 2, "hour": 19, "views": 300}] * 8
    learned = publish.TimingModel(results, {})
    assert learned.score(2, 9) > learned.score(2, 19)
    slots = model.best_slots(5, time.time(), [], max_per_day=2, gap_hours=3)
    assert len(slots) == 5 and all(b - a >= 3 * 3600 for a, b in zip(slots, slots[1:]))
    per_day = {}
    for t in slots:
        per_day[time.localtime(t)[:3]] = per_day.get(time.localtime(t)[:3], 0) + 1
    assert max(per_day.values()) <= 2


def test_scheduler_posts_when_due_retries_and_reads_results(cfg, tmp_path, monkeypatch):
    db = Database(tmp_path / "p.db")
    rep = Reporter()
    svc = publish.PostingService(cfg, db, rep, AgentBoard())
    svc.accounts.save("tiktok", {"access_token": "A", "mode": "inbox"})
    clip_dir = cfg.path("paths.output_dir") / "f"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "clip_01_x.mp4").write_bytes(b"v")
    calls = {"n": 0}

    class Client:
        def publish(self, video, caption):
            calls["n"] += 1
            if calls["n"] == 1:
                raise publish.PublishError("TikTok busy")
            return "remote-9"

        def stats(self, remote_id):
            return {"views": 1234}

    monkeypatch.setattr(svc, "client", lambda platform: Client())
    [p] = svc.schedule("f", "clip_01_x", ["tiktok"], "now", "hello")
    now = time.time()
    svc.tick(now + 1)  # first try fails -> retried in 15 min
    row = db.posts()[0]
    assert row["status"] == "scheduled" and row["attempts"] == 1 and row["scheduled_at"] > now + 800
    svc.tick(now + 1000)
    row = db.posts()[0]
    assert row["status"] == "posted" and row["remote_id"] == "remote-9"
    svc.tick(row["posted_at"] + 86400 + 60)  # a day later the timing analyst reads the results
    assert db.posts()[0]["stats"] == {"views": 1234}
    assert svc.timing("tiktok").results[0]["views"] == 1234
    best = svc.schedule("f", "clip_01_x", ["tiktok"], "best", "again")[0]
    assert best["scheduled_at"] > time.time()


def test_posting_endpoints(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from clipper.web.app import create_app

    monkeypatch.setattr(publish.TikTok, "whoami", lambda self: {"username": "mychannel"})
    client = TestClient(create_app(cfg))
    agents = client.get("/api/agents").json()
    assert agents["size"] == 150 and len(agents["divisions"]) == 8 and "queued" in agents["registry"]
    assert client.post("/api/posting/connect", json={"platform": "tiktok", "access_token": "tok",
                                                     "mode": "inbox"}).json()["connected"]
    st = client.get("/api/posting").json()
    assert st["accounts"]["tiktok"]["name"] == "mychannel" and not st["accounts"]["instagram"]["connected"]
    assert client.post("/api/posting/schedule", json={"folder": "nope", "name": "clip_01_x",
                                                      "platforms": ["tiktok"]}).status_code == 404
    out = cfg.path("paths.output_dir") / "f"
    out.mkdir(parents=True, exist_ok=True)
    (out / "clip_01_x.mp4").write_bytes(b"v")
    (out / "clip_01_x.txt").write_text("Wait for it #fyp", encoding="utf-8")
    done = client.post("/api/posting/schedule", json={"folder": "f", "name": "clip_01_x", "platforms": ["tiktok"],
                                                      "when": "best"}).json()
    assert done[0]["platform"] == "tiktok"
    post = client.get("/api/posting").json()["posts"][0]
    assert post["caption"] == "Wait for it #fyp" and post["status"] == "scheduled"
    assert client.post("/api/posting/schedule", json={"folder": "f", "name": "clip_01_x",
                                                      "platforms": ["instagram"]}).status_code == 400
    client.delete(f"/api/posting/post/{post['id']}")
    assert client.get("/api/posting").json()["posts"][0]["status"] == "cancelled"
    assert client.post("/api/posting/auto", json={"on": True}).json()["auto_schedule"]


def test_agent_team_watches_videos_in_parallel(cfg, monkeypatch):
    import threading

    from clipper import pipeline as pipeline_mod
    from clipper.analysis.moments import Clip
    from clipper.pipeline import Pipeline

    cfg["analysis"]["parallel_videos"] = 3
    live, peak, lock = [0], [0], threading.Lock()

    def fake_analyze(cand, *a, **k):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.3)
        with lock:
            live[0] -= 1
        clips = [Clip(start=i * 100, end=i * 100 + 40, title=f"t{i}", hook="h", category="story",
                      final_score=80 - i) for i in range(2)]
        return {"meta": {"id": cand["video_id"], "title": "v", "channel": "c"}, "clips": clips,
                "signals": {"raw": {}}, "transcript": {"words": []}, "video": None}
    pipe = Pipeline(cfg)
    monkeypatch.setattr(pipe, "_edit", lambda result, level, run_id="", layer=1:
                        [{"layer": layer, "category": c.category} for c in result["clips"]])
    monkeypatch.setattr(pipeline_mod, "analyze_video", fake_analyze)
    monkeypatch.setattr(pipeline_mod, "run_trend_analysis", lambda *a: {"n_videos": 400})
    monkeypatch.setattr(pipeline_mod, "discover", lambda *a, **k: [
        {"video_id": f"v{i}", "title": "x", "channel": "c"} for i in range(9)])
    t0 = time.time()
    out = pipe._run(None, [], 10)
    assert len(out) == 10 and peak[0] >= 2  # several videos watched at the same time
    assert time.time() - t0 < 0.3 * 5  # 5 videos x 0.3 s in well under the sequential time


def test_agent_states_watching_standby_offline():
    board = AgentBoard()
    board.beat("editor", "Ready to edit")
    board.beat("publish", "", reason="Standing by: connect TikTok or Instagram")
    states = {a["name"]: a for a in board.snapshot()}
    assert states["Editor 1"]["state"] == "watching" and states["Publisher 1"]["state"] == "standby"
    assert "connect TikTok" in states["Publisher 1"]["task"]
    assert states["Listener 1"]["state"] == "offline"  # never beat: shown with a reason, not hidden
    board._duty["editor"] = ("Ready", time.time() - 600, None)
    assert "No heartbeat" in {a["name"]: a for a in board.snapshot()}["Editor 1"]["task"]
    board.alive("s_hook-3")  # a crew agent's own heartbeat keeps just that agent online
    states = {a["id"]: a for a in board.snapshot()}
    assert states["s_hook-3"]["state"] == "watching" and states["s_hook-2"]["state"] == "offline"


def test_stop_halts_a_long_ffmpeg_job_right_away(tmp_path):
    import threading

    from clipper.media import CANCEL, Cancelled, run_ffmpeg

    CANCEL.clear()
    threading.Timer(0.8, CANCEL.set).start()
    t0 = time.time()
    with pytest.raises(Cancelled):
        run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=1280x720:d=600", "-c:v", "libx264", "-preset", "slow",
                    str(tmp_path / "long.mp4")])
    assert time.time() - t0 < 5
    CANCEL.clear()


def test_clips_come_from_many_videos(cfg, monkeypatch):
    from clipper import pipeline as pipeline_mod
    from clipper.analysis.moments import Clip
    from clipper.pipeline import Pipeline

    def fake_analyze(cand, *a, **k):  # every video has 6 strong moments
        clips = [Clip(start=i * 100, end=i * 100 + 40, title=f"t{i}", hook="h", category="story",
                      final_score=90 - i) for i in range(6)]
        return {"meta": {"id": cand["video_id"], "title": "v", "channel": cand["video_id"]}, "clips": clips,
                "signals": {"raw": {}}, "transcript": {"words": []}, "video": None}
    pipe = Pipeline(cfg)
    made = []
    monkeypatch.setattr(pipe, "_edit", lambda result, level, run_id="", layer=1:
                        made.extend((result["meta"]["id"], c.title) for c in result["clips"]) or
                        [{"layer": layer, "category": "story"} for _ in result["clips"]])
    monkeypatch.setattr(pipeline_mod, "analyze_video", fake_analyze)
    monkeypatch.setattr(pipeline_mod, "run_trend_analysis", lambda *a: {"n_videos": 400})
    monkeypatch.setattr(pipeline_mod, "discover", lambda *a, **k: [
        {"video_id": f"v{i}", "title": "x", "channel": f"c{i}"} for i in range(12)])
    out = pipe._run(None, [], 10)
    from collections import Counter
    per_video = Counter(v for v, _ in made)
    assert len(out) == 10 and max(per_video.values()) <= 2 and len(per_video) >= 5


def test_agents_report_live_findings_and_history(cfg):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from clipper import APP_VERSION
    from clipper.crew.narrate import describe
    from clipper.web.app import create_app

    board = AgentBoard()
    with board.work("download", "Downloading x") as me:
        board.report(me, "Downloaded x (4:37 long, 70 MB)")
    with board.work("download", "Downloading y"):
        pass  # no finding of its own: the board records what it finished
    snap = {a["id"]: a for a in board.snapshot()}
    assert snap[me]["last"].startswith("Done in") and snap[me]["last"].endswith("Downloading y")
    assert [h["text"] for h in board.history(me)][0] == "Downloaded x (4:37 long, 70 MB)"
    feed = board.feed()
    assert [e["seq"] for e in feed] == sorted(e["seq"] for e in feed) and board.feed(feed[-1]["seq"]) == []
    board.report("no-such-agent", "ignored")
    assert len(board.feed()) == len(feed)

    gate = SimpleNamespace(kind="gate", label="Captions check")
    assert describe(gate, {"score": 62.0, "issues": ["captions cover a face"], "facts": {"words": 80}}) \
        == "Scored 62/100 (words 80) - flagged: captions cover a face"
    assert describe(gate, {"score": 97, "issues": []}).endswith("no problems found")
    assert describe(SimpleNamespace(kind="scout"), {"score": [0.9], "I": [0], "J": [3]}, ev=None)  # never raises

    client = TestClient(create_app(cfg))
    assert client.get("/api/version").json() == {"app": "ai-clipper", "version": APP_VERSION, "agents": TEAM_SIZE}
    d = client.get("/api/agents").json()
    assert len(d["agents"]) == TEAM_SIZE == 150 and "feed" in d and "last" in d["agents"][0]
    one = client.get(f"/api/agents/{d['agents'][0]['id']}").json()
    assert one["id"] == d["agents"][0]["id"] and isinstance(one["history"], list)
    assert client.get("/api/agents/nobody").status_code == 404
    assert client.get("/").headers["cache-control"] == "no-store"
