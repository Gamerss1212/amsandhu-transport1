"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .config import Config, write_default_config
from .utils import console, error, info, warn


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS keeps a subparser's default from clobbering a flag already set on
    # the top-level parser, so `-v` works on either side of the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="show full tracebacks")

    parser = argparse.ArgumentParser(
        parents=[common],
        prog="viralforge",
        description="Turn long-form video into finished, ready-to-post vertical clips.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  viralforge run "https://youtube.com/watch?v=..."
  viralforge run video.mp4 --clips 8 --niche "business podcast"
  viralforge run URL --style clean --no-music --fps 60
  viralforge run URL --dry-run          # pick clips, render nothing
  viralforge trends analyze --samples my-analytics.csv --niche fitness
  viralforge doctor
""",
    )
    parser.add_argument("--version", action="version", version=f"viralforge {__version__}")
    parser.add_argument("-c", "--config", help="path to a viralforge.yaml")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="process a video end to end", parents=[common])
    run.add_argument("target", help="YouTube URL, any yt-dlp URL, or a local video file")
    run.add_argument("-n", "--clips", type=int, help="how many clips to produce")
    run.add_argument("-o", "--out", help="output directory")
    run.add_argument("--niche", help='e.g. "business podcast", "fitness"')
    run.add_argument("--platforms", help="comma-separated: tiktok,instagram,youtube_shorts")
    run.add_argument("--style", choices=["impact", "clean", "bold_box", "karaoke"],
                     help="caption style")
    run.add_argument("--min-duration", type=float)
    run.add_argument("--max-duration", type=float)
    run.add_argument("--fps", type=int, choices=[24, 30, 60])
    run.add_argument("--crf", type=int, help="quality, lower is better (default 19)")
    run.add_argument("--preset", help="x264 preset (default slow)")
    run.add_argument("--music", help="path to a background music bed")
    run.add_argument("--no-music", action="store_true")
    run.add_argument("--no-captions", action="store_true")
    run.add_argument("--no-reframe", action="store_true", help="keep the original framing")
    run.add_argument("--no-llm", action="store_true", help="heuristics only, no API calls")
    run.add_argument("--transcript", help="use an existing .srt/.vtt/.json transcript")
    run.add_argument("--no-desilence", action="store_true", help="keep the pauses")
    run.add_argument("--watermark", help="handle to burn into the top of the frame")
    run.add_argument("--voice", help='brand voice for captions, e.g. "blunt, lowercase"')
    run.add_argument("--model", help="Claude model id (default claude-opus-5)")
    run.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("--keep-intermediates", action="store_true")
    run.add_argument("--refresh-trends", action="store_true",
                     help="re-collect trend data instead of using the cached profile")
    run.add_argument("--dry-run", action="store_true",
                     help="score and select clips, but render nothing")

    trends = sub.add_parser("trends", help="inspect or rebuild the trend profile",
                            parents=[common])
    trends_sub = trends.add_subparsers(dest="trends_command")
    show = trends_sub.add_parser("show", help="print the active profile")
    show.add_argument("--niche")
    show.add_argument("--json", action="store_true")
    an = trends_sub.add_parser("analyze", help="build a profile from real post data")
    an.add_argument("--samples", help="JSON/JSONL/CSV of posts to learn from")
    an.add_argument("--provider", choices=["local", "file", "apify"])
    an.add_argument("--niche")
    an.add_argument("--platforms")
    an.add_argument("--out", help="where to save the profile (default trends.json)")
    an.add_argument("--no-llm", action="store_true")

    init = sub.add_parser("init", help="write a starter viralforge.yaml", parents=[common])
    init.add_argument("--path", default="viralforge.yaml")
    init.add_argument("--force", action="store_true")

    sub.add_parser("doctor", help="check that everything this needs is installed",
                   parents=[common])
    return parser


# --------------------------------------------------------------------------- #


def _apply_run_flags(cfg: Config, args: argparse.Namespace) -> None:
    if args.clips:
        cfg.output.clips = max(1, args.clips)
    if args.out:
        cfg.output.directory = args.out
    if args.niche:
        cfg.trends.niche = args.niche
    if args.platforms:
        cfg.trends.platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    if args.style:
        cfg.captions.style = args.style
    if args.min_duration:
        cfg.candidates.min_duration = args.min_duration
    if args.max_duration:
        cfg.candidates.max_duration = args.max_duration
    if args.fps:
        cfg.render.fps = args.fps
    if args.crf is not None:
        cfg.render.crf = args.crf
    if args.preset:
        cfg.render.preset = args.preset
    if args.music:
        cfg.audio.music_path = args.music
    if args.no_music:
        cfg.audio.music_path = None
    if args.no_captions:
        cfg.captions.enabled = False
    if args.no_reframe:
        cfg.reframe.enabled = False
    if args.no_llm:
        cfg.scoring.enable_llm = False
    if args.transcript:
        cfg.transcribe.external_path = args.transcript
    if args.no_desilence:
        cfg.audio.de_silence = False
    if args.watermark:
        cfg.render.watermark_text = args.watermark
    if args.voice:
        cfg.copy.voice = args.voice
    if args.model:
        cfg.scoring.model = args.model
        cfg.copy.model = args.model
    if args.effort:
        cfg.scoring.effort = args.effort
        cfg.copy.effort = args.effort
    if args.overwrite:
        cfg.output.overwrite = True
    if args.keep_intermediates:
        cfg.output.keep_intermediates = True
    if getattr(args, "verbose", False):
        cfg.verbose = True


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from .pipeline import Pipeline, PipelineError

    _apply_run_flags(cfg, args)
    try:
        result = Pipeline(cfg).run(args.target, dry_run=args.dry_run,
                                   refresh_trends=args.refresh_trends)
    except PipelineError as exc:
        error(str(exc))
        return 1
    except KeyboardInterrupt:
        error("Interrupted.")
        return 130
    except Exception as exc:
        error(f"{type(exc).__name__}: {exc}")
        if cfg.verbose:
            raise
        info("Re-run with --verbose for the full traceback.")
        return 1

    if args.dry_run:
        console.print("\n[bold]Dry run - nothing was rendered.[/bold]")
        return 0

    console.print(
        f"\n[bold green]Done.[/bold green] {len(result.deliverables)} clips in "
        f"[bold]{result.out_dir}[/bold] ({result.elapsed / 60:.1f} min)")
    if result.warnings:
        console.print(f"[yellow]{len(result.warnings)} warning(s) - see manifest.json[/yellow]")
    return 0


def cmd_trends(cfg: Config, args: argparse.Namespace) -> int:
    from .analyze.llm import LLMClient
    from .trends.analyze import resolve_profile
    from .trends.providers.local import build_baseline_profile

    command = getattr(args, "trends_command", None) or "show"
    if getattr(args, "niche", None):
        cfg.trends.niche = args.niche
    if getattr(args, "platforms", None):
        cfg.trends.platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]

    if command == "show":
        profile = build_baseline_profile(cfg.trends.niche, cfg.trends.platforms) \
            if cfg.trends.provider == "local" and not cfg.trends.profile_path \
            else resolve_profile(cfg, llm=None)
        if getattr(args, "json", False):
            print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        else:
            console.print(f"[bold]{profile.summary()}[/bold]\n")
            console.print(profile.prompt_block())
        return 0

    if command == "analyze":
        if args.provider:
            cfg.trends.provider = args.provider
        if args.samples:
            cfg.trends.provider = "file"
            cfg.trends.samples_path = args.samples
        cfg.trends.profile_path = args.out or cfg.trends.profile_path or "trends.json"
        llm = None if args.no_llm else LLMClient(model=cfg.scoring.model,
                                                 effort=cfg.scoring.effort)
        if llm is not None and not llm.available:
            warn("No Claude credentials - deriving the numeric half of the profile only.")
            llm = None
        try:
            profile = resolve_profile(cfg, llm=llm, force_refresh=True)
        except (ValueError, FileNotFoundError) as exc:
            error(str(exc))
            return 1
        console.print(f"\n[green]Saved[/green] {cfg.trends.profile_path}")
        console.print(profile.prompt_block())
        return 0

    error(f"Unknown trends command: {command}")
    return 1


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        error(f"{path} already exists - pass --force to overwrite.")
        return 1
    write_default_config(str(path))
    console.print(f"[green]Wrote[/green] {path}")
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    ok = True

    def check(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "[green]✓[/green]" if good else "[red]✗[/red]"
        console.print(f" {mark} {name}" + (f"  [dim]{detail}[/dim]" if detail else ""))
        ok = ok and good

    console.print("[bold]Required[/bold]")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    check("ffmpeg", bool(ffmpeg), ffmpeg or "install ffmpeg and put it on PATH")
    check("ffprobe", bool(ffprobe), ffprobe or "ships with ffmpeg")
    if ffmpeg:
        from .utils.ffmpeg import has_filter
        libass = has_filter("subtitles")
        check("libass (burned-in captions)", libass,
              "" if libass else "your ffmpeg build needs --enable-libass")
    try:
        import yt_dlp
        check("yt-dlp", True, getattr(yt_dlp.version, "__version__", ""))
    except ImportError:
        check("yt-dlp", False, "pip install yt-dlp")
    try:
        import numpy
        check("numpy", True, numpy.__version__)
    except ImportError:
        check("numpy", False, "pip install numpy")

    console.print("\n[bold]Optional[/bold]")
    try:
        import faster_whisper  # noqa: F401
        console.print(" [green]✓[/green] faster-whisper  [dim]word-level transcription[/dim]")
    except ImportError:
        console.print(" [yellow]-[/yellow] faster-whisper  [dim]not installed; will fall back "
                      "to YouTube captions. pip install 'viralforge\\[whisper]'[/dim]")
    try:
        import cv2  # noqa: F401
        console.print(" [green]✓[/green] opencv  [dim]speaker-aware reframing[/dim]")
    except ImportError:
        console.print(" [yellow]-[/yellow] opencv  [dim]not installed; reframing falls back to "
                      "motion tracking. pip install 'viralforge\\[vision]'[/dim]")

    from .analyze.llm import LLMClient
    client = LLMClient()
    if client.available:
        console.print(" [green]✓[/green] Claude credentials  [dim]clip judgement and "
                      "caption writing enabled[/dim]")
    else:
        console.print(" [yellow]-[/yellow] Claude credentials  [dim]set ANTHROPIC_API_KEY or "
                      "run `ant auth login`; without it, scoring is heuristics-only[/dim]")

    console.print("\n[bold]Fonts[/bold]")
    from .edit.render import _fonts_dir
    console.print(f" [dim]{_fonts_dir()}[/dim]")

    console.print("\n[bold green]Ready.[/bold green]" if ok else
                  "\n[bold red]Missing required components - see above.[/bold red]")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    argv = list(argv if argv is not None else sys.argv[1:])
    # `viralforge <url>` is the common case - let it work without typing `run`.
    known = {"run", "trends", "init", "doctor"}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv.insert(0, "run")
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        error(f"Config file not found: {args.config}")
        return 1
    except Exception as exc:
        error(f"Could not read config: {exc}")
        return 1
    cfg.verbose = cfg.verbose or bool(getattr(args, "verbose", False))

    handlers = {"run": cmd_run, "trends": cmd_trends, "init": cmd_init, "doctor": cmd_doctor}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
