"""End-to-end: button press -> trends -> discovery -> analysis -> strict judge -> edited clips."""
import json
import subprocess

from clipper import pipeline as pipeline_mod
from clipper.analysis.transcribe import group_segments
from clipper.db import Database
from clipper.media import ffmpeg_exe
from clipper.pipeline import Pipeline, list_outputs

from .conftest import synthetic_short_videos
from .test_moments import SENTENCES, FakeLLM, make_words


def test_full_run(cfg, monkeypatch):
    work = cfg.path("paths.work_dir") / "vid123"
    work.mkdir(parents=True)
    words = make_words(SENTENCES)
    duration = words[-1]["e"] + 2
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=640x360:r=30:d={duration}", "-f", "lavfi", "-i", f"sine=f=200:d={duration}",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(work / "source.mp4")], check=True)
    # pretend the download + transcription already happened (both are cached on disk)
    (work / "info.json").write_text(json.dumps({"id": "vid123", "title": "Big Podcast Ep", "channel": "Big Pod",
                                                "duration": duration, "heatmap": []}))
    (work / "transcript.json").write_text(json.dumps({"source": "test", "words": words,
                                                      "segments": group_segments(words)}))
    Database(cfg.path("paths.db")).save_short_videos(synthetic_short_videos())

    cand = {"video_id": "vid123", "title": "Big Podcast Ep", "channel": "Big Pod", "duration": duration,
            "views": 1e6, "published": 0, "source": "search"}
    monkeypatch.setattr(pipeline_mod, "discover", lambda *a, **k: [cand])
    cfg["analysis"].update(judge_with_frames=False, chunk_minutes=100, min_clip_seconds=10, use_comments=False)
    cfg["discovery"]["videos_per_run"] = 1
    pipe = Pipeline(cfg)
    monkeypatch.setattr(pipe, "_llm", lambda: FakeLLM(group_segments(words)))

    clips = pipe.run("simple")

    assert len(clips) == 1
    assert pipe.db.is_processed("vid123")
    listed = list_outputs(cfg.path("paths.output_dir"))
    assert listed[0]["title"] == "Great clip" and "#podcast" in listed[0]["caption"]
    assert "Credit: Big Pod" in listed[0]["caption"]
    kinds = [e.kind for e in pipe.rep.history]
    assert "clip" in kinds and kinds[-1] == "done"
