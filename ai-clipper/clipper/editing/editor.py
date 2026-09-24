"""Render a finished vertical clip for TikTok / Instagram Reels with ffmpeg.

Pass A: cut the moment out of the long video, removing pauses/fillers (jump cuts)
        and applying the speed-up.
Pass B: reframe to 9:16 following the speaker, add captions, hook, zooms,
        colour, progress bar, flashes, b-roll split, music ducking, sfx, loudness.
"""
from __future__ import annotations

import dataclasses
import functools
import json
import os
import random
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..media import extract_frame, ffmpeg_exe, probe, run_ffmpeg
from .builtin_assets import builtin
from .captions import build_ass, build_srt
from .fonts import size_scale
from .safety import censor
from .levels import Preset
from .review import review_and_fix
from .reframe import Track, analyze_faces, plan_track, speaker_box, x_expression
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


def detect_borders(source: Path, start: float, duration: float) -> str | None:
    """Black bars baked into the picture (4:3 shows in 16:9 files, letterboxed films) as an
    ffmpeg crop, so they never end up inside the vertical frame. None when there are none."""
    info = probe(source)
    w, h = info["width"], info["height"]
    if not w or not h:
        return None
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-ss", f"{max(0.0, start):.2f}", "-i", str(source),
                          "-t", f"{min(12.0, max(1.0, duration)):.2f}", "-vf", "fps=4,cropdetect=limit=24:round=2:reset=0",
                          "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err)
    if not found:
        return None
    cw, ch, cx, cy = map(int, found[-1])  # reset=0: the area that was ever non-black - never cuts picture
    if cw < w * 0.3 or ch < h * 0.3:  # a dark scene, not bars
        return None
    if w - cw < w * 0.04 and h - ch < h * 0.04:
        return None
    return f"crop={cw}:{ch}:{cx}:{cy}"


def vignette_image(folder: Path, W: int, H: int) -> Path | None:
    path = folder / f"vignette_{W}x{H}.png"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"tmp_vignette_{W}x{H}_{os.getpid()}_{id(path)}.png"
        try:
            run_ffmpeg(["-f", "lavfi", "-i", f"color=black:s={W}x{H},format=rgba,geq=r=0:g=0:b=0:"
                        f"a='255*0.55*pow(min(1\\,hypot((X-W/2)/(W/2)\\,(Y-H/2)/(H/2))/1.25)\\,2.2)'",
                        "-frames:v", "1", str(tmp)])
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            return None
    return path


def face_safe_caption_y(faces, in_w: int, in_h: int, H: int, zoom: float = 1.18) -> int:
    """Captions sit below the speaker's chin, never across the face: from the tracked faces, find how
    low the face reaches (allowing for punch-in zooms) and move the captions under it, staying inside
    the area TikTok / Reels leave clear (above ~1500 px)."""
    _, per_sample, _ = faces
    bottoms = []
    for fs in per_sample:
        if fs:
            cx, fw, *rest = max(fs, key=lambda f: f[1])
            cy = rest[0] if rest else 0.4
            bottoms.append(cy + 0.65 * fw * in_w / in_h)  # chin, from the face box
    if not bottoms:
        return 1380
    bottom = sorted(bottoms)[int(0.8 * (len(bottoms) - 1))]  # the lower positions the face reaches
    bottom = 0.5 + (bottom - 0.5) * zoom
    return int(min(1500, max(1380, bottom * H + 150)))


def fit_filters(src: str, out: str, W: int, H: int, pre: str = "") -> str:
    """The whole frame, sharp, over a blurred and darkened fill of itself (wide shots, b-roll, screens)."""
    tag = out[1:-1]
    return (f"{src}{pre.lstrip(',') + ',' if pre else ''}split[bg{tag}][fg{tag}];"
            f"[bg{tag}]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=6:2,scale={W}:{H},eq=brightness=-0.10:saturation=1.2[b{tag}];"
            f"[fg{tag}]scale={W}:-2:flags=lanczos[f{tag}];[b{tag}][f{tag}]overlay=(W-w)/2:(H-h)/2-120,setsar=1{out}")


def stack_filters(src: str, out: str, track: Track, in_w: int, in_h: int, W: int, H: int, accent: str,
                  pre: str = "") -> str:
    """Podcast two-shot: left speaker on top, right speaker below, a thin accent line between."""
    half = H // 2
    boxes = [speaker_box(sp, in_w, in_h, W / half) for sp in track.speakers]
    parts = [f"{src}split[spa{out[1:-1]}][spb{out[1:-1]}]"]
    for lbl, (bw, bh, bx, by) in zip(("spa", "spb"), boxes):
        parts.append(f"[{lbl}{out[1:-1]}]crop={bw}:{bh}:{bx}:{by}{pre},scale={W}:{half}:flags=lanczos,setsar=1"
                     f"[{lbl}o{out[1:-1]}]")
    parts.append(f"[spao{out[1:-1]}][spbo{out[1:-1]}]vstack=inputs=2,drawbox=x=0:y={half - 3}:w={W}:h=6:"
                 f"color={accent.replace('#', '0x')}@0.9:t=fill{out}")
    return ";".join(parts)


def cut_pass(job: RenderJob, preset: Preset, work: Path) -> tuple[Path, list, float]:
    ranges = keep_ranges(job.words, job.start, job.end, preset.max_pause, preset.remove_fillers)
    if output_duration(ranges) < min(job.end - job.start, max(5.0, 0.4 * (job.end - job.start))):
        ranges = [(job.start, job.end)]  # mostly silence / music: jump cuts would leave almost nothing
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
    borders = detect_borders(job.source, job.start, job.end - job.start)
    parts.append(f"[vc]{borders + ',' if borders else ''}fps=30{v_speed}[vout]")
    parts.append(f"[ac]aresample=48000{a_speed}[aout]")
    out = work / "cut.mkv"
    run_ffmpeg(["-ss", f"{seek:.3f}", "-t", f"{job.end - seek + 1.0:.3f}", "-i", str(job.source),
                "-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
                "-c:a", "pcm_s16le", str(out)])
    return out, ranges, output_duration(ranges, speed)


def zoom_events(words: list[dict], emphasis: set[str], cuts: list[float], preset: Preset,
                duration: float, reactions: list[float] | None = None) -> list[tuple[float, float, float]]:
    """(start, end, extra zoom) punch-ins."""
    events: list[tuple[float, float, float]] = []
    if preset.reaction_zoom:  # the laugh / biggest reaction: a slow, strong push-in on the face
        for t in (reactions or [])[:3]:
            events.append((max(0.0, t - 0.2), min(duration, t + 1.6), 0.18))
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


def _ramp(t: str, a: float, b: float, rise: float = 0.12, fall: float = 0.22) -> str:
    """0 -> 1 -> 0 envelope over [a, b] with eased edges (no hard jumps in framing)."""
    x = f"clip(min(({t}-{a:.3f})/{rise},({b:.3f}-{t})/{fall}),0,1)"
    return f"(0.5-0.5*cos(PI*{x}))"


def zoom_expr(events: list[tuple[float, float, float]], slow_push: bool, duration: float, fps: int) -> str:
    t = f"(on/{fps})"
    expr = "1"
    if slow_push:
        expr += f"+0.05*{t}/{max(duration, 1):.2f}"
    for a, b, z in events:
        expr += f"+{z}*{_ramp(t, a, b)}"
    return expr


def shake_expr(events: list[tuple[float, float, float]], fps: int, axis: int) -> str:
    """Short decaying camera shake at the start of every big punch-in."""
    t = f"(on/{fps})"
    terms = []
    for a, _, z in events:
        if z >= 0.1:
            freq = 47 if axis == 0 else 61
            terms.append(f"14*sin({freq}*{t})*clip(1-({t}-{a:.3f})/0.35,0,1)*between({t},{a:.3f},{a + 0.35:.3f})")
    return "+".join(terms[:20]) or "0"


def _render_pass(job: RenderJob, preset: Preset, cfg, encode: list[str]) -> dict:
    """One edit of the clip (cut, reframe, captions, effects, audio mix, export)."""
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
    cache = cfg.path("paths.work_dir").parent / "builtin_assets" if e.get("builtin_assets", True) else None
    if cache:  # your own files win; otherwise the built-in ones keep every feature on
        # no built-in b-roll: a generated background under a real speaker looks cheap, so the split
        # screen is used only with your own gameplay / b-roll (assets/broll); otherwise full-screen
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
    stack_windows: list[tuple[float, float]] = []  # times the single-speaker crop is covered by another view
    track_stack_windows: list[tuple[float, float]] = []
    overlays: list[tuple[str, list[tuple[float, float]]]] = []
    # old / low-res footage is blown up ~3x: clean the noise first, then restore edge detail
    low_res = min(in_w, in_h) < 700
    # detail is restored at the source resolution, before the upscale: same look, a fraction of the pixels
    clean = ",hqdn3d=2:1.5:5:4" if low_res else ""
    clean += ",unsharp=5:5:0.9:5:5:0.0" if low_res else ",unsharp=5:5:0.55:5:5:0.0" if preset.color_grade else ""
    vertical_source = in_h > in_w
    if broll_i is not None and not vertical_source:
        top_h = H // 2
        track = plan_track(*analyze_faces(str(cut_file)), mode=preset.reframe)
        crop_w = min(in_w, int(round(in_h * W / top_h / 2)) * 2)
        x = x_expression(track if track.layout == "crop" else Track("crop", [(0.0, 0.5)]),
                         str(in_w), str(crop_w))
        g.append(f"[0:v]crop=w={crop_w}:h={in_h}:x='{x}':y=0{clean},scale={W}:{top_h}:flags=lanczos,setsar=1[top]")
        g.append(f"[{broll_i}:v]scale={W}:{H - top_h}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H - top_h},setsar=1,fps={fps}[bot]")
        g.append("[top][bot]vstack=inputs=2[base]")
        caption_y, layout = top_h + 170, "split"
    elif vertical_source:
        g.append(f"[0:v]{clean.lstrip(',') + ',' if clean else ''}scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},setsar=1[base]")
        caption_y, layout = 1380, "vertical"
    else:
        faces = analyze_faces(str(cut_file)) if preset.reframe != "center" else None
        track = plan_track(*faces, mode=preset.reframe) if faces else Track("crop", [(0.0, 0.5)])
        if track.layout == "stack":
            g.append(stack_filters("[0:v]", "[base]", track, in_w, in_h, W, H, e["accent_color"], clean))
            caption_y, layout = H // 2 + 150, "stack"
        elif track.layout == "fit":
            g.append(fit_filters("[0:v]", "[base]", W, H, clean))
            caption_y, layout = 1420, "fit"
        else:
            crop_w = min(in_w, int(round(in_h * W / H / 2)) * 2)
            x = x_expression(track, str(in_w), str(crop_w))
            src = "[0:v]"
            # two-shots switch to the stacked view (framed per shot), shots with nobody in them to the full frame
            extra = [(f"stk{k}", [w], sp) for k, (w, sp) in
                     enumerate(zip(track.stack_windows, track.window_speakers or [track.speakers] * 9))]
            if track.fit_windows:
                extra.append(("fitv", track.fit_windows, None))
            if extra:
                g.append(f"[0:v]split={len(extra) + 1}[cmain]" + "".join(f"[c{lbl}]" for lbl, _, _ in extra))
                for lbl, wins, sp in extra:
                    if sp is not None:
                        g.append(stack_filters(f"[c{lbl}]", f"[{lbl}]", Track("stack", [], sp), in_w, in_h, W, H,
                                               e["accent_color"], clean))
                    else:
                        g.append(fit_filters(f"[c{lbl}]", f"[{lbl}]", W, H, clean))
                    overlays.append((lbl, wins))
                stack_windows = track.stack_windows + track.fit_windows
                track_stack_windows = track.stack_windows
                src = "[cmain]"
            g.append(f"{src}crop=w={crop_w}:h={in_h}:x='{x}':y=0{clean},scale={W}:{H}:flags=lanczos,setsar=1[base]")
            caption_y = face_safe_caption_y(faces, in_w, in_h, H) if faces else 1380
            layout = "+".join(["crop"] + (["stack"] if track.stack_windows else [])
                              + (["full-frame"] if track.fit_windows else []))

    chain = "[base]"
    emphasis = {_norm(w) for phrase in job.emphasis for w in phrase.split()}
    flashes = [t for t in (remap(h, ranges, preset.speed) for h in job.highlights) if t is not None and t > 0.3]
    zooms = zoom_events(words, emphasis, cuts, preset, duration, flashes) if layout != "stack" else []
    if stack_windows:  # punch-ins belong to the single-speaker shots, never to the stacked view
        zooms = [z for z in zooms if not any(a - 0.3 < z[0] < b for a, b in stack_windows)]
    if zooms or (preset.slow_push and layout != "stack"):
        sx = shake_expr(zooms, fps, 0) if preset.shake else "0"
        sy = shake_expr(zooms, fps, 1) if preset.shake else "0"
        g.append(f"{chain}zoompan=z='{zoom_expr(zooms, preset.slow_push, duration, fps)}':"
                 f"x='iw/2-(iw/zoom/2)+({sx})':y='ih/2-(ih/zoom/2)+({sy})':d=1:s={W}x{H}:fps={fps}[zoomed]")
        chain = "[zoomed]"
    for lbl, wins in overlays:
        on = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in wins)
        g.append(f"{chain}[{lbl}]overlay=0:0:enable='{on}'[over{lbl}]")
        chain = f"[over{lbl}]"
    if preset.color_grade:
        g.append(f"{chain}eq=contrast=1.07:saturation=1.18:brightness=0.012[graded]")
        chain = "[graded]"
    vig = vignette_image(cache or work, W, H) if preset.vignette else None
    if vig:  # a pre-rendered darkening mask: ~25x cheaper than computing the vignette every frame
        inputs += ["-i", str(vig)]
        g.append(f"{chain}[{idx}:v]overlay=0:0:format=auto[vig]")
        idx += 1
        chain = "[vig]"
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
    # the hook card sits at the top - unless the clip opens on the stacked two-speaker view, where the
    # top speaker's face is: then it goes in the gap between the two panels
    opens_stacked = layout == "stack" or any(a < 3.0 for a, _ in track_stack_windows)
    hook_y = H // 2 - 230 if opens_stacked else 250
    ass = build_ass(words, preset.captions, preset.caption_words, preset.uppercase, e["font"],
                    e["accent_color"], e["highlight_color"], emphasis, duration,
                    hook=(censor(job.hook) if safe else job.hook) if preset.hook_overlay else None,
                    caption_y=caption_y, hook_y=hook_y,
                    size_scale=size_scale(e["font"], font_files), emphasis_pop=preset.emphasis_pop,
                    mood=preset.mood)
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
    # rumble cut, noise reduction, compression, presence boost, then de-essing (harsh "s" sounds)
    voice = ("highpass=f=75,afftdn=nf=-25,acompressor=threshold=0.1:ratio=3:attack=5:release=120:makeup=2,"
             "equalizer=f=3200:t=q:w=1.2:g=2.5,deesser=i=0.35,") if preset.voice_enhance else ""
    g.append(f"[0:a]{voice}aformat=sample_rates=48000:channel_layouts=stereo[speech]")
    a_chain = "[speech]"
    def mix_at(input_i: int, times: list[float], volume: float, tag: str, lead: float = 0.0) -> None:
        """Lay one sound effect at each time (seconds) on top of the audio so far."""
        nonlocal a_chain
        g.append(f"[{input_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={volume},"
                 f"asplit={len(times)}" + "".join(f"[{tag}{i}]" for i in range(len(times))))
        for i, t in enumerate(times):
            ms = int(max(0.0, t - lead) * 1000)
            g.append(f"[{tag}{i}]adelay={ms}|{ms}[{tag}d{i}]")
        g.append(f"{a_chain}{''.join(f'[{tag}d{i}]' for i in range(len(times)))}"
                 f"amix=inputs={len(times) + 1}:duration=first:normalize=0[with{tag}]")
        a_chain = f"[with{tag}]"

    if sfx_i is not None:
        mix_at(sfx_i, cuts[:6], 0.45, "sx", lead=0.15)  # whoosh lands on the jump cut
    # sound design: a soft bloop on key words (never two within 1.5 s), a bass hit on the biggest moments
    pops, last = [], -9.0
    if preset.word_pops:
        for w in words:
            if _norm(w["w"]) in emphasis and w["s"] > 0.4 and w["s"] - last >= 1.5:
                pops.append(w["s"])
                last = w["s"]
    for kind, times, volume in (("pop", pops[:8], 0.3), ("hit", flashes[:2] if preset.impact_hits else [], 0.6)):
        path = builtin(kind, cache) if times and cache else None
        if path:
            inputs += ["-i", str(path)]
            mix_at(idx, times, volume, kind)
            idx += 1
    if music_i is not None:
        g.append(f"{a_chain}asplit[sp1][sp2]")
        # any track is first brought to a background level, then dipped ~6 dB under speech (not muted)
        g.append(f"[{music_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-27:TP=-8:LRA=7,"
                 f"aresample=48000,afade=t=in:d=1.0,afade=t=out:st={max(0.0, duration - 1.5):.3f}:d=1.5[mus]")
        g.append("[mus][sp2]sidechaincompress=threshold=0.08:ratio=4:attack=20:release=400[duck]")
        g.append("[sp1][duck]amix=inputs=2:duration=first:normalize=0[withmusic]")
        a_chain = "[withmusic]"
    # platform loudness, then a limiter so nothing ever clips on a phone speaker
    audio_filters = (["loudnorm=I=-14:TP=-1.5:LRA=11"] if preset.loudnorm else []) + \
        ["alimiter=limit=0.89:level=false"]
    g.append(f"{a_chain}{','.join(audio_filters + ['aresample=48000'])}[aout]")

    out = job.out_dir / f"{job.name}.mp4"
    partial = work / "render.mp4"  # moved into place only once complete
    run_ffmpeg([*inputs, "-filter_complex", ";".join(g), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{duration:.3f}", "-r", str(fps), *encode,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(partial)])
    os.replace(partial, out)

    (job.out_dir / f"{job.name}.srt").write_text(build_srt(words), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return {"video": out.name, "subtitles": f"{job.name}.srt", "duration": round(duration, 2),
            "layout": layout, "jump_cuts": len(ranges) - 1, "zooms": len(zooms),
            "music": music.name if music else None, "broll": broll.name if broll else None}


# ---------------------------------------------------------------- encoding
@functools.lru_cache(maxsize=1)
def _nvenc_works() -> bool:
    """An NVIDIA GPU encodes several times faster than the CPU; checked once with a tiny test encode."""
    try:
        proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                               "color=s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                              capture_output=True, timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


_gpu_failed = False


def encoder_args(preset: Preset, cfg) -> list[str]:
    use_gpu = cfg["editing"].get("gpu_encode", "auto")
    if use_gpu and use_gpu != "off" and not _gpu_failed and _nvenc_works():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(preset.crf + 2), "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    # x264 "veryfast" one quality step higher looks the same as slower presets (the platforms re-encode
    # every upload anyway) and exports ~2x faster; "speed" in the level only sets how slow it may go
    fast = {"slow": "medium", "medium": "fast", "fast": "veryfast"}.get(preset.x264_preset, preset.x264_preset)
    return ["-c:v", "libx264", "-preset", fast, "-crf", str(max(14, preset.crf - 1)),
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def safer(preset: Preset, step: int) -> Preset:
    """Fallbacks when an edit fails its review: first without motion effects, then a plain clean cut."""
    if step == 1:
        return dataclasses.replace(preset, zoom_punch=False, pattern_zoom=False, slow_push=False, shake=False,
                                   reaction_zoom=False, flash=False, word_pops=False, impact_hits=False)
    return dataclasses.replace(preset, reframe="center", broll_split=False, music=False, sfx=False,
                               zoom_punch=False, pattern_zoom=False, slow_push=False, shake=False,
                               reaction_zoom=False, flash=False, word_pops=False, impact_hits=False,
                               vignette=False, x264_preset="veryfast")


def render(job: RenderJob, preset: Preset, cfg, log=None) -> dict:
    """EDIT -> REVIEW -> FIX -> REVIEW AGAIN -> FINALIZE. Problems a patch can fix (loudness, black
    edges) are patched; anything else triggers a re-edit with safer settings. Never gives up on the
    first failure: a crashed or broken render is retried with a more compatible method."""
    global _gpu_failed
    e = cfg["editing"]
    W, H, fps = e["width"], e["height"], e["fps"]
    say = log or (lambda m: None)
    out, srt = job.out_dir / f"{job.name}.mp4", job.out_dir / f"{job.name}.srt"
    keep = [out.with_name(out.stem + ".keep.mp4"), srt.with_name(srt.stem + ".keep.srt")]
    history: list[str] = []
    last_exc: Exception | None = None
    best: dict | None = None
    attempt, step = 0, 0
    while attempt < 4 and step <= 2:
        attempt += 1
        p = preset if step == 0 else safer(preset, step)
        encode = encoder_args(p, cfg)
        try:
            info = _render_pass(job, p, cfg, encode)
        except Exception as exc:
            last_exc = exc
            if "nvenc" in " ".join(encode) and not _gpu_failed:
                _gpu_failed = True  # GPU encoder unusable here: same edit again on the CPU
                history.append("GPU encoder failed - switched to CPU encoding")
                continue
            reason = str(exc).strip().splitlines()[-1][:120] if str(exc).strip() else type(exc).__name__
            history.append(f"render failed ({reason}) - retrying with a more compatible edit")
            say(history[-1])
            step += 1
            continue
        fixed, retry, notes = review_and_fix(out, info["duration"], W, H, fps, srt, encode)
        info["duration"] = round(probe(out)["duration"], 2)
        history += [f"fixed {f.code}: {f.detail}" for f in fixed]
        result = {**info, "review": {"passes": attempt, "fixed": [f.code for f in fixed],
                                     "problems": [f"{f.code}: {f.detail}" for f in retry],
                                     "notes": [f"{f.code}: {f.detail}" for f in notes], "log": history}}
        if best is not None and len(retry) >= len(best["review"]["problems"]):
            # the safer edit didn't help, so the issue is in the footage itself: keep the better edit
            for k, f in zip(keep, (out, srt)):
                if k.exists():
                    k.replace(f)
            history.append("safer re-edit did not improve it - kept the original edit")
            break
        best = result
        if not retry:
            break
        for f, k in zip((out, srt), keep):  # keep this version in case a re-edit is not better
            if f.exists():
                shutil.copy2(f, k)
        history.append("review found " + "; ".join(f"{f.code} ({f.detail})" for f in retry) +
                       " - re-editing with safer settings")
        say(history[-1])
        step += 1
    for k in keep:
        k.unlink(missing_ok=True)
    if best is None:
        raise last_exc or RuntimeError("render failed")
    thumb = extract_frame(out, min(1.2, best["duration"] / 2), job.out_dir / f"{job.name}.jpg", width=W)
    best["thumbnail"] = thumb.name
    best["review"]["ok"] = not best["review"]["problems"]
    return best


def write_post_files(out_dir: Path, name: str, clip: dict, render_info: dict, source: dict,
                     safe: bool = True) -> None:
    tags = " ".join("#" + t for t in clip.get("hashtags", []))
    caption = f"{clip.get('caption') or clip.get('title', '')}\n\n{tags}".strip()
    if safe:
        caption = censor(caption)
        clip = {**clip, "title": censor(clip.get("title", "")), "hook": censor(clip.get("hook", ""))}
    link = source.get("webpage_url") or (f"youtube.com/watch?v={source['id']}"
                                          if re.fullmatch(r"[\w-]{11}", str(source.get("id") or "")) else "")
    who = source.get("channel") or (source.get("title", "") if link else "")
    credit = f"\n\nCredit: {' - '.join(x for x in (who, link) if x)}" if (who or link) else ""  # none for your own files
    (out_dir / f"{name}.txt").write_text(caption + credit + "\n", encoding="utf-8")
    meta = {"name": name, **render_info, "clip": clip, "post_caption": caption + credit,
            "source": {"id": source.get("id"), "title": source.get("title"), "channel": source.get("channel")}}
    (out_dir / f"{name}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
