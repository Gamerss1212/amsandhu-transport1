"""Stress backtest: render N clips from real and deliberately awkward videos and check every result.

Real videos (archive.org) go through the real analysis (download, whisper, judge) once and are cached;
edge cases (vertical / square / tiny sources, 24/60 fps, quiet / clipping / mono / silent audio,
profanity, unicode, subtitle-breaking characters, huge hooks, 10 s and 90 s clips...) are generated.
Every clip is checked for: size, fps, duration, audio loudness, black frames, captions (timing,
overlaps, text), subtitles file, post caption, thumbnail, and clean start / end of the moment.

usage: python tools/stress_backtest.py [N=100] [--out DIR] [--seed S] [--fast]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipper.analysis import analyze_video, clip_words  # noqa: E402
from clipper.analysis.transcribe import group_segments  # noqa: E402
from clipper.config import load_config  # noqa: E402
from clipper.editing import RenderJob, get_preset, render, write_post_files  # noqa: E402
from clipper.editing.auto import choose_level  # noqa: E402
from clipper.editing.levels import LEVEL_NAMES  # noqa: E402
from clipper.editing.safety import censor  # noqa: E402
from clipper.events import Reporter  # noqa: E402
from clipper.media import ffmpeg_exe, probe  # noqa: E402

REAL = [
    "https://archive.org/details/George_Soros_1998_60_Minutes_Interview",
    "https://archive.org/download/Norm_Macdonald_Live/Norm%20Macdonald%20Live%20-%20S01E02%20-%20Norm%20Macdonald%20with%20Guest%20Tom%20Green.mp4",
]

TEXT = ("So here is the thing nobody tells you about starting out. I was broke, I mean really broke. "
        "I had forty dollars in my account and my landlord was knocking on the door every single day. "
        "And then one phone call changed everything. My friend said come to Los Angeles right now. "
        "I sold my car that same night. Crazy right? But that's exactly why I'm sitting here today. "
        "You have to bet on yourself when nobody else will. That is the whole secret honestly. ")
EDGE = [
    dict(name="vertical", size="1080x1920"), dict(name="square", size="720x720"), dict(name="4x3", size="640x480"),
    dict(name="tiny", size="320x180"), dict(name="fps60", fps=60), dict(name="fps24", fps=24),
    dict(name="quiet", volume=0.01), dict(name="clipping", volume=40), dict(name="mono", mono=True),
    dict(name="no_audio_speech", silent=True),
    dict(name="profanity", text="What the fuck is this shit man. I swear this is the craziest bullshit ever. " * 4),
    dict(name="unicode", text="Café naïve résumé — “quoted” words ¿qué? 日本語 emoji 🔥🔥 end. " * 5),
    dict(name="ass_chars", text="Braces {\\an8} and \\N backslashes {weird} tags \\ here. " * 6),
    dict(name="huge_hook", hook=("This is the longest hook anyone has ever written for a short video clip and it "
                                 "keeps going and going because the model got carried away")),
    dict(name="empty_hook", hook=""), dict(name="short10", length=10), dict(name="long90", length=90),
    dict(name="gappy", gaps=True), dict(name="fillers", text="Um so uh I think um the uh thing is uh right. " * 8),
    dict(name="one_word", text="Wow. "),
]


def sh(args: list[str]) -> str:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", *args], capture_output=True, text=True).stderr


def synth_words(text: str, gaps: bool = False, rng: random.Random | None = None) -> list[dict]:
    words, t = [], 0.3
    for w in text.split():
        d = 0.18 + 0.03 * len(w)
        words.append({"w": w, "s": round(t, 3), "e": round(t + d, 3)})
        t += d + (rng.uniform(0.6, 2.5) if gaps and rng and rng.random() < 0.15 else 0.06)
        if w.endswith((".", "?", "!")):
            t += 0.25
    return words


def make_edge_video(case: dict, work: Path, rng: random.Random) -> tuple[Path, list[dict]]:
    words = synth_words(case.get("text", TEXT * 2), case.get("gaps", False), rng)
    dur = words[-1]["e"] + 1.5
    size, fps = case.get("size", "1280x720"), case.get("fps", 30)
    path = work / f"edge_{case['name']}.mp4"
    if not path.exists():
        vol = case.get("volume", 0.3)
        audio = "anullsrc=r=48000:cl=stereo" if case.get("silent") else \
            f"sine=f=220:r=48000,volume={vol},aformat=channel_layouts={'mono' if case.get('mono') else 'stereo'}"
        subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=s={size}:r={fps}:d={dur:.2f}", "-f", "lavfi", "-t", f"{dur:.2f}", "-i", audio,
                        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return path, words


def real_videos(cfg, rep, log) -> list[dict]:
    out = []
    for url in REAL:
        try:
            r = analyze_video({"video_id": url, "input": url, "title": "", "channel": ""}, cfg, rep, None, None)
            clips = r["clips"]
            judged = json.loads((r["video"].parent / "analysis.json").read_text(encoding="utf-8"))["judged"]
            out.append({**r, "judged": judged})
            log(f"real video ready: {r['meta'].get('title')} - {len(clips)} approved, {len(judged)} judged")
        except Exception as exc:
            log(f"real video failed: {url}: {exc}")
    return out


# ---------------------------------------------------------------- checks
def check_clip(out_dir: Path, name: str, info: dict, words: list[dict], hook: str, safe: bool,
               expect_audio: bool, preset) -> list[str]:
    p = []
    video = out_dir / f"{name}.mp4"
    if not video.exists():
        return ["no video file"]
    pr = probe(video)
    if (pr["width"], pr["height"]) != (1080, 1920):
        p.append(f"size {pr['width']}x{pr['height']}")
    if abs(pr["fps"] - 30) > 0.5:
        p.append(f"fps {pr['fps']}")
    if not pr["has_audio"]:
        p.append("no audio track")
    if abs(pr["duration"] - info["duration"]) > 0.35:
        p.append(f"duration {pr['duration']:.2f} vs planned {info['duration']:.2f}")
    if info["duration"] < 5:
        p.append(f"clip too short: {info['duration']:.1f}s")
    err = sh(["-i", str(video), "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"])
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", err)
    if m and expect_audio:
        lufs = float(m[-1])
        if not -17.5 <= lufs <= -11.0:
            p.append(f"loudness {lufs} LUFS (want about -14)")
    if tp and float(tp[-1]) > 0.0:
        p.append(f"true peak {tp[-1]} dBFS clips")
    err = sh(["-i", str(video), "-vf", "blackdetect=d=0.5:pix_th=0.08", "-an", "-f", "null", "-"])
    if "black_start" in err:
        p.append("black frames: " + re.findall(r"black_start:\S+ black_end:\S+", err)[0])
    for ext in ("jpg", "txt", "json", "srt"):
        if not (out_dir / f"{name}.{ext}").exists():
            p.append(f"missing .{ext}")
    srt = (out_dir / f"{name}.srt").read_text(encoding="utf-8") if (out_dir / f"{name}.srt").exists() else ""
    times = re.findall(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)", srt)
    last = -1.0
    for t in times:
        a = int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2]) + int(t[3]) / 1000
        b = int(t[4]) * 3600 + int(t[5]) * 60 + int(t[6]) + int(t[7]) / 1000
        if b <= a or a < last - 0.2 or b > info["duration"] + 0.5:
            p.append(f"srt timing bad at {a:.2f}-{b:.2f}")
            break
        last = b
    if words and len([w for w in words if re.search(r"\w", w["w"])]) > 3 and not times:
        p.append("srt empty")
    txt = (out_dir / f"{name}.txt").read_text(encoding="utf-8") if (out_dir / f"{name}.txt").exists() else ""
    if len(txt) > 2200:
        p.append(f"post caption {len(txt)} chars (TikTok max 2200)")
    if safe and re.search(r"\b(fuck|shit|bitch)", txt + srt, re.I):
        p.append("profanity left in captions")
    if txt.count("#") > 8:
        p.append(f"{txt.count('#')} hashtags (too many)")
    return p


def check_moment(words: list[dict], start: float, end: float) -> list[str]:
    p = []
    inside = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    if not inside:
        return ["no words in the clip"]
    before = [w for w in words if w["e"] <= start + 0.05]
    if before and not re.search(r"[.!?]['\"]?$", before[-1]["w"]) and inside[0]["s"] - before[-1]["e"] < 0.5:
        p.append(f"starts mid-sentence: '...{before[-1]['w']} | {inside[0]['w']}'")
    after = [w for w in words if w["s"] >= end - 0.05]
    if not re.search(r"[.!?]['\"]?$", inside[-1]["w"]) and after and after[0]["s"] - inside[-1]["e"] < 0.5:
        p.append(f"ends mid-sentence: '{inside[-1]['w']} | {after[0]['w']}...'")
    return p


# ---------------------------------------------------------------- run
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=100)
    ap.add_argument("--out", default="backtest_out")
    ap.add_argument("--seed", type=int, default=int(time.time()) % 100000)
    ap.add_argument("--fast", action="store_true", help="ultrafast x264 (checks correctness, not quality)")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    cfg = load_config()
    out_root = Path(args.out).resolve()
    work = out_root / "_edge_sources"
    work.mkdir(parents=True, exist_ok=True)
    report_path = out_root / "report.json"
    started = time.time()

    def log(m: str) -> None:
        print(f"[{time.time() - started:7.1f}s] {m}", flush=True)

    rep = Reporter()
    reals = real_videos(cfg, rep, log)
    results = []
    for i in range(args.n):
        use_real = reals and i % 5 != 4 and (i % 5 != 3 or not EDGE)  # 3/5 real, 2/5 edge cases
        level_mode = rng.choice(["auto", "auto", *LEVEL_NAMES])
        row = {"i": i, "level_mode": level_mode}
        try:
            if use_real:
                r = rng.choice(reals)
                pool = r["clips"] + [c for c in r["judged"] if rng.random() < 0.3]
                c = rng.choice(pool) if pool else None
                if c is None:
                    continue
                c = c if isinstance(c, dict) else c.to_dict()
                all_words = r["transcript"]["words"]
                start, end = c["start"], c["end"]
                src, hook, emph = r["video"], c.get("hook", ""), c.get("emphasis_words", [])
                category, meta = c.get("category", "other"), r["meta"]
                row.update(kind="real", video=meta.get("title"), start=start, end=end, category=category,
                           approved=any(abs(a.start - start) < 0.01 for a in r["clips"]))
                row["moment_problems"] = check_moment(all_words, start, end) if row["approved"] else []
                comedy = float(r["signals"].get("comedy") or 0)
                energy = (c.get("signal_scores", {}).get("energy") or 50) / 100
                expect_audio = True
            else:
                case = EDGE[i // 5 % len(EDGE)] if i % 5 == 4 else rng.choice(EDGE)
                src, all_words = make_edge_video(case, work, rng)
                total = all_words[-1]["e"]
                length = min(total, case.get("length", rng.uniform(20, 60)))
                start = 0.0 if length >= total else round(rng.uniform(0, total - length), 2)
                end = round(min(total + 0.5, start + length), 2)
                hook = case.get("hook", "Nobody expected what happened next")
                emph = ["broke", "changed", "secret", "crazy"]
                category, meta = rng.choice(["funny", "story", "emotional", "insightful"]), \
                    {"id": case["name"], "title": case["name"], "channel": "Test"}
                row.update(kind="edge", case=case["name"], start=start, end=end, category=category)
                row["moment_problems"] = []
                comedy, energy = 0.0, 0.5
                expect_audio = not case.get("silent") and case.get("volume", 1) > 0.05
            level = choose_level(category, end - start, comedy, energy)[0] if level_mode == "auto" else level_mode
            preset = get_preset(level)
            if args.fast:
                preset = dataclasses.replace(preset, x264_preset="ultrafast")
            row["level"] = level
            words = clip_words(all_words, start, end)
            out_dir = out_root / f"{i:03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            name = f"clip_{i:03d}"
            job = RenderJob(source=Path(src), start=start, end=end, words=words, hook=hook, emphasis=emph,
                            out_dir=out_dir, name=name, highlights=[start + (end - start) / 2])
            t0 = time.time()
            info = render(job, preset, cfg)
            row["render_s"] = round(time.time() - t0, 1)
            safe = cfg["editing"].get("censor_profanity", True)
            write_post_files(out_dir, name, {"title": hook or "clip", "hook": hook, "caption": hook,
                                             "hashtags": ["fyp", "viral", category]}, info, meta, safe)
            row["problems"] = check_clip(out_dir, name, info, words, hook, safe, expect_audio, preset)
            row["problems"] += row.pop("moment_problems")
        except Exception as exc:
            row["problems"] = [f"CRASH {type(exc).__name__}: {str(exc)[-300:]}"]
            row["trace"] = traceback.format_exc()[-1500:]
            row.pop("moment_problems", None)
        results.append(row)
        log(f"#{i:03d} {row.get('kind')} {row.get('case') or row.get('video', '')[:30]} [{row.get('level')}] "
            f"{'OK' if not row['problems'] else 'FAIL: ' + '; '.join(row['problems'])}")
        report_path.write_text(json.dumps({"seed": args.seed, "results": results}, indent=1), encoding="utf-8")

    fails = [r for r in results if r["problems"]]
    kinds = Counter(re.sub(r"[-\d.:]+", "#", p)[:70] for r in fails for p in r["problems"])
    log(f"DONE {len(results) - len(fails)}/{len(results)} passed in {(time.time() - started) / 60:.1f} min")
    for k, n in kinds.most_common():
        log(f"  {n:3d}x {k}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
