"""Animated captions as an ASS subtitle file (burned in by ffmpeg/libass)."""
from __future__ import annotations

import re

from .timeline import is_filler

W, H = 1080, 1920


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).replace("\n", " ").strip()


def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def group_words(words: list[dict], per_group: int, max_gap: float = 0.6) -> list[list[dict]]:
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= per_group or w["s"] - cur[-1]["e"] > max_gap
                    or re.search(r"[.!?]$", cur[-1]["w"])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_srt(words: list[dict], per_group: int = 6) -> str:
    """Plain subtitles for re-uploading captions (YouTube Shorts, Instagram, editors)."""
    def t(x: float) -> str:
        ms = int(round(max(0.0, x) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        sec, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    words = [w for w in words if not is_filler(w["w"])]
    groups = group_words(words, per_group)
    blocks = []
    for i, g in enumerate(groups, 1):
        end = g[-1]["e"] + 0.1
        if i < len(groups):  # never overlap the next line (players would show two at once)
            end = min(end, groups[i][0]["s"])
        blocks.append(f"{i}\n{t(g[0]['s'])} --> {t(max(end, g[0]['s'] + 0.05))}\n{' '.join(clean(w['w']) for w in g)}\n")
    return "\n".join(blocks)


def build_vtt(words: list[dict], per_group: int = 6) -> str:
    """WebVTT subtitles; when speakers are known each line carries a voice tag (<v Speaker A>)."""
    def t(x: float) -> str:
        ms = int(round(max(0.0, x) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        sec, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"
    words = [w for w in words if not is_filler(w["w"])]
    groups: list[list[dict]] = []
    for g in group_words(words, per_group):  # a new speaker always starts a new line
        cur: list[dict] = []
        for w in g:
            if cur and w.get("spk") != cur[-1].get("spk"):
                groups.append(cur)
                cur = []
            cur.append(w)
        groups.append(cur)
    out = ["WEBVTT", ""]
    for i, g in enumerate(groups):
        end = g[-1]["e"] + 0.1
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0]["s"])
        text = " ".join(clean(w["w"]) for w in g)
        spk = g[0].get("spk")
        out += [f"{t(g[0]['s'])} --> {t(max(end, g[0]['s'] + 0.05))}",
                f"<v Speaker {spk}>{text}" if spk else text, ""]
    return "\n".join(out)


def build_labeled_srt(words: list[dict], per_group: int = 6) -> str:
    """SRT with a speaker label whenever the speaker changes ("A: ..."), for editors and accessibility."""
    blocks = build_vtt(words, per_group).split("\n\n")[1:]
    out = []
    for n, b in enumerate((b for b in blocks if b.strip()), 1):
        timing, text = b.split("\n", 1)
        text = re.sub(r"^<v Speaker (\w+)>", r"\1: ", text)
        out.append(f"{n}\n{timing.replace('.', ',')}\n{text}\n")
    return "\n".join(out)


def hook_time(hook: str) -> float:
    """Long enough to read the hook: ~0.28 s per word, between 2.2 and 4 s."""
    return min(4.0, max(2.2, 0.8 + 0.28 * len(hook.split())))


def hook_fit(hook: str, size: int, max_words: int = 12) -> str:
    """Keep the hook card to about 3 lines: long hooks are shortened and set smaller."""
    words = clean(hook).upper().split()
    if len(words) > max_words:
        words = words[:max_words]
        while len(words) > 5 and _norm(words[-1]) in {"a", "an", "the", "for", "of", "to", "in", "on", "and", "but",
                                                       "or", "with", "my", "your", "his", "her", "is", "was"}:
            words.pop()
        words[-1] = words[-1].rstrip(",.;:!?") + "..."
    n = len(words)
    scale = 1.0 if n <= 6 else 0.86 if n <= 9 else 0.74
    return (f"{{\\fs{round(size * scale)}}}" if scale < 1 else "") + " ".join(words)


def build_ass(words: list[dict], style: str, per_group: int, uppercase: bool, font: str,
              accent: str, highlight: str, emphasis: set[str], duration: float,
              hook: str | None = None, caption_y: int = 1380, hook_seconds: float | None = None,
              hook_y: int = 250,
              size_scale: float = 1.0, emphasis_pop: bool = False, mood: str = "hype") -> str:
    """style: 'basic' | 'pop' | 'karaoke'. size_scale evens out differences between fonts.
    mood 'calm' (heartfelt moments): smaller sentence-case text that fades in, a soft warm highlight
    and no bouncing - big bouncy captions would undercut an emotional moment."""
    words = [w for w in words if not is_filler(w["w"])]
    big = style != "basic"
    calm = mood == "calm"
    if calm:
        highlight = "#FFE7A3"
    size = round((92 if big else 70) * size_scale * (0.82 if calm else 1.0))
    hook_size = round(74 * size_scale)
    outline = 7 if big else 5
    margin_v = H - caption_y
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,{outline},3,2,70,70,{margin_v},1
Style: Hook,{font},{hook_size},&H00000000,&H00000000,{ass_color(accent)},{ass_color(accent)},-1,0,0,0,100,100,0,0,3,18,0,8,80,80,{hook_y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    def add(start: float, end: float, text: str, style_name: str = "Cap", layer: int = 0) -> None:
        end = min(end, duration)
        if end - start >= 0.02:
            events.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style_name},,0,0,0,,{text}")

    def fmt(w: dict) -> str:
        t = clean(w["w"])
        return t.upper() if uppercase else t

    groups = group_words(words, per_group)
    for gi, g in enumerate(groups):
        g_start = g[0]["s"]
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else duration
        g_end = nxt if nxt - g[-1]["e"] < 0.35 else g[-1]["e"] + 0.15  # no flicker between groups

        def colored(w: dict) -> str:
            text = fmt(w)
            if _norm(w["w"]) not in emphasis:
                return text
            if emphasis_pop:  # key words are bigger, tilted and in the accent colour
                return (f"{{\\c{ass_color(accent)}\\fscx122\\fscy122\\frz3}}{text}"
                        f"{{\\c&H00FFFFFF&\\fscx100\\fscy100\\frz0}}")
            return f"{{\\c{ass_color(accent)}}}{text}{{\\c&H00FFFFFF&}}"

        if style == "basic":
            add(g_start, g_end, " ".join(fmt(w) for w in g))
        elif style == "pop":
            pop = "{\\fad(120,0)}" if calm else "{\\fscx70\\fscy70\\t(0,80,\\fscx112\\fscy112)\\t(80,150,\\fscx100\\fscy100)}"
            add(g_start, g_end, pop + " ".join(colored(w) for w in g))
        else:  # karaoke: the word being spoken lights up and pops
            for wi, w in enumerate(g):
                w_start = g_start if wi == 0 else w["s"]
                w_end = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
                parts = []
                for wj, other in enumerate(g):
                    if wj == wi:
                        grow = 100 if calm else 112
                        parts.append(f"{{\\c{ass_color(highlight)}\\fscx{grow}\\fscy{grow}}}{fmt(other)}"
                                     f"{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    else:
                        parts.append(colored(other))
                intro = ("{\\fad(150,0)}" if calm else "{\\fscx80\\fscy80\\t(0,70,\\fscx100\\fscy100)}") \
                    if wi == 0 else ""
                add(w_start, w_end, intro + " ".join(parts))

    if hook:
        # hook card drops in with an overshoot, holds, then fades
        anim = "{\\fad(80,250)\\fscx60\\fscy60\\frz-4\\t(0,160,\\fscx108\\fscy108\\frz2)\\t(160,260,\\fscx100\\fscy100\\frz0)}"
        add(0.0, min(hook_seconds or hook_time(hook), duration), anim + hook_fit(hook, hook_size), "Hook", layer=1)
    return header + "\n".join(events) + "\n"
