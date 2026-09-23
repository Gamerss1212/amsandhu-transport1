"""Read a font file's name and vertical size, so any caption font renders at the intended size.

The subtitle renderer sizes text by the font's full line height (OS/2 winAscent + winDescent), which
varies a lot between fonts: at the same font size Poppins letters are ~20% smaller than DejaVu Sans.
Caption sizes were tuned with DejaVu Sans, so other fonts are scaled to match it.
"""
from __future__ import annotations

import struct
from pathlib import Path

REFERENCE_EM = (1901 + 483) / 2048  # DejaVu Sans Bold


def font_info(path: Path) -> dict | None:
    try:
        data = path.read_bytes()
        num = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(num):
            tag, _, off, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            tables[tag.decode("latin-1")] = (off, length)
        upm = struct.unpack(">H", data[tables["head"][0] + 18: tables["head"][0] + 20])[0]
        os2 = tables["OS/2"][0]
        win_ascent, win_descent = struct.unpack(">HH", data[os2 + 74: os2 + 78])
        names: set[str] = set()
        off = tables["name"][0]
        _, count, str_off = struct.unpack(">HHH", data[off: off + 6])
        for i in range(count):
            pid, _, _, nid, length, noff = struct.unpack(">HHHHHH", data[off + 6 + 12 * i: off + 18 + 12 * i])
            if nid not in (1, 4, 16):  # family, full name, typographic family
                continue
            raw = data[off + str_off + noff: off + str_off + noff + length]
            names.add((raw.decode("utf-16-be", "ignore") if pid in (0, 3) else raw.decode("latin-1")).lower())
        return {"names": names, "em": (win_ascent + win_descent) / upm}
    except (KeyError, struct.error, OSError, ValueError):
        return None


def size_scale(font: str, files: list[Path]) -> float:
    """How much to enlarge font sizes so `font` looks as big as the reference font. 1.0 if unknown."""
    for f in files:
        info = font_info(f)
        if info and font.lower() in info["names"]:
            return max(0.7, min(1.6, info["em"] / REFERENCE_EM))
    return 1.0
