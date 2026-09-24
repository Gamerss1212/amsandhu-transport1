"""Jump-cut planning: which parts of the clip to keep, and mapping times onto the new timeline."""
from __future__ import annotations

import re

FILLERS = {"um", "uh", "uhm", "umm", "uhh", "erm", "er", "ah", "hmm", "mm", "mhm"}


def _bare(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def is_filler(word: str) -> bool:
    return _bare(word) in FILLERS


# short words people stumble on ("I I think", "the the"); a repeat of these is a stutter, not emphasis
STUTTER = {"i", "i'm", "the", "a", "an", "and", "but", "so", "to", "it", "it's", "that", "we", "you", "he", "she",
           "they", "is", "was", "in", "of", "my", "if", "like", "just", "this", "what", "when", "then", "for"}


def stutters(words: list[dict]) -> set[int]:
    """Indexes of stuttered repeats: the first of two identical short words in a row, said close together
    with no punctuation between (keeps "no, no, no" and "very very")."""
    out = set()
    for k in range(len(words) - 1):
        a, b = words[k], words[k + 1]
        if _bare(a["w"]) == _bare(b["w"]) and _bare(a["w"]) in STUTTER and b["s"] - a["e"] < 0.6 \
                and not re.search(r"[,.!?;:]$", a["w"]):
            out.add(k)
    return out


def keep_ranges(words: list[dict], start: float, end: float, max_pause: float | None,
                remove_fillers: bool) -> list[tuple[float, float]]:
    """Ranges (source seconds) to keep, cutting long pauses and filler words."""
    inside = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    if not inside or (max_pause is None and not remove_fillers):
        return [(start, end)]
    stuttered = stutters(inside) if remove_fillers else set()
    kept = [w for k, w in enumerate(inside) if not (remove_fillers and (is_filler(w["w"]) or k in stuttered))]
    if not kept:
        return [(start, end)]

    lead, tail = 0.08, 0.14  # keep a little air around words so cuts don't clip syllables
    first = start if kept[0] is inside[0] else max(start, kept[0]["s"] - lead)  # a stumble on the first word
    ranges: list[list[float]] = [[first, kept[0]["e"]]]
    for prev, cur in zip(kept, kept[1:]):
        gap = cur["s"] - prev["e"]
        dropped_filler = any(prev["e"] <= w["s"] < cur["s"] and w not in kept for w in inside) \
            if remove_fillers else False
        limit = max_pause if max_pause is not None else float("inf")
        if gap > limit or (dropped_filler and gap > 0.12):
            ranges[-1][1] = min(prev["e"] + tail, cur["s"])
            ranges.append([max(cur["s"] - lead, ranges[-1][1]), cur["e"]])
        else:
            ranges[-1][1] = cur["e"]
    ranges[-1][1] = end

    merged: list[list[float]] = []
    for r in ranges:  # drop slivers that would just flicker
        if merged and (r[1] - r[0] < 0.25 or r[0] - merged[-1][1] < 0.06):
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a > 0.05]


def remap(t: float, ranges: list[tuple[float, float]], speed: float = 1.0) -> float | None:
    """Source time -> output time, or None if `t` falls in a removed part."""
    offset = 0.0
    for a, b in ranges:
        if a <= t <= b:
            return (offset + t - a) / speed
        offset += b - a
    return None


def output_duration(ranges: list[tuple[float, float]], speed: float = 1.0) -> float:
    return sum(b - a for a, b in ranges) / speed


def remap_words(words: list[dict], ranges: list[tuple[float, float]], speed: float = 1.0) -> list[dict]:
    """Words whose middle survived the cuts, on the output timeline."""
    out = []
    for w in words:
        mid = remap((w["s"] + w["e"]) / 2, ranges, speed)
        if mid is None:
            continue
        s = remap(w["s"], ranges, speed)
        e = remap(w["e"], ranges, speed)
        if s is None:  # started inside a cut
            s = max(0.0, mid - 0.1)
        if e is None:
            e = s + 0.2
        out.append({**w, "s": round(s, 3), "e": round(max(e, s + 0.05), 3)})
    return out


def cut_points(ranges: list[tuple[float, float]], speed: float = 1.0, min_removed: float = 0.4) -> list[float]:
    """Output times of jump cuts that removed a noticeable chunk."""
    out, offset = [], 0.0
    for (a, b), (c, _) in zip(ranges, ranges[1:]):
        offset += b - a
        if c - b >= min_removed:
            out.append(offset / speed)
    return out
