"""Full-video mappers and section planning.

The mappers read the entire video first and publish maps (scene cuts, speaker turns, topic changes,
structure, names). The chief coordinator then splits the video into sections along those natural seams.

Sections: the cores tile the video exactly (no gaps, no double ownership of a clip start), and each
section's reading range extends past its core by more than the longest allowed clip, so every possible
clip lies completely inside at least one section a scout reads. That is what the coverage report proves.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter

import numpy as np

from ..media import pcm_chunks, run_ffmpeg
from . import lexicon as lx
from .evidence import Evidence

LEAD = 30.0  # seconds each section also reads before its core (context for the opening lines)


# ---------------------------------------------------------------- scenes
def map_scenes(ev: Evidence) -> dict:
    """Camera cuts over the whole video. Only keyframes are decoded, at thumbnail size, so this runs far
    faster than real time (encoders put keyframes on scene changes, so cuts are still found)."""
    if ev.video is None:
        ev.scenes = []
        return {"cuts": 0, "note": "audio-only download (very long video): scenes are checked per clip later"}
    t0 = time.time()
    err = run_ffmpeg(["-skip_frame", "nokey", "-i", str(ev.video), "-an", "-sn",
                      "-vf", "scale=128:-2,select='gt(scene,0.28)',showinfo", "-f", "null", "-"])
    cuts = sorted({round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", err)})
    ev.scenes = cuts
    wall = max(1e-3, time.time() - t0)
    ev.scan_speed = round(ev.duration / wall, 1) if ev.duration else None
    return {"cuts": len(cuts), "speed": ev.scan_speed,
            "note": f"{len(cuts)} scene changes; watched at {ev.scan_speed:.0f}x real time" if ev.scan_speed else ""}


# ---------------------------------------------------------------- voices
def _band_matrix(n_fft: int = 512, sr: int = 16000, bands: int = 20) -> np.ndarray:
    """Triangular mel-like filter bank, 90 Hz - 4 kHz (where voices differ most)."""
    def mel(f):
        return 2595 * np.log10(1 + f / 700)

    def imel(m):
        return 700 * (10 ** (m / 2595) - 1)

    pts = imel(np.linspace(mel(90), mel(4000), bands + 2))
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    fb = np.zeros((bands, len(freqs)))
    for b in range(bands):
        lo, mid, hi = pts[b], pts[b + 1], pts[b + 2]
        fb[b] = np.clip(np.minimum((freqs - lo) / (mid - lo), (hi - freqs) / (hi - mid)), 0, None)
    return fb


def _kmeans(X: np.ndarray, k: int, seed: int = 0, iters: int = 25) -> tuple[np.ndarray, float]:
    rng = np.random.default_rng(seed)
    C = X[rng.choice(len(X), k, replace=False)]
    for _ in range(iters):
        d = ((X[:, None, :] - C[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        newC = np.array([X[lab == c].mean(0) if np.any(lab == c) else C[c] for c in range(k)])
        if np.allclose(newC, C):
            break
        C = newC
    within = float(((X - C[lab]) ** 2).sum(-1).mean())
    return lab, within


def map_voices(ev: Evidence) -> dict:
    """Speaker turns. Caption files mark them (">>"); otherwise each sentence's voice timbre (its average
    spectrum shape) is clustered into up to 4 voices - only when the voices are clearly distinct."""
    n = ev.n
    if n == 0:
        return {"speakers": 0}
    if ev.new_speaker.sum() >= 3:  # captions mark every change of speaker: alternate A/B/... by turn
        lab, cur = np.zeros(n, dtype=int), 0
        for i in range(n):
            if i and ev.new_speaker[i]:
                cur = 1 - cur
            lab[i] = cur
        ev.speaker, ev.speakers_found = lab, 2
        return {"speakers": 2, "method": "caption speaker markers", "turns": int(ev.new_speaker.sum())}
    if ev.wav is None:
        return {"speakers": 0, "method": "no audio"}
    fb = _band_matrix()
    feats = np.zeros((n, fb.shape[0]))
    frames = np.zeros(n)
    sr, n_fft, hop = 16000, 512, 1024
    win = np.hanning(n_fft)
    t_base = 0.0
    for chunk in pcm_chunks(ev.wav, 120.0, sr):
        a = chunk.astype(np.float32) / 32768.0
        m = (len(a) - n_fft) // hop
        if m > 0:
            idx = np.arange(m)[:, None] * hop + np.arange(n_fft)[None]
            spec = np.abs(np.fft.rfft(a[idx] * win, axis=1)) ** 2
            logb = np.log(spec @ fb.T + 1e-8)
            t = t_base + (np.arange(m) * hop + n_fft / 2) / sr
            k = np.searchsorted(ev.E, t)  # sentence each frame falls into
            inside = (k < n) & (t >= ev.S[np.minimum(k, n - 1)])
            loud = logb.mean(1) > np.percentile(logb.mean(1), 35)  # skip near-silent frames
            sel = inside & loud
            np.add.at(feats, k[sel], logb[sel])
            np.add.at(frames, k[sel], 1)
        t_base += len(a) / sr
    ok = frames >= 20  # about 1.3 s of voiced audio
    if ok.sum() < 12:
        return {"speakers": 0, "method": "too little speech to tell voices apart"}
    X = feats[ok] / frames[ok, None]
    X = X - X.mean(1, keepdims=True)          # timbre (spectrum shape), not loudness
    X = X - X.mean(0)
    total = float((X ** 2).sum(-1).mean())
    best_k, best_lab, best_gain = 1, np.zeros(len(X), dtype=int), 0.0
    for k in (2, 3, 4):
        if len(X) < 6 * k:
            break
        lab, within = min((_kmeans(X, k, seed) for seed in range(3)), key=lambda r: r[1])
        gain = 1 - within / max(total, 1e-6)
        sizes = np.bincount(lab, minlength=k) / len(lab)
        if sizes.min() < 0.06:  # a "voice" heard in under 6% of sentences is noise, not a speaker
            continue
        C = np.array([X[lab == c].mean(0) for c in range(k)])
        gap = min(float(np.sqrt(((C[a] - C[b]) ** 2).mean())) for a in range(k) for b in range(a + 1, k))
        if gap < 0.45:  # voices must differ by ~2 dB across the spectrum, not just by recording noise
            continue
        if gain > best_gain + (0.18 if k == 2 else 0.08):
            best_k, best_lab, best_gain = k, lab, gain
    if best_k == 1:
        ev.speakers_found = 1
        ev.speaker[:] = 0
        return {"speakers": 1, "method": "voice timbre clustering (one clear voice)"}
    full = np.full(n, -1, dtype=int)
    full[np.flatnonzero(ok)] = best_lab
    for i in range(n):  # short sentences take their neighbour's voice
        if full[i] < 0:
            full[i] = full[i - 1] if i and full[i - 1] >= 0 else 0
    order = {c: r for r, c in enumerate(dict.fromkeys(full.tolist()))}  # A = first voice heard
    ev.speaker = np.array([order[c] for c in full], dtype=int)
    ev.speakers_found = best_k
    turns = int(np.sum(ev.speaker[1:] != ev.speaker[:-1]))
    return {"speakers": best_k, "method": "voice timbre clustering (estimated)", "turns": turns,
            "separation": round(best_gain, 2)}


# ---------------------------------------------------------------- topics
def _vec(words: list[str], dims: int = 1024) -> np.ndarray:
    v = np.zeros(dims)
    for w in words:
        if w not in lx.STOP and len(w) > 2:
            v[hash(w) % dims] += 1.0
    return v


def map_topics(ev: Evidence, block_s: float = 75.0, min_gap: float = 120.0) -> dict:
    """TextTiling: where the words used on one side of a moment stop matching the other side."""
    n = ev.n
    if n < 8:
        ev.topics = [{"start": 0.0, "end": ev.duration, "keywords": []}]
        return {"topics": 1}
    vecs = np.array([_vec(s.words) for s in ev.sents])
    P = np.vstack([np.zeros(vecs.shape[1]), np.cumsum(vecs, 0)])
    sims = np.ones(n)
    for i in range(1, n):
        a = int(np.searchsorted(ev.S, ev.S[i] - block_s))
        b = int(np.searchsorted(ev.S, ev.S[i] + block_s))
        left, right = P[i] - P[a], P[min(b, n)] - P[i]
        den = np.linalg.norm(left) * np.linalg.norm(right)
        sims[i] = float(left @ right / den) if den else 1.0
    # depth of each valley relative to the peaks around it
    depth = np.zeros(n)
    for i in range(1, n - 1):
        lp = sims[max(0, i - 12):i + 1].max()
        rp = sims[i:i + 13].max()
        depth[i] = (lp - sims[i]) + (rp - sims[i])
    cut = depth.mean() + 0.5 * depth.std()
    bounds = []
    for i in np.argsort(-depth):
        if depth[i] < cut:
            break
        if all(abs(ev.S[i] - ev.S[b]) >= min_gap for b in bounds) and ev.S[i] > min_gap / 2:
            bounds.append(int(i))
    bounds = sorted(bounds)
    edges = [0] + bounds + [n]
    df = Counter()
    blocks = []
    for a, b in zip(edges, edges[1:]):
        words = [w for s in ev.sents[a:b] for w in s.words if w not in lx.STOP and len(w) > 3]
        blocks.append(Counter(words))
        df.update(set(words))
    ev.topics = []
    for t, ((a, b), counts) in enumerate(zip(zip(edges, edges[1:]), blocks)):
        ev.topic[a:b] = t
        kw = sorted(counts, key=lambda w: -counts[w] * math.log((1 + len(blocks)) / (1 + df[w])))[:5]
        ev.topics.append({"start": float(ev.S[a]), "end": float(ev.E[b - 1]), "keywords": kw})
    return {"topics": len(ev.topics)}


# ---------------------------------------------------------------- structure
def map_structure(ev: Evidence) -> dict:
    """Intro, outro, sponsor reads and show breaks: parts that are reviewed but rarely make clips."""
    zones = []
    ad = ev.count["ad_read"] > 0
    i = 0
    while i < ev.n:
        if ad[i]:
            j = i
            while j + 1 < ev.n and (ad[j + 1] or (ev.S[j + 1] - ev.E[j] < 2 and np.any(ad[j + 1:j + 4]))):
                j += 1
            if ad[i:j + 1].sum() >= 2 or ev.count["promo"][i:j + 1].sum() >= 1:
                zones.append({"kind": "sponsor read", "start": float(ev.S[i]), "end": float(ev.E[j])})
            i = j + 1
        else:
            i += 1
    for k in np.flatnonzero(ev.count["show_break"] > 0):
        zones.append({"kind": "show break", "start": float(ev.S[k]), "end": float(ev.E[k])})
    greet = np.flatnonzero(ev.count["greeting"] > 0)
    head = [k for k in greet if ev.S[k] < min(180.0, 0.1 * ev.duration)]
    tail = [k for k in greet if ev.S[k] > ev.duration - min(180.0, 0.1 * ev.duration)]
    if head:
        zones.append({"kind": "intro", "start": 0.0, "end": float(ev.E[max(head)])})
    if tail:
        zones.append({"kind": "outro", "start": float(ev.S[min(tail)]), "end": ev.duration})
    ev.zones = sorted(zones, key=lambda z: z["start"])
    return {"zones": len(zones), "chapters": len(ev.chapters)}


# ---------------------------------------------------------------- names
def map_names(ev: Evidence) -> dict:
    """Who and what is named where: a clip that says "he" needs the name inside it (or a caption)."""
    names: dict[str, list[float]] = {}
    for k, t in enumerate(ev.text):
        for m in lx.NAMES.finditer(t):
            parts = [p for p in m.group(1).split() if p.lower() not in lx.STOP]
            if len(parts) >= 2:
                names.setdefault(" ".join(parts), []).append(float(ev.S[k]))
    ev.names = names
    return {"names": len(names)}


# ---------------------------------------------------------------- sections
def plan_sections(ev: Evidence) -> list[dict]:
    """Split the video along its natural seams: topic changes and chapters first, then scene cuts, speaker
    changes and long pauses. Section length scales with the video (about 12 sections an hour, more for
    short videos, never above 10 minutes), so the section scouts always have enough parallel work."""
    n, D = ev.n, ev.duration
    if n == 0:
        return [{"id": "S01", "core": [0.0, D], "read": [0.0, D], "i0": 0, "i1": 0, "why": "no speech"}]
    L = float(np.clip(D / 12, 90.0, 600.0))
    weight = np.zeros(n)
    why = [""] * n
    for tp in ev.topics[1:]:
        k = ev.idx_at(tp["start"])
        weight[k] += 3
        why[k] = why[k] or "topic change"
    for ch in ev.chapters[1:]:
        k = ev.idx_at(ch["start"])
        weight[k] += 3
        why[k] = why[k] or f"chapter: {ch['title'][:40]}"
    for c in ev.scenes or []:
        k = ev.idx_at(c)
        if abs(ev.S[k] - c) < 2.5:
            weight[k] += 1
            why[k] = why[k] or "scene change"
    if ev.speakers_found > 1:
        for k in np.flatnonzero(np.r_[False, ev.speaker[1:] != ev.speaker[:-1]]):
            weight[k] += 1
            why[k] = why[k] or "speaker change"
    for k in np.flatnonzero(np.r_[0.0, ev.gap_after[:-1]] >= 1.5):
        weight[k] += 1
        why[k] = why[k] or "pause"
    cuts, reasons, pos = [0], ["start"], 0.0
    while pos + 1.4 * L < D:
        lo, hi = int(np.searchsorted(ev.S, pos + 0.6 * L)), int(np.searchsorted(ev.S, pos + 1.4 * L))
        if hi <= lo:
            k = int(np.searchsorted(ev.S, pos + L))
            if k >= n or k <= cuts[-1]:
                break
            best, reason = k, "length"
        else:
            cand = np.arange(lo, min(hi, n))
            score = weight[cand] - np.abs(ev.S[cand] - (pos + L)) / L
            best = int(cand[np.argmax(score)])
            reason = why[best] if weight[best] > 0 else "length"
        if best <= cuts[-1]:
            break
        cuts.append(best)
        reasons.append(reason)
        pos = float(ev.S[best])
    over = max(ev.max_s + 10.0, 45.0)
    sections = []
    for k, (i0, reason) in enumerate(zip(cuts, reasons)):
        i1 = cuts[k + 1] if k + 1 < len(cuts) else n
        a = 0.0 if k == 0 else float(ev.S[i0])
        b = D if k + 1 == len(cuts) else float(ev.S[i1])
        sections.append({"id": f"S{k + 1:02d}", "core": [round(a, 2), round(b, 2)],
                         "read": [round(max(0.0, a - LEAD), 2), round(min(D, b + over), 2)],
                         "i0": int(i0), "i1": int(i1), "why": reason,
                         "topic": (ev.topics[int(ev.topic[i0])]["keywords"][:3] if ev.topics else [])})
    return sections


def coverage(ev: Evidence, sections: list[dict], done: dict[str, set[str]], lenses: list[str],
             full_done: set[str], full_roles: list[str]) -> dict:
    """Proof of coverage: the cores tile [0, duration], every section got every lens, every word is
    inside a fully reviewed section, and the reading overlap is longer than any clip."""
    gaps, pos = [], 0.0
    for s in sections:
        a, b = s["core"]
        if a > pos + 0.01:
            gaps.append([round(pos, 2), round(a, 2)])
        pos = max(pos, b)
    if pos < ev.duration - 0.01:
        gaps.append([round(pos, 2), round(ev.duration, 2)])
    rows, unreviewed = [], []
    for s in sections:
        got = done.get(s["id"], set())
        missing = [l for l in lenses if l not in got]
        rows.append({"id": s["id"], "core": s["core"], "read": s["read"], "why": s["why"],
                     "lenses": f"{len(lenses) - len(missing)}/{len(lenses)}", "missing": missing})
        if missing:
            unreviewed.append({"section": s["id"], "range": s["core"], "missing": missing})
    words_total = len(ev.words)
    bad = [r["range"] for r in unreviewed]
    words_ok = sum(1 for w in ev.words if not any(a <= w["s"] < b for a, b in bad))
    overlap_ok = all(s["read"][1] - s["core"][1] >= min(ev.max_s, ev.duration - s["core"][1]) - 0.01
                     for s in sections)
    full_missing = [r for r in full_roles if r not in full_done]
    complete = not gaps and not unreviewed and not full_missing and overlap_ok
    return {"complete": complete, "duration": round(ev.duration, 2), "sections": rows, "gaps": gaps,
            "unreviewed": unreviewed, "words_reviewed": f"{words_ok}/{words_total}",
            "every_clip_fits_a_section": overlap_ok, "full_video_reviews": f"{len(full_roles) - len(full_missing)}/"
                                                                           f"{len(full_roles)}",
            "full_video_missing": full_missing,
            "summary": ("Complete: every second was read by all "
                        f"{len(lenses)} section lenses and all {len(full_roles)} full-video analysts"
                        if complete else "Incomplete coverage - see gaps / unreviewed")}
