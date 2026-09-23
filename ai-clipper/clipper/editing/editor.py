"""Render a finished vertical clip for TikTok / Instagram Reels with ffmpeg.

Pass A: cut the moment out of the long video, removing pauses/fillers (jump cuts)
        and applying the speed-up.
Pass B: reframe to 9:16 following the speaker, add captions, hook, zooms,
        colour, progress bar, flashes, b-roll split, music ducking, sfx, loudness.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..media import extract_frame, probe, run_ffmpeg
from .builtin_assets import builtin
from .captions import build_ass
from .fonts import size_scale
from .safety import censor
from .levels import Preset
from .reframe import Track, analyze_faces, plan_track, x_expression
from .timeline import cut_points, keep_ranges, output_duration, remap, remap_words

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
BUNDLED_FONTS = Path(__file__).parent / "fonts"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}


@dataclass
class RenderJob:
    source: Path
    start: float
    end: float
    words: list[dict]           # source-timeline words of the clip
    hook: str
    emphasis: list[str]
    out_dir: Path
    name: str
    highlights: list[float] = field(default_factory=list)  # source times of the biggest moments


def _pick(folder: Path, exts: set[str], seed: str) -> Path | None:
    if not folder.exists():
        return None
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in exts)
    return random.Random(seed).choice(files) if files else None


def _esc(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option."""
    return "'" + str(path).replace("\\", "/").replace("'", r"'\''").replace(":", r"\:") + "'"


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def cut_pass(job: RenderJob, preset: Preset, work: Path) -> tuple[Path, list, float]:
    ranges = keep_ranges(job.words, job.start, job.end, preset.max_pause, preset.remove_fillers)
    seek = max(0.0, job.start - 2.0)
    rel = [(a - seek, b - seek) for a, b in ranges]
    parts, labels = [], []
    for i, (a, b) in enumerate(rel):
        d = b - a
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.012,afade=t=out:st={max(0.0, d - 0.012):.3f}:d=0.012[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    speed = preset.speed
    v_speed = f",setpts=PTS/{speed}" if speed != 1.0 else ""
    a_speed = f",atempo={speed}" if speed != 1.0 else ""
    parts.append(f"{''.join(labels)}concat=n={len(rel)}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]fps=30{v_speed}[vout]")
    parts.append(f"[ac]aresample=48000{a_speed}[aout]")
    out = work / "cut.mkv"
    run_ffmpeg(["-ss", f"{seek:.3f}", "-t", f"{job.end - seek + 1.0:.3f}", "-i", str(job.source),
                "-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
                "-c:a", "pcm_s16le", str(out)])
    return out, ranges, output_duration(ranges, speed)


def zoom_events(words: list[dict], emphasis: set[str], cuts: list[float], preset: Preset,
                duration: float) -> list[tuple[float, float, float]]:
    """(start, end, extra zoom) punch-ins."""
    events: list[tuple[float, float, float]] = []
    if preset.zoom_punch:
        for w in words:
            if _norm(w["w"]) in emphasis and w["s"] > 0.5:
                events.append((w["s"], min(duration, w["s"] + 0.9), 0.14))
    if preset.pattern_zoom:
        # alternate framing at sentence starts / jump cuts (hides cuts, resets attention)
        anchors = sorted(set([w["s"] for i, w in enumerate(words[1:], 1)
                              if re.search(r"[.!?]$", words[i - 1]["w"])] + cuts))
        tight = False
        for a, b in zip(anchors, anchors[1:] + [duration]):
            tight = not tight
            if tight and b - a > 0.6:
                events.append((a, b, 0.07))
    events.sort()
    return events[:40]


def zoom_expr(events: list[tuple[float, float, float]], slow_push: bool, duration: float, fps: int) -> str:
    t = f"(on/{fps})"
    expr = "1"
    if slow_push:
        expr += f"+0.05*{t}/{max(duration, 1):.2f}"
    for a, b, z in events:
        expr += f"+{z}*between({t},{a:.3f},{b:.3f})"
    return expr


def render(job: RenderJob, preset: Preset, cfg) -> dict:
    e = cfg["editing"]
    W, H, fps = e["width"], e["height"], e["fps"]
    work = job.out_dir / f".work_{job.name}"
    work.mkdir(parents=True, exist_ok=True)

    cut_file, ranges, duration = cut_pass(job, preset, work)
    words = remap_words(job.words, ranges, preset.speed)
    safe = e.get("censor_profanity", True)
    if safe:
        words = [{**w, "w": censor(w["w"])} for w in words]
    cuts = cut_points(ranges, preset.speed)
    info = probe(cut_file)
    in_w, in_h = info["width"], info["height"]

    # ---------------------------------------------------------------- inputs
    def asset(key: str) -> Path:
        return cfg.path(f"editing.{key}")

    inputs: list[str] = ["-i", str(cut_file)]
    broll = _pick(asset("broll_dir"), VIDEO_EXT, job.name) if preset.broll_split else None
    music = _pick(asset("music_dir"), AUDIO_EXT, job.name) if preset.music else None
    sfx = _pick(asset("sfx_dir"), AUDIO_EXT, job.name) if preset.sfx else None
    if e.get("builtin_assets", True):  # your own files win; otherwise the built-in ones keep every feature on
        cache = cfg.path("paths.work_dir").parent / "builtin_assets"
        broll = broll or (builtin("broll", cache) if preset.broll_split else None)
        music = music or (builtin("music", cache) if preset.music else None)
        sfx = sfx or (builtin("sfx", cache) if preset.sfx else None)
    idx = 1
    broll_i = music_i = sfx_i = None
    if broll:
        inputs += ["-stream_loop", "-1", "-i", str(broll)]
        broll_i, idx = idx, idx + 1
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_i, idx = idx, idx + 1
    if sfx and cuts:
        inputs += ["-i", str(sfx)]
        sfx_i, idx = idx, idx + 1

    # ---------------------------------------------------------------- video
    g: list[str] = []
    vertical_source = in_h > in_w
    if broll_i is not None and not vertical_source:
        top_h = H // 2
        track = plan_track(*analyze_faces(str(cut_file)), mode=preset.reframe)
        crop_w = min(in_w, int(round(in_h * W / top_h / 2)) * 2)
        x = x_expression(track if track.layout == "crop" else Track("crop", [(0.0, 0.5)]),
                         str(in_w), str(crop_w))
        g.append(f"[0:v]crop=w={crop_w}:h={in_h}:x='{x}':y=0,scale={W}:{top_h}:flags=lanczos,setsar=1[top]")
        g.append(f"[{broll_i}:v]scale={W}:{H - top_h}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H - top_h},setsar=1,fps={fps}[bot]")
        g.append("[top][bot]vstack=inputs=2[base]")
        caption_y, layout = top_h + 170, "split"
    elif vertical_source:
        g.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[base]")
        caption_y, layout = 1380, "vertical"
    else:
        track = plan_track(*analyze_faces(str(cut_file)), mode=preset.reframe) \
            if preset.reframe != "center" else Track("crop", [(0.0, 0.5)])
        if track.layout == "fit":
            g.append(f"[0:v]split[bgs][fgs];[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},boxblur=24:3,eq=brightness=-0.10:saturation=1.2[bg];"
                     f"[fgs]scale={W}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-120,setsar=1[base]")
            caption_y, layout = 1420, "fit"
        else:
            crop_w = min(in_w, int(round(in_h * W / H / 2)) * 2)
            x = x_expression(track, str(in_w), str(crop_w))
            g.append(f"[0:v]crop=w={crop_w}:h={in_h}:x='{x}':y=0,scale={W}:{H}:flags=lanczos,setsar=1[base]")
            caption_y, layout = 1380, "crop"

    chain = "[base]"
    emphasis = {_norm(w) for phrase in job.emphasis for w in phrase.split()}
    zooms = zoom_events(words, emphasis, cuts, preset, duration)
    if zooms or preset.slow_push:
        g.append(f"{chain}zoompan=z='{zoom_expr(zooms, preset.slow_push, duration, fps)}':"
                 f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}[zoomed]")
        chain = "[zoomed]"
    if preset.color_grade:
        g.append(f"{chain}eq=contrast=1.07:saturation=1.18:brightness=0.012,"
                 f"unsharp=5:5:0.55:5:5:0.0[graded]")
        chain = "[graded]"
    flashes = [t for t in (remap(h, ranges, preset.speed) for h in job.highlights) if t is not None and t > 0.3]
    if preset.flash and flashes:
        enable = "+".join(f"between(t,{p:.3f},{p + 0.09:.3f})" for p in flashes[:4])
        g.append(f"{chain}drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='{enable}'[flashed]")
        chain = "[flashed]"

    # the bundled caption font + any fonts you add, so captions look the same on every PC
    fonts_work = work / "fonts"
    fonts_work.mkdir(exist_ok=True)
    user_fonts = asset("fonts_dir")
    font_files = [*BUNDLED_FONTS.glob("*.ttf"), *(user_fonts.glob("*.[ot]tf") if user_fonts.exists() else [])]
    for f in font_files:
        shutil.copy(f, fonts_work / f.name)
    ass = build_ass(words, preset.captions, preset.caption_words, preset.uppercase, e["font"],
                    e["accent_color"], e["highlight_color"], emphasis, duration,
                    hook=(censor(job.hook) if safe else job.hook) if preset.hook_overlay else None,
                    caption_y=caption_y,
                    size_scale=size_scale(e["font"], font_files))
    ass_path = work / "captions.ass"
    ass_path.write_text(ass, encoding="utf-8")
    g.append(f"{chain}subtitles=filename={_esc(ass_path)}:fontsdir={_esc(fonts_work)}[subbed]")
    chain = "[subbed]"

    if preset.progress_bar:
        bar_h = 12
        g.append(f"color=c={e['accent_color'].replace('#', '0x')}:s={W}x{bar_h}:r={fps}:d={duration + 1:.3f}[bar]")
        g.append(f"{chain}[bar]overlay=x='-w+w*t/{duration:.3f}':y=H-{bar_h}:shortest=1[barred]")
        chain = "[barred]"
    g.append(f"{chain}format=yuv420p[vout]")

    # ---------------------------------------------------------------- audio
    g.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo[speech]")
    a_chain = "[speech]"
    if sfx_i is not None:
        hits = cuts[:6]
        g.append(f"[{sfx_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.45,asplit={len(hits)}" + "".join(f"[sx{i}]" for i in range(len(hits))))
        mixes = []
        for i, t in enumerate(hits):
            ms = int(max(0.0, t - 0.15) * 1000)
            g.append(f"[sx{i}]adelay={ms}|{ms}[sd{i}]")
            mixes.append(f"[sd{i}]")
        g.append(f"{a_chain}{''.join(mixes)}amix=inputs={len(hits) + 1}:duration=first:normalize=0[withsfx]")
        a_chain = "[withsfx]"
    if music_i is not None:
        g.append(f"{a_chain}asplit[sp1][sp2]")
        # any track is first brought to a background level, then dipped ~6 dB under speech (not muted)
        g.append(f"[{music_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-27:TP=-8:LRA=7,"
                 f"aresample=48000,afade=t=in:d=1.0,afade=t=out:st={max(0.0, duration - 1.5):.3f}:d=1.5[mus]")
        g.append("[mus][sp2]sidechaincompress=threshold=0.08:ratio=4:attack=20:release=400[duck]")
        g.append("[sp1][duck]amix=inputs=2:duration=first:normalize=0[withmusic]")
        a_chain = "[withmusic]"
    audio_filters = ["loudnorm=I=-14:TP=-1.5:LRA=11"] if preset.loudnorm else []
    g.append(f"{a_chain}{','.join(audio_filters + ['aresample=48000'])}[aout]")

    out = job.out_dir / f"{job.name}.mp4"
    partial = work / "render.mp4"  # moved into place only once complete
    run_ffmpeg([*inputs, "-filter_complex", ";".join(g), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{duration:.3f}", "-r", str(fps),
                "-c:v", "libx264", "-preset", preset.x264_preset, "-crf", str(preset.crf),
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(partial)])
    os.replace(partial, out)

    thumb = extract_frame(out, min(1.2, duration / 2), job.out_dir / f"{job.name}.jpg", width=W)
    shutil.rmtree(work, ignore_errors=True)
    return {"video": out.name, "thumbnail": thumb.name, "duration": round(duration, 2),
            "layout": layout, "jump_cuts": len(ranges) - 1, "zooms": len(zooms),
            "music": music.name if music else None, "broll": broll.name if broll else None}


def write_post_files(out_dir: Path, name: str, clip: dict, render_info: dict, source: dict,
                     safe: bool = True) -> None:
    tags = " ".join("#" + t for t in clip.get("hashtags", []))
    caption = f"{clip.get('caption') or clip.get('title', '')}\n\n{tags}".strip()
    if safe:
        caption = censor(caption)
        clip = {**clip, "title": censor(clip.get("title", "")), "hook": censor(clip.get("hook", ""))}
    link = source.get("webpage_url") or (f"youtube.com/watch?v={source['id']}"
                                          if re.fullmatch(r"[\w-]{11}", str(source.get("id") or "")) else "")
    name = source.get("channel") or (source.get("title", "") if link else "")
    credit = f"\n\nCredit: {' - '.join(x for x in (name, link) if x)}" if (name or link) else ""  # none for your own files
    (out_dir / f"{name}.txt").write_text(caption + credit + "\n", encoding="utf-8")
    meta = {"name": name, **render_info, "clip": clip, "post_caption": caption + credit,
            "source": {"id": source.get("id"), "title": source.get("title"), "channel": source.get("channel")}}
    (out_dir / f"{name}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
