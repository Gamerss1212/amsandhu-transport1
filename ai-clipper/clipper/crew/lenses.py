"""Independent specialist lenses.

Section lenses (12): every scout scores every candidate window of the section it took, through its own lens
only, without seeing anyone else's opinion. Each returns a 0-1 score and a verdict per window:
  +1 support  - this lens would put the window forward
   0 abstain  - not this lens's kind of moment (a humor scout has no view on a serious story)
  -1 oppose   - this lens found a concrete problem (a weak opening, dead air, an ad read...)
Only "universal" lenses (hook, retention, authenticity, audience fit) ever oppose; the others either
recognise their kind of moment or stay out of it. A scout nominates its strongest windows with its reasons.

Full-video reviewers (5) read the whole video at once and nominate what section scouts cannot see:
climaxes of the whole conversation, callbacks, audience peaks, stories that span sections, quiet gems.
"""
from __future__ import annotations

import math

import numpy as np

from ..analysis import local_judge as lj
from ..analysis.signals import peaks
from ..media import fmt_ts
from . import lexicon as lx
from .evidence import Evidence


def _clip(x) -> np.ndarray:
    return np.clip(np.asarray(x, dtype=float), 0.0, 1.0)


def _m(x, k: float) -> np.ndarray:
    """Saturating count: x/k, capped at 1."""
    return np.minimum(1.0, np.asarray(x, dtype=float) / k)


def _dur(ev: Evidence, I, J) -> np.ndarray:
    return np.maximum(0.1, ev.E[J] - ev.S[I])


def _nw(ev: Evidence, I, J) -> np.ndarray:
    return np.maximum(1.0, ev.wsum("nw", I, J))


def _quote(ev: Evidence, k: int, text: str | None = None) -> str:
    t = text if text is not None else ev.text[k]
    return '"' + (t if len(t) <= 90 else t[:87].rsplit(" ", 1)[0] + "...") + '"'


def _verdict(score, support: float, oppose_mask=None) -> np.ndarray:
    v = np.where(score >= support, 1, 0)
    if oppose_mask is not None:
        v = np.where(oppose_mask, -1, v)
    return v.astype(np.int8)


# ---------------------------------------------------------------- the 12 section lenses
def hook(ev: Evidence, I, J):
    h = ev.hook[I].copy()
    nxt = np.minimum(I + 1, J)
    h = np.where((ev.nw[I] < 8) & (J > I), np.maximum(h, 0.5 * h + 0.5 * ev.hook[nxt]), h)
    h = h - 0.15 * ev.ctx_start[I] - 0.1 * (ev.count["backref"][I] > 0)
    e3 = ev.csum("energy", ev.S[I], ev.S[I] + 3)
    if e3 is not None:
        h = 0.8 * h + 0.2 * e3
    early = ev.csum("laughs", ev.S[I], ev.S[I] + 8)
    if early is not None:  # a laugh in the first seconds means the opening already works
        h = np.maximum(h, 0.5 + 0.4 * _m(early * 8, 2) * min(1.0, ev.comedy + 0.3))
    s = _clip(h)
    return s, _verdict(s, 0.58, s < 0.3)


def hook_why(ev, i, j, s):
    return f"Opens with {_quote(ev, i)}" + (" - a strong, self-contained first line" if s >= 0.7 else "")


def retention(ev: Evidence, I, J):
    dur, nw = _dur(ev, I, J), _nw(ev, I, J)
    wps = nw / dur
    pace = _clip((wps - 1.5) / 0.9) - np.minimum(0.4, ev.wsum("filler", I, J) / nw * 6)
    inner_j = np.maximum(I, J - 1)
    dead = np.where(J > I, ev.wsum("deadair", I, inner_j), 0)
    loops = _clip(0.35 * (ev.wsum("question", I, inner_j) > 0) + 0.25 * _m(ev.wsum("turn", I, J), 1)
                  + 0.2 * _m(ev.wsum("surprise", I, J), 1) + 0.2 * _m(ev.wsum("story", I, J), 2))
    length = np.array([lj._length_score(d, ev.profile) for d in dur])
    ending = 0.5 * ev.clean_break[J] + 0.3 * ((ev.count["punch"][J] + ev.count["laugh"][J]) > 0) + \
        0.2 * ~ev.ends_open[J]
    s = _clip(0.33 * pace + 0.2 * (1 - _m(dead, 2)) + 0.17 * loops + 0.15 * length + 0.15 * ending)
    oppose = (wps < 1.5) | (dead >= 2) | ev.ends_open[J]
    return s, _verdict(np.where(loops >= 0.2, s, 0), 0.66, oppose)


def retention_why(ev, i, j, s):
    d = ev.E[j] - ev.S[i]
    nw = ev.wsum("nw", [i], [j])[0]
    return f"{nw / d:.1f} words/s with no dead air, and a reason to keep watching until the last line"


def humor(ev: Evidence, I, J):
    nw = _nw(ev, I, J)
    tags = ev.wsum("laugh", I, J)
    cue = ev.wsum("funny_cue", I, J) / nw
    inside = ev.csum("laughs", ev.S[I], ev.E[J] + 3)
    tail = ev.csum("laughs", ev.E[J] - 2, ev.E[J] + 3)
    secs = 0 if inside is None else inside * (_dur(ev, I, J) + 3)
    tail_secs = 0 if tail is None else tail * 5
    s = _clip(0.3 * _m(tags, 2) + 0.2 * _m(cue * 20, 1) + 0.35 * _m(secs, 4) + 0.15 * _m(tail_secs, 1))
    return s, _verdict(s, 0.5)


def humor_why(ev, i, j, s):
    return "Real laughter on the audio" + (" right after the punchline" if s > 0.7 else "") + \
        f" - ends on {_quote(ev, j)}"


def emotion(ev: Evidence, I, J):
    nw = _nw(ev, I, J)
    emo = sum(ev.wsum(k, I, J) for k in ("emo_sad", "emo_joy", "emo_anger", "emo_fear"))
    s = _clip(0.35 * _m(emo / nw * 25, 1) + 0.25 * _m(ev.wsum("vulnerable", I, J), 1)
              + 0.15 * _m(ev.wsum("family", I, J), 2) + 0.25 * _m(ev.wsum("intense", I, J) / nw * 12, 1))
    return s, _verdict(s, 0.52)


def emotion_why(ev, i, j, s):
    text = ev.span_text(i, j)
    words = lx.hits("vulnerable", text) + lx.hits("emo_sad", text) + lx.hits("family", text)
    return "Emotional weight: " + (", ".join(f'"{w}"' for w in words[:4]) or "strong feeling words")


def story(ev: Evidence, I, J):
    nw = _nw(ev, I, J)
    last2 = np.maximum(I, J - 1)
    s = _clip(0.3 * _m(ev.wsum("story", I, J), 2) + 0.25 * _m(ev.wsum("past", I, J) / nw * 6, 1)
              + 0.2 * _m(ev.wsum("turn", I, J), 1) + 0.25 * _m(ev.wsum("lesson", last2, J) + ev.count["punch"][J], 1))
    s = np.where(_dur(ev, I, J) < 20, s * 0.7, s)
    return s, _verdict(s, 0.52)


def story_why(ev, i, j, s):
    return f"A complete story: starts {_quote(ev, i)}, lands on {_quote(ev, j)}"


def education(ev: Evidence, I, J):
    nw = _nw(ev, I, J)
    s = _clip(0.4 * _m(ev.wsum("teach", I, J), 2) + 0.25 * _m(ev.wsum("explain", I, J), 2)
              + 0.15 * _m(ev.wsum("number", I, J), 3) + 0.2 * _m(ev.wsum("you", I, J) / nw * 8, 1))
    return s, _verdict(s, 0.55)


def education_why(ev, i, j, s):
    text = ev.span_text(i, j)
    return "Useful explanation: " + ", ".join(f'"{h}"' for h in (lx.hits("teach", text) + lx.hits("explain", text))[:3])


def insight(ev: Evidence, I, J):
    s = _clip(0.4 * _m(ev.wsum("surprise", I, J), 1.5) + 0.25 * _m(ev.wsum("aphorism", I, J), 1)
              + 0.2 * _m(ev.wsum("antithesis", I, J), 1) + 0.15 * _m(ev.wsum("number", I, J), 2))
    return s, _verdict(s, 0.52)


def insight_why(ev, i, j, s):
    text = ev.span_text(i, j)
    return "Counter-intuitive idea: " + ", ".join(f'"{h}"' for h in (lx.hits("surprise", text) +
                                                                       lx.hits("aphorism", text))[:3])


def controversy(ev: Evidence, I, J):
    pressed = (ev.wsum("question", I, J) > 0) & (ev.wsum("blunt", I, J) > 0)
    s = _clip(0.3 * _m(ev.wsum("absolute", I, J), 3) + 0.3 * _m(ev.wsum("disagree", I, J), 1)
              + 0.2 * _m(ev.wsum("debate", I, J), 2) + 0.2 * pressed - 0.1 * _m(ev.wsum("hedge", I, J), 3))
    return s, _verdict(s, 0.55)


def controversy_why(ev, i, j, s):
    text = ev.span_text(i, j)
    return "Strong opinion people will argue about: " + ", ".join(
        f'"{h}"' for h in (lx.hits("disagree", text) + lx.hits("absolute", text))[:3])


def _line_quality(ev: Evidence) -> np.ndarray:
    c = ev.count
    return ev.count["short_line"] * _clip(0.35 * _m(c["aphorism"], 1) + 0.35 * _m(c["antithesis"], 1)
                                          + 0.2 * _m(c["absolute"], 1) + 0.15 * _m(c["intense"], 1)
                                          + 0.2 * _m(c["punch"], 1))


def quotes(ev: Evidence, I, J):
    q = _line_quality(ev)
    best = np.array([q[i:j + 1].max() for i, j in zip(I, J)]) if len(I) else np.zeros(0)
    tail = np.array([q[max(i, j - 1):j + 1].max() for i, j in zip(I, J)]) if len(I) else np.zeros(0)
    s = _clip(best + 0.15 * (tail > 0.3))
    return s, _verdict(s, 0.5)


def quotes_why(ev, i, j, s):
    q = _line_quality(ev)[i:j + 1]
    return f"Quotable line: {_quote(ev, i + int(np.argmax(q)))}"


def authenticity(ev: Evidence, I, J):
    nw = _nw(ev, I, J)
    s = _clip(0.3 * _m(ev.wsum("first_person", I, J) / nw * 8, 1) + 0.25 * _m(ev.wsum("vulnerable", I, J), 1)
              + 0.2 * _m(ev.wsum("number", I, J) + ev.wsum("specific", I, J) + ev.wsum("family", I, J), 2)
              + 0.1 * _m(ev.wsum("laugh", I, J), 1)
              + 0.15 * (1 - _m(ev.wsum("filler", I, J) / nw * 10, 1)))
    oppose = (ev.wsum("ad_read", I, J) > 0) | (ev.wsum("promo", I, J) >= 2) | (ev.wsum("greeting", I, J) > 0)
    return s, _verdict(s, 0.6, oppose)


def authenticity_why(ev, i, j, s):
    return "Real, unscripted first-person moment" + (" with personal specifics" if s > 0.75 else "")


def relevance(ev: Evidence, I, J):
    nw = _nw(ev, I, J)
    trend = 1 / (1 + np.exp(-ev.wsum("trend_terms", I, J)))
    jargon = ev.wsum("jargon", I, J) / nw
    s = _clip(0.3 * trend + 0.2 * _m(ev.wsum("you", I, J) / nw * 8, 1)
              + 0.3 * _m(ev.wsum("broad", I, J) + ev.wsum("debate", I, J), 2) + 0.2 * (1 - _m(jargon * 20, 1)))
    oppose = (ev.wsum("backref", I, J) > 0) | (jargon > 0.08)
    return s, _verdict(s, 0.6, oppose)


def relevance_why(ev, i, j, s):
    text = ev.span_text(i, j)
    return "Broad-appeal subject: " + (", ".join(lj.topic_tags(text)) or "speaks straight to the viewer")


def energy(ev: Evidence, I, J):
    parts, weights = [], []
    for key, w in (("energy", 0.45), ("heatmap", 0.3), ("comments", 0.15), ("laughs", 0.1)):
        v = ev.csum(key, ev.S[I], ev.E[J])
        if v is not None:
            parts.append(w * (v if key != "laughs" else _m(v * _dur(ev, I, J), 3)))
            weights.append(w)
    if not weights:
        z = np.zeros(len(I))
        return z, z.astype(np.int8)
    s = _clip(sum(parts) / sum(weights))
    return s, _verdict(s, 0.66)


def energy_why(ev, i, j, s):
    bits = []
    if ev.curves.get("heatmap") is not None and (ev.cmax("heatmap", ev.S[i], ev.E[j]) or 0) > 0.85:
        bits.append("one of the most replayed parts of the video")
    if (ev.cmax("energy", ev.S[i], ev.E[j]) or 0) > 0.9:
        bits.append("a loudness peak")
    return "Audience and energy peak: " + (", ".join(bits) or "high energy throughout")


LENSES = {  # role -> (lens name, score function, reason function, universal?)
    "s_hook": ("hook", hook, hook_why, True),
    "s_retention": ("retention", retention, retention_why, True),
    "s_humor": ("humor", humor, humor_why, False),
    "s_emotion": ("emotion", emotion, emotion_why, False),
    "s_story": ("story", story, story_why, False),
    "s_education": ("education", education, education_why, False),
    "s_insight": ("insight", insight, insight_why, False),
    "s_controversy": ("controversy", controversy, controversy_why, False),
    "s_quotes": ("quotes", quotes, quotes_why, False),
    "s_authentic": ("authenticity", authenticity, authenticity_why, True),
    "s_relevance": ("audience_fit", relevance, relevance_why, True),
    "s_energy": ("energy", energy, energy_why, False),
}


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def pick(ev: Evidence, I, J, score, verdict, k: int, max_overlap: float = 0.25) -> list[int]:
    """The k best supported windows that do not overlap each other."""
    order = np.argsort(-np.where(verdict > 0, score, -1))
    chosen: list[int] = []
    for x in order:
        if verdict[x] <= 0 or len(chosen) >= k:
            break
        span = (ev.S[I[x]], ev.E[J[x]])
        if all(overlap(span, (ev.S[I[c]], ev.E[J[c]])) <= max_overlap for c in chosen):
            chosen.append(int(x))
    return chosen


def scout_section(ev: Evidence, role: str, section: dict) -> dict:
    """One scout, one section, one lens: scores every window, nominates its best with reasons."""
    name, fn, why, universal = LENSES[role]
    I, J = ev.grid(section["i0"], section["i1"])
    if not len(I):
        return {"lens": name, "section": section["id"], "windows": 0, "I": [], "J": [], "score": [], "verdict": [],
                "nominations": []}
    score, verdict = fn(ev, I, J)
    core = section["core"][1] - section["core"][0]
    k = max(2, math.ceil(core / 150))
    noms = [{"i": int(I[x]), "j": int(J[x]), "start": float(ev.S[I[x]]), "end": float(ev.E[J[x]]),
             "score": round(float(score[x]), 3), "why": why(ev, int(I[x]), int(J[x]), float(score[x]))}
            for x in pick(ev, I, J, score, verdict, k)]
    return {"lens": name, "section": section["id"], "windows": int(len(I)), "I": I.tolist(), "J": J.tolist(),
            "score": np.round(score, 3).tolist(), "verdict": verdict.tolist(), "nominations": noms,
            "opposed": int((verdict < 0).sum()), "supported": int((verdict > 0).sum())}


# ---------------------------------------------------------------- full-video reviewers
def _best_window(ev: Evidence, I, J, t: float, value: np.ndarray, before: float = 0.7):
    """Grid window containing time t (in its first `before` share), with the highest value."""
    dur = ev.E[J] - ev.S[I]
    ok = (ev.S[I] <= t) & (ev.S[I] + before * dur >= t)
    if not ok.any():
        ok = (ev.S[I] <= t) & (ev.E[J] >= t)
    if not ok.any():
        return None
    x = int(np.flatnonzero(ok)[np.argmax(value[ok])])
    return int(I[x]), int(J[x])


def _smooth_by_time(ev: Evidence, x: np.ndarray, half: float) -> np.ndarray:
    P = np.concatenate([[0.0], np.cumsum(x)])
    a = np.searchsorted(ev.S, ev.S - half)
    b = np.searchsorted(ev.S, ev.S + half, side="right")
    return (P[b] - P[a]) / np.maximum(1, b - a)


def _nom(ev, i, j, score, why, **extra) -> dict:
    return {"i": int(i), "j": int(j), "start": float(ev.S[i]), "end": float(ev.E[j]), "score": round(float(score), 3),
            "why": why, **extra}


def full_quota(ev: Evidence) -> int:
    return int(np.clip(ev.duration / 600, 2, 30))


def narrative(ev: Evidence) -> dict:
    """The arc of the whole conversation: where intensity builds to a climax."""
    if ev.n < 5:
        return {"nominations": [], "note": "too short for an arc"}
    c = ev.count
    arousal = (c["intense"] + c["emo_sad"] + c["emo_anger"] + c["emo_fear"] + c["disagree"] + c["laugh"]) / ev.nw
    e = ev.csum("energy", ev.S, ev.E)
    if e is not None:
        arousal = 0.6 * _m(arousal * 8, 1) + 0.4 * e
    arc = _smooth_by_time(ev, arousal, 45.0)
    I, J = ev.grid(0, ev.n)
    if not len(I):
        return {"nominations": []}
    csum = np.concatenate([[0.0], np.cumsum(arc)])
    value = (csum[J + 1] - csum[I]) / (J - I + 1)
    secs = np.zeros(int(ev.duration) + 2)
    for k in range(ev.n):
        secs[int(ev.S[k])] = max(secs[int(ev.S[k])], arc[k])
    noms = []
    top = float(np.percentile(arc, 90)) if len(arc) else 0
    for t in peaks(secs, n=full_quota(ev), min_gap=300):
        w = _best_window(ev, I, J, t, value)
        if w and secs[t] >= top:
            noms.append(_nom(ev, *w, min(1.0, secs[t] / max(1e-6, arc.max())),
                             f"Climax of the conversation around {fmt_ts(t)} (intensity builds to this point)"))
    return {"nominations": noms, "note": f"{len(noms)} climaxes across {fmt_ts(ev.duration)}"}


def callbacks(ev: Evidence) -> dict:
    """Phrases that come back much later: running jokes and callbacks. The payoff lands only with the
    earlier mention, so it is flagged for the context reviewers."""
    pos: dict[tuple, list[int]] = {}
    for k, s in enumerate(ev.sents):
        toks = [w for w in s.words if w not in lx.STOP and len(w) > 2]
        for g in zip(toks, toks[1:], toks[2:]):
            pos.setdefault(g, []).append(k)
    found = []
    for g, ks in pos.items():
        if len(ks) < 2 or len(ks) > 6:
            continue
        first, later = ks[0], [k for k in ks[1:] if ev.S[k] - ev.S[ks[0]] >= 180]
        for k in later:
            react = (ev.count["laugh"][k] > 0) or ((ev.cmax("laughs", ev.S[k], ev.E[k] + 3) or 0) > 0)
            found.append({"phrase": " ".join(g), "first": float(ev.S[first]), "later": float(ev.S[k]),
                          "k": k, "payoff": bool(react)})
    I, J = ev.grid(0, ev.n)
    noms = []
    for cb in sorted((f for f in found if f["payoff"]), key=lambda f: f["later"])[:full_quota(ev)]:
        w = _best_window(ev, I, J, cb["later"], -(ev.E[J] - ev.S[I]), before=0.9) if len(I) else None
        if w:
            noms.append(_nom(ev, *w, 0.7, f'Callback payoff: "{cb["phrase"]}" (first said at {fmt_ts(cb["first"])})',
                             callback_from=cb["first"]))
    return {"nominations": noms, "callbacks": found[:200], "note": f"{len(found)} repeated phrases, "
                                                                    f"{sum(f['payoff'] for f in found)} with a reaction"}


def audience(ev: Evidence) -> dict:
    """Most-replayed peaks and timestamps viewers quoted in comments."""
    noms = []
    I, J = ev.grid(0, ev.n)
    if not len(I):
        return {"nominations": []}
    raw = ev.signals.get("raw") or {}
    for key, label in (("heatmap", "One of the most replayed moments"), ("comments", "Viewers quote this moment")):
        curve = raw.get(key)
        if curve is None:
            continue
        value = ev.csum(key, ev.S[I], ev.E[J])
        for t in peaks(np.asarray(curve), n=full_quota(ev), min_gap=120):
            w = _best_window(ev, I, J, t, value)
            if w:
                noms.append(_nom(ev, *w, float(value[np.flatnonzero((I == w[0]) & (J == w[1]))[0]]),
                                 f"{label} ({fmt_ts(t)})"))
    return {"nominations": noms, "note": "no audience data for this video" if not noms else f"{len(noms)} audience peaks"}


def continuity(ev: Evidence) -> dict:
    """Stories longer than one clip: the resolution is nominated, with where the story began, so the
    context reviewers can decide whether it stands alone."""
    I, J = ev.grid(0, ev.n)
    if not len(I):
        return {"nominations": []}
    c = ev.count
    noms, spans = [], []
    onsets = np.flatnonzero((c["story"] > 0) & (c["first_person"] > 0))
    ends = np.flatnonzero((c["lesson"] + c["punch"]) > 0)
    for o in onsets:
        later = ends[(ev.S[ends] - ev.S[o] >= ev.max_s) & (ev.S[ends] - ev.S[o] <= 300)]
        if not len(later):
            continue
        k = int(later[0])
        if spans and spans[-1][1] == k:
            continue
        spans.append((int(o), k))
    for o, k in spans[: full_quota(ev)]:
        cand = np.flatnonzero((J == k) | (J == min(ev.n - 1, k + 1)))
        if not len(cand):
            continue
        x = int(cand[np.argmax(ev.E[J[cand]] - ev.S[I[cand]])])
        noms.append(_nom(ev, I[x], J[x], 0.6, f"Resolution of a story that began at {fmt_ts(ev.S[o])}",
                         story_from=float(ev.S[o])))
    return {"nominations": noms, "note": f"{len(spans)} stories longer than one clip"}


def gems(ev: Evidence) -> dict:
    """Quiet moments with a lot to say: sincere, insightful, low-volume - the ones loudness-driven
    reviewers rank low."""
    c = ev.count
    weight = (c["vulnerable"] + c["aphorism"] + c["surprise"] + c["emo_sad"] + c["antithesis"]) / ev.nw
    e = ev.csum("energy", ev.S, ev.E)
    quiet = np.ones(ev.n) if e is None else (1 - e)
    I, J = ev.grid(0, ev.n)
    if not len(I):
        return {"nominations": []}
    P = np.concatenate([[0.0], np.cumsum(weight * quiet)])
    value = (P[J + 1] - P[I]) / (J - I + 1)
    loud = ev.csum("energy", ev.S[I], ev.E[J])
    ok = value > 0 if loud is None else (value > 0) & (loud < 0.5)
    noms = []
    for x in np.argsort(-np.where(ok, value, -1)):
        if not ok[x] or len(noms) >= full_quota(ev):
            break
        span = (ev.S[I[x]], ev.E[J[x]])
        if all(overlap(span, (n["start"], n["end"])) <= 0.25 for n in noms) and value[x] > 0.02:
            noms.append(_nom(ev, I[x], J[x], min(1.0, value[x] * 12), f"Quiet gem: {_quote(ev, int(I[x]))}"))
    return {"nominations": noms, "note": f"{len(noms)} quiet moments worth a look"}


FULL_REVIEWERS = {"narrative": narrative, "callbacks": callbacks, "audience": audience, "continuity": continuity,
                  "gems": gems}
