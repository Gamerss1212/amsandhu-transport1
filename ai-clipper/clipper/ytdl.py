"""Shared yt-dlp settings: the JavaScript runtime YouTube now requires, cookies, quiet logging."""
from __future__ import annotations

import os
import shutil
from functools import lru_cache

from .config import BUNDLE

_cookies: dict = {}
_configured_login = False
BROWSERS = ("firefox", "edge", "chrome", "brave", "opera", "vivaldi", "chromium", "safari")
BOT_HINT = ("YouTube is asking to confirm you're not a bot. Open YouTube in your web browser (Firefox, Edge "
            "or Chrome) on this PC, make sure you are signed in, then try again. If it keeps happening, close "
            "the browser completely first, or set analysis.cookies_from_browser in config.yaml.")


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
    global _configured_login
    a = cfg["analysis"]
    _cookies.clear()
    if a.get("cookies_from_browser"):
        _cookies["cookiesfrombrowser"] = (a["cookies_from_browser"],)
    if a.get("cookies_file"):
        _cookies["cookiefile"] = a["cookies_file"]
    _configured_login = bool(_cookies)


def is_bot_check(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "not a bot" in msg or "sign in to confirm" in msg or "429" in msg


class BotCheck(RuntimeError):
    pass


def with_login_fallback(fn, log=None):
    """Runs fn(); if YouTube demands a login, retries with the YouTube login of each browser on this PC.

    The browser that works is remembered for the rest of the session."""
    try:
        return fn()
    except Exception as exc:
        if not is_bot_check(exc):
            raise
        if _configured_login:
            raise BotCheck(BOT_HINT) from exc
        first = exc
    for browser in BROWSERS:
        _cookies.clear()
        _cookies["cookiesfrombrowser"] = (browser,)
        try:
            result = fn()
        except Exception as exc:
            if log:
                log(f"YouTube login from {browser}: not usable ({str(exc).splitlines()[0][:90]})")
            continue
        if log:
            log(f"Using your YouTube login from {browser}")
        return result
    _cookies.clear()
    raise BotCheck(BOT_HINT) from first


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
