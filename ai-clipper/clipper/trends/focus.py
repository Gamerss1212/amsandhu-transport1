"""Your focus: the niche you want most (e.g. motivation from successful people).

It comes from config.yaml (`focus:`), and the Trends page can change it (saved in data/focus.json).
Everything that ranks - the live scan, the creator scout and the YouTube search - leans toward it by `weight`;
other niches are still covered.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FIELDS = ("name", "weight", "keywords", "creators", "searches", "short_searches", "tiktok_accounts",
          "instagram_accounts")


def _file(cfg) -> Path:
    return cfg.path("paths.db").parent / "focus.json"


def load(cfg) -> dict:
    f = dict(cfg.get("focus") or {})
    p = _file(cfg)
    if p.exists():
        try:
            f.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if k in FIELDS})
        except (OSError, ValueError):
            pass
    f.setdefault("name", "")
    f["weight"] = max(0.0, min(1.0, float(f.get("weight", 0.6) or 0)))
    for k in FIELDS[2:]:
        f[k] = [str(x).strip() for x in (f.get(k) or []) if str(x).strip()]
    return f


def save(cfg, changes: dict) -> dict:
    cur = load(cfg)
    for k in FIELDS:
        if k in changes and changes[k] is not None:
            cur[k] = changes[k]
    p = _file(cfg)
    p.write_text(json.dumps({k: cur[k] for k in FIELDS}, indent=2), encoding="utf-8")
    return load(cfg)


def people(focus: dict) -> list[tuple[str, list[str]]]:
    """[(display name, aliases)] from entries like "Gary Vee|GaryVee|Gary Vaynerchuk"."""
    out = []
    for entry in focus.get("creators", []):
        names = [n.strip() for n in str(entry).split("|") if n.strip()]
        if names:
            out.append((names[0], names))
    return out


def keyword_hits(text: str, focus: dict) -> int:
    low = (text or "").lower()
    return sum(1 for k in focus.get("keywords", []) if re.search(rf"\b{re.escape(k.lower())}\b", low))


def fit(text: str, focus: dict) -> float:
    """0-1: how clearly a caption or title is about your focus (2+ keywords = clearly)."""
    if not focus.get("keywords"):
        return 0.0
    return min(1.0, keyword_hits(text, focus) / 2)
