"""Platform-safe text: TikTok/Instagram quietly limit reach for explicit words shown on screen or in
captions, so clippers star them out (the audio is left as it is)."""
from __future__ import annotations

import re

_EXPLICIT = (
    r"(mother)?f+u+c+k+(s|ed|er|ers|ing|in|in'|ery|face|faces|head|heads|wit|wits|tard|boy|boys)?|"
    r"motherfuckers?|(bull|horse|dip|chicken)?shit(s|ty|tier|tiest|ting|ted|head|heads|show|shows|hole|holes|"
    r"load|faced)?|cock(s|sucker|suckers)?|dick(s|head|heads)?|puss(y|ies)|cunts?|"
    r"bitch(es|ing|y|ass|asses)?|ass(hole|holes|hat|hats)|whores?|sluts?|slutty|nigg(a|as|er|ers|uh)|fags?|"
    r"faggots?|cum|porno?|"
    r"retard(s|ed)?"
)
_PATTERN = re.compile(rf"\b({_EXPLICIT})\b", re.I)


def _mask(m: re.Match) -> str:
    word = m.group(0)
    masked = word[0] + re.sub(r"[aeiou]", "*", word[1:], flags=re.I)
    return masked if "*" in masked else word[0] + "*" * (len(word) - 1)


def censor(text: str) -> str:
    """'what the fuck' -> 'what the f*ck'. Leaves words like 'Dickens' or 'cocktail' alone."""
    return _PATTERN.sub(_mask, text) if text else text


_EMBEDDED = re.compile(_EXPLICIT.replace("(mother)?", "").replace("|cum|", "|"), re.I)


def clean_tag(tag: str) -> bool:
    """False for hashtags with an explicit word anywhere inside (#slutmobile), which get posts limited."""
    return not _EMBEDDED.search(tag)
