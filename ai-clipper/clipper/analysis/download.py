"""Download a YouTube video plus its metadata (heatmap, chapters) and, if available, captions."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .. import ytdl
from ..media import ffmpeg_exe, probe

KEEP_INFO = ("id", "title", "description", "channel", "channel_id", "duration", "view_count",
             "like_count", "comment_count", "heatmap", "chapters", "tags", "upload_date", "width",
             "height", "fps", "webpage_url")
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


def download(source: str, work_dir: Path, on_progress=None, log=None) -> tuple[Path, dict]:
    """source: a YouTube video id, any video URL yt-dlp supports, or a local video file."""
    out_dir = work_dir / source_key(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    info_path = out_dir / "info.json"
    cached = sorted(p for p in out_dir.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm", ".mov"))
    if cached and info_path.exists():
        return cached[0], json.loads(info_path.read_text(encoding="utf-8"))
    if Path(source).is_file():
        return _local(Path(source), out_dir)
    url = source if "://" in source else f"https://www.youtube.com/watch?v={source}"
    video_path = out_dir / "source.mp4"

    def hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    def fetch() -> dict:
        with ytdl.ydl(skip_download=True, socket_timeout=30) as y:
            raw = y.extract_info(url, download=False, process=False)
        hours = float(raw.get("duration") or 0) / 3600
        fmt = FORMAT
        if hours > 2:  # a 10-30 hour video at 1080p is tens of GB: 720p is plenty for 9:16 crops
            fmt = FORMAT.replace("1080", "720")
            if log:
                log(f"Long video ({hours:.1f} h) - downloading at 720p to save time and disk space")
        with ytdl.ydl(format=fmt, merge_output_format="mp4", outtmpl=str(out_dir / "source.%(ext)s"),
                      ffmpeg_location=ffmpeg_exe(), progress_hooks=[hook], socket_timeout=30, retries=10,
                      fragment_retries=10, continuedl=True) as y:
            return y.sanitize_info(y.process_ie_result(raw, download=True))

    info = ytdl.with_login_fallback(fetch, log)
    if not video_path.exists():  # merged into another container
        found = sorted(p for p in out_dir.glob("source.*") if p.suffix in (".mkv", ".webm", ".mp4"))
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
