"""Download a YouTube video plus its metadata (heatmap, chapters) and, if available, captions."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .. import ytdl
from ..media import check_cancel, ffmpeg_exe, probe, run_ffmpeg

KEEP_INFO = ("id", "title", "description", "channel", "channel_id", "duration", "view_count",
             "like_count", "comment_count", "heatmap", "chapters", "tags", "upload_date", "width",
             "height", "fps", "webpage_url", "audio_only")
MEDIA_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".opus", ".mp3", ".ogg")
AUDIO_ONLY_HOURS = 3.0  # longer videos: analyse the audio only, then download just the chosen clips
AUDIO_FORMAT = "ba[protocol^=http]/ba/b"
# plain HTTPS formats first: HLS (m3u8) streams are slow and can stall
FORMAT = ("bv*[height<=1080][ext=mp4][protocol^=http]+ba[ext=m4a][protocol^=http]/"
          "bv*[height<=1080][protocol^=http]+ba[protocol^=http]/"
          "bv*[height<=1080]+ba/b[height<=1080]/b")


_YT_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})")


def source_key(source: str) -> str:
    """Stable folder name for a YouTube id, a video URL or a local file."""
    if re.fullmatch(r"[\w-]{1,64}", source) and not Path(source).is_file():  # a video id
        return source
    if m := _YT_ID.search(source):
        return m.group(1)
    name = re.split(r"[\\/]", source.split("?")[0].rstrip("\\/"))[-1]
    stem = re.sub(r"[^\w-]+", "_", name.rsplit(".", 1)[0]).strip("_")[:40] or "video"
    return f"{stem}_{hashlib.sha1(source.encode()).hexdigest()[:8]}"


def _local(path: Path, out_dir: Path) -> tuple[Path, dict]:
    video_path = out_dir / f"source{path.suffix.lower() or '.mp4'}"
    if not video_path.exists():
        shutil.copy(path, video_path)
    info = {k: None for k in KEEP_INFO}
    info.update(id=out_dir.name, title=path.stem, channel="", duration=probe(video_path)["duration"])
    (out_dir / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return video_path, info


def download(source: str, work_dir: Path, on_progress=None, log=None,
             max_hours: float | None = None) -> tuple[Path, dict]:
    """source: a YouTube video id, any video URL yt-dlp supports, or a local video file."""
    out_dir = work_dir / source_key(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    info_path = out_dir / "info.json"
    cached = sorted(p for p in out_dir.glob("source.*") if p.suffix in MEDIA_EXT)
    if cached and info_path.exists():
        return cached[0], json.loads(info_path.read_text(encoding="utf-8"))
    if Path(source).is_file():
        return _local(Path(source), out_dir)
    url = source if "://" in source else f"https://www.youtube.com/watch?v={source}"
    video_path = out_dir / "source.mp4"

    def hook(d: dict) -> None:
        check_cancel()
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    def fetch() -> dict:
        with ytdl.ydl(skip_download=True, socket_timeout=30) as y:
            raw = y.extract_info(url, download=False, process=False)
        hours = float(raw.get("duration") or 0) / 3600
        if max_hours and hours > max_hours:
            raise RuntimeError(f"This video is {hours:.0f} hours long - the limit is {max_hours:.0f} hours")
        fmt, extra = FORMAT, {"merge_output_format": "mp4"}
        if hours > AUDIO_ONLY_HOURS:
            # a 200-hour video is hundreds of GB: listen to the audio only (about 50 MB an hour),
            # then fetch just the chosen clips in full quality when editing
            fmt, extra = AUDIO_FORMAT, {}
            if log:
                log(f"Very long video ({hours:.1f} h) - downloading the audio only to find the best moments; "
                    "the chosen clips are downloaded in full quality afterwards")
        elif hours > 1.5:  # 720p is plenty for 9:16 crops and halves the download
            fmt = FORMAT.replace("1080", "720")
            if log:
                log(f"Long video ({hours:.1f} h) - downloading at 720p to save time and disk space")
        with ytdl.ydl(format=fmt, outtmpl=str(out_dir / "source.%(ext)s"), ffmpeg_location=ffmpeg_exe(),
                      progress_hooks=[hook], socket_timeout=30, retries=10, fragment_retries=10,
                      continuedl=True, **extra) as y:
            got = y.sanitize_info(y.process_ie_result(raw, download=True))
        got["audio_only"] = hours > AUDIO_ONLY_HOURS
        got.setdefault("webpage_url", url)
        return got

    info = ytdl.with_login_fallback(fetch, log)
    if not video_path.exists():  # merged into another container
        found = sorted(p for p in out_dir.glob("source.*") if p.suffix in MEDIA_EXT)
        if not found:
            raise RuntimeError(f"download of {source} produced no video file")
        video_path = found[0]
    slim = {k: info.get(k) for k in KEEP_INFO}
    info_path.write_text(json.dumps(slim), encoding="utf-8")

    # captions are only a backup for Whisper, so failing to get them must never fail the download
    try:
        with ytdl.ydl(skip_download=True, writesubtitles=True, writeautomaticsub=True,
                      subtitleslangs=["en"], subtitlesformat="json3",
                      outtmpl=str(out_dir / "source.%(ext)s")) as y:
            y.extract_info(url, download=True)
    except Exception:
        pass
    return video_path, slim


def _section_via_relay(url: str, start: float, end: float, out: Path) -> Path:
    """ffmpeg cuts [start, end] straight out of the online file, fetching only the bytes it needs."""
    from .relay import Relay

    def info() -> dict:
        with ytdl.ydl(format=FORMAT, skip_download=True, socket_timeout=30) as y:
            return y.extract_info(url, download=False)

    got = info()
    fmts = got.get("requested_formats") or [got]
    if any(f.get("protocol", "https") not in ("http", "https") for f in fmts):
        raise RuntimeError("streaming-only format")
    targets = [(f["url"], f.get("http_headers") or got.get("http_headers") or {}) for f in fmts]
    tmp = out.with_name(out.stem + ".part.mp4")
    with Relay(targets) as relay:
        args: list[str] = []
        for n in range(len(targets)):
            args += ["-ss", f"{max(0.0, start):.3f}", "-i", relay.url(n)]
        maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(targets) > 1 else ["-map", "0:v:0", "-map", "0:a:0?"]
        run_ffmpeg([*args, "-t", f"{end - start:.3f}", *maps, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(tmp)])
    tmp.replace(out)
    return out


def download_section(url: str, start: float, end: float, out: Path, log=None) -> Path:
    """Just [start, end] of an online video in full quality (for clips of very long videos)."""
    from yt_dlp.utils import download_range_func

    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        return ytdl.with_login_fallback(lambda: _section_via_relay(url, start, end, out), log)
    except ytdl.BotCheck:
        raise
    except Exception as exc:
        if log:
            log(f"Direct clip download failed ({str(exc)[-120:]}) - trying yt-dlp's own clip download")
    stem = out.with_suffix("")

    def fetch() -> dict:
        with ytdl.ydl(format=FORMAT, merge_output_format="mp4", outtmpl=str(stem) + ".%(ext)s",
                      ffmpeg_location=ffmpeg_exe(), download_ranges=download_range_func(None, [(start, end)]),
                      force_keyframes_at_cuts=True, socket_timeout=30, retries=10, fragment_retries=10) as y:
            return y.extract_info(url, download=True)

    ytdl.with_login_fallback(fetch, log)
    found = sorted(p for p in out.parent.glob(stem.name + ".*") if p.suffix in MEDIA_EXT)
    if not found:
        raise RuntimeError(f"could not download {start:.0f}-{end:.0f}s of {url}")
    if found[0] != out:
        found[0].replace(out)
    return out


def caption_file(video_path: Path) -> Path | None:
    files = sorted(video_path.parent.glob("source*.json3"))
    return files[0] if files else None


def ytdlp_comments(video_id: str, max_comments: int = 500) -> list[dict]:
    """Top comments without an API key (used to find timestamps viewers quote)."""
    if not re.fullmatch(r"[\w-]{11}", video_id):
        return []
    with ytdl.ydl(skip_download=True, getcomments=True,
                  extractor_args={"youtube": {"max_comments": [str(max_comments), "all", "0", "0"],
                                              "comment_sort": ["top"]}}) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False) or {}
    return [{"text": c.get("text") or "", "likes": c.get("like_count") or 0} for c in info.get("comments") or []]
