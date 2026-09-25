"""Targeted verification: repeated, automated re-checks of promising clips only.

The whole video is never replayed: each pass looks at one clip (or its edges) from a new angle - a jittered
boundary, an earlier look-back, a fresh random frame, a different stretch of audio, a re-listen of its words.
The coordinator gives each clip a pass budget (10-100) from how valuable, how risky and how uncertain it
is, and stops early once the clip's score is statistically settled.
"""
from __future__ import annotations

import random
import re
import threading

import numpy as np

from ..analysis import local_judge as lj
from ..media import fmt_ts
from . import lexicon as lx
from .evidence import Evidence, norm_words
from .gates import _frames_db, _referent_gap, audio_slice
from .lenses import LENSES

# composite clip quality: a strong opening and pacing, plus the clip's single strongest reason to exist
DOMAIN = ("humor", "emotion", "story", "education", "insight", "controversy", "quotes", "energy")


def quality(scores: dict[str, float], jitter: dict[str, float] | None = None) -> float:
    j = jitter or {}
    g = lambda k: scores.get(k, 0.0) * j.get(k, 1.0)  # noqa: E731
    dom = sorted((g(k) for k in DOMAIN), reverse=True)
    return float(np.clip(0.3 * g("hook") + 0.2 * g("retention") + 0.28 * dom[0] + 0.1 * np.mean(dom[:3])
                         + 0.06 * g("authenticity") + 0.06 * g("audience_fit"), 0, 1))


def lens_scores(ev: Evidence, i: int, j: int) -> dict[str, float]:
    I, J = np.array([i]), np.array([j])
    return {name: float(fn(ev, I, J)[0][0]) for name, fn, _, _ in LENSES.values()}


def _valid(ev: Evidence, i: int, j: int) -> bool:
    return 0 <= i <= j < ev.n and ev.start_ok[i] and ev.min_s * 0.9 <= ev.E[j] - ev.S[i] <= ev.max_s * 1.1


# ---------------------------------------------------------------- probes
def p_stability(ev: Evidence, c: dict, k: int, peers) -> dict:
    """Re-analyze the clip under small changes: neighbouring boundaries and lens weights +-20%."""
    rng = random.Random(f"{c['id']}:{k}")
    i, j = c["i"], c["j"]
    for _ in range(6):
        i2 = i + rng.choice((-1, 0, 0, 1))
        j2 = j + rng.choice((-1, 0, 0, 1))
        if _valid(ev, i2, j2):
            break
    else:
        i2, j2 = i, j
    jit = {name: rng.uniform(0.8, 1.2) for name, *_ in LENSES.values()}
    q = quality(lens_scores(ev, i2, j2), jit)
    return {"value": q, "note": f"window {fmt_ts(ev.S[i2])}-{fmt_ts(ev.E[j2])}, quality {q * 100:.0f}"}


def p_boundary(ev: Evidence, c: dict, k: int, peers) -> dict:
    i, j = c["i"], c["j"]
    if k == 0:  # the first word starts cleanly
        prev = max((w["e"] for w in ev.words if w["e"] <= ev.S[i] + 1e-3), default=-9)
        ok = ev.S[i] - prev >= 0.15 or (i > 0 and not ev.ends_open[i - 1])
        return {"ok": ok, "note": "clean first word" if ok else "first word runs on from the previous one"}
    if k == 1:  # the last word finishes a thought
        ok = bool(re.search(r"[.!?]['\"]?$", ev.text[j].strip())) or bool(ev.clean_break[j])
        return {"ok": ok, "note": "ends on a finished sentence" if ok else "last line has no clear ending"}
    base = quality(lens_scores(ev, i, j))
    alts = [(i + d, j) for d in (-1, 1)] if k == 2 else [(i, j + d) for d in (-1, 1)]
    better = [(a, b, quality(lens_scores(ev, a, b))) for a, b in alts if _valid(ev, a, b)]
    best = max(better, key=lambda x: x[2], default=None)
    ok = best is None or best[2] <= base + 0.04
    note = "chosen boundary is the best nearby" if ok else \
        f"{'start' if k == 2 else 'end'} at {fmt_ts(ev.S[best[0]] if k == 2 else ev.E[best[1]])} scores higher"
    return {"ok": ok, "value": base, "note": note, "alt": None if ok else [best[0], best[1]]}


def p_hook(ev: Evidence, c: dict, k: int, peers) -> dict:
    """Would the opening still work a word or two later? (robust hooks do not hinge on one word)"""
    words = ev.text[c["i"]].split()
    text = " ".join(words[k:]) if k < len(words) else ""
    lift = {r["feature"]: r["lift"] for r in (ev.profile or {}).get("hook_lift", [])}
    h = lj._hook_strength(text, norm_words(text), lift) if text else 0.0
    return {"value": h, "note": f"opening from word {k + 1}: strength {h * 100:.0f}"}


LOOKBACKS = (5, 10, 20, 40, 60, 90)


def p_context(ev: Evidence, c: dict, k: int, peers) -> dict:
    lb = LOOKBACKS[k % len(LOOKBACKS)]
    gap = _referent_gap(ev, c["i"], c["j"], lookback=lb)
    q_before = c["i"] > 0 and ev.is_q[c["i"] - 1] and ev.S[c["i"]] - ev.E[c["i"] - 1] < 3.0 and lb <= 10
    ok = not gap and not q_before
    note = f"{lb}s look-back: " + ("nothing essential missing" if ok else
                                   f"'{gap[0]}' named before the clip" if gap else "a question comes just before")
    return {"ok": ok, "note": note}


def p_frames(ev: Evidence, c: dict, k: int, peers) -> dict:
    if ev.video is None:
        return {"skip": "audio-only source"}
    import cv2

    rng = random.Random(f"f{c['id']}:{k}")
    t = c["start"] + rng.uniform(0.03, 0.97) * (c["end"] - c["start"])
    img = ev.frame(t)
    if img is None:
        return {"ok": False, "note": f"no frame at {fmt_ts(t)}"}
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    black = gray.mean() < 18 and gray.std() < 12
    face = bool(ev.faces(t))
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return {"ok": not black, "face": face, "value": 1.0 if face and not black else 0.5 if not black else 0.0,
            "note": f"{fmt_ts(t)}: " + ("black" if black else ("face" if face else "no face") + f", sharpness {sharp:.0f}")}


def p_audio(ev: Evidence, c: dict, k: int, peers) -> dict:
    rng = random.Random(f"a{c['id']}:{k}")
    a = c["start"] + rng.uniform(0, max(0.0, c["end"] - c["start"] - 4))
    x = audio_slice(ev, a, a + 4)
    if x is None or len(x) < 8000:
        return {"skip": "no audio sample"}
    db = _frames_db(x)
    clipped = float(np.mean(np.abs(x) > 0.985))
    ok = clipped < 0.005 and float(np.percentile(db, 90)) > -45
    return {"ok": ok, "value": float(np.clip((np.percentile(db, 90) + 50) / 40, 0, 1)),
            "note": f"{fmt_ts(a)}-{fmt_ts(a + 4)}: level {np.percentile(db, 90):.0f} dB, clipped {clipped * 100:.2f}%"}


_WHISPER: dict = {}
_WHISPER_LOCK = threading.Lock()


def _whisper_model(size: str):
    with _WHISPER_LOCK:
        if size in _WHISPER:
            return _WHISPER[size]
        try:
            from faster_whisper import WhisperModel

            from ..analysis.transcribe import _model_cached

            model = WhisperModel(size, device="cpu", compute_type="int8") if _model_cached(size) else None
        except Exception:
            model = None
        _WHISPER[size] = model
        return model


def p_transcript(ev: Evidence, c: dict, k: int, peers) -> dict:
    """Re-listen to the clip (only this clip) and compare the words with the transcript."""
    import difflib

    model = _whisper_model(str(ev.cfg["analysis"].get("whisper_model", "small")))
    x = audio_slice(ev, c["start"] - 0.3, c["end"] + 0.3)
    if model is None or x is None or len(x) < 16000:
        return {"skip": "speech model not available offline" if model is None else "no audio sample"}
    segs, _ = model.transcribe(x, language=ev.cfg["discovery"].get("language") or None, beam_size=1,
                               vad_filter=False, condition_on_previous_text=False)
    heard = norm_words(" ".join(s.text for s in segs))
    have = norm_words(" ".join(w["w"] for w in ev.words if c["start"] <= w["s"] <= c["end"]))
    ratio = difflib.SequenceMatcher(None, have, heard, autojunk=False).ratio() if have or heard else 1.0
    return {"ok": ratio >= 0.8, "value": ratio, "note": f"re-listen agrees on {ratio * 100:.0f}% of words"}


def p_claims(ev: Evidence, c: dict, k: int, peers) -> dict:
    """On the final boundaries: are claims hedged or attributed, and do the policy words still hold?"""
    text = ev.span_text(c["i"], c["j"])
    around = " ".join(ev.text[x] for x in range(max(0, c["i"] - 3), min(ev.n, c["j"] + 4)))
    claims = lx.count("claim", text)
    hedged = lx.count("hedge", around) > 0
    flags = [k2 for k2 in ("pii_phone", "pii_email", "pii_address", "pii_secret", "hate", "misinfo") if lx.count(k2, text)]
    ok = not flags and (not claims or hedged)
    return {"ok": ok, "flags": flags, "note": (f"{claims} claim(s), " + ("hedged / attributed" if hedged else
                                               "stated as fact") if claims else "no factual claims")
            + (f"; flags: {', '.join(flags)}" if flags else "")}


def shingles(text: str, k: int = 4) -> set:
    w = norm_words(text)
    return {tuple(w[x:x + k]) for x in range(max(0, len(w) - k + 1))}


def p_duplicates(ev: Evidence, c: dict, k: int, peers) -> dict:
    """Against every other finalist (from any video in this run) and clips made before."""
    mine = shingles(ev.span_text(c["i"], c["j"]))
    best, who = 0.0, ""
    for pid, text in peers or []:
        if pid == c["id"] or not text:
            continue
        other = shingles(text)
        if mine and other:
            sim = len(mine & other) / len(mine | other)
            if sim > best:
                best, who = sim, pid
    return {"value": 1 - best, "ok": best < 0.5, "similar_to": who if best >= 0.3 else "",
            "note": f"most similar: {who} ({best * 100:.0f}% shared phrasing)" if who else "no similar clip"}


PROBES = {"v_stability": p_stability, "v_boundary": p_boundary, "v_hook": p_hook, "v_context": p_context,
          "v_frames": p_frames, "v_audio": p_audio, "v_transcript": p_transcript, "v_claims": p_claims,
          "v_duplicates": p_duplicates}
# fixed passes every finalist gets (role -> passes); stability passes are added until the score settles
FIXED = {"v_boundary": 4, "v_hook": 3, "v_context": 6, "v_frames": 4, "v_audio": 3, "v_claims": 1, "v_duplicates": 1,
         "v_transcript": 1}


def budget(value: float, risk: float, uncertainty: float) -> int:
    """Passes a clip deserves: ~30 for a safe, clear-cut clip (the fixed checks plus a few stability passes),
    up to 100 for a valuable, risky or disputed one."""
    return int(np.clip(round(30 + 40 * uncertainty + 20 * value + 10 * risk), 30, 100))


def settled(values: list[float], min_n: int = 8, half_width: float = 0.025) -> bool:
    """Stop once the 95% interval of the clip's quality is within +-2.5 points."""
    if len(values) < min_n:
        return False
    return 1.96 * float(np.std(values, ddof=1)) / np.sqrt(len(values)) <= half_width
