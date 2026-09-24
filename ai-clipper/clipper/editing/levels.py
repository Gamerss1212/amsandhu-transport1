"""Editing levels, from a clean cut to everything the editor can do."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    reframe: str               # "center" | "face" | "face_smooth"
    captions: str              # "basic" | "pop" | "karaoke"
    caption_words: int         # words per caption group
    uppercase: bool
    max_pause: float | None    # silences longer than this are cut (None = keep)
    remove_fillers: bool       # cut "um", "uh", ...
    hook_overlay: bool         # big hook title for the first seconds
    zoom_punch: bool           # punch-in zooms on emphasis words
    pattern_zoom: bool         # alternate framing every sentence (keeps eyes busy)
    slow_push: bool            # slow continuous push-in
    progress_bar: bool
    color_grade: bool
    loudnorm: bool
    music: bool                # background music ducked under speech (needs assets/music)
    sfx: bool                  # whooshes on cuts (needs assets/sfx)
    broll_split: bool          # split screen with b-roll/gameplay (needs assets/broll)
    flash: bool                # white flash on the biggest moments
    speed: float
    crf: int
    x264_preset: str
    voice_enhance: bool = False  # de-noise, de-rumble, compress + presence boost on the voice
    vignette: bool = False       # darkened edges pull the eye to the speaker
    shake: bool = False          # quick camera shake on the punch-in words
    emphasis_pop: bool = False   # emphasis words grow and tilt in the captions
    word_pops: bool = False      # a soft "bloop" sound on key words
    impact_hits: bool = False    # sub-bass hit on the biggest moments
    reaction_zoom: bool = False  # punch in on laughs / the loudest reactions
    mood: str = "hype"           # caption personality: "hype" (bold, uppercase) | "calm" (soft, sentence case)


LEVELS: dict[str, Preset] = {
    "simple": Preset("simple", reframe="center", captions="basic", caption_words=6, uppercase=False,
                     max_pause=None, remove_fillers=False, hook_overlay=False, zoom_punch=False,
                     pattern_zoom=False, slow_push=False, progress_bar=False, color_grade=False,
                     loudnorm=True, music=False, sfx=False, broll_split=False, flash=False,
                     speed=1.0, crf=21, x264_preset="veryfast"),
    "normal": Preset("normal", reframe="face", captions="pop", caption_words=3, uppercase=True,
                     max_pause=0.8, remove_fillers=False, hook_overlay=True, zoom_punch=False,
                     pattern_zoom=False, slow_push=False, progress_bar=False, color_grade=False,
                     loudnorm=True, music=False, sfx=False, broll_split=False, flash=False,
                     speed=1.0, crf=20, x264_preset="fast"),
    "hard": Preset("hard", reframe="face", captions="karaoke", caption_words=3, uppercase=True,
                   max_pause=0.45, remove_fillers=True, hook_overlay=True, zoom_punch=True,
                   pattern_zoom=False, slow_push=False, progress_bar=False, color_grade=True,
                   loudnorm=True, music=False, sfx=False, broll_split=False, flash=False,
                   speed=1.0, crf=19, x264_preset="medium", voice_enhance=True,
                   emphasis_pop=True),
    "professional": Preset("professional", reframe="face_smooth", captions="karaoke", caption_words=3,
                           uppercase=True, max_pause=0.35, remove_fillers=True, hook_overlay=True,
                           zoom_punch=True, pattern_zoom=True, slow_push=False, progress_bar=True,
                           color_grade=True, loudnorm=True, music=True, sfx=True, broll_split=False,
                           flash=False, speed=1.03, crf=18, x264_preset="medium",
                           voice_enhance=True, vignette=True, emphasis_pop=True, word_pops=True,
                           reaction_zoom=True),
    "extreme": Preset("extreme", reframe="face_smooth", captions="karaoke", caption_words=2,
                      uppercase=True, max_pause=0.25, remove_fillers=True, hook_overlay=True,
                      zoom_punch=True, pattern_zoom=True, slow_push=True, progress_bar=True,
                      color_grade=True, loudnorm=True, music=True, sfx=True, broll_split=True,
                      flash=True, speed=1.07, crf=17, x264_preset="slow",
                      voice_enhance=True, vignette=True, shake=True, emphasis_pop=True, word_pops=True,
                      impact_hits=True, reaction_zoom=True),
}

LEVEL_NAMES = list(LEVELS)


def get_preset(level: str) -> Preset:
    try:
        return LEVELS[level.lower()]
    except KeyError:
        raise ValueError(f"unknown editing level {level!r}; choose one of {', '.join(LEVELS)}") from None
