"""Assemble the output folder: the thing you actually open and post from."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..config import Config
from ..models import Deliverable, EditPlan, PostCopy, SourceVideo
from ..trends.profile import TrendProfile
from ..utils import extract_frame, info


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return (slug or "clip")[:limit].rstrip("-")


def clip_basename(index: int, plan: EditPlan) -> str:
    title = plan.candidate.title or plan.candidate.hook_line or f"clip-{index}"
    return f"{index:02d}-{slugify(title)}"


def make_thumbnail(video_path: str, dst: str, at: float = 0.6) -> Optional[str]:
    try:
        extract_frame(video_path, at, dst, width=720)
        return dst
    except Exception:
        return None


def write_deliverable(index: int, plan: EditPlan, video_path: str, srt_path: str,
                      copy: Dict[str, PostCopy], out_dir: Path) -> Deliverable:
    base = clip_basename(index, plan)
    thumb = make_thumbnail(video_path, str(out_dir / f"{base}.jpg")) or ""

    deliverable = Deliverable(
        index=index,
        video_path=str(video_path),
        thumbnail_path=thumb,
        srt_path=str(srt_path),
        duration=plan.out_duration,
        candidate=plan.candidate,
        copy=copy,
    )
    (out_dir / f"{base}.txt").write_text(_readable_post_sheet(deliverable, plan),
                                         encoding="utf-8")
    (out_dir / f"{base}.json").write_text(
        json.dumps(deliverable.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return deliverable


def _readable_post_sheet(d: Deliverable, plan: EditPlan) -> str:
    c = d.candidate
    lines = [
        f"CLIP {d.index:02d} — {c.title or c.hook_line or 'clip'}",
        "=" * 64,
        f"File        : {Path(d.video_path).name}",
        f"Length      : {d.duration:.1f}s  (source {_ts(c.start)}–{_ts(c.end)})",
        f"Score       : {c.scores.total:.1f}/100",
        f"  hook {c.scores.hook:.0f} · payoff {c.scores.payoff:.0f} · standalone "
        f"{c.scores.standalone:.0f} · emotion {c.scores.emotion:.0f} · share "
        f"{c.scores.shareability:.0f} · rewatch {c.scores.rewatch:.0f} · trend-fit "
        f"{c.scores.trend_fit:.0f}",
        f"On-screen hook: {plan.hook_text or '(none)'}",
    ]
    if c.reason:
        lines.append(f"Why this one : {c.reason}")
    if c.risk_notes:
        lines.append(f"⚠ Heads up   : {c.risk_notes}")
    lines.append("")

    for platform, copy in d.copy.items():
        lines.append("-" * 64)
        lines.append(f"{platform.upper()}")
        lines.append("-" * 64)
        lines.append("CAPTION (copy from here):")
        lines.append(copy.caption)
        if copy.hashtags:
            lines.append("")
            lines.append(" ".join(copy.hashtags))
        if copy.alt_captions:
            lines.append("")
            lines.append("Alternates:")
            for i, alt in enumerate(copy.alt_captions, 1):
                lines.append(f"  {i}. {alt}")
        if copy.first_comment:
            lines.append("")
            lines.append(f"Pinned first comment: {copy.first_comment}")
        lines.append("")

    lines.append("-" * 64)
    lines.append("TRANSCRIPT")
    lines.append("-" * 64)
    lines.append(c.transcript)
    return "\n".join(lines) + "\n"


def write_manifest(out_dir: Path, source: SourceVideo, profile: TrendProfile,
                   deliverables: Sequence[Deliverable], cfg: Config,
                   started_at: str, warnings: Sequence[str] = ()) -> Path:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started_at": started_at,
        "source": {
            "title": source.title,
            "url": source.url,
            "uploader": source.uploader,
            "duration": round(source.duration, 2),
            "resolution": f"{source.width}x{source.height}",
        },
        "trend_profile": profile.to_dict(),
        "settings": cfg.to_dict(),
        "warnings": list(warnings),
        "clips": [d.to_dict() for d in deliverables],
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_index(out_dir: Path, source: SourceVideo, deliverables: Sequence[Deliverable],
                profile: TrendProfile) -> Path:
    """A one-page README for the folder, so it reads without any tooling."""
    lines = [
        f"# Clips from: {source.title}",
        "",
        f"Source: {source.url}",
        f"Trend profile: {profile.summary()}",
        f"Clips: {len(deliverables)}",
        "",
        "| # | Title | Length | Score | Files |",
        "|---|-------|--------|-------|-------|",
    ]
    for d in deliverables:
        name = Path(d.video_path).stem
        lines.append(
            f"| {d.index:02d} | {d.candidate.title or d.candidate.hook_line or '—'} | "
            f"{d.duration:.0f}s | "
            f"{d.candidate.scores.total:.0f} | `{name}.mp4`, `{name}.txt` |")
    lines += [
        "",
        "Each clip ships with:",
        "",
        "- `NN-title.mp4` — the finished 1080x1920 video, captions burned in",
        "- `NN-title.txt` — captions and hashtags per platform, ready to paste",
        "- `NN-title.srt` — subtitles, if you want to upload them separately",
        "- `NN-title.json` — scores and metadata for this clip",
        "- `NN-title.jpg` — a cover frame",
        "",
        "`manifest.json` holds the full run: every setting, the trend profile used, "
        "and every clip's scores.",
    ]
    path = out_dir / "README.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
