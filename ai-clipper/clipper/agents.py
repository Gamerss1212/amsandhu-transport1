"""The agent team: 25 specialised agents that work at the same time and share one brain.

Each agent is a worker slot with a role. The pipeline hands work to a free agent of the right role;
agents of different roles run side by side (scouts search while downloaders fetch, listeners
transcribe, judges score and editors render), and they all read and write the same shared memory:
the database (trends, processed videos, uploads) and the brain (what was made, what you removed,
which posting times worked). The board is what the app shows live.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

# (role, display name, how many agents, what they do)
ROLES = [
    ("trend", "Trend scout", 2, "Studies hundreds of short videos to learn what is going viral right now"),
    ("scout", "Video scout", 3, "Searches for long-form videos worth clipping"),
    ("download", "Downloader", 3, "Fetches videos (or just the audio of very long ones)"),
    ("listen", "Listener", 3, "Transcribes every word, at high speed"),
    ("audio", "Audio analyst", 2, "Finds laughter, reactions, energy and dead air"),
    ("judge", "Moment judge", 2, "Scores every moment and picks the strongest standalone clips"),
    ("hook", "Hook writer", 1, "Writes the on-screen hook, caption and hashtags"),
    ("director", "Style director", 1, "Chooses the edit each clip needs"),
    ("editor", "Editor", 3, "Cuts, reframes, captions and mixes the clips"),
    ("review", "Reviewer", 2, "Watches every finished clip and fixes problems"),
    ("publish", "Publisher", 1, "Posts clips to TikTok and Instagram"),
    ("timing", "Timing analyst", 1, "Learns the best time to post from your own results"),
    ("brain", "Brain curator", 1, "Keeps the shared memory: what worked, what you removed"),
]
TEAM_SIZE = sum(n for _, _, n, _ in ROLES)


def parallel_videos(cfg) -> int:
    """How many videos are watched at the same time: listening is the heavy part, so it scales with
    the machine (an NVIDIA GPU or many cores lets several listeners work at once)."""
    n = cfg["analysis"].get("parallel_videos", "auto")
    if n in (None, "auto"):
        cores = os.cpu_count() or 4
        n = 1 if cores < 8 else 2 if cores < 16 else 3
    return max(1, min(3, int(n)))


class AgentBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots = {role: [None] * n for role, _, n, _ in ROLES}
        self._done = {role: 0 for role, _, _, _ in ROLES}
        self._names = {role: name for role, name, _, _ in ROLES}
        self._about = {role: about for role, _, _, about in ROLES}

    @contextmanager
    def work(self, role: str, task: str):
        """Run a piece of work as one of this role's agents (it shows as busy on the board)."""
        with self._lock:
            slots = self._slots[role]
            k = next((i for i, s in enumerate(slots) if s is None), None)
            if k is not None:
                slots[k] = (task[:90], time.time())
        try:
            yield
        finally:
            with self._lock:
                if k is not None:
                    self._slots[role][k] = None
                self._done[role] += 1

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            for role, slots in self._slots.items():
                for i, s in enumerate(slots):
                    out.append({"role": role, "name": f"{self._names[role]} {i + 1}", "about": self._about[role],
                                "busy": s is not None, "task": s[0] if s else "",
                                "for": round(time.time() - s[1]) if s else 0, "done": self._done[role]})
            return out

    def busy(self) -> int:
        with self._lock:
            return sum(1 for slots in self._slots.values() for s in slots if s is not None)


BOARD = AgentBoard()  # one team per app, shared by every part of it
