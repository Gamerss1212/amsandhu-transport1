"""Filter-graph construction.  These run without ffmpeg - they inspect the string."""

from pathlib import Path

import pytest

from viralforge.config import Config
from viralforge.edit.planner import build_plan
from viralforge.edit.render import _build_graph, crop_target
from viralforge.models import (
    AudioAnalysis, ClipCandidate, SourceVideo, Transcript, TranscriptSegment, VisualAnalysis, Word,
)


def build(cfg=None, silences=((15.0, 16.5), (25.0, 26.4)), cuts=(20.0,)):
    cfg = cfg or Config()
    words = [Word(text=f"w{i}", start=10.0 + i * 0.4, end=10.0 + i * 0.4 + 0.3)
             for i in range(70)]
    transcript = Transcript(language="en", segments=[
        TranscriptSegment(start=10.0, end=40.0, text="t", words=words)])
    src = SourceVideo(path="s.mp4", video_id="s", title="s", url="s", duration=120.0,
                      width=1920, height=1080, fps=30.0)
    cand = ClipCandidate(start=10.0, end=40.0, transcript="t", hook_line="a hook")
    audio = AudioAnalysis(hop=0.02, silences=list(silences), duration=120.0)
    plan = build_plan(1, cand, src, transcript, audio, VisualAnalysis(scene_cuts=list(cuts)), cfg)
    cmd_files = {i: Path(f"/tmp/s{i}.cmd") for i, s in enumerate(plan.shots)}
    graph, inputs, maps = _build_graph(plan, cfg, {"has_audio": True}, 10.0,
                                       Path("/tmp/i.mp4"), Path("/tmp/c.ass"), cmd_files)
    return plan, graph, inputs, maps


def test_each_shot_crop_gets_its_own_instance_name():
    """sendcmd dispatches by filter class name, so shots must not share one.

    With a bare `crop` target every shot's tracking data drives every other
    shot's framing, and the subject leaves the frame.
    """
    plan, graph, _, _ = build()
    assert len(plan.shots) > 1
    for i in range(len(plan.shots)):
        assert f"{crop_target(i)}=w=" in graph
    assert ",crop=w=" not in graph


def test_concat_arity_matches_the_shot_count():
    plan, graph, _, _ = build()
    assert f"concat=n={len(plan.shots)}:v=1:a=1" in graph


def test_output_is_normalised_for_the_platform():
    _, graph, _, maps = build()
    cfg = Config()
    assert f"scale={cfg.render.width}:{cfg.render.height}" in graph
    assert "setsar=1" in graph
    assert f"loudnorm=I={cfg.audio.loudness_target}" in graph
    assert "format=yuv420p" in graph
    assert maps == ["[vout]", "[aout]"]


def test_progress_bar_is_animated_not_static():
    """drawbox evaluates its width once; overlay evaluates per frame."""
    _, graph, _, _ = build()
    assert "overlay=x='-w+w*min(1" in graph
    assert "drawbox" not in graph


def test_progress_bar_can_be_turned_off():
    cfg = Config()
    cfg.render.progress_bar = False
    _, graph, _, _ = build(cfg)
    assert "overlay" not in graph and "[vout]" in graph


def test_music_bed_is_ducked_against_the_voice(tmp_path):
    music = tmp_path / "bed.mp3"
    music.write_bytes(b"\x00" * 16)
    cfg = Config()
    cfg.audio.music_path = str(music)
    _, graph, inputs, _ = build(cfg)
    assert "sidechaincompress" in graph
    assert "amix=inputs=2" in graph
    assert any("-stream_loop" in spec for spec in inputs)


def test_silent_source_gets_a_silent_track():
    cfg = Config()
    words = [Word(text="w", start=10.0, end=10.3)]
    transcript = Transcript(language="en", segments=[
        TranscriptSegment(start=10.0, end=40.0, text="t", words=words)])
    src = SourceVideo(path="s.mp4", video_id="s", title="s", url="s", duration=120.0,
                      width=1920, height=1080, fps=30.0)
    plan = build_plan(1, ClipCandidate(start=10.0, end=40.0), src, transcript, None,
                      VisualAnalysis(), cfg)
    graph, inputs, maps = _build_graph(plan, cfg, {"has_audio": False}, 10.0,
                                       Path("/tmp/i.mp4"), Path("/tmp/c.ass"), {})
    assert "anullsrc" in " ".join(" ".join(s) for s in inputs)
    assert maps[-1] == "1:a"


def test_filter_paths_are_escaped():
    from viralforge.utils import escape_filter_path
    assert escape_filter_path(r"C:\clips\my video.ass") == r"C\:/clips/my video.ass"
    assert escape_filter_path("/tmp/a'b.ass") == r"/tmp/a\'b.ass"
