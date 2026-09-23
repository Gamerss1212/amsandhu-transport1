"""Download a YouTube video plus its metadata (heatmap, chapters) and, if available, captions."""
from __future__ import annotations

import json
from pathlib import Path

from .. import ytdl
from ..media import ffmpeg_exe

KEEP_INFO = ("id", "title", "description", "channel", "channel_id", "duration", "view_count",
             "like_count", "comment_count", "heatmap", "chapters", "tags", "upload_date", "width",
             "height", "fps")
# plain HTTPS formats first: HLS (m3u8) streams are slow and can stall
FORMAT = ("bv*[height<=1080][ext=mp4][protocol^=http]+ba[ext=m4a][protocol^=http]/"
          "bv*[height<=1080][protocol^=http]+ba[protocol^=http]/"
          "bv*[height<=1080]+ba/b[height<=1080]/b")


def download(video_id: str, work_dir: Path, on_progress=None) -> tuple[Path, dict]:
    out_dir = work_dir / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path, info_path = out_dir / "source.mp4", out_dir / "info.json"
    if video_path.exists() and info_path.exists():
        return video_path, json.loads(info_path.read_text(encoding="utf-8"))
    url = f"https://www.youtube.com/watch?v={video_id}"

    def hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    with ytdl.ydl(silent=False, format=FORMAT, merge_output_format="mp4",
                  outtmpl=str(out_dir / "source.%(ext)s"), ffmpeg_location=ffmpeg_exe(),
                  progress_hooks=[hook], socket_timeout=30, retries=10, fragment_retries=10,
                  continuedl=True) as y:
        info = y.sanitize_info(y.extract_info(url, download=True))
    if not video_path.exists():  # merged into another container
        found = sorted(p for p in out_dir.glob("source.*") if p.suffix in (".mkv", ".webm", ".mp4"))
        if not found:
            raise RuntimeError(f"download of {video_id} produced no video file")
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
    with ytdl.ydl(skip_download=True, getcomments=True,
                  extractor_args={"youtube": {"max_comments": [str(max_comments), "all", "0", "0"],
                                              "comment_sort": ["top"]}}) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False) or {}
    return [{"text": c.get("text") or "", "likes": c.get("like_count") or 0} for c in info.get("comments") or []]
