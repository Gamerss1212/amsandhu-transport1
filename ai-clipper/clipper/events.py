"""Progress reporting shared by the CLI and the web UI."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("clipper")

STEPS = ("trends", "discovery", "analysis", "editing")


@dataclass
class Event:
    kind: str  # "log" | "progress" | "clip" | "done" | "error" | "upload"
    step: str
    message: str = ""
    progress: float | None = None
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def to_dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "step": self.step, "message": self.message,
                "progress": self.progress, "data": self.data, "ts": self.ts}


class Reporter:
    """Fan-out of pipeline events to any number of listeners."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self.history: list[Event] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[Event], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def emit(self, event: Event) -> None:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self.history.append(event)
            self.history = self.history[-2000:]
            listeners = list(self._listeners)
        level = logging.ERROR if event.kind == "error" else logging.INFO
        if event.message:
            log.log(level, "[%s] %s", event.step, event.message)
        for fn in listeners:
            try:
                fn(event)
            except Exception:  # a broken listener must never stop the pipeline
                log.exception("event listener failed")

    def since(self, seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self.history if e.seq > seq]

    def info(self, step: str, message: str, **data) -> None:
        self.emit(Event("log", step, message, data=data))

    def progress(self, step: str, fraction: float, message: str = "") -> None:
        self.emit(Event("progress", step, message, progress=max(0.0, min(1.0, fraction))))

    def error(self, step: str, message: str) -> None:
        self.emit(Event("error", step, message))
