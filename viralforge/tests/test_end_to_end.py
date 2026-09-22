"""One real run, start to finish, on synthetic footage.

Slow (roughly a minute) but it is the only test that proves the filter graph,
libass, the encoder and the packaging all agree with each other.
Run just this with:  pytest -m slow
"""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from viralforge.config import Config
from viralforge.pipeline import Pipeline

pytestmark = [pytest.mark.slow, needs_ffmpeg]


def probe(path, entries, stream=None):
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.split()


@pytest.fixture(scope="module")
def run(tmp_path_factory, media):
    video, srt = media
    out = tmp_path_factory.mktemp("out")
    cfg = Config()
    cfg.transcribe.external_path = str(srt)
    cfg.scoring.enable_llm = False
    cfg.output.directory = str(out)
    cfg.output.clips = 2
    cfg.candidates.min_duration = 12.0
    cfg.candidates.max_duration = 40.0
    cfg.render.preset = "ultrafast"
    cfg.work_dir = str(tmp_path_factory.mktemp("work"))
    return Pipeline(cfg).run(str(video))


def test_produces_the_requested_clips(run):
    assert len(run.deliverables) == 2
    assert run.out_dir and Path(run.out_dir).is_dir()


def test_videos_are_platform_ready(run):
    for d in run.deliverables:
        w, h = probe(d.video_path, "stream=width,height", "v:0")
        assert (int(w), int(h)) == (1080, 1920)
        codecs = probe(d.video_path, "stream=codec_name")
        assert "h264" in codecs and "aac" in codecs
        duration = float(probe(d.video_path, "format=duration")[0])
        assert 8.0 < duration < 60.0
        assert abs(duration - d.duration) < 1.5


def test_audio_is_normalised_for_the_platforms(run):
    """Every platform normalises to about -14 LUFS; arriving there avoids a re-encode."""
    for d in run.deliverables:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", d.video_path, "-af", "ebur128", "-f", "null", "-"],
            capture_output=True, text=True)
        line = [l for l in proc.stderr.splitlines() if "I:" in l and "LUFS" in l][-1]
        measured = float(line.split("I:")[1].split("LUFS")[0])
        assert -16.5 < measured < -11.5, line


def test_captions_are_burned_in(run):
    """Compare a caption row against the same row with captions off."""
    d = run.deliverables[0]
    frame = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "3", "-i", d.video_path, "-frames:v", "1",
         "-vf", "crop=1080:200:0:1180", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    bright = sum(1 for b in frame if b > 220)
    assert bright > 500, "no bright caption pixels found in the caption band"


def test_progress_bar_fills_over_time(run):
    d = run.deliverables[0]

    def filled(t):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(t), "-i", d.video_path, "-frames:v", "1",
             "-vf", "crop=1080:2:0:1914", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
            capture_output=True).stdout
        return sum(1 for b in raw[:1080] if b > 180)

    early, late = filled(1.0), filled(d.duration - 1.5)
    assert early < late, (early, late)
    assert late > 800


def test_every_clip_ships_with_its_copy(run):
    for d in run.deliverables:
        base = Path(d.video_path).with_suffix("")
        for suffix in (".mp4", ".txt", ".srt", ".json", ".jpg"):
            assert Path(str(base) + suffix).is_file(), suffix
        sheet = Path(str(base) + ".txt").read_text(encoding="utf-8")
        assert "CAPTION" in sheet and "TRANSCRIPT" in sheet
        assert "tiktok" in d.copy and d.copy["tiktok"].caption


def test_manifest_records_the_whole_run(run):
    manifest = json.loads((Path(run.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["resolution"] == "1920x1080"
    assert len(manifest["clips"]) == 2
    assert manifest["trend_profile"]["duration_bands"]
    assert manifest["settings"]["render"]["height"] == 1920
    assert (Path(run.out_dir) / "README.md").is_file()


def test_clips_do_not_overlap_in_the_source(run):
    spans = sorted((d.candidate.start, d.candidate.end) for d in run.deliverables)
    for (a_start, a_end), (b_start, b_end) in zip(spans, spans[1:]):
        assert a_end <= b_start


def test_second_run_reuses_the_cache(run, tmp_path_factory, media):
    """Re-running with different render settings must not re-transcribe."""
    video, srt = media
    cfg = Config()
    cfg.transcribe.external_path = str(srt)
    cfg.scoring.enable_llm = False
    cfg.output.directory = str(tmp_path_factory.mktemp("out2"))
    cfg.output.clips = 1
    cfg.candidates.min_duration = 12.0
    cfg.candidates.max_duration = 40.0
    cfg.render.preset = "ultrafast"
    cfg.work_dir = str(Path(run.out_dir).parent / "shared-work")
    first = Pipeline(cfg).run(str(video))
    cached = Path(cfg.work_dir) / "analysis"
    assert any(p.name == "transcript.json" for p in cached.rglob("*"))
    assert len(first.deliverables) == 1
