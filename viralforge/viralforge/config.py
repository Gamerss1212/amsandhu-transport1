"""Configuration: layered defaults -> YAML file -> environment -> CLI flags."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_NAMES = ("viralforge.yaml", "viralforge.yml", ".viralforge.yaml")


@dataclass
class IngestConfig:
    # yt-dlp format selector.  Capped at 1080p by default: clips are delivered at
    # 1080x1920, so a 4K source costs a lot of decode time for no visible gain.
    format: str = "bestvideo[height<=1080][vcodec!*=av01]+bestaudio/best[height<=1080]/best"
    cookies_from_browser: Optional[str] = None   # e.g. "chrome" for age-gated videos
    cookies_file: Optional[str] = None
    proxy: Optional[str] = None
    prefer_youtube_subs: bool = False            # skip Whisper when subs exist
    max_source_duration: float = 4 * 3600.0


@dataclass
class TranscribeConfig:
    backend: str = "auto"            # auto | faster-whisper | youtube-subs
    model: str = "small.en"          # tiny.en | base.en | small.en | medium.en | large-v3
    device: str = "auto"             # auto | cpu | cuda
    compute_type: str = "auto"       # auto | int8 | float16
    language: Optional[str] = None
    vad_filter: bool = True
    beam_size: int = 5
    # Skip transcription entirely and use a transcript you already have
    # (.srt, .vtt, .json3, or a ViralForge transcript .json).
    external_path: Optional[str] = None


@dataclass
class CandidateConfig:
    min_duration: float = 15.0
    max_duration: float = 75.0
    target_duration: float = 34.0
    # Candidates are generated on a sliding window before scoring.
    window_stride: float = 3.0
    max_candidates: int = 220
    # Reject clips that start or end mid-sentence unless the model insists.
    snap_to_sentence: bool = True


@dataclass
class ScoringConfig:
    model: str = "claude-opus-5"
    effort: str = "high"             # low | medium | high | xhigh | max
    batch_size: int = 12             # candidates per LLM request
    shortlist_size: int = 40         # heuristic prefilter before the LLM sees them
    heuristic_weight: float = 0.3    # blend of measured signal vs model judgement
    enable_llm: bool = True
    max_output_tokens: int = 16000
    use_server_fallbacks: bool = True


@dataclass
class TrendsConfig:
    provider: str = "local"          # local | file | apify
    platforms: List[str] = field(default_factory=lambda: ["tiktok", "instagram"])
    niche: str = ""                  # free text, e.g. "fitness", "business podcast"
    profile_path: Optional[str] = None
    sample_limit: int = 120
    max_age_days: int = 21
    apify_token_env: str = "APIFY_TOKEN"
    apify_tiktok_actor: str = "clockworks~tiktok-scraper"
    apify_instagram_actor: str = "apify~instagram-scraper"
    samples_path: Optional[str] = None   # for provider=file


@dataclass
class ReframeConfig:
    enabled: bool = True
    mode: str = "auto"               # auto | face | center | motion | none
    # How hard the camera follows the subject.  Lower = calmer.
    smoothing: float = 0.12          # 0..1 exponential factor per keyframe
    keyframe_rate: float = 8.0       # tracking keyframes per second
    max_pan_speed: float = 340.0     # source px/second - stops whip-pans
    deadzone: float = 0.045          # ignore subject motion below this (frac of width)
    headroom: float = 0.36           # put the face this far down the frame (0=top)
    safe_margin: float = 0.02
    detect_fps: float = 4.0          # frames/second analysed for faces
    # OpenCV 5 dropped the bundled Haar cascades; the YuNet model it uses
    # instead is a 340 KB download, cached under ~/.cache/viralforge.
    download_models: bool = True
    # A "punch-in" re-frames tighter on an emphasis beat.  Implemented as a new
    # shot rather than an animated zoom - see CropWindow's docstring.
    punch_in: bool = True
    punch_in_zoom: float = 1.18
    punch_in_min_gap: float = 3.5
    punch_in_max_per_clip: int = 6
    punch_in_duration: float = 2.2
    # Hard ceiling on how much the crop may be upscaled to reach the output
    # height. 1080p source -> 9:16 crop is already ~1.8x; past ~2.4x the
    # softness is visible on a phone.
    max_upscale: float = 2.4


@dataclass
class CaptionConfig:
    enabled: bool = True
    style: str = "impact"            # impact | clean | bold_box | karaoke
    font: str = "DejaVu Sans"
    font_size: int = 86
    max_words_per_card: int = 4
    max_chars_per_card: int = 30
    # Captions may use this fraction of the frame width before they wrap.
    max_width_ratio: float = 0.88
    max_lines_per_card: int = 2
    position: float = 0.66           # vertical centre as a fraction of height
    primary_color: str = "FFFFFF"    # RRGGBB
    highlight_color: str = "FFE100"
    outline_color: str = "000000"
    outline: int = 7
    shadow: int = 3
    uppercase: bool = True
    pop_scale: int = 112             # % scale of the pop-in animation
    censor_profanity: bool = True


@dataclass
class AudioConfig:
    loudness_target: float = -14.0   # LUFS, the platform norm
    true_peak: float = -1.5
    loudness_range: float = 11.0
    highpass_hz: int = 80
    de_silence: bool = True
    silence_threshold_db: float = -34.0
    min_silence: float = 0.32        # gaps shorter than this are left alone
    keep_padding: float = 0.08       # breathing room kept around each cut
    music_path: Optional[str] = None
    music_gain_db: float = -22.0
    duck_db: float = -9.0


@dataclass
class RenderConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 19
    preset: str = "slow"
    audio_bitrate: str = "256k"
    progress_bar: bool = True
    progress_bar_height: int = 9
    progress_bar_color: str = "FFFFFF"
    hook_card: bool = True
    hook_duration: float = 1.6
    watermark_text: str = ""
    grade: bool = True               # light contrast/saturation lift
    threads: int = 0                 # 0 = ffmpeg decides
    intermediate_crf: int = 16


@dataclass
class CopyConfig:
    model: str = "claude-opus-5"
    effort: str = "high"
    variants: int = 3
    hashtags_per_post: int = 6
    include_cta: bool = True
    voice: str = ""                  # free text: "blunt, no emojis, lowercase"
    max_output_tokens: int = 8000


@dataclass
class OutputConfig:
    directory: str = "out"
    clips: int = 5
    min_gap_between_clips: float = 10.0
    overwrite: bool = False
    keep_intermediates: bool = False
    write_plan: bool = True


@dataclass
class Config:
    ingest: IngestConfig = field(default_factory=IngestConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    trends: TrendsConfig = field(default_factory=TrendsConfig)
    reframe: ReframeConfig = field(default_factory=ReframeConfig)
    captions: CaptionConfig = field(default_factory=CaptionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    work_dir: str = ".viralforge-cache"
    verbose: bool = False

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg = cls()
        data = _read_yaml(path) if path else _read_nearest_yaml()
        if data:
            cfg.apply(data)
        cfg.apply_env()
        return cfg

    def apply(self, data: Dict[str, Any]) -> None:
        _merge_into(self, data)

    def apply_env(self) -> None:
        """Environment overrides use VF_<SECTION>_<KEY>, e.g. VF_RENDER_FPS=60."""
        for section_field in fields(self):
            section = getattr(self, section_field.name)
            if not is_dataclass(section):
                continue
            for f in fields(section):
                env = f"VF_{section_field.name.upper()}_{f.name.upper()}"
                if env in os.environ:
                    setattr(section, f.name, _coerce(os.environ[env], getattr(section, f.name)))

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = {sf.name: getattr(v, sf.name) for sf in fields(v)} if is_dataclass(v) else v
        return out


def _read_yaml(path: str) -> Dict[str, Any]:
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _read_nearest_yaml() -> Dict[str, Any]:
    here = Path.cwd()
    for directory in [here, *here.parents]:
        for name in DEFAULT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return _read_yaml(str(candidate))
    return {}


def _merge_into(target: Any, data: Dict[str, Any]) -> None:
    for key, value in (data or {}).items():
        if not hasattr(target, key):
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge_into(current, value)
        else:
            setattr(target, key, value if current is None else _coerce_value(value, current))


def _coerce_value(value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, float) and isinstance(value, (int, float)):
        return float(value)
    if isinstance(current, int) and not isinstance(current, bool) and isinstance(value, (int, float)):
        return int(value)
    return value


def _coerce(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def write_default_config(path: str) -> None:
    """Emit a fully commented starter config."""
    Path(path).write_text(_DEFAULT_YAML, encoding="utf-8")


_DEFAULT_YAML = """\
# ViralForge configuration.  Everything here is optional - these are the
# defaults, shown so you can see what is tunable.  Env overrides use
# VF_<SECTION>_<KEY>, e.g. VF_RENDER_FPS=60.

output:
  directory: out
  clips: 5                 # how many finished clips per source video
  min_gap_between_clips: 10.0

ingest:
  # Cap at 1080p - delivery is 1080x1920, so 4K only costs decode time.
  format: "bestvideo[height<=1080][vcodec!*=av01]+bestaudio/best[height<=1080]/best"
  # cookies_from_browser: chrome   # needed for age-restricted / members-only
  prefer_youtube_subs: false       # true = skip Whisper when captions exist

transcribe:
  backend: auto            # auto | faster-whisper | youtube-subs
  model: small.en          # base.en is faster; large-v3 is the most accurate

candidates:
  min_duration: 15.0
  max_duration: 75.0
  target_duration: 34.0

scoring:
  model: claude-opus-5
  effort: high
  enable_llm: true         # false = heuristics only, no API key needed
  heuristic_weight: 0.3    # 0 = trust the model entirely, 1 = measurements only

trends:
  provider: local          # local | file | apify
  platforms: [tiktok, instagram]
  niche: ""                # e.g. "business podcast", "fitness", "gaming"
  # provider: file
  # samples_path: my-analytics.json
  # provider: apify        # needs APIFY_TOKEN in the environment

reframe:
  enabled: true
  mode: auto               # auto | face | center | motion | none
  smoothing: 0.12          # lower = calmer camera
  punch_in: true           # cut tighter on emphasis beats

captions:
  enabled: true
  style: impact            # impact | clean | bold_box | karaoke
  font: "DejaVu Sans"
  font_size: 86
  max_words_per_card: 4
  max_width_ratio: 0.88    # wrap before captions reach the frame edge
  highlight_color: FFE100
  uppercase: true

audio:
  loudness_target: -14.0   # LUFS - the platform norm
  de_silence: true         # trim dead air, which is most of the pacing win
  # music_path: assets/bed.mp3
  # music_gain_db: -22.0

render:
  width: 1080
  height: 1920
  fps: 30
  crf: 19
  preset: slow
  progress_bar: true
  hook_card: true
  # watermark_text: "@yourhandle"

copy:
  model: claude-opus-5
  variants: 3
  hashtags_per_post: 6
  voice: ""                # e.g. "blunt, lowercase, no emojis"
"""
