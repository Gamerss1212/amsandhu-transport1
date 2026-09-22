from viralforge.config import Config
from viralforge.edit.reframe import (
    base_crop_size, build_crop_window, sendcmd_script, sized_crop,
)
from viralforge.models import FaceSample, SourceVideo


def source(width=1920, height=1080) -> SourceVideo:
    return SourceVideo(path="x.mp4", video_id="x", title="x", url="x", duration=120.0,
                       width=width, height=height, fps=30.0)


def test_crop_matches_output_aspect():
    w, h = base_crop_size(1920, 1080, 1080, 1920)
    assert (w, h) == (608, 1080)
    assert abs(w / h - 1080 / 1920) < 0.01


def test_vertical_source_crops_top_and_bottom():
    w, h = base_crop_size(1080, 1920, 1080, 1920)
    assert (w, h) == (1080, 1920)
    w, h = base_crop_size(1080, 2400, 1080, 1920)          # taller than 9:16
    assert w == 1080 and h == 1920


def test_crop_sizes_are_even_and_in_bounds():
    for zoom in (1.0, 1.2, 1.6, 2.5):
        w, h = sized_crop(1920, 1080, 1080, 1920, zoom)
        assert w % 2 == 0 and h % 2 == 0
        assert 0 < w <= 1920 and 0 < h <= 1080


def test_zoom_does_not_upscale_past_the_softness_limit():
    _, h = sized_crop(1920, 1080, 1080, 1920, 3.0, max_upscale=2.4)
    assert h >= 1920 / 2.4 - 2                       # never softer than 2.4x
    _, punch = sized_crop(1920, 1080, 1080, 1920, 1.18, max_upscale=2.4)
    assert punch < 1080                              # ...but a punch-in still bites
    _, tight = sized_crop(3840, 2160, 1080, 1920, 2.2, max_upscale=2.4)
    assert tight < 2160 / 2.0                        # 4K can take a tighter one


def test_keyframes_stay_inside_the_frame():
    faces = [FaceSample(t=t / 4, cx=0.05 + 0.9 * (t / 80), cy=0.3, size=0.15, score=1.0)
             for t in range(81)]
    crop = build_crop_window(source(), 0.0, 20.0, faces, Config())
    for _, x, y in crop.keyframes:
        assert 0 <= x <= 1920 - crop.w
        assert 0 <= y <= 1080 - crop.h


def test_pan_speed_is_capped():
    cfg = Config()
    # A subject that teleports across the frame every half second.
    faces = [FaceSample(t=t / 4, cx=0.1 if t % 2 else 0.9, cy=0.3, size=0.15, score=1.0)
             for t in range(81)]
    crop = build_crop_window(source(), 0.0, 20.0, faces, cfg)
    limit = cfg.reframe.max_pan_speed / cfg.reframe.keyframe_rate + 1
    xs = [x for _, x, _ in crop.keyframes]
    assert all(abs(b - a) <= limit for a, b in zip(xs, xs[1:]))


def test_catches_up_on_real_movement():
    """Heavy smoothing must not mean the subject walks out of frame."""
    cfg = Config()
    faces = [FaceSample(t=t / 4, cx=min(0.95, 0.05 + 0.05 * t), cy=0.35, size=0.15, score=1.0)
             for t in range(81)]
    crop = build_crop_window(source(), 0.0, 20.0, faces, cfg)
    last_t, last_x, _ = crop.keyframes[-1]
    target = min(0.95 * 1920 - crop.w / 2, 1920 - crop.w)
    # The deadzone is deliberate: the camera stops chasing once the subject is
    # close enough to centre, rather than hunting forever.
    assert abs(last_x - target) <= cfg.reframe.deadzone * 1920 + 4


def test_static_subject_produces_a_static_camera():
    faces = [FaceSample(t=t / 4, cx=0.5, cy=0.35, size=0.15, score=1.0) for t in range(41)]
    crop = build_crop_window(source(), 0.0, 10.0, faces, Config())
    assert len(crop.keyframes) == 1          # redundant keyframes are dropped


def test_center_mode_ignores_faces():
    cfg = Config()
    cfg.reframe.mode = "center"
    faces = [FaceSample(t=1.0, cx=0.9, cy=0.2, size=0.2, score=1.0)]
    crop = build_crop_window(source(), 0.0, 5.0, faces, cfg)
    assert crop.keyframes == [(0.0, (1920 - crop.w) // 2, (1080 - crop.h) // 2)]


def test_sendcmd_targets_the_named_instance():
    """A bare `crop` target drives every crop in the graph - see the docstring."""
    crop = build_crop_window(source(), 0.0, 4.0,
                             [FaceSample(t=t / 4, cx=0.2 + t / 100, cy=0.3, size=0.1, score=1.0)
                              for t in range(17)], Config())
    script = sendcmd_script(crop, "crop@s7")
    assert "crop@s7 x" in script
    assert not any(line.strip().split(" ")[1] == "crop" for line in script.splitlines() if line)
