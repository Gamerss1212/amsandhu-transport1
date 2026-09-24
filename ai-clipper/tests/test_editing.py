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
    wide = plan_track(times, [[(0.8, 0.1, 0.4), (0.2, 0.1, 0.3)]] * 30, [], "face")
    assert wide.layout == "stack"  # podcast two-shot: one speaker per half, left one on top
    assert wide.speakers == [pytest.approx((0.2, 0.3, 0.1)), pytest.approx((0.8, 0.4, 0.1))]
    from clipper.editing.reframe import speaker_box
    w, h, x, y = speaker_box(wide.speakers[0], 1920, 1080, 1080 / 960)
    assert abs(w / h - 1.125) < 0.02 and 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080
    assert x < 0.2 * 1920 < x + w
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


def test_black_bars_are_detected(tmp_path):
    import subprocess

    from clipper.editing.editor import detect_borders
    from clipper.media import ffmpeg_exe

    boxed = tmp_path / "boxed.mp4"  # a 4:3 show inside a 16:9 file, like old TV uploads
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=480x360:r=30:d=3",
                    "-vf", "pad=640:360:80:0:black", "-c:v", "libx264", "-preset", "ultrafast", str(boxed)], check=True)
    crop = detect_borders(boxed, 0, 3)
    w, h, x, _ = map(int, crop.split("=")[1].split(":"))
    assert 470 <= w <= 484 and h >= 356 and 76 <= x <= 84
    clean = tmp_path / "clean.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clean)], check=True)
    assert detect_borders(clean, 0, 3) is None


def test_srt_lines_never_overlap():
    from clipper.editing.captions import build_srt

    words = [{"w": f"w{i}", "s": i * 0.3, "e": i * 0.3 + 0.28} for i in range(30)]
    import re as _re
    times = _re.findall(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)", build_srt(words, 3))
    secs = [(int(a[2]) + int(a[3]) / 1000, int(a[6]) + int(a[7]) / 1000) for a in times]
    assert all(b1 <= a2 for (_, b1), (a2, _) in zip(secs, secs[1:]))


def test_long_hooks_are_shortened_and_smaller():
    from clipper.editing.captions import hook_fit

    assert hook_fit("Nobody expected this", 74) == "NOBODY EXPECTED THIS"
    long = hook_fit("This is the longest hook anyone has ever written for a short video clip and it keeps going", 74)
    assert long.startswith("{\\fs55}") and long.endswith("SHORT...") and len(long.split()) == 12
    assert hook_fit("one two three four five six seven eight nine ten eleven the twelve", 74).endswith("ELEVEN...")


def test_mostly_silent_clip_is_not_cut_to_nothing(cfg, tmp_path):
    import subprocess

    from clipper.editing import RenderJob, get_preset
    from clipper.editing.editor import cut_pass
    from clipper.media import ffmpeg_exe

    src = tmp_path / "quiet.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=12",
                    "-f", "lavfi", "-i", "sine=d=12", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", str(src)],
                   check=True)
    job = RenderJob(source=src, start=0, end=12, words=[{"w": "Wow.", "s": 5.0, "e": 5.6}], hook="",
                    emphasis=[], out_dir=tmp_path, name="q")
    _, ranges, duration = cut_pass(job, get_preset("extreme"), tmp_path)
    assert duration > 10 and ranges == [(0, 12)]


def test_two_shots_inside_a_clip_switch_to_the_stacked_view():
    times = [i / 3 for i in range(60)]  # 20 s: single shot, two-shot from 6.7 s to 13.3 s, single again
    single, two = [(0.5, 0.2, 0.4)], [(0.2, 0.1, 0.35), (0.8, 0.1, 0.4)]
    faces = [single] * 20 + [two] * 20 + [single] * 20
    track = plan_track(times, faces, cuts=[6.6, 13.4], mode="face_smooth")
    assert track.layout == "crop" and len(track.stack_windows) == 1
    a, b = track.stack_windows[0]
    assert a == pytest.approx(6.6) and b == pytest.approx(13.4)  # switches exactly on the scene cuts
    assert track.speakers[0][0] < track.speakers[1][0]


def test_heartfelt_clips_get_calm_captions_and_no_effects():
    from clipper.editing.auto import tune
    from clipper.editing.levels import get_preset

    calm = tune(get_preset("extreme"), "emotional")
    assert calm.mood == "calm" and not calm.uppercase and not (calm.shake or calm.flash or calm.word_pops)
    assert tune(get_preset("extreme"), "funny") == get_preset("extreme")
    words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.35} for i, w in enumerate("I miss her every day".split())]
    hype = build_ass(words, "karaoke", 2, True, "Poppins", "#FFE600", "#00FF88", set(), 3.0)
    soft = build_ass(words, "karaoke", 4, False, "Poppins", "#FFE600", "#00FF88", set(), 3.0, mood="calm")
    assert "fscx112" in hype and "fscx112" not in soft and "\\fad(150,0)" in soft and "miss" in soft


def test_reaction_zoom_and_sound_design_render(cfg, tmp_path):
    import subprocess

    from clipper.editing import RenderJob, get_preset, render
    from clipper.editing.editor import zoom_events
    from clipper.media import ffmpeg_exe

    events = zoom_events([], set(), [], get_preset("extreme"), 20.0, reactions=[8.0])
    assert any(a < 8.0 < b and z >= 0.15 for a, b, z in events)
    src = tmp_path / "s.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8",
                    "-f", "lavfi", "-i", "sine=f=200:d=8", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    words = [{"w": w, "s": 0.5 + i * 0.5, "e": 0.9 + i * 0.5} for i, w in enumerate(
        "This is insane money and totally insane right now honestly crazy stuff here.".split())]
    job = RenderJob(source=src, start=0, end=8, words=words, hook="Wait for it", emphasis=["insane", "crazy"],
                    out_dir=tmp_path, name="fx", highlights=[4.0])
    info = render(job, get_preset("extreme"), cfg)  # low-res source + pops + hit + reaction zoom
    assert (tmp_path / "fx.mp4").exists() and info["zooms"] >= 1


def test_self_review_trims_black_frames_and_fixes_quiet_audio(tmp_path):
    import subprocess

    from clipper.editing.review import measure, review_and_fix
    from clipper.media import ffmpeg_exe

    clip = tmp_path / "c.mp4"  # 0.6 s of black at the start, then picture; very quiet audio
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=black:s=1080x1920:r=30:d=0.6",
                    "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=6", "-f", "lavfi", "-i", "sine=f=300:d=6.6",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume=0.3[a]", "-map", "[v]", "-map",
                    "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(clip)], check=True)
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    before = measure(clip)
    assert before.black and -40 < before.lufs < -25
    fixed, retry, notes = review_and_fix(clip, 6.6, 1080, 1920, 30, srt, ["-c:v", "libx264", "-preset", "ultrafast"])
    after = measure(clip)
    assert {"black_start", "loudness"} <= {f.code for f in fixed} and not retry
    assert not after.black and abs(after.lufs + 14) < 1.5 and after.true_peak < 0
    assert "00:00:00,4" in srt.read_text()  # subtitles moved with the trim


def test_failed_render_is_retried_with_a_safer_edit(cfg, tmp_path, monkeypatch):
    import subprocess

    from clipper.editing import RenderJob, editor, get_preset
    from clipper.media import ffmpeg_exe

    src = tmp_path / "s.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=6",
                    "-f", "lavfi", "-i", "sine=f=200:d=6", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    real, calls = editor._render_pass, []

    def flaky(job, preset, cfg, encode):
        calls.append(preset.shake)
        if len(calls) == 1:
            raise RuntimeError("ffmpeg failed: zoompan exploded")
        return real(job, preset, cfg, encode)
    monkeypatch.setattr(editor, "_render_pass", flaky)
    words = [{"w": w, "s": 0.4 + i * 0.4, "e": 0.7 + i * 0.4} for i, w in enumerate("this is a test clip.".split())]
    job = RenderJob(source=src, start=0, end=6, words=words, hook="Test", emphasis=[], out_dir=tmp_path, name="r")
    info = editor.render(job, get_preset("extreme"), cfg)
    assert calls == [True, False]  # second try without motion effects
    assert (tmp_path / "r.mp4").exists() and info["review"]["passes"] == 2
    assert any("render failed" in h for h in info["review"]["log"])


def test_stutters_are_cut_but_emphasis_is_kept():
    from clipper.editing.timeline import keep_ranges, stutters

    w = [{"w": x, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, x in enumerate("I I think the the answer is no, no, no.".split())]
    assert stutters(w) == {0, 3}
    ranges = keep_ranges(w, 0, 4, 0.4, True)
    assert ranges[0][0] > 0.2 and len(ranges) == 2  # first "I" and one "the" gone


def test_captions_move_below_a_low_face():
    from clipper.editing.editor import face_safe_caption_y

    assert face_safe_caption_y(([0], [[(0.5, 0.1, 0.35)]] * 5, []), 1280, 720, 1920) == 1380
    assert face_safe_caption_y(([0], [[(0.5, 0.25, 0.6)]] * 5, []), 1280, 720, 1920) == 1500


def test_shots_with_nobody_show_the_full_frame():
    times = [i / 3 for i in range(45)]  # 15 s: speaker, then 5 s of b-roll with no faces, then speaker
    face = [(0.5, 0.2, 0.4)]
    track = plan_track(times, [face] * 15 + [[]] * 15 + [face] * 15, cuts=[4.9, 10.1], mode="face_smooth")
    assert track.layout == "crop" and track.fit_windows == [(4.9, 10.1)]
    assert plan_track(times, [[]] * 45, cuts=[], mode="face").layout == "fit"  # screen recording: never blind-crop


def test_clip_running_past_the_end_of_the_file_is_clamped(cfg, tmp_path):
    import subprocess

    from clipper.editing import RenderJob, get_preset, render
    from clipper.media import ffmpeg_exe

    src = tmp_path / "short.mp4"  # 8 s of media, but the transcript claims words up to 14 s
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8",
                    "-f", "lavfi", "-i", "sine=f=200:d=8", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    words = [{"w": f"word{i}.", "s": 0.5 + i, "e": 1.2 + i} for i in range(14)]
    job = RenderJob(source=src, start=0, end=14.5, words=words, hook="", emphasis=[], out_dir=tmp_path, name="e")
    info = render(job, get_preset("simple"), cfg)
    assert info["review"]["ok"] and 7.5 <= info["duration"] <= 8.1
    assert "word13" not in (tmp_path / "e.srt").read_text()
