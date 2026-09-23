"""Backtest the built-in judge against real viewer data.

For every video in DATA_DIR (VIDEO.json3 captions + VIDEO.info.json with YouTube's "most replayed"
graph), the judge picks its top clips from the words alone - it never sees the graph - and each pick
is scored by how replayed that part of the video really was.

usage: python tools/backtest_judge.py DATA_DIR [TOP_K]
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipper.analysis.local_judge import find_windows  # noqa: E402
from clipper.analysis.signals import heatmap_curve, to_percentiles  # noqa: E402
from clipper.analysis.transcribe import group_segments, parse_json3  # noqa: E402


def window_pct(pct: np.ndarray, s: float, e: float) -> float:
    return float(np.mean(pct[int(s):max(int(s) + 1, int(e))]))


def evaluate(data: Path, top_k: int = 5, min_s: float = 20, max_s: float = 75, seed: int = 0) -> dict:
    rng = random.Random(seed)
    rows = []
    for info_path in sorted(data.glob("*.info.json")):
        info = json.loads(info_path.read_text(encoding="utf-8"))
        vid = info["id"]
        dur = int(info["duration"]) + 1
        pct = to_percentiles(heatmap_curve(info, dur))
        t = parse_json3(data / f"{vid}.json3")
        segs = group_segments(t["words"])
        wins = find_windows(segs, {"pct": {}}, None, min_s, max_s, top_n=top_k)
        if pct is None or not wins:
            continue
        picked = [window_pct(pct, w["start"], w["end"]) for w in wins]
        # baseline: random sentence-aligned clips of the same lengths
        starts = [s["s"] for s in segs if s["s"] < dur - max_s]
        rand = [window_pct(pct, st, st + (w["end"] - w["start"]))
                for _ in range(200) for w in wins[:1] for st in [rng.choice(starts)]]
        # "hit": the pick overlaps one of the video's 10 most-replayed 30-second stretches
        best = sorted(range(0, dur - 30, 10), key=lambda s: -window_pct(pct, s, s + 30))
        peaks: list[int] = []
        for s in best:
            if all(abs(s - p) >= 60 for p in peaks):
                peaks.append(s)
            if len(peaks) >= 10:
                break
        hits = sum(any(w["start"] < p + 30 and w["end"] > p for p in peaks) for w in wins)
        rows.append({"id": vid, "title": info["title"][:50], "manual": info.get("manual_captions"),
                     "judge": float(np.mean(picked)), "random": float(np.mean(rand)), "hit_rate": hits / len(wins)})
    return {"videos": rows,
            "judge": float(np.mean([r["judge"] for r in rows])) if rows else 0.0,
            "random": float(np.mean([r["random"] for r in rows])) if rows else 0.0,
            "hit_rate": float(np.mean([r["hit_rate"] for r in rows])) if rows else 0.0}


def main() -> None:
    data = Path(sys.argv[1])
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    res = evaluate(data, k)
    for r in res["videos"]:
        print(f"{r['judge'] * 100:5.1f} vs random {r['random'] * 100:5.1f}  hits {r['hit_rate'] * 100:3.0f}%  "
              f"{'manual' if r['manual'] else 'auto  '}  {r['title']}")
    print(f"\n{len(res['videos'])} videos | judge picks replay percentile {res['judge'] * 100:.1f} "
          f"vs random {res['random'] * 100:.1f} | top-{k} picks hitting a top-10 replayed moment: "
          f"{res['hit_rate'] * 100:.0f}%")


if __name__ == "__main__":
    main()
