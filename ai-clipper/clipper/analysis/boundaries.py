"""The last word on a clip's edges: every clip starts at the start of a sentence and ends at the end of one,
and never cuts its first or last word (transcripts often give a word zero length, or end it early)."""
from __future__ import annotations

import re

_END = re.compile(r"[.!?…]['\")\]]*$")
JOIN = 0.5      # words closer than this belong to the same breath: cutting between them is audible
PAD_IN, PAD_OUT = 0.12, 0.35


def _ends(w: dict) -> bool:
    return bool(_END.search(w["w"].strip()))


def snap(words: list[dict], start: float, end: float, min_s: float, max_s: float) -> tuple[float, float, list[str]]:
    """(start, end, notes). Extends to the sentence edge when that still fits max_s (+5%), otherwise trims
    back to the last whole sentence when that still meets min_s; then pads so no word is clipped."""
    if not words or end <= start:
        return start, end, []
    notes = []
    inside = [k for k, w in enumerate(words)
              if w["s"] >= start - 0.05 and (w["s"] < end - 0.02 or w["e"] <= end + 0.05)]
    if not inside:
        return start, end, []
    a, b = inside[0], inside[-1]
    limit = max_s * 1.05 + 1.0
    # ---- end: finish the sentence, or step back to the last one that finished
    if not _ends(words[b]) and b + 1 < len(words) and words[b + 1]["s"] - words[b]["e"] < JOIN:
        fwd = next((k for k in range(b + 1, min(len(words), b + 80)) if _ends(words[k])), None)
        back = next((k for k in range(b - 1, a, -1) if _ends(words[k])), None)
        # a short fragment of the next sentence is dropped; a long unfinished one is finished
        short_tail = back is not None and words[b]["e"] - words[back]["e"] < 4.0 \
            and words[back]["e"] - words[a]["s"] >= min_s
        if fwd is not None and not short_tail and words[fwd]["e"] + PAD_OUT - words[a]["s"] <= limit:
            b = fwd
            notes.append("extended to finish the sentence")
        elif back is not None and words[back]["e"] - words[a]["s"] >= min_s:
            b = back
            notes.append("trimmed back to the last finished sentence")
        elif fwd is not None and words[fwd]["e"] + PAD_OUT - words[a]["s"] <= limit:
            b = fwd
            notes.append("extended to finish the sentence")
    # ---- start: begin at the start of a sentence
    if a > 0 and not _ends(words[a - 1]) and words[a]["s"] - words[a - 1]["e"] < JOIN:
        bk = next((k for k in range(a - 1, max(-1, a - 80), -1) if k == 0 or _ends(words[k - 1])), None)
        fw = next((k for k in range(a + 1, b) if _ends(words[k - 1])), None)
        if bk is not None and words[b]["e"] - words[bk]["s"] <= limit:
            a = bk
            notes.append("moved back to the start of the sentence")
        elif fw is not None and words[b]["e"] - words[fw]["s"] >= min_s:
            a = fw
            notes.append("moved forward to the next sentence")
    # ---- pads: never clip the first or last word (zero-length words included)
    prev_e = words[a - 1]["e"] if a > 0 else 0.0
    next_s = words[b + 1]["s"] if b + 1 < len(words) else words[b]["e"] + 5
    new_start = max(prev_e + 0.02, min(start if a == inside[0] else words[a]["s"], words[a]["s"] - PAD_IN))
    last_end = max(words[b]["e"], words[b]["s"] + 0.25)
    new_end = min(next_s - 0.02, max(last_end + PAD_OUT, end if b == inside[-1] else 0.0))
    new_end = max(new_end, last_end)
    return round(max(0.0, new_start), 3), round(new_end, 3), notes
