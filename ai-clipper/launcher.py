"""Entry point for AIClipper.exe: first-run setup, then the web app in the browser. No keys needed."""
from __future__ import annotations

import shutil
import sys
import threading
import traceback
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
        sys.argv.append("serve")
        threading.Timer(2.5, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
        print("Keep this window open while you use AI Clipper. Close it to quit.\n")
    cli()


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        traceback.print_exc()
        input("\nAI Clipper stopped because of the error above. Press Enter to close...")
        sys.exit(1)
