"""Command line entry point.

  python -m clipper            start the web app (the one-button UI)
  python -m clipper run        run the whole pipeline once from the terminal
  python -m clipper watch      watch channels for new uploads in real time
  python -m clipper doctor     check keys, tools and settings
"""
from __future__ import annotations

import argparse
import logging
import time

from .config import load_config
from .editing import LEVEL_NAMES


def main() -> None:
    parser = argparse.ArgumentParser(prog="clipper", description="Fully automated AI clipper")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="start the web app (default)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    run = sub.add_parser("run", help="run the pipeline once")
    run.add_argument("--level", choices=LEVEL_NAMES)
    watch = sub.add_parser("watch", help="poll watched channels for new uploads")
    watch.add_argument("--auto-clip", action="store_true", help="run the pipeline when a new upload appears")
    watch.add_argument("--level", choices=LEVEL_NAMES)
    sub.add_parser("doctor", help="check the setup")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    cmd = args.cmd or "serve"

    if cmd == "serve":
        import uvicorn

        from .web.app import create_app

        print(f"AI Clipper running at http://{getattr(args, 'host', '127.0.0.1')}:{getattr(args, 'port', 8000)}")
        uvicorn.run(create_app(cfg), host=getattr(args, "host", "127.0.0.1"),
                    port=getattr(args, "port", 8000), log_level="warning")
    elif cmd == "run":
        from .pipeline import Pipeline

        clips = Pipeline(cfg).run(args.level)
        for c in clips:
            print(f"{c['score']:>5}  {cfg.path('paths.output_dir') / c['folder'] / c['video']}")
    elif cmd == "watch":
        from .discovery import poll_watchlist
        from .events import Reporter
        from .pipeline import Pipeline

        rep = Reporter()
        pipe = Pipeline(cfg, rep)
        if not cfg["discovery"]["watch_channels"]:
            raise SystemExit("Add channel IDs to discovery.watch_channels in config.yaml first")
        interval = cfg["discovery"]["watch_interval_minutes"] * 60
        print(f"Watching {len(cfg['discovery']['watch_channels'])} channels every "
              f"{cfg['discovery']['watch_interval_minutes']} min (Ctrl+C to stop)")
        while True:
            new = poll_watchlist(cfg, pipe.db, rep)
            if new and args.auto_clip:
                try:
                    pipe.run(args.level)
                except Exception as exc:
                    logging.error("run failed: %s", exc)
            time.sleep(interval)
    elif cmd == "doctor":
        from .doctor import doctor

        raise SystemExit(0 if doctor(cfg) else 1)


if __name__ == "__main__":
    main()
