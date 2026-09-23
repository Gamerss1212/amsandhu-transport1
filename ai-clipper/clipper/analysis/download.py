"""Download a YouTube video plus its metadata (heatmap, chapters, auto-captions)."""
from __future__ import annotations

import json
from pathlib import Path

from ..media import ffmpeg_exe

KEEP_INFO = ("id", "title", "description", "channel", "channel_id", "duration", "view_count",
             "like_count", "comment_count", "heatmap", "chapters", "tags", "upload_date", "width",
             "height", "fps")


def download(video_id: str, work_dir: Path, on_progress=None, cookies_from_browser: str | None = None,
             cookies_file: str | None = None) -> tuple[Path, dict]:
    import yt_dlp

    out_dir = work_dir / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path, info_path = out_dir / "source.mp4", out_dir / "info.json"
    if video_path.exists() and info_path.exists():
        return video_path, json.loads(info_path.read_text())

    def hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "ffmpeg_location": ffmpeg_exe(),
        "quiet": True,
        "noprogress": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-orig", "en-US"],
        "subtitlesformat": "json3",
        "ignoreerrors": False,
        "progress_hooks": [hook],
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                                                 download=True))
    if not video_path.exists():  # merged into another container
        found = sorted(p for p in out_dir.glob("source.*") if p.suffix in (".mkv", ".webm", ".mp4"))
        if not found:
            raise RuntimeError(f"download of {video_id} produced no video file")
        video_path = found[0]
    slim = {k: info.get(k) for k in KEEP_INFO}
    info_path.write_text(json.dumps(slim))
    return video_path, slim


def caption_file(video_path: Path) -> Path | None:
    files = sorted(video_path.parent.glob("source*.json3"))
    return files[0] if files else None
