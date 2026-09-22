"""Provider interface: something that can hand back observed posts."""

from __future__ import annotations

from typing import List, Protocol

from ..profile import PostSample


class TrendProvider(Protocol):
    name: str

    def collect(self, niche: str, platforms: List[str], limit: int) -> List[PostSample]:
        ...
