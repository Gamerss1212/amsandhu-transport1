import re

import pytest

from viralforge.config import Config
from viralforge.edit.captions import (
    ass_color, ass_escape, ass_inline_color, build_ass, build_srt, clean_word,
    estimate_width, group_into_cards, layout_card,
)
from viralforge.models import CaptionWord


def words_from(text: str, step: float = 0.30) -> list:
    return [CaptionWord(text=w, start=i * step, end=i * step + step * 0.85)
            for i, w in enumerate(text.split())]


def parse_events(ass: str):
    def secs(stamp: str) -> float:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            out.append((parts[3], secs(parts[1]), secs(parts[2]), parts[9]))
    return out


def test_colour_byte_order_is_bgr():
    assert ass_color("FFE100") == "&H0000E1FF"      # &HAABBGGRR
    assert ass_inline_color("FFE100") == "&H00E1FF&"


def test_escapes_ass_control_characters():
    assert ass_escape("{drop}") == "\\{drop\\}"
    assert ass_escape("a\\b") == "a\\\\b"


def test_captions_never_overlap_in_time():
    """Two cards on screen at once is unreadable - a regression guard."""
    cfg = Config()
    words = words_from("And I started asking whether I could survive being wrong about it")
    events = [e for e in parse_events(build_ass(words, cfg, 8.0)) if e[0] == "Caption"]
    assert events
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            assert not (a[1] < b[2] - 1e-6 and b[1] < a[2] - 1e-6), f"{a} overlaps {b}"


def test_cards_fit_the_frame_width():
    cfg = Config()
    limit = cfg.render.width * cfg.captions.max_width_ratio
    words = words_from("Extraordinarily complicated terminology absolutely everywhere now")
    cards = group_into_cards(words, cfg.captions.max_words_per_card,
                             cfg.captions.max_chars_per_card,
                             font_size=cfg.captions.font_size, max_width=limit,
                             max_lines=cfg.captions.max_lines_per_card, uppercase=True)
    for card in cards:
        rendered = [clean_word(w.text, True, False) for w in card]
        lines, scale = layout_card(rendered, cfg.captions.font_size, limit,
                                   cfg.captions.max_lines_per_card)
        for line in lines:
            text = " ".join(rendered[i] for i in line)
            assert estimate_width(text, cfg.captions.font_size) * scale / 100 <= limit + 1


def test_layout_never_drops_words():
    """Dropping a word is a bug the viewer sees; an ugly line is not."""
    rendered = ["SUPERCALIFRAGILISTICEXPIALIDOCIOUS", "ANTIDISESTABLISHMENTARIANISM",
                "PNEUMONOULTRAMICROSCOPIC"]
    lines, _ = layout_card(rendered, 86, 400, 2)
    assert sorted(i for line in lines for i in line) == [0, 1, 2]


def test_every_word_reaches_the_screen():
    cfg = Config()
    words = words_from("one two three four five six seven eight nine ten eleven twelve")
    ass = build_ass(words, cfg, 8.0)
    shown = " ".join(re.sub(r"\{[^}]*\}", "", e[3]) for e in parse_events(ass)
                     if e[0] == "Caption")
    for w in words:
        assert w.text.upper() in shown


def test_profanity_is_masked_not_dropped():
    assert clean_word("shit", False, True) == "s**t"
    assert clean_word("shit", False, False) == "shit"
    assert clean_word("ship", False, True) == "ship"


def test_highlight_steps_through_each_word():
    cfg = Config()
    words = words_from("stop doing this now")
    events = [e for e in parse_events(build_ass(words, cfg, 4.0)) if e[0] == "Caption"]
    highlighted = [e for e in events if ass_inline_color(cfg.captions.highlight_color) in e[3]]
    assert len(highlighted) == len(words)


def test_clean_style_has_no_per_word_events():
    cfg = Config()
    cfg.captions.style = "clean"
    words = words_from("stop doing this now")
    events = [e for e in parse_events(build_ass(words, cfg, 4.0)) if e[0] == "Caption"]
    assert len(events) < len(words)


def test_srt_covers_every_word_in_order():
    words = words_from("the quick brown fox jumps over the lazy dog today")
    srt = build_srt(words)
    body = " ".join(l for l in srt.splitlines() if l and "-->" not in l and not l.isdigit())
    assert body.split() == [w.text for w in words]


def test_events_stay_inside_the_clip():
    cfg = Config()
    words = words_from("one two three four five six", step=0.5)
    duration = 2.0
    for style, start, end, _ in parse_events(build_ass(words, cfg, duration)):
        assert start >= 0.0 and end <= duration + 1e-6
