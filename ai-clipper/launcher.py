"""Entry point for AIClipper.exe: first-run setup, then the web app in the browser."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser

from dotenv import load_dotenv

from clipper.config import BUNDLE, ROOT


def first_run_setup() -> None:
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        if not (ROOT / dst).exists():
            shutil.copy(BUNDLE / src, ROOT / dst)
    for sub in ("music", "sfx", "broll", "fonts"):
        (ROOT / "assets" / sub).mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "trend_imports").mkdir(parents=True, exist_ok=True)


def ensure_api_key() -> None:
    env = ROOT / ".env"
    load_dotenv(env)
    while not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nAI Clipper needs your API keys before it can run.")
        print(f"Opening {env} in Notepad. Paste your keys after the = signs, save, and close Notepad.")
        print("  ANTHROPIC_API_KEY  -> https://console.anthropic.com  (required)")
        print("  APIFY_TOKEN        -> https://apify.com  (TikTok/Instagram trend data)")
        print("  YOUTUBE_API_KEY    -> Google Cloud Console, YouTube Data API v3 (recommended)\n")
        if sys.platform == "win32":
            subprocess.run(["notepad.exe", str(env)])
        else:
            input("Edit the file, then press Enter...")
        load_dotenv(env, override=True)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            if input("No ANTHROPIC_API_KEY found. Try again? [Y/n] ").strip().lower() == "n":
                print("Starting anyway - clip picking will not work until a key is added.")
                return


def main() -> None:
    from clipper.__main__ import main as cli

    first_run_setup()
    if len(sys.argv) == 1:
        ensure_api_key()
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
