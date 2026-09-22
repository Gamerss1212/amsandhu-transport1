import json

import pytest

from viralforge.config import Config
from viralforge.trends.analyze import build_profile
from viralforge.trends.profile import DurationBand, PostSample, TrendProfile
from viralforge.trends.providers import FileProvider
from viralforge.trends.providers.local import build_baseline_profile, match_niche


def samples(n=40):
    out = []
    for i in range(n):
        winner = i < n // 2
        out.append(PostSample(
            platform="tiktok",
            caption=("stop doing this if you want to grow" if winner
                     else "here is another update from my week hope you enjoy it a lot"),
            hashtags=["business", "founder", "fyp"] if winner else ["vlog", "daily", "fyp"],
            duration=28.0 + (i % 5) if winner else 90.0 + i,
            views=500_000 - i * 1000 if winner else 900 + i,
            likes=40_000 if winner else 30,
            comments=900 if winner else 2,
            followers=100_000,
        ))
    return out


def test_baseline_is_labelled_as_a_baseline():
    p = build_baseline_profile("business podcast", ["tiktok"])
    assert "local-baseline" in p.source
    assert p.sample_size == 0
    assert p.duration_bands and p.hook_patterns and p.rubric
    assert "EVIDENCE" in p.prompt_block()


def test_niche_matching():
    assert match_niche("business podcast", {"business": {}, "podcast": {}}) in ("business", "podcast")
    assert match_niche("heavy lifting and gym stuff", {"fitness": {}}) == "fitness"
    assert match_niche("", {"fitness": {}}) == ""
    assert match_niche("underwater basket weaving", {"fitness": {}}) == ""


def test_duration_fit_peaks_inside_the_band():
    p = TrendProfile(duration_bands=[DurationBand(20, 35)])
    assert p.duration_fit(28) == 100.0
    assert p.duration_fit(60) < p.duration_fit(40) < 100.0


def test_profile_learns_from_the_winners():
    p = build_profile(samples(), "business", ["tiktok"], source="file", llm=None)
    assert p.sample_size == 40
    assert p.source == "file"
    assert any(b.low < 40 for b in p.duration_bands), p.duration_bands
    assert "business" in p.hashtag_pool
    assert "fyp" not in p.hashtag_pool          # generic tags carry no signal


def test_thin_data_falls_back_to_the_baseline():
    p = build_profile(samples(4), "business", ["tiktok"], source="file", llm=None)
    assert "local-baseline" in p.source


def test_profile_round_trips(tmp_path):
    p = build_baseline_profile("fitness", ["tiktok", "instagram"])
    path = tmp_path / "trends.json"
    p.save(str(path))
    back = TrendProfile.load(str(path))
    assert back.niche == "fitness"
    assert [b.low for b in back.duration_bands] == [b.low for b in p.duration_bands]


def test_file_provider_reads_csv_with_unfamiliar_headers(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(
        "Video link,Description,Play count,Likes,Comments,Video length\n"
        "https://x/1,first post #growth,120000,4000,120,31\n"
        "https://x/2,second post #growth,900,10,1,95\n",
        encoding="utf-8")
    rows = FileProvider(str(path)).collect("business", ["tiktok"], 50)
    assert len(rows) == 2
    assert rows[0].views == 120000            # sorted by velocity
    assert rows[0].duration == 31.0
    assert rows[0].hashtags == ["growth"]


def test_file_provider_reads_json_list(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps([
        {"webVideoUrl": "u", "text": "hello", "playCount": 10, "diggCount": 2,
         "hashtags": ["a"], "videoDuration": 20},
    ]), encoding="utf-8")
    rows = FileProvider(str(path)).collect("", ["tiktok"], 10)
    assert rows and rows[0].views == 10 and rows[0].hashtags == ["a"]


def test_file_provider_explains_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        FileProvider(str(tmp_path / "nope.csv")).collect("", ["tiktok"], 10)


def test_engagement_rate_and_velocity():
    s = PostSample(platform="tiktok", views=1000, likes=100, comments=10, shares=5,
                   saves=4, followers=500)
    assert s.engagement_rate == pytest.approx((100 + 30 + 25 + 16) / 1000)
    assert s.velocity == pytest.approx(2.0)
    assert PostSample(platform="tiktok", views=0).engagement_rate == 0.0
