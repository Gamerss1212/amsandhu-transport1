import random
import time

import pytest

from clipper.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config(tmp_path / "missing.yaml")
    c["paths"] = {"work_dir": str(tmp_path / "work"), "output_dir": str(tmp_path / "out"),
                  "db": str(tmp_path / "db.sqlite")}
    (tmp_path / "work").mkdir()
    (tmp_path / "out").mkdir()
    for key in ("music_dir", "sfx_dir", "broll_dir", "fonts_dir"):
        c["editing"][key] = str(tmp_path / key)
    c["trends"]["import_dir"] = str(tmp_path / "imports")
    c["trends"]["free"]["enabled"] = False  # tests never touch the network
    return c


def synthetic_short_videos(n: int = 1200, seed: int = 7) -> list[dict]:
    """Fake TikTok/IG data where 'insane' captions and ~25s videos genuinely perform better."""
    rng = random.Random(seed)
    now = time.time()
    videos = []
    words = ["podcast", "interview", "life", "advice", "money", "gym", "daily", "vlog", "coffee", "morning"]
    for i in range(n):
        viral_trait = rng.random() < 0.3
        duration = rng.uniform(20, 30) if viral_trait else rng.uniform(5, 180)
        caption = " ".join(rng.sample(words, 3))
        if viral_trait:
            caption = "this is insane " + caption
        base = rng.lognormvariate(10, 1.2) * (8 if viral_trait else 1)
        followers = rng.lognormvariate(11, 1)
        videos.append({
            "platform": "tiktok" if i % 2 else "instagram", "video_id": str(i), "url": "", "caption": caption,
            "hashtags": ["fyp"] + (["podcastclips"] if viral_trait else []), "views": base,
            "likes": base * rng.uniform(0.02, 0.1) * (1.5 if viral_trait else 1),
            "comments": base * 0.002, "shares": base * rng.uniform(0.001, 0.01) * (2 if viral_trait else 1),
            "saves": base * 0.003, "duration": duration, "author": f"a{i % 50}",
            "author_followers": followers, "created_at": now - rng.uniform(3600, 86400 * 20), "music": "",
        })
    return videos
