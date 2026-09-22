"""Small text helpers shared by the analysis, editing and publishing stages."""

from __future__ import annotations

import re

HOOK_MAX_WORDS = 12
HOOK_MAX_CHARS = 64

_BREAKS = (". ", "? ", "! ", " - ", "—", ", ")


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def trim_to_words(text: str, max_words: int, max_chars: int) -> str:
    """Shorten to fit, always on a word boundary.

    Mid-word truncation ("...tell me I w") is the tell of a generated tool, and
    it shows up on screen and in the caption the operator pastes.
    """
    clean = collapse(text).strip(" -—")
    if not clean:
        return ""
    words = clean.split()
    if len(words) > max_words:
        words = words[:max_words]
    clean = " ".join(words)
    while len(clean) > max_chars and " " in clean:
        clean = clean.rsplit(" ", 1)[0]
    return clean.rstrip(" ,;:.-")


def trim_hook(text: str, max_words: int = HOOK_MAX_WORDS,
              max_chars: int = HOOK_MAX_CHARS) -> str:
    """A hook card is a card, not a sentence - keep it readable at a glance."""
    clean = collapse(text).strip(" -—")
    if not clean:
        return ""
    for mark in _BREAKS:
        head = clean.split(mark)[0]
        if 12 <= len(head) <= max_chars:
            clean = head
            break
    return trim_to_words(clean, max_words, max_chars)


def title_case_label(text: str, max_words: int = 8, max_chars: int = 60) -> str:
    """A short human label for a clip, used in filenames and listings."""
    return trim_to_words(text, max_words, max_chars)
