"""Word-level animated captions, rendered as ASS and burned in by libass.

Short-form captions are not subtitles.  They are a second visual track that
carries the pacing: a small group of words on screen at once, the word being
spoken highlighted on the frame it is spoken, and a short scale-pop on the
first frame of each card.  That is what the ``impact`` style does; the others
dial it back.

One card produces one Dialogue event per word (each showing the whole card with
a different word highlighted).  That is more events than a subtitle file would
have, but libass handles thousands of them without complaint and it is the only
way to get per-word colour without a compositing pass.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import Config
from ..models import CaptionWord
from ..utils.text import trim_hook

# Masked rather than dropped: the audio still says it, so a bleep-style mask
# keeps the caption honest while staying advertiser-safe.
PROFANITY = {
    "fuck", "fucking", "fucked", "fucker", "shit", "shitty", "bitch", "bastard",
    "cunt", "dick", "pussy", "asshole", "motherfucker", "bullshit", "goddamn",
}

STYLE_PRESETS: Dict[str, Dict[str, object]] = {
    "impact": {"bold": True, "outline": 7, "shadow": 3, "highlight": True, "box": False,
               "pop": True, "uppercase": True},
    "clean": {"bold": True, "outline": 4, "shadow": 1, "highlight": False, "box": False,
              "pop": False, "uppercase": False},
    "bold_box": {"bold": True, "outline": 2, "shadow": 0, "highlight": True, "box": True,
                 "pop": True, "uppercase": True},
    "karaoke": {"bold": True, "outline": 6, "shadow": 2, "highlight": True, "box": False,
                "pop": False, "uppercase": True},
}


def ass_color(rgb: str, alpha: str = "00") -> str:
    """RRGGBB -> &HAABBGGRR, the byte order ASS actually uses."""
    value = rgb.strip().lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def ass_inline_color(rgb: str) -> str:
    """&Hbbggrr& - the form the inline \\c override tag expects."""
    return f"&H{ass_color(rgb)[4:]}&"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def clean_word(raw: str, uppercase: bool, censor: bool) -> str:
    text = raw.strip()
    text = re.sub(r"\s+", " ", text)
    if censor:
        bare = re.sub(r"[^a-z']", "", text.lower())
        if bare in PROFANITY and len(bare) > 2:
            keep = text[0]
            tail = text[-1] if text[-1].isalpha() else ""
            text = keep + "*" * max(1, len(bare) - 1 - len(tail)) + tail
    return text.upper() if uppercase else text


# Relative advance widths for a bold humanist sans (DejaVu Sans Bold, Inter and
# Helvetica Bold all sit within a few percent of these).  Measuring the real
# font would mean a freetype dependency for a job that only needs to know
# whether a line fits, and being a few percent wrong costs nothing here - the
# margin absorbs it.
_ADVANCE = {
    " ": 0.32, "i": 0.33, "j": 0.35, "l": 0.34, "I": 0.38, "t": 0.47, "f": 0.44,
    "r": 0.50, "!": 0.36, ".": 0.34, ",": 0.34, "'": 0.30, '"': 0.48, ":": 0.36,
    ";": 0.36, "|": 0.33, "(": 0.42, ")": 0.42, "-": 0.42, "1": 0.58,
    "m": 0.98, "M": 0.94, "W": 0.92, "w": 0.86, "@": 1.05, "%": 1.02,
}
_DEFAULT_UPPER = 0.74
_DEFAULT_LOWER = 0.63
_DEFAULT_DIGIT = 0.64


def estimate_width(text: str, font_size: int) -> float:
    """Approximate rendered width of ``text`` in pixels."""
    total = 0.0
    for ch in text:
        if ch in _ADVANCE:
            total += _ADVANCE[ch]
        elif ch.isdigit():
            total += _DEFAULT_DIGIT
        elif ch.isupper():
            total += _DEFAULT_UPPER
        elif ch.isalpha():
            total += _DEFAULT_LOWER
        else:
            total += 0.55
    return total * font_size


def layout_card(rendered: Sequence[str], font_size: int, max_width: float,
                max_lines: int) -> Tuple[List[List[int]], int]:
    """Break one card's words into lines that fit, shrinking only if forced.

    Returns the word indices per line and a percentage scale (100 = no shrink).
    """
    if not rendered:
        return [], 100
    space = estimate_width(" ", font_size)

    def wrap(scale: float):
        limit = max_width / scale
        lines = [[]]
        width = 0.0
        for i, word in enumerate(rendered):
            w = estimate_width(word, font_size)
            if w > limit:                      # one word wider than the whole line
                return None
            gap = space if lines[-1] else 0.0
            if lines[-1] and width + gap + w > limit:
                if len(lines) >= max_lines:
                    return None
                lines.append([i])
                width = w
            else:
                lines[-1].append(i)
                width += gap + w
        return [line for line in lines if line]

    def balance(lines, scale: float):
        """Even out a two-line card - a 5-word/1-word split reads badly."""
        if len(lines) != 2 or len(lines[0]) < 2:
            return lines
        limit = max_width / scale

        def width_of(line):
            return (sum(estimate_width(rendered[i], font_size) for i in line)
                    + space * max(0, len(line) - 1))

        best = lines
        best_delta = abs(width_of(lines[0]) - width_of(lines[1]))
        first, second = list(lines[0]), list(lines[1])
        while len(first) > 1:
            second = [first.pop()] + second
            if width_of(second) > limit:
                break
            delta = abs(width_of(first) - width_of(second))
            if delta < best_delta:
                best, best_delta = [list(first), list(second)], delta
            else:
                break
        return best

    for scale in (1.0, 0.92, 0.84, 0.76, 0.68):
        lines = wrap(scale)
        if lines is not None:
            return balance(lines, scale), int(round(scale * 100))
    # Nothing fits even at the smallest scale.  Spread every word across the
    # lines we have rather than dropping any - a clipped word is a bug the
    # viewer sees, an overfull line is only ugly.
    per_line = max(1, -(-len(rendered) // max_lines))
    spread = [list(range(i, min(i + per_line, len(rendered))))
              for i in range(0, len(rendered), per_line)]
    return spread[:max_lines], 68


def group_into_cards(words: Sequence[CaptionWord], max_words: int, max_chars: int,
                     max_gap: float = 0.75, font_size: int = 0,
                     max_width: float = 0.0, max_lines: int = 2,
                     uppercase: bool = False, censor: bool = False) -> List[List[CaptionWord]]:
    """Break the word stream into on-screen cards.

    Cards break on sentence punctuation, on a real pause, or when they no
    longer fit the frame - in that order of priority, so a card never straddles
    a full stop just to use up horizontal space.
    """
    cards: List[List[CaptionWord]] = []
    current: List[CaptionWord] = []
    chars = 0

    def fits(candidate: Sequence[CaptionWord]) -> bool:
        if not (font_size and max_width):
            return True
        rendered = [clean_word(w.text, uppercase, censor) for w in candidate]
        rendered = [r for r in rendered if r]
        _, scale = layout_card(rendered, font_size, max_width, max_lines)
        return scale >= 84          # shrinking past this looks inconsistent

    for i, word in enumerate(words):
        text = word.text.strip()
        if not text:
            continue
        would_be = chars + len(text) + (1 if current else 0)
        too_long = len(current) >= max_words or would_be > max_chars
        if current and (too_long or not fits(current + [word])):
            cards.append(current)
            current, chars = [], 0
            would_be = len(text)
        current.append(word)
        chars = would_be

        ends_sentence = bool(re.search(r"[.!?][\'\")\]]?$", text))
        gap = words[i + 1].start - word.end if i + 1 < len(words) else 0.0
        if current and (ends_sentence or gap >= max_gap):
            cards.append(current)
            current, chars = [], 0

    if current:
        cards.append(current)
    return cards


def build_ass(words: Sequence[CaptionWord], cfg: Config, duration: float,
              hook_text: str = "", hook_duration: float = 0.0,
              watermark: str = "") -> str:
    """Full ASS document for one clip."""
    render = cfg.render
    cap = cfg.captions
    preset = STYLE_PRESETS.get(cap.style, STYLE_PRESETS["impact"])
    uppercase = cap.uppercase and bool(preset["uppercase"])

    primary = ass_color(cap.primary_color)
    highlight = ass_color(cap.highlight_color)
    outline_col = ass_color(cap.outline_color)
    back = ass_color("000000", "40" if preset["box"] else "A0")
    border_style = 3 if preset["box"] else 1
    bold = -1 if preset["bold"] else 0

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {render.width}
PlayResY: {render.height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{cap.font},{cap.font_size},{primary},{highlight},{outline_col},{back},{bold},0,0,0,100,100,0,0,{border_style},{cap.outline},{cap.shadow},5,60,60,60,1
Style: Hook,{cap.font},{int(cap.font_size * 1.12)},{primary},{highlight},{outline_col},{ass_color('000000', '30')},-1,0,0,0,100,100,0,0,1,{cap.outline + 1},{cap.shadow},5,80,80,80,1
Style: Mark,{cap.font},{int(cap.font_size * 0.42)},{ass_color('FFFFFF', '50')},{primary},{ass_color('000000', '60')},{back},0,0,0,0,100,100,0,0,1,2,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: List[str] = []
    cx = render.width // 2
    cy = int(render.height * cap.position)

    if hook_text and hook_duration > 0 and render.hook_card:
        hook_event = _hook_event(hook_text, hook_duration, cfg, uppercase)
        if hook_event:
            events.append(hook_event)

    if cap.enabled and words:
        events.extend(_caption_events(words, cfg, preset, uppercase, cx, cy, duration))

    if watermark:
        events.append(
            f"Dialogue: 0,{ass_time(0.0)},{ass_time(duration)},Mark,,0,0,0,,"
            f"{{\\pos({cx},{int(render.height * 0.055)})}}{ass_escape(watermark)}"
        )

    return header + "\n".join(events) + "\n"


def _hook_event(text: str, hook_duration: float, cfg: Config, uppercase: bool) -> str:
    render = cfg.render
    size = int(cfg.captions.font_size * 1.12)
    max_width = render.width * cfg.captions.max_width_ratio
    text = trim_hook(text)
    if not text:
        return ""
    body = ass_escape(text.upper() if uppercase else text)
    lines, scale = layout_card(body.split(), size, max_width, 3)
    words = body.split()
    body = "\\N".join(" ".join(words[i] for i in line) for line in lines if line)
    x, y = render.width // 2, int(render.height * 0.30)
    tags = [f"\\pos({x},{y})", "\\fad(120,160)"]
    if scale < 100:
        tags.append(f"\\fscx{scale}\\fscy{scale}")
    else:
        tags.append("\\t(0,180,\\fscx106\\fscy106)")
    return (f"Dialogue: 1,{ass_time(0.0)},{ass_time(hook_duration)},Hook,,0,0,0,,"
            f"{{{''.join(tags)}}}{body}")


def _caption_events(words: Sequence[CaptionWord], cfg: Config, preset: Dict[str, object],
                    uppercase: bool, cx: int, cy: int, duration: float) -> List[str]:
    cap = cfg.captions
    max_width = cfg.render.width * cap.max_width_ratio
    cards = group_into_cards(list(words), cap.max_words_per_card, cap.max_chars_per_card,
                             font_size=cap.font_size, max_width=max_width,
                             max_lines=cap.max_lines_per_card, uppercase=uppercase,
                             censor=cap.censor_profanity)
    highlight = ass_inline_color(cap.highlight_color)
    primary = ass_inline_color(cap.primary_color)
    events: List[str] = []

    for index, card in enumerate(cards):
        rendered = [clean_word(w.text, uppercase, cap.censor_profanity) for w in card]
        keep = [(w, r) for w, r in zip(card, rendered) if r]
        if not keep:
            continue
        card = [w for w, _ in keep]
        rendered = [r for _, r in keep]

        lines, scale = layout_card(rendered, cap.font_size, max_width,
                                   cap.max_lines_per_card)
        card_start = min(max(card[0].start, 0.0), duration)
        # Hold each card a beat past its last word so it does not flicker - but
        # never past the next card's first word, or both render at once and the
        # overlap is unreadable.
        next_start = cards[index + 1][0].start if index + 1 < len(cards) else duration
        card_end = min(duration, max(card[-1].end + 0.14, card_start + 0.28))
        card_end = min(card_end, max(next_start - 0.01, card_start + 0.05))
        # Multi-line cards grow upward from the same baseline, so the block
        # stays put instead of jumping as line counts change.
        y = cy - int((len(lines) - 1) * cap.font_size * 0.58 * 0.5)
        scale_tag = f"\\fscx{scale}\\fscy{scale}" if scale < 100 else ""

        def compose(active: int) -> str:
            out_lines = []
            for line in lines:
                parts = []
                for j in line:
                    escaped = ass_escape(rendered[j])
                    if j == active:
                        parts.append(f"{{\\c{highlight}}}{escaped}{{\\c{primary}}}")
                    else:
                        parts.append(escaped)
                out_lines.append(" ".join(parts))
            return "\\N".join(out_lines)

        if not preset["highlight"]:
            events.append(
                f"Dialogue: 0,{ass_time(card_start)},{ass_time(card_end)},Caption,,0,0,0,,"
                f"{{\\pos({cx},{y})\\fad(50,50){scale_tag}}}{compose(-1)}")
            continue

        for i, word in enumerate(card):
            start = word.start if i else card_start
            end = card[i + 1].start if i + 1 < len(card) else card_end
            # build_ass is called with word timings from the planner, but it is
            # public - clamp rather than trusting the caller to have done it.
            start = min(max(start, 0.0), duration)
            end = min(max(end, 0.0), duration)
            if end <= start:
                if start >= duration - 1e-6:
                    continue
                end = min(duration, start + 0.08)
            tags = [f"\\pos({cx},{y})"]
            if scale_tag:
                tags.append(scale_tag)
            if i == 0:
                tags.append("\\fad(60,0)")
                if preset["pop"] and not scale_tag:
                    pop = max(100, min(140, cap.pop_scale))
                    tags.append(f"\\fscx{pop}\\fscy{pop}\\t(0,110,\\fscx100\\fscy100)")
            elif word.emphasis and preset["pop"] and not scale_tag:
                tags.append("\\fscx104\\fscy104\\t(0,90,\\fscx100\\fscy100)")
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,"
                f"{{{''.join(tags)}}}{compose(i)}")
    return events


# --------------------------------------------------------------------------- #


def srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words: Sequence[CaptionWord], max_words: int = 8, max_chars: int = 42) -> str:
    """A plain SRT alongside each clip, for platform upload or repurposing."""
    cards = group_into_cards(list(words), max_words, max_chars)
    blocks: List[str] = []
    for i, card in enumerate(cards, 1):
        text = " ".join(w.text.strip() for w in card if w.text.strip())
        if not text:
            continue
        blocks.append(f"{i}\n{srt_time(card[0].start)} --> {srt_time(card[-1].end)}\n{text}\n")
    return "\n".join(blocks)
