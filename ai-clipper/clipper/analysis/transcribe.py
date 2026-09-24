"""Word-level transcript of the entire video.

Primary: faster-whisper (local, accurate word timings).
Fallback: YouTube's own captions (json3), which also carry per-word timings.

Transcript shape:
  {"source": str, "words": [{"w", "s", "e"}], "segments": [{"s", "e", "text"}]}
"""
from __future__ import annotations

import json
import os
import re
import time
import wave
from pathlib import Path

from ..media import extract_audio

_SENTENCE_END = re.compile(r"[.!?…]['\"]?$")


def group_segments(words: list[dict], max_words: int = 28, max_gap: float = 1.2) -> list[dict]:
    """Group words into sentence-like segments (used for boundary snapping)."""
    segments, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        if _SENTENCE_END.search(w["w"]) or gap > max_gap or len(cur) >= max_words or nxt is None:
            segments.append({"s": cur[0]["s"], "e": cur[-1]["e"],
                             "text": " ".join(x["w"] for x in cur).strip()})
            cur = []
    return segments


def _model_cached(model_size: str) -> bool:
    try:
        from faster_whisper.utils import download_model

        download_model(model_size, local_files_only=True)
        return True
    except Exception:
        return False


class WrongLanguage(RuntimeError):
    pass


def _wav_seconds(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def _whisper(wav: Path, model_size: str, device: str, language: str | None = None, fast: bool = False,
             progress=None) -> dict:
    """Transcribe in 20-minute pieces (a 30-hour video never has to fit in memory).
    fast=True batches the audio through the model at once - several times faster on long videos."""
    import ctranslate2
    import numpy as np
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    gpu = device == "cuda" or (device == "auto" and ctranslate2.get_cuda_device_count() > 0)
    # 8-bit on CPU is several times faster than the float32 fallback with near-identical accuracy
    model = WhisperModel(model_size, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8",
                         cpu_threads=min(16, os.cpu_count() or 4))
    runner = BatchedInferencePipeline(model) if fast else model
    words: list[dict] = []
    detected = None
    with wave.open(str(wav), "rb") as w:
        sr, total = w.getframerate(), w.getnframes()
        piece = 20 * 60 * sr
        for off in range(0, max(total, 1), piece):
            raw = w.readframes(piece)
            if not raw:
                break
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            opts = {"word_timestamps": True, "vad_filter": True, "beam_size": 1 if fast else 5}
            if fast:
                opts["batch_size"] = 16
            if detected:
                opts["language"] = detected
            segments, info = runner.transcribe(audio, **opts)
            if detected is None:
                # segments is lazy: the language is known before any real transcription work is done
                if language and info.language != language and info.language_probability >= 0.5:
                    raise WrongLanguage(f"video is in '{info.language}', expected '{language}'")
                detected = info.language
            base = off / sr
            for seg in segments:
                for wd in seg.words or []:
                    text = wd.word.strip()
                    if text:
                        words.append({"w": text, "s": round(base + wd.start, 3), "e": round(base + wd.end, 3)})
            if progress:
                progress(min(1.0, (off + piece) / max(total, 1)))
    words.sort(key=lambda x: x["s"])
    return {"source": "whisper", "language": detected, "words": words, "segments": group_segments(words)}


def parse_json3(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    words: list[dict] = []
    for ev in data.get("events", []):
        start = ev.get("tStartMs", 0) / 1000
        end = start + ev.get("dDurationMs", 0) / 1000
        segs = [s for s in ev.get("segs", []) or [] if s.get("utf8", "").strip()]
        for i, s in enumerate(segs):
            ws = start + s.get("tOffsetMs", 0) / 1000
            we = start + segs[i + 1].get("tOffsetMs", 0) / 1000 if i + 1 < len(segs) else end
            for token in s["utf8"].split():
                words.append({"w": token, "s": round(ws, 3), "e": round(max(we, ws + 0.05), 3)})
    words.sort(key=lambda w: w["s"])
    # auto-captions overlap between events; clamp ends to the next word start
    for a, b in zip(words, words[1:]):
        a["e"] = round(min(a["e"], max(b["s"], a["s"] + 0.05)), 3)
    return {"source": "youtube-captions", "words": words, "segments": group_segments(words)}


def transcribe(video: Path, model_size: str = "small", device: str = "auto",
               captions: Path | None = None, log=None, language: str | None = None,
               long_hours: float = 1.5, long_model: str = "base", progress=None) -> dict:
    """Videos longer than `long_hours` are "watched" at high speed: the video's own captions when it
    has them (instant), otherwise batched speech recognition with a lighter model past 6 hours."""
    cache = video.parent / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    result = None
    try:
        wav = extract_audio(video, video.parent / "audio16k.wav")
        seconds = _wav_seconds(wav)
        fast = seconds > long_hours * 3600
        if fast and captions:
            result = parse_json3(captions)
            if result["words"] and log:
                log(f"Long video ({seconds / 3600:.1f} h): read its captions - watched in seconds")
        if not result or not result["words"]:
            size = long_model if fast and seconds > 6 * 3600 and model_size not in ("tiny", "base") else model_size
            if log and not _model_cached(size):
                log(f"First run: downloading the '{size}' speech-recognition model (about 250-500 MB, "
                    "only once). This can take a few minutes.")
            if log and fast:
                log(f"Long video ({seconds / 3600:.1f} h): high-speed mode (batched, '{size}' model)")
            t0 = time.time()

            def tick(f: float) -> None:
                if progress:
                    speed = f * seconds / max(1.0, time.time() - t0)
                    progress(f, f"Listening at {speed:.0f}x speed - {f * seconds / 3600:.1f} of "
                                f"{seconds / 3600:.1f} h done")
            result = _whisper(wav, size, device, language, fast=fast, progress=tick)
            if log:
                log(f"Watched {seconds / 60:.0f} min of video at {seconds / max(1.0, time.time() - t0):.0f}x speed")
    except WrongLanguage:
        raise
    except ImportError:
        if log:
            log("faster-whisper not installed - falling back to YouTube captions")
    except Exception as exc:
        if log:
            log(f"Whisper failed ({exc}) - falling back to YouTube captions")
    if (not result or not result["words"]) and captions:
        result = parse_json3(captions)
    if not result or not result["words"]:
        raise RuntimeError("No transcript available (install faster-whisper or use a video with captions)")
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
