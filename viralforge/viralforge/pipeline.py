"""The orchestrator: URL in, folder of ready-to-post clips out.

Stage results are cached under ``work_dir`` keyed by video id, so a re-run with
different render settings skips the download, the transcription and the audio
analysis - which is where almost all of the wall-clock time lives.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analyze.audio import analyze_audio
from .analyze.candidates import build_candidates
from .analyze.llm import LLMClient, LLMUnavailable
from .analyze.scoring import score_candidates, select_clips
from .analyze.transcribe import transcribe
from .analyze.visual import analyze_visual
from .config import Config
from .edit.planner import build_plan
from .edit.render import RenderError, render_clip, write_srt
from .ingest import fetch_source
from .models import (
    AudioAnalysis, ClipCandidate, Deliverable, SourceVideo, Transcript, VisualAnalysis,
)
from .publish.copywriter import write_copy
from .publish.package import clip_basename, write_deliverable, write_index, write_manifest
from .trends.analyze import resolve_profile
from .trends.profile import TrendProfile
from .utils import console, error, info, require_ffmpeg, step, warn


@dataclass
class RunResult:
    source: SourceVideo
    profile: TrendProfile
    deliverables: List[Deliverable] = field(default_factory=list)
    out_dir: str = ""
    warnings: List[str] = field(default_factory=list)
    elapsed: float = 0.0


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.warnings: List[str] = []

    # ------------------------------------------------------------------ #

    def run(self, target: str, *, dry_run: bool = False,
            refresh_trends: bool = False) -> RunResult:
        started = time.time()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        require_ffmpeg()

        cfg = self.cfg
        llm = self._llm(cfg.scoring.model, cfg.scoring.effort, cfg.scoring.max_output_tokens)

        step("Fetching source")
        work_root = Path(cfg.work_dir)
        source = fetch_source(target, cfg, str(work_root / "downloads" / _key(target)))
        info(f"{source.title} — {source.duration / 60:.1f} min, "
             f"{source.width}x{source.height} @ {source.fps:.0f}fps")
        cache_dir = work_root / "analysis" / source.video_id
        cache_dir.mkdir(parents=True, exist_ok=True)

        step("Transcribing")
        transcript = self._transcript(source, cache_dir)
        info(f"{len(transcript.words)} words via {transcript.source}")

        step("Measuring audio")
        audio = self._audio(source, cache_dir)
        if audio.silences:
            dead = sum(b - a for a, b in audio.silences)
            info(f"{len(audio.silences)} silences, {dead:.0f}s of dead air in the source")

        step("Loading trend profile")
        profile = resolve_profile(cfg, llm=llm, force_refresh=refresh_trends)

        step("Finding candidate moments")
        candidates = build_candidates(transcript, audio, cfg)
        if not candidates:
            raise PipelineError(
                "No candidate moments found. The transcript may be empty or the source "
                "may be shorter than candidates.min_duration."
            )
        info(f"{len(candidates)} candidate windows")

        step("Scoring")
        scored = score_candidates(candidates, source, profile, audio, cfg, llm)
        chosen = select_clips(scored, cfg.output.clips, cfg.output.min_gap_between_clips)
        if not chosen:
            raise PipelineError("Nothing scored high enough to cut. Try raising output.clips.")
        self._report_selection(chosen)

        if dry_run:
            return RunResult(source=source, profile=profile, warnings=self.warnings,
                             elapsed=time.time() - started)

        step("Analysing framing")
        visual = self._visual(source, chosen, cache_dir)
        if visual.faces:
            info(f"tracking mode: {visual.method}, {len(visual.scene_cuts)} scene cuts")

        out_dir = self._prepare_out_dir(source)
        step(f"Editing {len(chosen)} clips → {out_dir}")
        deliverables = self._render_all(chosen, source, transcript, audio, visual,
                                        profile, out_dir, llm)

        if not deliverables:
            raise PipelineError("Every clip failed to render - see the errors above.")

        write_manifest(out_dir, source, profile, deliverables, cfg, started_at, self.warnings)
        write_index(out_dir, source, deliverables, profile)

        elapsed = time.time() - started
        return RunResult(source=source, profile=profile, deliverables=deliverables,
                         out_dir=str(out_dir), warnings=self.warnings, elapsed=elapsed)

    # ------------------------------------------------------------------ #
    # Stages, each cached
    # ------------------------------------------------------------------ #

    def _llm(self, model: str, effort: str, max_tokens: int) -> Optional[LLMClient]:
        if not self.cfg.scoring.enable_llm:
            return None
        client = LLMClient(model=model, effort=effort, max_tokens=max_tokens,
                           use_fallbacks=self.cfg.scoring.use_server_fallbacks)
        if not client.available:
            self._warn("No Claude credentials found — running on measured signal only. "
                       "Set ANTHROPIC_API_KEY for clip judgement and caption writing.")
            return None
        return client

    def _transcript(self, source: SourceVideo, cache_dir: Path) -> Transcript:
        path = cache_dir / "transcript.json"
        if path.is_file():
            try:
                cached = Transcript.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if cached.segments:
                    info("Using cached transcript.")
                    return cached
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        transcript = transcribe(source, self.cfg, str(Path(source.path).parent))
        path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
        return transcript

    def _audio(self, source: SourceVideo, cache_dir: Path) -> AudioAnalysis:
        path = cache_dir / "audio.json"
        if path.is_file():
            try:
                return AudioAnalysis.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        audio = analyze_audio(source.path, self.cfg)
        path.write_text(json.dumps(audio.to_dict()), encoding="utf-8")
        return audio

    def _visual(self, source: SourceVideo, chosen: Sequence[ClipCandidate],
                cache_dir: Path) -> VisualAnalysis:
        ranges = [(c.start, c.end) for c in chosen]
        key = "-".join(f"{a:.1f}_{b:.1f}" for a, b in ranges)
        path = cache_dir / f"visual_{abs(hash(key)) % (10 ** 12)}.json"
        if path.is_file():
            try:
                return VisualAnalysis.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        visual = analyze_visual(source, ranges, self.cfg)
        try:
            path.write_text(json.dumps(visual.to_dict()), encoding="utf-8")
        except OSError:
            pass
        return visual

    # ------------------------------------------------------------------ #

    def _prepare_out_dir(self, source: SourceVideo) -> Path:
        from .publish.package import slugify
        base = Path(self.cfg.output.directory) / slugify(source.title or source.video_id)
        if base.exists() and not self.cfg.output.overwrite:
            n = 2
            while (base.parent / f"{base.name}-{n}").exists():
                n += 1
            base = base.parent / f"{base.name}-{n}"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _render_all(self, chosen: Sequence[ClipCandidate], source: SourceVideo,
                    transcript: Transcript, audio: AudioAnalysis, visual: VisualAnalysis,
                    profile: TrendProfile, out_dir: Path,
                    llm: Optional[LLMClient]) -> List[Deliverable]:
        deliverables: List[Deliverable] = []
        render_work = Path(self.cfg.work_dir) / "render" / source.video_id
        render_work.mkdir(parents=True, exist_ok=True)
        copy_llm = None
        if llm is not None:
            copy_llm = LLMClient(model=self.cfg.copy.model, effort=self.cfg.copy.effort,
                                 max_tokens=self.cfg.copy.max_output_tokens,
                                 use_fallbacks=self.cfg.scoring.use_server_fallbacks)

        for i, candidate in enumerate(chosen, 1):
            try:
                plan = build_plan(i, candidate, source, transcript, audio, visual, self.cfg)
                if plan.out_duration < 5.0:
                    self._warn(f"Clip {i} collapsed to {plan.out_duration:.1f}s after "
                               "dead-air trimming — skipped.")
                    continue

                base = clip_basename(i, plan)
                video_path = out_dir / f"{base}.mp4"
                render_clip(plan, source, self.cfg, str(render_work), str(video_path))
                srt_path = write_srt(plan, str(out_dir / f"{base}.srt"))

                copy = write_copy(candidate, source, profile, self.cfg, copy_llm,
                                  profile.platforms)
                deliverable = write_deliverable(i, plan, str(video_path), srt_path,
                                                copy, out_dir)
                deliverables.append(deliverable)

                if self.cfg.output.write_plan:
                    (render_work / f"plan{i:02d}.json").write_text(
                        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
                console.print(f"  [green]✓[/green] {base}.mp4  "
                              f"[dim]{plan.out_duration:.0f}s · score "
                              f"{candidate.scores.total:.0f}[/dim]")
            except (RenderError, OSError) as exc:
                self._warn(f"Clip {i} failed to render: {exc}")
            except Exception as exc:  # keep going - one bad clip is not a failed run
                self._warn(f"Clip {i} failed ({type(exc).__name__}): {exc}")
        return deliverables

    def _report_selection(self, chosen: Sequence[ClipCandidate]) -> None:
        for i, c in enumerate(chosen, 1):
            info(f"{i}. {_ts(c.start)}–{_ts(c.end)} ({c.duration:.0f}s) "
                 f"score {c.scores.total:.0f} — {c.title or c.hook_line or '?'}")

    def _warn(self, message: str) -> None:
        warn(message)
        self.warnings.append(message)


class PipelineError(RuntimeError):
    pass


def _key(target: str) -> str:
    import hashlib
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
