"""A small self-learning memory, so every run brings new, different clips.

- Remembers every moment it has made and never makes the same one twice.
- Learns from what you delete: kinds of clips / channels / lengths you remove get ranked lower.
- Pushes for variety: a kind of clip it has made a lot of lately has to be better to win again.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path

RECENT = 30


def length_bucket(seconds: float) -> str:
    return "short" if seconds < 30 else "medium" if seconds < 55 else "long"


class Brain:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        for k in ("made", "removed"):
            self.data.setdefault(k, [])
        self.data.setdefault("weights", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _features(category: str, channel: str, duration: float) -> list[str]:
        return [f"cat:{category or 'other'}", f"len:{length_bucket(duration)}"] + \
               ([f"chan:{channel.lower()}"] if channel else [])

    def already_made(self, video_id: str, start: float, end: float) -> bool:
        for m in self.data["made"]:
            if m["video"] == video_id and min(end, m["end"]) - max(start, m["start"]) > 0.5 * (end - start):
                return True
        return False

    def adjust(self, category: str, channel: str, duration: float, batch: Counter | None = None) -> float:
        """Score points to add: learned taste (+-15) and a variety push."""
        w = self.data["weights"]
        taste = sum(w.get(f, 0.0) for f in self._features(category, channel, duration))
        recent = Counter(m.get("category") for m in self.data["made"][-RECENT:])
        share = recent[category] / max(1, min(RECENT, len(self.data["made"])))
        variety = -8.0 * max(0.0, share - 0.3)  # more than ~30% of recent clips were this kind
        if batch:  # and spread the kinds inside one run too
            variety -= 3.0 * batch[category]
        return max(-15.0, min(15.0, taste)) + variety

    def record_made(self, video_id: str, start: float, end: float, category: str, channel: str,
                    name: str = "") -> None:
        with self.lock:
            self.data["made"].append({"video": video_id, "start": round(start, 2), "end": round(end, 2),
                                      "category": category, "channel": channel, "name": name, "ts": time.time()})
            self.data["made"] = self.data["made"][-5000:]
            for f in self._features(category, channel, end - start):  # making it is a small vote for it
                self.data["weights"][f] = self.data["weights"].get(f, 0.0) + 0.3
            self._save()

    def learn_removed(self, meta: dict) -> None:
        """You deleted a clip: that is a strong vote against clips like it."""
        clip, src = meta.get("clip", {}), meta.get("source", {})
        dur = float(meta.get("duration") or (clip.get("end", 0) - clip.get("start", 0)) or 40)
        with self.lock:
            for f in self._features(clip.get("category", "other"), src.get("channel") or "", dur):
                self.data["weights"][f] = self.data["weights"].get(f, 0.0) - 3.0
            self.data["removed"].append({"title": clip.get("title"), "category": clip.get("category"),
                                         "channel": src.get("channel"), "ts": time.time()})
            self.data["removed"] = self.data["removed"][-2000:]
            self._save()

    def summary(self) -> dict:
        w = self.data["weights"]
        liked = sorted((k for k, v in w.items() if v > 0 and k.startswith("cat:")), key=lambda k: -w[k])[:3]
        disliked = sorted((k for k, v in w.items() if v < 0), key=lambda k: w[k])[:3]
        return {"made": len(self.data["made"]), "removed": len(self.data["removed"]),
                "likes": [k.split(":", 1)[1] for k in liked], "avoids": [k.split(":", 1)[1] for k in disliked]}
