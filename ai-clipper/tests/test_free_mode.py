"""No-key mode: built-in judge, free short-video collection, full keyless run."""
import json
import subprocess

from clipper import pipeline as pipeline_mod
from clipper.analysis import local_judge
from clipper.analysis.moments import select_moments
from clipper.analysis.transcribe import group_segments
from clipper.db import Database
from clipper.events import Reporter
from clipper.media import ffmpeg_exe
from clipper.pipeline import Pipeline, list_outputs
from clipper.trends import free, run_trend_analysis

from .conftest import synthetic_short_videos
from .test_moments import make_words

DULL = ("We went over the schedule for next week. The meeting room was booked on Tuesday. "
        "I think the numbers were fine overall. The coffee machine on the third floor works now. ") * 8
STRONG = ("Nobody talks about the day I lost everything. I was twenty two and I had one million dollars "
          "in the bank. Six months later I was broke and homeless. My own brother lied to me and stole "
          "all of it. I never told anyone this story until today. That's why I trust nobody with money now. ")
MIDTHOUGHT = ("And then he told me the secret about the million dollars and the prison and the war. "
              "But it was insane and crazy and nobody believed the truth about the money, ")
TEXT = DULL + STRONG + DULL + MIDTHOUGHT + DULL


def transcript():
    words = make_words(TEXT)
    return {"words": words, "segments": group_segments(words)}


def strong_span(t):
    segs = t["segments"]
    first = next(s for s in segs if s["text"].startswith("Nobody talks"))
    last = next(s for s in segs if s["text"].startswith("That's why I trust"))
    return first["s"], last["e"]


def test_built_in_judge_finds_the_strong_moment():
    t = transcript()
    wins = local_judge.find_windows(t["segments"], {"pct": {}}, None, 15, 60)
    best = wins[0]
    s, e = strong_span(t)
    assert best["start"] <= s + 0.5 and best["end"] >= e - 12
    assert not best["flaws"] and best["parts"]["hook"] > 0.7
    # nothing starting with "And..." or "But..." is ever proposed
    assert not any(w["text"].startswith(("And ", "But ")) for w in wins)


def test_select_moments_without_any_key(cfg, tmp_path):
    t = transcript()
    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60)
    approved, judged = select_moments({"title": "Big Podcast", "channel": "Big Pod", "duration": 999}, t,
                                      {"pct": {}, "raw": {}}, None, cfg, tmp_path / "v.mp4", None)
    assert len(approved) == 1
    clip = approved[0]
    s, e = strong_span(t)
    assert abs(clip.start - s) < 1.0
    assert clip.hook.startswith("Nobody talks about the day I lost everything")
    assert clip.caption and "bigpod" in clip.hashtags
    assert clip.judge_score is None and clip.judge_reasons.startswith("Built-in judge")
    assert all(c.ai_score < cfg["analysis"]["local_content_threshold"] or c.fatal_flaws
               for c in judged if c is not clip)


def test_free_collector(monkeypatch):
    listing = {"https://www.youtube.com/@Big/shorts": [{"id": "a", "view_count": 10}, {"id": "b", "view_count": 5},
                                                       {"id": "old", "view_count": 7}],
               "https://www.tiktok.com/@blocked": []}
    monkeypatch.setattr(free, "list_source", lambda url, limit: listing[url])
    full = {"a": {"id": "a", "title": "Crazy story #podcast", "view_count": 1000, "like_count": 50,
                  "comment_count": 5, "duration": 31, "upload_date": "20260901",
                  "channel_follower_count": 2000, "tags": ["Podcast", "shorts"]}}
    monkeypatch.setattr(free, "details", lambda url: full.get(url.rsplit("/", 1)[-1]))
    logs = []
    got = free.collect_free({"youtube_channels": ["Big"], "tiktok_creators": ["blocked"]}, {"old"},
                            log=logs.append)
    by_id = {v["video_id"]: v for v in got}
    assert set(by_id) == {"a", "b"}  # "old" is already stored, "b" falls back to listing data
    a = by_id["a"]
    assert a["platform"] == "youtube_shorts" and a["views"] == 1000 and a["hashtags"] == ["podcast"]
    assert a["created_at"] and a["author_followers"] == 2000 and a["duration"] == 31
    assert by_id["b"]["views"] == 5
    assert any("TikTok often blocks" in m for m in logs)


def test_trend_step_runs_on_free_data_alone(cfg, monkeypatch):
    cfg["trends"]["free"]["enabled"] = True
    fake = synthetic_short_videos(1100)
    for v in fake:
        v["platform"] = "youtube_shorts"
    monkeypatch.setattr("clipper.trends.collect_free", lambda *a, **k: fake)
    assert not cfg.apify_token
    profile = run_trend_analysis(cfg, Database(cfg.path("paths.db")), Reporter())
    assert profile["n_videos"] == 1100 and profile["platforms"]["youtube_shorts"]["viral"] > 0


def test_full_run_without_keys(cfg, monkeypatch):
    work = cfg.path("paths.work_dir") / "vid9"
    work.mkdir(parents=True)
    t = transcript()
    duration = t["words"][-1]["e"] + 2
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=640x360:r=30:d={duration}", "-f", "lavfi", "-i", f"sine=f=200:d={duration}",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(work / "source.mp4")], check=True)
    (work / "info.json").write_text(json.dumps({"id": "vid9", "title": "Big Podcast Ep", "channel": "Big Pod",
                                                "duration": duration, "heatmap": []}))
    (work / "transcript.json").write_text(json.dumps({"source": "test", **t}))
    Database(cfg.path("paths.db")).save_short_videos(synthetic_short_videos())
    cand = {"video_id": "vid9", "title": "Big Podcast Ep", "channel": "Big Pod", "duration": duration,
            "views": 1e6, "published": 0, "source": "search"}
    monkeypatch.setattr(pipeline_mod, "discover", lambda *a, **k: [cand])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60, use_comments=False)
    cfg["discovery"]["max_videos_per_run"] = 1

    clips = Pipeline(cfg).run("simple")

    assert len(clips) == 1
    listed = list_outputs(cfg.path("paths.output_dir"))
    assert listed[0]["title"].startswith("Nobody talks about the day I lost everything")


def test_source_key():
    from clipper.analysis.download import source_key

    assert source_key("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert source_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5") == "dQw4w9WgXcQ"
    assert source_key("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    k = source_key(r"C:\Users\me\Videos\my podcast.mp4")
    assert k.startswith("my_podcast_") and k == source_key(r"C:\Users\me\Videos\my podcast.mp4")


def test_login_fallback(monkeypatch):
    import pytest

    from clipper import ytdl

    monkeypatch.setattr(ytdl, "_configured_login", False)
    calls = []

    def fn():
        calls.append(dict(ytdl._cookies))
        if ytdl._cookies.get("cookiesfrombrowser") != ("edge",):
            raise RuntimeError("ERROR: Sign in to confirm you're not a bot")
        return "ok"

    assert ytdl.with_login_fallback(fn) == "ok"
    assert calls[0] == {} and ytdl._cookies == {"cookiesfrombrowser": ("edge",)}
    ytdl._cookies.clear()
    with pytest.raises(ytdl.BotCheck, match="signed in"):
        ytdl.with_login_fallback(lambda: (_ for _ in ()).throw(RuntimeError("HTTP Error 429")))
    with pytest.raises(ValueError):
        ytdl.with_login_fallback(lambda: (_ for _ in ()).throw(ValueError("other problem")))


def test_clip_a_video_file(cfg, tmp_path, monkeypatch):
    from clipper.analysis.download import source_key

    t = transcript()
    duration = t["words"][-1]["e"] + 2
    src = tmp_path / "My Podcast Ep 1.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=640x360:r=30:d={duration}", "-f", "lavfi", "-i", f"sine=f=200:d={duration}",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    work = cfg.path("paths.work_dir") / source_key(str(src))
    work.mkdir(parents=True)
    (work / "transcript.json").write_text(json.dumps({"source": "test", **t}))  # skip Whisper in tests
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60, use_comments=False)

    clips = Pipeline(cfg).clip_video(f'"{src}"', "simple")  # quotes from "Copy as path" are fine

    assert len(clips) == 1
    listed = list_outputs(cfg.path("paths.output_dir"))
    assert "Credit" not in listed[0]["caption"]  # your own file: no credit line
    assert "#storytime" in listed[0]["caption"]


def test_web_clip_endpoint(cfg):
    from fastapi.testclient import TestClient

    from clipper.web.app import create_app

    client = TestClient(create_app(cfg))
    assert client.post("/api/clip", json={"source": "  "}).status_code == 400
    assert client.post("/api/clip", json={"source": "x", "level": "ultra"}).status_code == 400
    assert "Use my videos" in client.get("/").text


def test_news_broadcasts_are_skipped(cfg, monkeypatch):
    from clipper.discovery import youtube

    now = __import__("time").time()
    base = {"description": "", "channel": "c", "channel_id": "", "published": now - 86400, "duration": 3600,
            "views": 5e6, "likes": 0.0, "comments": 0.0, "live": False}
    found = [{**base, "video_id": "a" * 11, "title": "ABC World News Tonight Full Broadcast"},
             {**base, "video_id": "b" * 11, "title": "Theo Von podcast full episode"}]
    monkeypatch.setattr(youtube, "ytdlp_search", lambda *a, **k: found)
    monkeypatch.setattr(youtube, "ytdlp_enrich", lambda cands: None)
    ranked = youtube.discover(cfg, Database(cfg.path("paths.db")), Reporter(), None)
    assert [c["video_id"] for c in ranked] == ["b" * 11]


def test_split_sources():
    from clipper.pipeline import split_sources

    text = ('https://youtu.be/aaaaaaaaaaa https://youtu.be/bbbbbbbbbbb\n'
            '"C:\\Users\\me\\Videos\\My Podcast.mp4"\n\n https://youtu.be/aaaaaaaaaaa ')
    assert split_sources(text) == ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb",
                                   "C:\\Users\\me\\Videos\\My Podcast.mp4"]


STRONG_B = ("Nobody believed me when I said I would quit my job. I had twenty thousand dollars of debt and I was "
            "terrified. My boss laughed at me and said I would be broke in a month. Six months later I made my first "
            "million dollars. That's why you should never listen to people who never tried anything. ")


def _video_with(tmp_path, cfg, name, text):
    from clipper.analysis.download import source_key

    words = make_words(text)
    t = {"words": words, "segments": group_segments(words)}
    duration = words[-1]["e"] + 2
    src = tmp_path / name
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=640x360:r=30:d={duration}", "-f", "lavfi", "-i", f"sine=f=200:d={duration}",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    work = cfg.path("paths.work_dir") / source_key(str(src))
    work.mkdir(parents=True)
    (work / "transcript.json").write_text(json.dumps({"source": "test", **t}))
    return src


def test_clips_are_pooled_across_videos(cfg, tmp_path, monkeypatch):
    a = _video_with(tmp_path, cfg, "a.mp4", TEXT)
    b = _video_with(tmp_path, cfg, "b.mp4", DULL + STRONG_B + DULL)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60, use_comments=False)

    clips = Pipeline(cfg).clip_video(f"{a}\n{b}", "simple", clips=2)

    assert len(clips) == 2
    assert len({c["folder"] for c in clips}) == 2  # one from each video
    titles = " ".join(c["title"] for c in clips)
    assert "Nobody talks about the day" in titles
    assert "Nobody believed me" in titles or "twenty thousand dollars" in titles


def test_pasted_videos_are_all_considered(cfg, tmp_path, monkeypatch):
    a = _video_with(tmp_path, cfg, "a.mp4", TEXT)
    b = _video_with(tmp_path, cfg, "b.mp4", DULL + STRONG_B + DULL)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60, use_comments=False)
    pipe = Pipeline(cfg)
    pipe.clip_video(f"{a}\n{b}", "simple", clips=1)  # 1 clip wanted, but both videos must be watched
    watched = [e.message for e in pipe.rep.history if "strong clip" in e.message or "Nothing in this video" in e.message]
    assert len(watched) == 2
