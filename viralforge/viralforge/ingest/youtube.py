"""Fetch a long-form source video (YouTube or any yt-dlp supported site).

Also handles local files - pass a path instead of a URL and the rest of the
pipeline behaves identically.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models import Chapter, SourceVideo
from ..utils import ffprobe_media, info, warn


class SourceFetchError(RuntimeError):
    pass


_URL_RE = re.compile(r"^https?://", re.I)


def is_url(target: str) -> bool:
    return bool(_URL_RE.match(target.strip()))


def fetch_source(target: str, cfg: Config, work_dir: str) -> SourceVideo:
    if not is_url(target):
        return _from_local_file(target)
    return _download(target, cfg, work_dir)


# --------------------------------------------------------------------------- #


def _from_local_file(path: str) -> SourceVideo:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SourceFetchError(f"No such file: {p}")
    media = ffprobe_media(str(p))
    if not media["duration"]:
        raise SourceFetchError(f"Could not read a duration from {p} - is it a video file?")
    return SourceVideo(
        path=str(p),
        video_id=p.stem[:64],
        title=p.stem.replace("_", " ").replace("-", " ").strip(),
        url=str(p),
        duration=media["duration"],
        width=media["width"],
        height=media["height"],
        fps=media["fps"],
    )


def _ydl_options(cfg: Config, out_dir: Path) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "format": cfg.ingest.format,
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en.*", "en", "orig"],
        "subtitlesformat": "json3/vtt/best",
        "skip_download": False,
        # Force a broadly compatible container so the renderer never has to
        # deal with exotic codecs mid-pipeline.
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }
    if cfg.ingest.cookies_from_browser:
        opts["cookiesfrombrowser"] = (cfg.ingest.cookies_from_browser,)
    if cfg.ingest.cookies_file:
        opts["cookiefile"] = cfg.ingest.cookies_file
    if cfg.ingest.proxy:
        opts["proxy"] = cfg.ingest.proxy
    return opts


def _download(url: str, cfg: Config, work_dir: str) -> SourceVideo:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:  # pragma: no cover
        raise SourceFetchError("yt-dlp is not installed - run `pip install yt-dlp`.") from exc

    out_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with YoutubeDL({**_ydl_options(cfg, out_dir), "skip_download": True}) as ydl:
        try:
            probe = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            raise SourceFetchError(_explain(str(exc))) from exc

    if probe.get("_type") == "playlist":
        entries = [e for e in (probe.get("entries") or []) if e]
        if not entries:
            raise SourceFetchError("That playlist is empty.")
        probe = entries[0]
        url = probe.get("webpage_url") or url
        info(f"URL is a playlist - using the first entry: {probe.get('title', '?')}")

    duration = float(probe.get("duration") or 0.0)
    if duration and duration > cfg.ingest.max_source_duration:
        raise SourceFetchError(
            f"Source is {duration / 3600:.1f}h, over the "
            f"{cfg.ingest.max_source_duration / 3600:.1f}h limit "
            "(raise ingest.max_source_duration to override)."
        )
    if probe.get("is_live"):
        raise SourceFetchError("That is a live stream - wait for the VOD.")

    existing = _find_downloaded(out_dir)
    if existing:
        info(f"Reusing cached download: {existing.name}")
        path = existing
    else:
        with YoutubeDL(_ydl_options(cfg, out_dir)) as ydl:
            try:
                result = ydl.extract_info(url, download=True)
            except DownloadError as exc:
                raise SourceFetchError(_explain(str(exc))) from exc
        path = Path(result.get("requested_downloads", [{}])[0].get("filepath", "")) \
            if result.get("requested_downloads") else Path("")
        if not path.is_file():
            found = _find_downloaded(out_dir)
            if not found:
                raise SourceFetchError("yt-dlp finished but no media file was produced.")
            path = found

    media = ffprobe_media(str(path))
    if not media["has_audio"]:
        warn("The download has no audio track - transcription and pacing will not work.")

    chapters: List[Chapter] = []
    for ch in probe.get("chapters") or []:
        try:
            chapters.append(Chapter(start=float(ch["start_time"]), end=float(ch["end_time"]),
                                    title=str(ch.get("title", "")).strip()))
        except (KeyError, TypeError, ValueError):
            continue

    return SourceVideo(
        path=str(path),
        video_id=str(probe.get("id") or path.stem)[:64],
        title=str(probe.get("title") or path.stem),
        url=str(probe.get("webpage_url") or url),
        duration=media["duration"] or duration,
        width=media["width"],
        height=media["height"],
        fps=media["fps"],
        uploader=str(probe.get("uploader") or probe.get("channel") or ""),
        description=str(probe.get("description") or "")[:8000],
        chapters=chapters,
        tags=[str(t) for t in (probe.get("tags") or [])][:40],
    )


def _find_downloaded(out_dir: Path) -> Optional[Path]:
    candidates = [p for p in out_dir.glob("source.*")
                  if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")
                  and p.stat().st_size > 0]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def find_subtitle_file(work_dir: str) -> Optional[Path]:
    """yt-dlp writes subs beside the media; prefer json3 (it has word timings)."""
    d = Path(work_dir)
    for pattern in ("source*.json3", "source*.en*.json3", "source*.vtt", "source*.srt"):
        matches = sorted(d.glob(pattern))
        if matches:
            return matches[0]
    return None


def _explain(message: str) -> str:
    low = message.lower()
    if "sign in to confirm" in low or "age" in low and "restrict" in low:
        return ("YouTube wants a signed-in session for this video. Set "
                "`ingest.cookies_from_browser: chrome` (or firefox/edge) in your config.")
    if "private video" in low:
        return "That video is private."
    if "video unavailable" in low:
        return "That video is unavailable (removed, or blocked in this region)."
    if "members-only" in low or "join this channel" in low:
        return "That video is members-only - supply cookies for an account that has access."
    if "http error 429" in low or "too many requests" in low:
        return "YouTube is rate-limiting this IP. Wait a few minutes, or set `ingest.proxy`."
    return f"Download failed: {message.strip()}"
