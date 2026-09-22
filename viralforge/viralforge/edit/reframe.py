"""Auto-reframe: turn a 16:9 shot into a 9:16 one that follows the speaker.

The crop rectangle keeps a constant size for the whole life of a shot and moves
only on x/y.  That is not a simplification - it is the difference between a
render that takes seconds and one that takes minutes.  ffmpeg reconfigures the
entire filter graph whenever a filter's output dimensions change, so animating
crop w/h costs ~100x more than animating x/y.  Zoom is therefore expressed by
starting a new shot with a tighter constant crop, which also happens to be the
punch-in cut that reads better on short-form anyway.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..config import Config
from ..models import CropWindow, FaceSample, SourceVideo


def base_crop_size(src_w: int, src_h: int, out_w: int, out_h: int) -> Tuple[int, int]:
    """Largest crop of the source that has the output's aspect ratio."""
    target = out_w / out_h
    if src_w / src_h > target:          # source is wider - crop the sides
        h = src_h
        w = int(round(h * target))
    else:                               # source is already tall - crop top/bottom
        w = src_w
        h = int(round(w / target))
    return _even(min(w, src_w)), _even(min(h, src_h))


def sized_crop(src_w: int, src_h: int, out_w: int, out_h: int, zoom: float,
               max_upscale: float = 2.4) -> Tuple[int, int]:
    """Crop size for a given zoom, floored so the upscale stays acceptable."""
    bw, bh = base_crop_size(src_w, src_h, out_w, out_h)
    zoom = max(1.0, min(2.2, zoom))
    w, h = _even(bw / zoom), _even(bh / zoom)

    # The floor is expressed as total upscale to the delivery height, not as an
    # absolute size: a 4K source can take a much tighter punch-in than a 720p
    # one for the same apparent sharpness.
    floor_h = _even(min(bh, max(out_h / max(max_upscale, 1.0), 240)))
    if h < floor_h:
        h = floor_h
        w = _even(h * out_w / out_h)
    return max(16, min(w, src_w)), max(16, min(h, src_h))


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def build_crop_window(source: SourceVideo, src_start: float, src_end: float,
                      faces: Sequence[FaceSample], cfg: Config,
                      zoom: float = 1.0, speed: float = 1.0) -> CropWindow:
    """Crop path for one shot, keyframed in shot-local output seconds."""
    out_w, out_h = cfg.render.width, cfg.render.height
    src_w, src_h = source.width, source.height
    w, h = sized_crop(src_w, src_h, out_w, out_h, zoom, cfg.reframe.max_upscale)

    free_x, free_y = src_w - w, src_h - h
    if cfg.reframe.mode == "center" or not cfg.reframe.enabled or (free_x <= 0 and free_y <= 0):
        return CropWindow(w=w, h=h, keyframes=[(0.0, free_x // 2, free_y // 2)])

    duration = max(0.0, src_end - src_start)
    rate = max(1.0, cfg.reframe.keyframe_rate)
    steps = max(2, int(duration * rate) + 1)
    in_shot = [f for f in faces if src_start - 0.5 <= f.t <= src_end + 0.5]

    # Default framing: horizontally centred, biased above centre vertically so a
    # standing or seated subject keeps headroom instead of being cropped at the chin.
    default_cx = 0.5
    default_cy = 0.5 if free_y <= 0 else min(0.62, cfg.reframe.headroom + (h / src_h) * 0.25)

    keyframes: List[Tuple[float, int, int]] = []
    cur_x: Optional[float] = None
    cur_y: Optional[float] = None
    prev_t = 0.0
    alpha = max(0.01, min(1.0, cfg.reframe.smoothing))
    max_step_px = cfg.reframe.max_pan_speed / rate
    deadzone_px = cfg.reframe.deadzone * src_w

    for i in range(steps):
        local_src_t = src_start + min(duration, i / rate)
        cx, cy, confident = _subject_at(in_shot, local_src_t, default_cx, default_cy)

        target_x = cx * src_w - w / 2.0
        if confident:
            # Put the face at `headroom` down the crop, not dead centre.
            target_y = cy * src_h - h * cfg.reframe.headroom
        else:
            target_y = cy * src_h - h / 2.0

        target_x = _clamp(target_x, 0.0, float(free_x))
        target_y = _clamp(target_y, 0.0, float(free_y))

        if cur_x is None:
            cur_x, cur_y = target_x, target_y        # snap on the first frame of a shot
        else:
            if abs(target_x - cur_x) > deadzone_px:
                cur_x = _approach(cur_x, target_x, alpha, max_step_px, w)
            if abs(target_y - cur_y) > deadzone_px * 0.8:
                cur_y = _approach(cur_y, target_y, alpha * 0.7, max_step_px * 0.6, h)

        out_t = (i / rate) / max(speed, 0.01)
        x, y = int(round(_clamp(cur_x, 0, free_x))), int(round(_clamp(cur_y, 0, free_y)))
        if keyframes and keyframes[-1][1] == x and keyframes[-1][2] == y:
            continue                                  # skip redundant sendcmd lines
        keyframes.append((round(out_t, 3), x, y))
        prev_t = out_t

    if not keyframes:
        keyframes = [(0.0, free_x // 2, free_y // 2)]
    return CropWindow(w=w, h=h, keyframes=keyframes)


def _subject_at(faces: Sequence[FaceSample], t: float, default_cx: float,
                default_cy: float) -> Tuple[float, float, bool]:
    """Nearest confident detection within 0.6s, else the default framing."""
    best: Optional[FaceSample] = None
    best_dt = 1e9
    for f in faces:
        dt = abs(f.t - t)
        if dt < best_dt and f.score > 0.5:
            best, best_dt = f, dt
    if best is not None and best_dt <= 0.6:
        return best.cx, best.cy, True

    loose: Optional[FaceSample] = None
    loose_dt = 1e9
    for f in faces:
        dt = abs(f.t - t)
        if dt < loose_dt and f.score > 0.0:
            loose, loose_dt = f, dt
    if loose is not None and loose_dt <= 1.0:
        # Motion centroid: useful horizontally, too noisy to trust vertically.
        return 0.5 * loose.cx + 0.5 * default_cx, default_cy, False
    return default_cx, default_cy, False


# How far off-centre the subject has to drift before the camera stops being
# polite about it, as a fraction of the crop's own size.
_CATCHUP_SPAN = 0.22
_CATCHUP_GAIN = 4.0


def _approach(current: float, target: float, alpha: float, max_step: float,
              extent: float) -> float:
    """Move toward the target, faster the further behind we are.

    A single smoothing constant cannot serve both jobs: low enough to keep the
    camera calm while someone gestures, and it lags a metre behind when they
    actually cross the room.  Scaling the rate with the error gives a camera
    that ignores fidget and still keeps up with real movement.
    """
    error = target - current
    urgency = min(1.0, abs(error) / max(extent * _CATCHUP_SPAN, 1.0))
    effective = min(1.0, alpha * (1.0 + _CATCHUP_GAIN * urgency * urgency))
    delta = error * effective
    return current + _clamp(delta, -max_step, max_step)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sendcmd_script(crop: CropWindow, target: str = "crop") -> str:
    """Render a crop path as an ffmpeg sendcmd script.

    ``target`` must be the crop filter's *instance* name (``crop@s3``), never
    the bare class name.  ffmpeg dispatches a sendcmd command to every filter
    whose class name matches, so with one clip cut into several shots a bare
    ``crop`` target drives all of them at once and every shot after the first
    is framed by some other shot's tracking data.
    """
    lines = []
    for t, x, y in crop.keyframes:
        lines.append(f"{max(0.0, t):.3f} {target} x {x}, {target} y {y};")
    return "\n".join(lines) + "\n"
