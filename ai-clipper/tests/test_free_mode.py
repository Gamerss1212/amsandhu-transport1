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
    cfg["discovery"]["videos_per_run"] = 1

    clips = Pipeline(cfg).run("simple")

    assert len(clips) == 1
    listed = list_outputs(cfg.path("paths.output_dir"))
    assert listed[0]["title"].startswith("Nobody talks about the day I lost everything")
