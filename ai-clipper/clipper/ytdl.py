"""Shared yt-dlp settings: the JavaScript runtime YouTube now requires, cookies, quiet logging."""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from .config import BUNDLE

_cookies: dict = {}
_configured_login = False
# Firefox first: on Windows, Chrome and Edge encrypt their cookies so other programs usually can't read them
BROWSERS = ("firefox", "edge", "chrome", "brave", "opera", "vivaldi", "chromium", "safari")
BOT_HINT = ("YouTube is blocking downloads from this internet connection (\"confirm you're not a bot\"). "
            "Fix it once: sign in to YouTube in Firefox and press the button again - or use \"Fix YouTube access\" "
            "in the app to upload your YouTube cookies.txt. You can also choose \"Use my videos\" and paste video "
            "files you already downloaded.")
EXPIRED_HINT = ("YouTube still asks to confirm you're not a bot even with your saved YouTube login - it has probably "
                "expired. Export a fresh cookies.txt from a browser where you're signed in to YouTube and upload it "
                "again under \"Fix YouTube access\".")
_cookie_file: Path | None = None


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
    """Remember the YouTube login to use: config settings first, then a cookies.txt you uploaded in the app."""
    global _configured_login, _cookie_file
    a = cfg["analysis"]
    _cookie_file = cfg.path("paths.db").parent / "youtube_cookies.txt"
    _cookies.clear()
    if a.get("cookies_from_browser"):
        _cookies["cookiesfrombrowser"] = (a["cookies_from_browser"],)
    if a.get("cookies_file"):
        _cookies["cookiefile"] = a["cookies_file"]
    elif _cookie_file.exists():
        _cookies["cookiefile"] = str(_cookie_file)
    _configured_login = bool(_cookies)
    from .media import ensure_ffmpeg_on_path

    try:
        ensure_ffmpeg_on_path(cfg.path("paths.db").parent / "bin")
    except OSError:
        pass  # only partial downloads of very long videos need it


def login_status() -> dict:
    if "cookiefile" in _cookies:
        return {"signed_in": True, "source": "cookies.txt"}
    if "cookiesfrombrowser" in _cookies:
        return {"signed_in": True, "source": _cookies["cookiesfrombrowser"][0]}
    return {"signed_in": False, "source": None}


def save_cookies(text: str) -> int:
    """Stores an uploaded cookies.txt (Netscape format) and starts using it. Returns the YouTube cookie count."""
    global _configured_login
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    yt = [ln for ln in lines if not ln.startswith("#") and ln.count("\t") >= 6
          and ("youtube.com" in ln.split("\t")[0] or "google.com" in ln.split("\t")[0])]
    if not yt:
        raise ValueError("That file has no YouTube cookies. Export cookies.txt while you're on youtube.com "
                         "and signed in.")
    if _cookie_file is None:
        raise RuntimeError("settings not loaded")
    _cookie_file.parent.mkdir(parents=True, exist_ok=True)
    _cookie_file.write_text("# Netscape HTTP Cookie File\n" + "\n".join(ln for ln in lines if not ln.startswith("# Netscape")) + "\n",
                            encoding="utf-8")
    _cookies.clear()
    _cookies["cookiefile"] = str(_cookie_file)
    _configured_login = True
    return len(yt)


def forget_cookies() -> None:
    global _configured_login
    if _cookie_file and _cookie_file.exists():
        _cookie_file.unlink()
    _cookies.pop("cookiefile", None)
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
            raise BotCheck(EXPIRED_HINT) from exc
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
