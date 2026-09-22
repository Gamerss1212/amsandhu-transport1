import pytest

from viralforge.config import Config
from viralforge.edit.planner import TimelineMap, build_plan
from viralforge.models import (
    AudioAnalysis, ClipCandidate, SourceVideo, Transcript, TranscriptSegment,
    VisualAnalysis, Word,
)


def make_transcript(start=0.0, end=60.0, step=0.4) -> Transcript:
    words, t = [], start
    i = 0
    while t < end:
        words.append(Word(text=f"w{i}", start=t, end=t + step * 0.8))
        t += step
        i += 1
    seg = TranscriptSegment(start=start, end=end, text=" ".join(w.text for w in words),
                            words=words)
    return Transcript(language="en", segments=[seg])


def source() -> SourceVideo:
    return SourceVideo(path="x.mp4", video_id="x", title="x", url="x", duration=120.0,
                       width=1920, height=1080, fps=30.0)


def candidate(start=10.0, end=40.0) -> ClipCandidate:
    return ClipCandidate(start=start, end=end, transcript="text")


def test_shots_are_contiguous_on_the_output_timeline():
    audio = AudioAnalysis(hop=0.02, silences=[(15.0, 16.5), (25.0, 26.2)], duration=120.0)
    plan = build_plan(1, candidate(), source(), make_transcript(), audio,
                      VisualAnalysis(), Config())
    cursor = 0.0
    for shot in plan.shots:
        assert abs(shot.out_start - cursor) < 1e-6
        cursor += shot.out_duration
    assert abs(plan.out_duration - cursor) < 1e-6


def test_silence_is_removed():
    audio = AudioAnalysis(hop=0.02, silences=[(15.0, 18.0)], duration=120.0)
    plan = build_plan(1, candidate(), source(), make_transcript(), audio,
                      VisualAnalysis(), Config())
    assert plan.out_duration < 30.0 - 2.5


def test_silence_removal_backs_off_when_it_would_gut_the_clip():
    """A wrong gate on quiet audio must not eat most of the clip."""
    audio = AudioAnalysis(hop=0.02, duration=120.0,
                          silences=[(t, t + 0.9) for t in range(10, 40)])
    plan = build_plan(1, candidate(), source(), make_transcript(), audio,
                      VisualAnalysis(), Config())
    assert plan.out_duration == pytest.approx(30.0, abs=0.2)


def test_captions_land_inside_the_output():
    audio = AudioAnalysis(hop=0.02, silences=[(15.0, 16.5)], duration=120.0)
    plan = build_plan(1, candidate(), source(), make_transcript(), audio,
                      VisualAnalysis(), Config())
    assert plan.caption_words
    for w in plan.caption_words:
        assert 0.0 <= w.start < plan.out_duration
        assert w.start < w.end <= plan.out_duration


def test_caption_words_stay_in_order():
    audio = AudioAnalysis(hop=0.02, silences=[(15.0, 16.5), (22.0, 23.4)], duration=120.0)
    plan = build_plan(1, candidate(), source(), make_transcript(), audio,
                      VisualAnalysis(), Config())
    starts = [w.start for w in plan.caption_words]
    assert starts == sorted(starts)


def test_scene_cuts_split_shots():
    visual = VisualAnalysis(scene_cuts=[20.0, 30.0])
    plan = build_plan(1, candidate(), source(), make_transcript(), None, visual, Config())
    assert len(plan.shots) == 3
    assert abs(plan.shots[1].src_start - 20.0) < 1e-6


def test_every_shot_gets_a_crop():
    plan = build_plan(1, candidate(), source(), make_transcript(), None,
                      VisualAnalysis(), Config())
    assert all(s.crop is not None and s.crop.w > 0 for s in plan.shots)


def test_timeline_map_pins_gaps_to_the_seam():
    from viralforge.models import Shot
    shots = [Shot(src_start=0.0, src_end=5.0, out_start=0.0),
             Shot(src_start=8.0, src_end=12.0, out_start=5.0)]
    m = TimelineMap(shots)
    assert m.to_out(2.5) == pytest.approx(2.5)
    assert m.to_out(6.5) == pytest.approx(5.0)       # inside the removed gap
    assert m.to_out(10.0) == pytest.approx(7.0)


def test_hook_is_short_enough_to_read():
    cand = candidate()
    cand.hook_line = ("an extremely long hook line that nobody could possibly read "
                      "inside a second and a half of screen time")
    plan = build_plan(1, cand, source(), make_transcript(), None, VisualAnalysis(), Config())
    assert plan.hook_duration > 0
