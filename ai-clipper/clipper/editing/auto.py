"""Pick the editing style each clip needs, so nobody has to choose a level."""
from __future__ import annotations

import dataclasses

HYPE = {"funny", "shocking", "controversial", "drama"}
CALM = {"emotional", "serious", "wholesome", "motivational"}


def choose_level(category: str, duration: float, comedy: float = 0.0, energy: float = 0.5) -> tuple[str, str]:
    """(level, why). The edit follows the content: high-energy moments get the full treatment,
    heartfelt ones a clean edit (shakes and flashes would kill them), teaching moments stay clear
    and uncluttered, talk gets a polished edit, and only the very loudest talk goes to maximum."""
    if category in CALM:
        return "professional", "heartfelt moment - clean, polished edit with music"
    if category in HYPE or comedy >= 0.4:
        return "extreme", "high-energy moment - fast cuts, shake, flashes and sound effects"
    if category == "educational":
        return "hard", "teaching moment - tight cuts and clear captions, no music to distract"
    if energy >= 0.85:
        return "extreme", "the loudest, most intense part of the video - maximum retention edit"
    if duration <= 25:
        return "extreme", "short, punchy clip - maximum retention edit"
    return "professional", "talking moment - smooth tracking, music and polished captions"


def tune(preset, category: str):
    """The same editing level, dressed for the moment: heartfelt clips get calm, sentence-case captions
    in longer phrases with no sound effects or shakes; everything else keeps the bold hype style."""
    if category in CALM:
        # emotional pauses carry the moment: only pauses longer than 0.8 s are cut
        pause = None if preset.max_pause is None else max(preset.max_pause, 0.8)
        return dataclasses.replace(preset, mood="calm", uppercase=False, caption_words=max(preset.caption_words, 4),
                                   max_pause=pause, speed=1.0,
                                   emphasis_pop=False, word_pops=False, impact_hits=False, shake=False,
                                   flash=False, sfx=False)
    return preset
