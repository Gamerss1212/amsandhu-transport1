"""Entry point for AIClipper.exe: first-run setup, then the web app in the browser. No keys needed."""
from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

from clipper.config import BUNDLE, ROOT


def first_run_setup() -> None:
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        if not (ROOT / dst).exists():
            shutil.copy(BUNDLE / src, ROOT / dst)
    for sub in ("music", "sfx", "broll", "fonts"):
        (ROOT / "assets" / sub).mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "trend_imports").mkdir(parents=True, exist_ok=True)


def main() -> None:
    from clipper.__main__ import main as cli

    first_run_setup()
    if len(sys.argv) == 1:
        port = pick_port()
        if port is None:  # this same version is already open: just show it
            webbrowser.open("http://127.0.0.1:8000")
            return
        sys.argv += ["serve", "--port", str(port)]
        threading.Timer(2.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        print("Keep this window open while you use AI Clipper. Close it to quit.\n")
    cli()


def _ask(url: str, method: str = "GET") -> dict | None:
    try:
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError:
        return {}
    except Exception:
        return None


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":  # like the server does; on Windows this flag would allow stealing a live port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> int | None:
    """An older AI Clipper left open on port 8000 would otherwise be what the browser shows (the old
    25-agent team, old pages). Close it, or run this version on the next free port."""
    from clipper import APP_VERSION

    if _free(8000):
        return 8000
    v = _ask("http://127.0.0.1:8000/api/version")
    if v and v.get("version") == APP_VERSION:
        print("AI Clipper is already open - showing it in your browser.")
        return None
    if v is not None:
        print("An older AI Clipper is still open - closing it so you get this version...")
        _ask("http://127.0.0.1:8000/api/shutdown", "POST")
        for _ in range(20):
            time.sleep(0.25)
            if _free(8000):
                return 8000
        print("Could not close the old copy: close its black window yourself when you can.")
    return next((p for p in range(8001, 8050) if _free(p)), 0)


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        traceback.print_exc()
        input("\nAI Clipper stopped because of the error above. Press Enter to close...")
        sys.exit(1)
