"""Shared yt-dlp settings: the JavaScript runtime YouTube now requires, cookies, quiet logging."""
from __future__ import annotations

import os
import shutil
from functools import lru_cache

from .config import BUNDLE

_cookies: dict = {}


class Silent:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


@lru_cache(maxsize=1)
def deno_path() -> str | None:
    """Bundled deno (exe build), then the pip `deno` package, then one on PATH."""
    bundled = BUNDLE / "denobin" / ("deno.exe" if os.name == "nt" else "deno")
    if bundled.exists():
        return str(bundled)
    try:
        from deno import find_deno_bin

        return find_deno_bin()
    except Exception:
        return shutil.which("deno")


def configure(cfg) -> None:
    """Remember the browser-login settings so every YouTube request can use them."""
    a = cfg["analysis"]
    _cookies.clear()
    if a.get("cookies_from_browser"):
        _cookies["cookiesfrombrowser"] = (a["cookies_from_browser"],)
    if a.get("cookies_file"):
        _cookies["cookiefile"] = a["cookies_file"]


def options(silent: bool = True, **extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, **_cookies}
    if deno := deno_path():
        opts["js_runtimes"] = {"deno": {"path": deno}}
    if silent:
        opts["logger"] = Silent()
    opts.update(extra)
    return opts


def ydl(silent: bool = True, **extra):
    import yt_dlp

    return yt_dlp.YoutubeDL(options(silent, **extra))
