"""Royalty-free stand-ins, generated on first use, so every advertised feature works without any files:
a lo-fi music bed, a whoosh for cuts, and animated "satisfying" b-roll for the split screen.
Anything you put in assets/music, assets/sfx or assets/broll is used instead."""
from __future__ import annotations

from pathlib import Path

from ..media import run_ffmpeg

BAR = 60 / 84 * 4   # 84 BPM, 4/4
BEAT = BAR / 4


def _pick(values: tuple) -> str:
    k = f"mod(floor(t/{BAR:.5f})\\,4)"
    a, b, c, d = values
    return f"if(eq({k}\\,0)\\,{a}\\,if(eq({k}\\,1)\\,{b}\\,if(eq({k}\\,2)\\,{c}\\,{d})))"


def make_music(path: Path) -> Path:
    """Am-F-C-G lo-fi loop: warm detuned pad, bass, kick, snare and hats (~34 s)."""
    loop = BAR * 12
    root, third, fifth = _pick((220, 174.61, 261.63, 196)), _pick((261.63, 220, 329.63, 246.94)), \
        _pick((329.63, 261.63, 392, 293.66))
    bass = _pick((110, 87.31, 130.81, 98))
    tb, tq, tk = f"mod(t\\,{BAR:.5f})", f"mod(t\\,{BEAT:.5f})", f"mod(t\\,{2 * BEAT:.5f})"
    ts, th = f"mod(t-{BEAT:.5f}\\,{2 * BEAT:.5f})", f"mod(t\\,{BEAT / 2:.5f})"
    env = f"(1-exp(-{tb}*5))*(0.55+0.45*exp(-{tb}*1.3))"

    def voice(f: str) -> str:
        return f"(sin(2*PI*{f}*t)+0.6*sin(2*PI*{f}*1.004*t)+0.25*sin(4*PI*{f}*t))"

    tracks = {
        "p": (f"0.09*{env}*({voice(root)}+{voice(third)}+{voice(fifth)})", "lowpass=f=2400,aecho=0.8:0.6:120:0.25"),
        "b": (f"0.22*sin(2*PI*{bass}*t)*(1-exp(-{tq}*40))*exp(-{tq}*2.2)", "lowpass=f=400"),
        "k": (f"0.7*sin(2*PI*(48*{tk}+2.3*(1-exp(-{tk}*35))))*exp(-{tk}*9)", "anull"),
        "s": (f"0.28*(random(0)*2-1)*exp(-{ts}*16)", "bandpass=f=1800:w=1400,volume=1.3"),
        "h": (f"0.10*(random(1)*2-1)*exp(-{th}*70)", "highpass=f=7000"),
    }
    graph = ";".join(f"aevalsrc=exprs='{expr}':s=44100:d={loop:.3f},{fx}[{name}]"
                     for name, (expr, fx) in tracks.items())
    graph += ";[p][b][k][s][h]amix=inputs=5:normalize=0,alimiter=limit=0.9,aformat=channel_layouts=stereo[out]"
    run_ffmpeg(["-filter_complex", graph, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "160k", str(path)])
    return path


def make_whoosh(path: Path) -> Path:
    run_ffmpeg(["-f", "lavfi", "-i", "anoisesrc=d=0.55:c=pink:a=0.9:r=44100",
                "-af", "highpass=f=450,lowpass=f=6500,aphaser=in_gain=0.6:out_gain=0.9:delay=2.5:decay=0.6:speed=1.8,"
                       "afade=t=in:d=0.28:curve=exp,afade=t=out:st=0.28:d=0.27:curve=exp,volume=6.3,"
                       "aformat=channel_layouts=stereo", str(path)])
    return path


def make_pop(path: Path) -> Path:
    """A soft, rounded 'bloop' for key words (pitch drops 1100 -> 500 Hz in 90 ms)."""
    run_ffmpeg(["-f", "lavfi", "-i", "aevalsrc=exprs='0.8*sin(2*PI*(500*t+3300*(1-exp(-t*18))/18))"
                "*(1-exp(-t*900))*exp(-t*38)':s=44100:d=0.16",
                "-af", "lowpass=f=5000,volume=1.6,aformat=channel_layouts=stereo", str(path)])
    return path


def make_hit(path: Path) -> Path:
    """A cinematic sub-bass hit for the biggest moment (sine drop 70 -> 38 Hz plus a short click)."""
    run_ffmpeg(["-f", "lavfi", "-i", "aevalsrc=exprs='0.95*sin(2*PI*(38*t+32*(1-exp(-t*9))/9))*exp(-t*3.2)"
                "+0.35*(random(0)*2-1)*exp(-t*160)':s=44100:d=1.1",
                "-af", "lowpass=f=2200,alimiter=limit=0.95,aformat=channel_layouts=stereo", str(path)])
    return path


def make_broll(path: Path, seconds: int = 30) -> Path:
    """Colour-shifting gradients with drifting cell patterns - hypnotic, never distracting."""
    run_ffmpeg(["-f", "lavfi", "-i", f"gradients=s=540x480:type=spiral:speed=0.02:c0=0xff2d95:c1=0x7a00ff:"
                f"c2=0x00e5ff:c3=0x00ff9c:c4=0xffe600:n=5:r=30:d={seconds}:seed=3",
                "-f", "lavfi", "-i", f"gradients=s=540x480:type=radial:speed=0.035:c0=0x000000:c1=0xffffff:"
                f"c2=0x000000:c3=0xffffff:n=4:r=30:d={seconds}:seed=9",
                "-f", "lavfi", "-i", "life=s=135x120:mold=10:r=30:ratio=0.12:death_color=#101030:"
                "life_color=#00ffd0:mold_color=#ff2d95,scale=540:480:flags=neighbor",
                "-filter_complex", "[0][1]blend=all_mode=softlight[a];"
                "[a][2]blend=all_mode=screen:all_opacity=0.5,hue=h=t*12,eq=saturation=1.05:brightness=-0.12:contrast=0.95",
                "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(path)])
    return path


MAKERS = {"music": ("lofi_beat.mp3", make_music), "sfx": ("whoosh.wav", make_whoosh),
          "pop": ("pop.wav", make_pop), "hit": ("hit.wav", make_hit),
          "broll": ("satisfying_loop_v2.mp4", make_broll)}


def builtin(kind: str, cache_dir: Path) -> Path | None:
    """The built-in asset of this kind, generating it the first time. None if generation fails."""
    name, make = MAKERS[kind]
    path = cache_dir / name
    if path.exists() and path.stat().st_size > 1000:
        return path
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / f"tmp_{name}"
    try:
        make(tmp)
        tmp.replace(path)
        return path
    except Exception:
        tmp.unlink(missing_ok=True)
        return None
