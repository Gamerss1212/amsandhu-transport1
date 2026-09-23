import json

import pytest

from clipper.db import Database
from clipper.events import Reporter
from clipper.trends import NotEnoughTrendData, run_trend_analysis
from clipper.trends.analyzer import build_profile, hooks_in, playbook_text, trend_fit
from clipper.trends.providers import load_imports, normalize

from .conftest import synthetic_short_videos


def test_profile_learns_what_is_viral():
    profile = build_profile(synthetic_short_videos())
    assert profile["n_videos"] == 1200
    assert 200 <= profile["n_viral"] <= 280
    hooks = {r["feature"]: r["lift"] for r in profile["hook_lift"]}
    assert hooks["shock"] > 2.0
    assert "insane" in profile["top_viral_terms"][:10]
    assert profile["model_auc"] > 0.75
    assert profile["duration_lift"][0]["feature"] == "20-30s"
    json.dumps(profile)  # must be storable


def test_trend_fit_rewards_viral_traits():
    profile = build_profile(synthetic_short_videos())
    good = trend_fit(profile, "this is insane, you won't believe it", 25)
    bad = trend_fit(profile, "a calm chat about the weather", 150)
    assert good > bad + 15
    assert "Analyzed 1200" in playbook_text(profile)


def test_hooks_detection():
    assert "question" in hooks_in("Why does nobody talk about this?")
    assert "pov" in hooks_in("POV: you just got fired")
    assert "number_list" in hooks_in("3 signs you are underpaid")


def test_normalize_tiktok_and_instagram_shapes():
    tt = normalize({"id": "1", "text": "wow #fyp", "playCount": 5000, "diggCount": 100, "shareCount": 3,
                    "commentCount": 7, "collectCount": 2, "videoMeta": {"duration": 21},
                    "authorMeta": {"name": "a", "fans": 1000}, "createTimeISOString": "2026-09-01T10:00:00.000Z"},
                   "tiktok")
    assert tt["views"] == 5000 and tt["duration"] == 21 and tt["hashtags"] == ["fyp"]
    assert tt["author_followers"] == 1000 and tt["created_at"] > 0
    ig = normalize({"shortCode": "abc", "caption": "hey", "videoPlayCount": "1.2M", "likesCount": 10,
                    "commentsCount": 1, "videoDuration": 30.5, "ownerUsername": "x",
                    "timestamp": "2026-09-01T10:00:00Z", "hashtags": ["reels"]}, "instagram")
    assert ig["views"] == 1_200_000 and ig["video_id"] == "abc" and ig["hashtags"] == ["reels"]
    assert normalize({"id": "2", "playCount": 0}, "tiktok") is None


def test_load_imports_csv_and_json(tmp_path):
    (tmp_path / "tiktok_export.csv").write_text("id,caption,views,likes\n1,hello #fyp,1000,10\n2,bye,50,1\n")
    (tmp_path / "instagram.json").write_text(json.dumps([{"id": "9", "caption": "x", "views": 77}]))
    videos = load_imports(tmp_path)
    assert {v["platform"] for v in videos} == {"tiktok", "instagram"}
    assert len(videos) == 3


def test_run_requires_minimum_videos(cfg):
    db = Database(cfg.path("paths.db"))
    db.save_short_videos(synthetic_short_videos(300))
    with pytest.raises(NotEnoughTrendData):
        run_trend_analysis(cfg, db, Reporter())
    db.save_short_videos(synthetic_short_videos(1200, seed=3))
    profile = run_trend_analysis(cfg, db, Reporter())
    assert profile["n_videos"] >= 1000 and "playbook" in profile
    assert db.latest_profile()["n_videos"] == profile["n_videos"]
