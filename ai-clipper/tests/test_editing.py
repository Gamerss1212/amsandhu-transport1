import subprocess

import pytest

from clipper.editing import LEVELS, RenderJob, get_preset, render
from clipper.editing.captions import ass_color, ass_time, build_ass
from clipper.editing.reframe import plan_track, x_expression
from clipper.editing.timeline import cut_points, keep_ranges, output_duration, remap, remap_words
from clipper.media import ffmpeg_exe, probe


def words_from(spec):
    return [{"w": w, "s": s, "e": e} for w, s, e in spec]


WORDS = words_from([("Hello", 1.0, 1.3), ("um", 1.4, 1.6), ("world.", 1.7, 2.0),
                    ("This", 4.0, 4.3), ("is", 4.35, 4.5), ("great.", 4.6, 5.0)])


def test_keep_ranges_cuts_pauses_and_fillers():
    assert keep_ranges(WORDS, 0.8, 5.3, None, False) == [(0.8, 5.3)]
    ranges = keep_ranges(WORDS, 0.8, 5.3, 0.5, True)
    assert len(ranges) == 3  # filler cut + long pause cut
    assert ranges[0][0] == 0.8 and ranges[-1][1] == 5.3
    assert not any(a <= 1.5 <= b for a, b in ranges)  # the "um" is gone
    assert output_duration(ranges) < 4.5 - 1.5


def test_remap_to_new_timeline():
    ranges = [(0.0, 2.0), (4.0, 6.0)]
    assert remap(1.0, ranges) == 1.0
    assert remap(3.0, ranges) is None
    assert remap(5.0, ranges) == 3.0
    assert remap(5.0, ranges, speed=2.0) == 1.5
    assert [w["w"] for w in remap_words(WORDS, keep_ranges(WORDS, 0.8, 5.3, 0.5, True))] == \
        ["Hello", "world.", "This", "is", "great."]
    assert cut_points(ranges) == [2.0]


def test_ass_helpers():
    assert ass_color("#FFE600") == "&H0000E6FF"
    assert ass_time(3723.456) == "1:02:03.46"


def test_build_ass_styles():
    for style in ("basic", "pop", "karaoke"):
        ass = build_ass(WORDS, style, 3, True, "DejaVu Sans", "#FFE600", "#00FF88", {"great"}, 6.0,
                        hook="Wait for it")
        assert "Style: Cap" in ass and "WAIT FOR IT" in ass
        assert "UM" not in ass.split("[Events]")[1]  # fillers never shown
    karaoke = build_ass(WORDS, "karaoke", 3, True, "Arial", "#FFE600", "#00FF88", set(), 6.0)
    assert karaoke.count("Dialogue:") == 5  # one event per spoken word


def test_track_planning():
    times = [i / 3 for i in range(30)]
    faces = [[(0.3, 0.1)]] * 15 + [[(0.7, 0.1)]] * 15
    track = plan_track(times, faces, cuts=[5.0], mode="face_smooth")
    assert track.layout == "crop"
    assert track.keyframes[0][1] == pytest.approx(0.3) and track.keyframes[-1][1] == pytest.approx(0.7)
    expr = x_expression(track, "1920", "608")
    assert expr.startswith("if(lt(t,") and "608" in expr
    wide = plan_track(times, [[(0.2, 0.1), (0.8, 0.1)]] * 30, [], "face")
    assert wide.layout == "fit"
    assert plan_track(times, faces, [], "center").keyframes == [(0.0, 0.5)]


def test_levels():
    assert list(LEVELS) == ["simple", "normal", "hard", "professional", "extreme"]
    assert get_preset("EXTREME").broll_split
    with pytest.raises(ValueError):
        get_preset("ultra")


@pytest.mark.parametrize("level", ["simple", "hard", "extreme"])
def test_render_real_video(cfg, tmp_path, level):
    src = tmp_path / "src.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=8",
                    "-f", "lavfi", "-i", "sine=f=300:d=8", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    str(src)], check=True)
    out = tmp_path / "clips"
    out.mkdir()
    job = RenderJob(src, 0.8, 5.3, WORDS, "Wait for it", ["great"], out, f"clip_{level}")
    info = render(job, get_preset(level), cfg)
    result = probe(out / info["video"])
    assert (result["width"], result["height"]) == (1080, 1920)
    assert result["has_audio"]
    assert abs(result["duration"] - info["duration"]) < 0.3
    assert (out / info["thumbnail"]).exists()
    assert not any(p.name.startswith(".work") for p in out.iterdir())


def test_censor_explicit_words_only():
    from clipper.editing.safety import censor

    assert censor("What the FUCK is this shit") == "What the F*CK is this sh*t"
    assert censor("I was sucking his cock") == "I was sucking his c*ck"
    assert censor("Dickens ordered a cocktail and shiitake") == "Dickens ordered a cocktail and shiitake"
    assert censor("") == ""


def test_builtin_assets_are_generated_once(tmp_path):
    from clipper.editing.builtin_assets import builtin, make_broll

    music, sfx = builtin("music", tmp_path), builtin("sfx", tmp_path)
    assert probe(music)["has_audio"] and probe(music)["duration"] > 30
    assert probe(sfx)["has_audio"] and 0.4 < probe(sfx)["duration"] < 1.0
    stamp = music.stat().st_mtime
    assert builtin("music", tmp_path).stat().st_mtime == stamp  # cached, not regenerated
    clip = make_broll(tmp_path / "b.mp4", seconds=2)
    assert probe(clip)["width"] == 540 and abs(probe(clip)["duration"] - 2) < 0.2


def test_srt_and_hook_time():
    from clipper.editing.captions import build_srt, hook_time
    srt = build_srt(WORDS, 3)
    assert srt.startswith("1\n00:00:0") and " --> " in srt
    assert hook_time("short hook") == 2.2 and hook_time("word " * 40) == 4.0
