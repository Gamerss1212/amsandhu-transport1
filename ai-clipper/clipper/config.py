"""Configuration loading: defaults <- config.yaml <- environment (.env)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = yaml.safe_load((ROOT / "config.example.yaml").read_text())


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(dict):
    """A dict with helpers for dotted lookups and project-relative paths."""

    def get_path(self, dotted: str) -> Any:
        node: Any = self
        for part in dotted.split("."):
            node = node[part]
        return node

    def path(self, dotted: str) -> Path:
        p = Path(self.get_path(dotted))
        return p if p.is_absolute() else ROOT / p

    @property
    def anthropic_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None

    @property
    def youtube_key(self) -> str | None:
        return os.environ.get("YOUTUBE_API_KEY") or None

    @property
    def apify_token(self) -> str | None:
        return os.environ.get("APIFY_TOKEN") or None


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    user = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg = Config(_deep_merge(DEFAULTS, user or {}))
    for key in ("paths.work_dir", "paths.output_dir"):
        cfg.path(key).mkdir(parents=True, exist_ok=True)
    cfg.path("paths.db").parent.mkdir(parents=True, exist_ok=True)
    return cfg
