"""Collect an answer key for judge backtests: YouTube "most replayed" graphs + captions.

usage: python tools/collect_ground_truth.py OUT_DIR [N_VIDEOS]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipper import ytdl  # noqa: E402

QUERIES = ["podcast full episode", "comedy podcast full episode", "interview podcast episode",
           "business podcast full episode", "celebrity interview full", "true crime podcast episode",
           "sports podcast full episode", "motivational podcast episode"]


def main() -> None:
    out = Path(sys.argv[1])
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    out.mkdir(parents=True, exist_ok=True)
    pool: dict[str, dict] = {}
    for q in QUERIES:
        with ytdl.ydl(extract_flat="in_playlist", skip_download=True, ignoreerrors=True) as y:
            info = y.extract_info(f"ytsearch40:{q}", download=False) or {}
        for e in info.get("entries") or []:
            if e and e.get("id") and 1200 <= (e.get("duration") or 0) <= 10800 and (e.get("view_count") or 0) >= 1e6:
                pool.setdefault(e["id"], e)
    print(f"{len(pool)} candidate videos", flush=True)
    got = 0
    for vid, e in sorted(pool.items(), key=lambda kv: -kv[1]["view_count"]):
        if got >= want:
            break
        try:
            with ytdl.ydl(skip_download=True, writesubtitles=True, writeautomaticsub=True, subtitleslangs=["en"],
                          subtitlesformat="json3", outtmpl=str(out / "%(id)s.%(ext)s")) as y:
                info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
        except Exception as exc:
            print(f"skip {vid}: {str(exc)[:120]}", flush=True)
            time.sleep(5)
            continue
        subs = list(out.glob(f"{vid}*.json3"))
        if not info or not info.get("heatmap") or not subs:
            for f in subs:
                f.unlink()
            print(f"skip {vid}: heatmap={bool(info and info.get('heatmap'))} subs={len(subs)}", flush=True)
            continue
        manual = "en" in (info.get("subtitles") or {})
        slim = {k: info.get(k) for k in ("id", "title", "channel", "duration", "view_count", "heatmap")}
        slim["manual_captions"] = manual
        (out / f"{vid}.info.json").write_text(json.dumps(slim), encoding="utf-8")
        subs[0].rename(out / f"{vid}.json3")
        got += 1
        print(f"ok {got}/{want} {vid} manual_captions={manual} {slim['title'][:60]}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
