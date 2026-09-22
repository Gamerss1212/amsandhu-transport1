"""Word-level transcription.

Two backends, tried in this order when ``backend: auto``:

1. faster-whisper - real word timestamps, works on any source.  This is what
   makes the captions land on the beat and the sentence-boundary snapping work.
2. YouTube's own caption track - instant and free, but auto-captions carry
   per-word timings only in the ``json3`` format and are noticeably less
   accurate on names and numbers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from ..config import Config
from ..models import SourceVideo, Transcript, TranscriptSegment, Word
from ..utils import extract_audio, info, progress_bar, warn


class TranscriptionError(RuntimeError):
    pass


def transcribe(source: SourceVideo, cfg: Config, work_dir: str) -> Transcript:
    if cfg.transcribe.external_path:
        external = load_transcript_file(cfg.transcribe.external_path)
        info(f"Using the supplied transcript ({len(external.words)} words).")
        return external

    backend = cfg.transcribe.backend
    if backend == "youtube-subs" or (backend == "auto" and cfg.ingest.prefer_youtube_subs):
        subs = _load_youtube_subs(work_dir)
        if subs:
            info(f"Using YouTube caption track ({len(subs.words)} words).")
            return subs
        if backend == "youtube-subs":
            raise TranscriptionError(
                "No usable YouTube caption track was downloaded for this video."
            )
        warn("No YouTube caption track found - falling back to Whisper.")

    if backend in ("auto", "faster-whisper"):
        try:
            return _whisper(source, cfg, work_dir)
        except ImportError:
            if backend == "faster-whisper":
                raise TranscriptionError(
                    "faster-whisper is not installed. Run `pip install 'viralforge[whisper]'`, "
                    "or set transcribe.backend: youtube-subs."
                )
            warn("faster-whisper not installed - falling back to the YouTube caption track.")

    subs = _load_youtube_subs(work_dir)
    if subs:
        return subs
    raise TranscriptionError(
        "No transcript available. Install faster-whisper (`pip install "
        "'viralforge[whisper]'`) or use a source that has captions."
    )


def load_transcript_file(path: str) -> Transcript:
    """Read a transcript the user already has: SRT, VTT, json3, or our own JSON."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise TranscriptionError(f"No transcript file at {p}")
    suffix = p.suffix.lower()
    if suffix == ".json3":
        parsed = _parse_json3(p)
    elif suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "segments" in data:
            parsed = Transcript.from_dict(data)
            parsed.source = "imported"
        else:
            parsed = _parse_json3(p)
    elif suffix in (".srt", ".vtt", ".txt"):
        parsed = _parse_vtt(p)
    else:
        raise TranscriptionError(
            f"Unsupported transcript format {suffix!r} - use .srt, .vtt, .json3 or .json.")
    if parsed is None or not parsed.segments:
        raise TranscriptionError(f"Could not read any cues from {p}")
    parsed.source = "imported"
    return parsed


# --------------------------------------------------------------------------- #
# faster-whisper
# --------------------------------------------------------------------------- #


def _resolve_device(cfg: Config):
    device, compute = cfg.transcribe.device, cfg.transcribe.compute_type
    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:
            pass
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _whisper(source: SourceVideo, cfg: Config, work_dir: str) -> Transcript:
    from faster_whisper import WhisperModel  # may raise ImportError - handled by caller

    wav = Path(work_dir) / "audio16k.wav"
    if not wav.is_file():
        extract_audio(source.path, str(wav))

    device, compute = _resolve_device(cfg)
    info(f"Transcribing with faster-whisper ({cfg.transcribe.model}, {device}/{compute}).")
    model = WhisperModel(cfg.transcribe.model, device=device, compute_type=compute)

    segments_iter, meta = model.transcribe(
        str(wav),
        language=cfg.transcribe.language,
        beam_size=cfg.transcribe.beam_size,
        word_timestamps=True,
        vad_filter=cfg.transcribe.vad_filter,
        vad_parameters={"min_silence_duration_ms": 400} if cfg.transcribe.vad_filter else None,
        condition_on_previous_text=False,   # stops runaway repetition on long podcasts
    )

    segments: List[TranscriptSegment] = []
    total = source.duration or meta.duration or 0.0
    with progress_bar("transcribing", total=100.0) as update:
        for seg in segments_iter:
            words = [
                Word(text=w.word.strip(), start=float(w.start), end=float(w.end),
                     prob=float(getattr(w, "probability", 1.0) or 1.0))
                for w in (seg.words or [])
                if w.word and w.word.strip() and w.end is not None and w.start is not None
            ]
            if not words:
                words = [Word(text=seg.text.strip(), start=float(seg.start), end=float(seg.end))]
            segments.append(TranscriptSegment(start=float(seg.start), end=float(seg.end),
                                              text=seg.text.strip(), words=words))
            if total:
                update(min(100.0, 100.0 * seg.end / total))

    if not segments:
        raise TranscriptionError("Whisper produced an empty transcript - is there speech here?")
    return Transcript(language=meta.language or "en", segments=segments, source="whisper")


# --------------------------------------------------------------------------- #
# YouTube caption tracks
# --------------------------------------------------------------------------- #


def _load_youtube_subs(work_dir: str) -> Optional[Transcript]:
    from ..ingest.youtube import find_subtitle_file
    path = find_subtitle_file(work_dir)
    if not path:
        return None
    try:
        if path.suffix == ".json3":
            return _parse_json3(path)
        return _parse_vtt(path)
    except (OSError, ValueError, KeyError):
        return None


def _parse_json3(path: Path) -> Optional[Transcript]:
    """json3 is YouTube's own format and carries per-word offsets."""
    data = json.loads(path.read_text(encoding="utf-8"))
    segments: List[TranscriptSegment] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs or "tStartMs" not in event:
            continue
        base = event["tStartMs"] / 1000.0
        words: List[Word] = []
        for s in segs:
            text = (s.get("utf8") or "").strip()
            if not text or text == "\n":
                continue
            start = base + s.get("tOffsetMs", 0) / 1000.0
            words.append(Word(text=text, start=start, end=start))
        if not words:
            continue
        span = event.get("dDurationMs", 0) / 1000.0
        # json3 gives start offsets only - close each word at the next one's start.
        for i, w in enumerate(words):
            w.end = words[i + 1].start if i + 1 < len(words) else base + (span or 2.0)
            if w.end <= w.start:
                w.end = w.start + 0.22
        segments.append(TranscriptSegment(
            start=words[0].start, end=words[-1].end,
            text=" ".join(w.text for w in words), words=words))
    if not segments:
        return None
    return Transcript(language="en", segments=segments, source="youtube-subs")


_VTT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")


def _parse_vtt(path: Path) -> Optional[Transcript]:
    """VTT/SRT have cue-level timings only; words are spread across the cue."""
    segments: List[TranscriptSegment] = []
    current: Optional[tuple] = None
    lines: List[str] = []

    def flush() -> None:
        if not current or not lines:
            return
        start, end = current
        text = _TAG.sub("", " ".join(lines)).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            return
        tokens = text.split(" ")
        span = max(end - start, 0.2)
        per = span / len(tokens)
        words = [Word(text=tok, start=start + i * per, end=start + (i + 1) * per)
                 for i, tok in enumerate(tokens)]
        segments.append(TranscriptSegment(start=start, end=end, text=text, words=words))

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _VTT_TIME.search(raw)
        if m:
            flush()
            lines = []
            g = [int(x) for x in m.groups()]
            current = (g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0,
                       g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0)
        elif raw.strip() and current and not raw.strip().isdigit() and "-->" not in raw:
            if raw.strip() not in lines:     # auto-captions repeat the rolling line
                lines.append(raw.strip())
    flush()

    if not segments:
        return None
    segments.sort(key=lambda s: s.start)
    return Transcript(language="en", segments=segments, source="youtube-subs")
