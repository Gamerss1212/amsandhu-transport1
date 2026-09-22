"""Execute an EditPlan with ffmpeg.

Two passes, deliberately:

1. Extract just the clip's source range to a high-quality intermediate.  A
   filter-graph ``trim`` on the original would force ffmpeg to decode the whole
   hour-long source - input seeking here turns that into seconds.
2. Build the real graph on that small file: cut into shots, reframe each shot,
   concat, grade, burn captions, mix audio, encode.

Everything in pass 2 lives in one filter graph so the frames are only touched
once.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import Config
from ..models import EditPlan, Shot, SourceVideo
from ..utils import escape_filter_path, info, progress_bar, run_ffmpeg, warn
from ..utils.ffmpeg import ffprobe_media
from .captions import build_ass, build_srt
from .reframe import sendcmd_script


class RenderError(RuntimeError):
    pass


def render_clip(plan: EditPlan, source: SourceVideo, cfg: Config, work_dir: str,
                out_path: str) -> str:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    intermediate = work / f"clip{plan.clip_index:02d}_src.mp4"
    offset = _extract_range(plan, source, cfg, intermediate)

    ass_path = work / f"clip{plan.clip_index:02d}.ass"
    ass_path.write_text(
        build_ass(plan.caption_words, cfg, plan.out_duration, plan.hook_text,
                  plan.hook_duration, cfg.render.watermark_text),
        encoding="utf-8",
    )

    cmd_files = _write_sendcmd_files(plan, work)
    media = ffprobe_media(str(intermediate))
    graph, inputs, maps = _build_graph(plan, cfg, media, offset, intermediate,
                                       ass_path, cmd_files)

    args: List[str] = []
    for spec in inputs:
        args += spec
    args += ["-filter_complex", graph]
    for m in maps:
        args += ["-map", m]
    args += _encode_args(cfg) + [out_path]

    with progress_bar(f"rendering clip {plan.clip_index}", total=1.0) as update:
        run_ffmpeg(args, on_progress=update, total_duration=plan.out_duration)

    if not Path(out_path).is_file() or Path(out_path).stat().st_size == 0:
        raise RenderError(f"ffmpeg produced no output for clip {plan.clip_index}.")

    if not cfg.output.keep_intermediates:
        for p in (intermediate, *cmd_files.values()):
            p.unlink(missing_ok=True)
    return out_path


def write_srt(plan: EditPlan, path: str) -> str:
    Path(path).write_text(build_srt(plan.caption_words), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Pass 1
# --------------------------------------------------------------------------- #


def _extract_range(plan: EditPlan, source: SourceVideo, cfg: Config,
                   dst: Path) -> float:
    """Cut [clip_start, clip_end] out of the source.  Returns the time offset."""
    start = min(s.src_start for s in plan.shots) if plan.shots else plan.candidate.start
    end = max(s.src_end for s in plan.shots) if plan.shots else plan.candidate.end
    start = max(0.0, start)
    end = min(source.duration or end, end)
    duration = max(0.5, end - start)

    args = [
        "-ss", f"{start:.3f}", "-i", source.path, "-t", f"{duration:.3f}",
        "-map", "0:v:0",
    ]
    media = ffprobe_media(source.path)
    if media["has_audio"]:
        args += ["-map", "0:a:0"]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.render.intermediate_crf),
        "-pix_fmt", "yuv420p", "-g", "48", "-an" if not media["has_audio"] else "-c:a",
    ]
    if media["has_audio"]:
        args += ["pcm_s16le", "-ar", "48000"]
    args += ["-movflags", "+faststart", str(dst)]
    run_ffmpeg(args)
    return start


def crop_target(index: int) -> str:
    """Per-shot filter instance name - see sendcmd_script's docstring."""
    return f"crop@s{index}"


def _write_sendcmd_files(plan: EditPlan, work: Path) -> dict:
    files = {}
    for i, shot in enumerate(plan.shots):
        if not shot.crop or len(shot.crop.keyframes) <= 1:
            continue
        path = work / f"clip{plan.clip_index:02d}_shot{i:03d}.cmd"
        path.write_text(sendcmd_script(shot.crop, crop_target(i)), encoding="utf-8")
        files[i] = path
    return files


# --------------------------------------------------------------------------- #
# Pass 2 - the filter graph
# --------------------------------------------------------------------------- #


def _build_graph(plan: EditPlan, cfg: Config, media: dict, offset: float,
                 intermediate: Path, ass_path: Path,
                 cmd_files: dict) -> Tuple[str, List[List[str]], List[str]]:
    render = cfg.render
    has_audio = media["has_audio"]
    inputs: List[List[str]] = [["-i", str(intermediate)]]
    parts: List[str] = []
    v_labels: List[str] = []
    a_labels: List[str] = []

    for i, shot in enumerate(plan.shots):
        a = max(0.0, shot.src_start - offset)
        b = max(a + 0.04, shot.src_end - offset)
        vlabel, alabel = f"v{i}", f"a{i}"

        chain = [f"trim={a:.3f}:{b:.3f}", "setpts=PTS-STARTPTS"]
        if shot.speed != 1.0:
            chain[-1] = f"setpts=(PTS-STARTPTS)/{shot.speed:.4f}"
        if i in cmd_files:
            chain.append(f"sendcmd=f='{escape_filter_path(str(cmd_files[i]))}'")
        if shot.crop:
            x0, y0 = (shot.crop.keyframes[0][1], shot.crop.keyframes[0][2]) \
                if shot.crop.keyframes else (0, 0)
            chain.append(f"{crop_target(i)}=w={shot.crop.w}:h={shot.crop.h}"
                         f":x={x0}:y={y0}:exact=1")
        chain.append(f"scale={render.width}:{render.height}:flags=lanczos:force_original_aspect_ratio=disable")
        chain.append("setsar=1")
        chain.append(f"fps={render.fps}")
        parts.append(f"[0:v]{','.join(chain)}[{vlabel}]")
        v_labels.append(vlabel)

        if has_audio:
            achain = [f"atrim={a:.3f}:{b:.3f}", "asetpts=PTS-STARTPTS"]
            if shot.speed != 1.0:
                achain.append(f"atempo={max(0.5, min(2.0, shot.speed)):.4f}")
            achain.append("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")
            parts.append(f"[0:a]{','.join(achain)}[{alabel}]")
            a_labels.append(alabel)

    # -- concat --------------------------------------------------------- #
    n = len(v_labels)
    if n == 0:
        raise RenderError("The edit plan has no shots to render.")
    if has_audio:
        pairs = "".join(f"[{v}][{a}]" for v, a in zip(v_labels, a_labels))
        parts.append(f"{pairs}concat=n={n}:v=1:a=1[vcat][acat]")
    else:
        parts.append("".join(f"[{v}]" for v in v_labels) + f"concat=n={n}:v=1:a=0[vcat]")

    # -- video finish ---------------------------------------------------- #
    vchain: List[str] = []
    if render.grade:
        # A light lift only: short-form is watched on phones at full brightness,
        # and anything heavier starts to look like a filter.
        vchain.append("eq=contrast=1.05:saturation=1.10:gamma=1.02")
        vchain.append("unsharp=5:5:0.35:5:5:0.0")
    vchain.append(f"subtitles='{escape_filter_path(str(ass_path))}'"
                  f":fontsdir='{escape_filter_path(_fonts_dir())}'")

    if render.progress_bar:
        # drawbox evaluates its width once, not per frame, so a `t`-driven
        # expression there renders as a permanently full bar.  Sliding a
        # full-width bar in from the left with overlay (which does evaluate
        # per frame) gives a bar that actually fills.
        colour = render.progress_bar_color
        bar_h = max(2, render.progress_bar_height)
        span = max(plan.out_duration, 0.1)
        parts.append(f"[vcat]{','.join(vchain)}[vbody]")
        parts.append(f"color=c=0x{colour}@0.92:s={render.width}x{bar_h}:"
                     f"d={span + 1:.3f}:r={render.fps},format=yuva420p[bar]")
        parts.append(
            f"[vbody][bar]overlay=x='-w+w*min(1\\,t/{span:.3f})':y=H-h:"
            f"eval=frame:format=auto,format=yuv420p[vout]")
    else:
        vchain.append("format=yuv420p")
        parts.append(f"[vcat]{','.join(vchain)}[vout]")
    maps = ["[vout]"]

    # -- audio finish ---------------------------------------------------- #
    if has_audio:
        voice_chain = [f"highpass=f={cfg.audio.highpass_hz}",
                       "acompressor=threshold=0.09:ratio=3:attack=8:release=180:makeup=1.6"]
        parts.append(f"[acat]{','.join(voice_chain)}[voice]")
        music = plan.music if plan.music and Path(plan.music).is_file() else None
        if plan.music and not music:
            warn(f"Music bed not found at {plan.music} - rendering without it.")

        if music:
            inputs.append(["-stream_loop", "-1", "-i", music])
            fade_out = max(0.0, plan.out_duration - 1.2)
            parts.append(
                f"[1:a]atrim=0:{plan.out_duration:.3f},asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"volume={plan.music_gain_db:.1f}dB,"
                f"afade=t=in:st=0:d=0.6,afade=t=out:st={fade_out:.3f}:d=1.2[music]")
            # Duck the bed against the voice rather than riding a fixed level.
            parts.append("[voice]asplit=2[voice_main][voice_key]")
            parts.append(
                f"[music][voice_key]sidechaincompress=threshold=0.035:ratio=12:"
                f"attack=12:release=420:makeup=1[ducked]")
            parts.append("[voice_main][ducked]amix=inputs=2:duration=first:normalize=0[amixed]")
            src_label = "amixed"
        else:
            src_label = "voice"

        parts.append(
            f"[{src_label}]loudnorm=I={cfg.audio.loudness_target}:TP={cfg.audio.true_peak}:"
            f"LRA={cfg.audio.loudness_range},aresample=48000:first_pts=0,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")
        maps.append("[aout]")
    else:
        inputs.append(["-f", "lavfi", "-t", f"{plan.out_duration:.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
        maps.append("1:a")

    return ";".join(parts), inputs, maps


def _encode_args(cfg: Config) -> List[str]:
    render = cfg.render
    args = [
        "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf),
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-x264-params", "keyint=60:min-keyint=30:scenecut=0",
        "-r", str(render.fps),
        "-c:a", "aac", "-b:a", render.audio_bitrate, "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
    ]
    if render.threads:
        args += ["-threads", str(render.threads)]
    return args


def _fonts_dir() -> str:
    bundled = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
    if bundled.is_dir():
        return str(bundled)
    for candidate in ("/usr/share/fonts", "/System/Library/Fonts",
                      str(Path.home() / "Library/Fonts"), "C:/Windows/Fonts"):
        if Path(candidate).is_dir():
            return candidate
    return "."
