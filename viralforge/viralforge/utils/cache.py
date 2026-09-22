"""A tiny on-disk JSON cache so re-runs skip work already done."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")


class Cache:
    def __init__(self, root: str, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str, suffix: str = ".json") -> Path:
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        return self.root / f"{safe}{suffix}"

    def dir_for(self, key: str) -> Path:
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        d = self.root / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_json(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        p = self.path_for(key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set_json(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        p = self.path_for(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def memoize_json(self, key: str, produce: Callable[[], Any]) -> Any:
        cached = self.get_json(key)
        if cached is not None:
            return cached
        value = produce()
        self.set_json(key, value)
        return value
