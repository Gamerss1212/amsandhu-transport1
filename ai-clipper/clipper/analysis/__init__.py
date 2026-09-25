"""Step 2b: watch an entire video and pick the moments worth clipping."""
from __future__ import annotations

import json
import re
import time

from ..agents import BOARD
from ..config import Config
from ..events import Event, Reporter
from ..llm import Claude
from ..media import audio_for_analysis
from .download import caption_file, download, ytdlp_comments
from .moments import Clip, clip_words, select_moments
from .signals import compute_signals
from .transcribe import transcribe

__all__ = ["Clip", "analyze_video", "clip_words"]


def analyze_video(cand: dict, cfg: Config, rep: Reporter, profile: dict | None,
                  llm: Claude | None, yt_api=None) -> dict:
    a = cfg["analysis"]
    vid = cand["video_id"]
    rep.progress("analysis", 0.02, f"Downloading {cand.get('title') or vid}...")
    name = cand.get("title") or cand.get("input") or vid
    yt_id = vid if re.fullmatch(r"[\w-]{11}", str(vid)) else ""
    base = {"kind": "video", "video_id": vid, "title": cand.get("title") or "", "channel": cand.get("channel") or "",
            "thumb": f"https://i.ytimg.com/vi/{yt_id}/mqdefault.jpg" if yt_id else "",
            "url": f"https://www.youtube.com/watch?v={yt_id}" if yt_id else cand.get("input", "")}

    last = {"stage": "", "t": 0.0}

    def watch(stage: str, progress: float | None = None, **extra) -> None:  # the live "now watching" feed
        now = time.time()
        if stage == last["stage"] and now - last["t"] < 0.7 and (progress or 0) < 1:
            return  # at most ~1 update a second per video
        last.update(stage=stage, t=now)
        rep.emit(Event("watch", "analysis", data={**base, **extra, "stage": stage, "progress": progress}))

    watch("download", 0.0)
    with BOARD.work("download", f"Downloading {name}"):
        video, info = download(cand.get("input") or vid, cfg.path("paths.work_dir"),
                               lambda f: (rep.progress("analysis", 0.02 + 0.18 * f, "Downloading..."),
                                          watch("download", f)),
                               log=lambda m: rep.info("analysis", m))  # any length: no limit on videos you choose
    base.update(title=info.get("title") or base["title"], channel=info.get("channel") or base["channel"],
                duration=float(info.get("duration") or 0))
    watch("listen", 0.0)
    meta = {**info, "title": info.get("title") or cand.get("title", ""),
            "channel": info.get("channel") or cand.get("channel", ""),
            "duration": float(info.get("duration") or cand.get("duration") or 0)}

    rep.progress("analysis", 0.2, "Transcribing the whole video (word by word)...")
    # the built-in judge reads English; Claude (optional) handles any language
    language = None if llm else cfg["discovery"].get("language")
    with BOARD.work("listen", f"Listening to {name}"):
        transcript = transcribe(video, a["whisper_model"], a["whisper_device"], caption_file(video),
                                log=lambda m: rep.info("analysis", m), language=language,
                                long_hours=float(a.get("long_video_hours", 0.33)),
                                long_model=a.get("long_video_model", "base"),
                                progress=lambda f, m: (rep.progress("analysis", 0.2 + 0.15 * f, m),
                                                       watch("listen", f, note=m)))
    rep.info("analysis", f"Transcript: {len(transcript['words'])} words via {transcript['source']}")

    comments = []
    if a["use_comments"]:
        rep.progress("analysis", 0.35, "Reading viewer comments for timestamped moments...")
        try:
            comments = yt_api.comments(vid, a["max_comment_pages"]) if yt_api else \
                ytdlp_comments(vid, 100 * a["max_comment_pages"])
            rep.info("analysis", f"Read {len(comments)} comments")
        except Exception as exc:
            rep.info("analysis", f"Comments unavailable: {exc}")

    watch("audio")
    with BOARD.work("audio", f"Laughter, energy and reactions in {name}"):
        wav = audio_for_analysis(video, video.parent / "audio16k.wav")
        signals = compute_signals(info, transcript, wav, comments, meta["duration"])
    found = [k for k, v in signals["raw"].items() if v is not None and k != "pace"]
    rep.info("analysis", f"Audience/audio signals available: {', '.join(found) or 'none'}")

    watch("judge")
    with BOARD.work("judge", f"Scoring every moment of {name}"):
        approved, judged = select_moments(
            meta, transcript, signals, profile, cfg, video, llm,
            progress=lambda f, m="": rep.progress("analysis", 0.4 + 0.58 * f, m),
            log=lambda m: rep.info("analysis", m))
    (video.parent / "analysis.json").write_text(json.dumps(
        {"meta": {k: meta[k] for k in ("id", "title", "channel", "duration") if k in meta},
         "approved": [c.to_dict() for c in approved], "judged": [c.to_dict() for c in judged]}, indent=2), encoding="utf-8")
    rep.progress("analysis", 1.0, f"{len(approved)} clips approved")
    watch("done", 1.0, clips=len(approved))
    return {"video": video, "meta": meta, "transcript": transcript, "signals": signals, "clips": approved}
