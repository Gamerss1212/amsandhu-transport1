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


def _whisper(wav: Path, model_size: str, device: str, language: str | None = None) -> dict:
    import ctranslate2
    from faster_whisper import WhisperModel

    gpu = device == "cuda" or (device == "auto" and ctranslate2.get_cuda_device_count() > 0)
    # 8-bit on CPU is several times faster than the float32 fallback with near-identical accuracy
    model = WhisperModel(model_size, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8",
                         cpu_threads=min(16, os.cpu_count() or 4))
    segments, info = model.transcribe(str(wav), word_timestamps=True, vad_filter=True, beam_size=5)
    # segments is lazy: the language is known before any real transcription work is done
    if language and info.language != language and info.language_probability >= 0.5:
        raise WrongLanguage(f"video is in '{info.language}', expected '{language}'")
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start, 3), "e": round(w.end, 3)})
    return {"source": "whisper", "language": info.language, "words": words, "segments": group_segments(words)}


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
               captions: Path | None = None, log=None, language: str | None = None) -> dict:
    cache = video.parent / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    result = None
    try:
        wav = extract_audio(video, video.parent / "audio16k.wav")
        if log and not _model_cached(model_size):
            log(f"First run: downloading the '{model_size}' speech-recognition model (about 250-500 MB, "
                "only once). This can take a few minutes.")
        result = _whisper(wav, model_size, device, language)
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
