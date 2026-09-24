"""Pick the editing style each clip needs, so nobody has to choose a level."""
from __future__ import annotations

import dataclasses

HYPE = {"funny", "shocking", "controversial", "drama"}
CALM = {"emotional", "serious", "wholesome", "motivational"}


def choose_level(category: str, duration: float, comedy: float = 0.0, energy: float = 0.5) -> tuple[str, str]:
    """(level, why). High-energy moments get the full treatment; heartfelt ones get a clean, polished
    edit, because shakes and flashes kill an emotional moment."""
    if category in CALM:
        return "professional", "heartfelt moment - clean, polished edit with music"
    if category in HYPE or comedy >= 0.4:
        return "extreme", "high-energy moment - fast cuts, shake, flashes and b-roll"
    if energy >= 0.65:
        return "extreme", "loud, fast moment - maximum retention edit"
    if duration <= 35:
        return "extreme", "short clip - maximum retention edit"
    return "professional", "longer talking clip - smooth, polished edit"


def tune(preset, category: str):
    """The same editing level, dressed for the moment: heartfelt clips get calm, sentence-case captions
    in longer phrases with no sound effects or shakes; everything else keeps the bold hype style."""
    if category in CALM:
        return dataclasses.replace(preset, mood="calm", uppercase=False, caption_words=max(preset.caption_words, 4),
                                   emphasis_pop=False, word_pops=False, impact_hits=False, shake=False,
                                   flash=False, sfx=False)
    return preset
