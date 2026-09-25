"""What the command agents do once findings are unsealed: consensus, de-duplication, final scores, decisions.

Consensus: nominations that cover the same moment are clustered; the cluster's window is the one the lenses
collectively rate highest. Support = lenses that nominated the moment or rated that window a clear yes;
dissent = lenses that found a concrete problem with it. Support is counted per lens (per perspective), not
per agent: two scouts using the same lens on overlapping reading ranges are redundancy, not independent votes.

A moment with too little support can still advance as a minority report when one lens rates it
exceptionally (top 3% of every window it scored and at least 0.8) - it must then be confirmed by
verification before it can be approved.
"""
from __future__ import annotations

import math

import numpy as np

from ..trends.analyzer import trend_fit
from .evidence import Evidence
from .gates import RISK_GATES
from .lenses import LENSES, overlap
from .verify import lens_scores, quality

LENS_NAMES = [v[0] for v in LENSES.values()]
UNIVERSAL = {v[0] for v in LENSES.values() if v[3]}


def build_tables(submissions: list[tuple[str, dict]]) -> tuple[dict, dict]:
    """(lens -> {(i, j): (score, verdict, agent)}, lens -> all scores) from the unsealed section findings."""
    table: dict[str, dict] = {n: {} for n in LENS_NAMES}
    scores: dict[str, list] = {n: [] for n in LENS_NAMES}
    for agent, r in submissions:
        t = table[r["lens"]]
        for i, j, s, v in zip(r["I"], r["J"], r["score"], r["verdict"]):
            t[(i, j)] = (s, v, agent)
        scores[r["lens"]] += r["score"]
    return table, scores


def cluster(noms: list[dict], min_overlap: float = 0.35) -> list[list[dict]]:
    noms = sorted(noms, key=lambda n: n["start"])
    parent = list(range(len(noms)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(noms)):
        for b in range(a + 1, len(noms)):
            if noms[b]["start"] >= noms[a]["end"]:
                break
            if overlap((noms[a]["start"], noms[a]["end"]), (noms[b]["start"], noms[b]["end"])) >= min_overlap:
                parent[find(b)] = find(a)
    groups: dict[int, list] = {}
    for k, n in enumerate(noms):
        groups.setdefault(find(k), []).append(n)
    return list(groups.values())


def consensus(ev: Evidence, table: dict, all_scores: dict, noms: list[dict], full_roles: list[str]) -> list[dict]:
    pct = {n: (np.percentile(v, 97) if len(v) else 1.0) for n, v in all_scores.items()}
    out = []
    for k, members in enumerate(cluster(noms)):
        windows = {(m["i"], m["j"]) for m in members}

        def view(w):
            return {n: table[n].get(w) for n in LENS_NAMES}

        def q(w):
            v = view(w)
            return quality({n: (x[0] if x else 0.0) for n, x in v.items()})

        def backed(w):  # how many lenses say yes to exactly this window
            return sum(1 for x in view(w).values() if x and x[1] > 0) + sum((m["i"], m["j"]) == w for m in members)

        rep = max(windows, key=lambda w: q(w) + 0.03 * backed(w))
        at = view(rep)
        nominated = {m["lens"]: m for m in members}
        supporters, dissenters, abstained, missing = [], [], [], []
        for n in LENS_NAMES:
            x = at[n]
            if n in nominated:
                m = nominated[n]
                supporters.append({"by": n, "agent": m["agent"], "score": m["score"], "why": m["why"],
                                   "own_window": (m["i"], m["j"]) != rep})
            elif x is None:
                missing.append(n)
            elif x[1] > 0:
                supporters.append({"by": n, "agent": x[2], "score": round(x[0], 3), "why": "rated this window a clear yes"})
            elif x[1] < 0:
                dissenters.append({"by": n, "agent": x[2], "score": round(x[0], 3)})
            else:
                abstained.append(n)
        lens_support = len(supporters)
        full = [m for m in members if m["lens"] in full_roles]
        for m in {m["lens"]: m for m in full}.values():
            supporters.append({"by": m["lens"], "agent": m["agent"], "score": m["score"], "why": m["why"], "full_video": True})
        scores = {n: (at[n][0] if at[n] else 0.0) for n in LENS_NAMES}
        base = quality(scores)
        n_sup = len(supporters)
        strength = float(np.clip(base * (1 + 0.08 * (n_sup - 1)) - 0.05 * len(dissenters), 0, 1.5))
        star = max(((n, scores[n]) for n in LENS_NAMES if n in nominated), key=lambda x: x[1], default=(None, 0))
        minority = lens_support <= 1 and star[0] is not None and star[1] >= max(0.8, pct[star[0]])
        notes = [m["why"] for m in full if m.get("callback_from") is not None or m.get("story_from") is not None]
        out.append({"id": "", "i": rep[0], "j": rep[1], "start": float(ev.S[rep[0]]), "end": float(ev.E[rep[1]]),
                    "strength": round(strength, 4), "quality": round(base, 4), "scores": {n: round(v, 3) for n, v in scores.items()},
                    "supporters": supporters, "dissenters": dissenters, "abstained": abstained, "missing": missing,
                    "support": n_sup, "lens_support": lens_support, "minority": bool(minority),
                    "nominations": len(members), "notes": notes, "alternatives": []})
    out.sort(key=lambda c: -c["strength"])
    for k, c in enumerate(out, 1):
        c["id"] = f"C{k:03d}"
    return out


def dedupe(cands: list[dict], already_made=None, max_overlap: float = 0.25) -> tuple[list[dict], list[dict]]:
    """Keeps the strongest version of every moment; the others are recorded as rejected alternatives."""
    kept, rejected = [], []
    for c in cands:
        if already_made and already_made(c["start"], c["end"]):
            rejected.append({**_brief(c), "reason": "already made before (shared memory)"})
            continue
        twin = next((k for k in kept if overlap((c["start"], c["end"]), (k["start"], k["end"])) > max_overlap), None)
        if twin:
            twin["alternatives"].append({**_brief(c), "reason": f"near-duplicate of {twin['id']} (weaker)"})
            continue
        kept.append(c)
    return kept, rejected


def _brief(c: dict) -> dict:
    return {"id": c["id"], "start": round(c["start"], 2), "end": round(c["end"], 2), "strength": c["strength"],
            "support": c["support"], "dissent": len(c["dissenters"])}


# ---------------------------------------------------------------- boundaries after gate review
def adjust(ev: Evidence, c: dict, gates: dict) -> list[str]:
    """Apply the boundary and context gates' proposals (never beyond 110% of the longest clip)."""
    notes = []
    i, j = c["i"], c["j"]
    for name in ("context", "boundaries", "reputation"):
        p = (gates.get(name) or {}).get("proposal") or {}
        if "i" in p and p["i"] < i and ev.E[j] - ev.S[p["i"]] <= ev.max_s * 1.1:
            notes.append(f"{name}: start moved to {ev.S[p['i']]:.1f}s")
            i = p["i"]
        if "j" in p and p["j"] > j and ev.E[p["j"]] - ev.S[i] <= ev.max_s * 1.1:
            notes.append(f"{name}: end moved to {ev.E[p['j']]:.1f}s")
            j = p["j"]
    c["i"], c["j"] = i, j
    c["start"], c["end"] = float(ev.S[i]), float(ev.E[j])
    c["land"] = bool(((gates.get("boundaries") or {}).get("proposal") or {}).get("land"))
    return notes


def final_times(ev: Evidence, c: dict) -> tuple[float, float]:
    """Exact cut points: a breath before the first word, a beat after the last - never into another word -
    and, after a punchline, long enough for the laugh to land."""
    s, e = ev.S[c["i"]], ev.E[c["j"]]
    prev_end = max((w["e"] for w in ev.words if w["e"] <= s + 1e-3), default=s - 0.3)
    next_start = min((w["s"] for w in ev.words if w["s"] >= e - 1e-3), default=e + 0.6)
    s2, e2 = max(prev_end, s - 0.15, 0.0), min(next_start, e + 0.45)
    laughs = ev.curves.get("laughs")
    if c.get("land") and laughs is not None:
        k = int(e)
        while k + 1 < len(laughs) and laughs[k + 1] > 0 and k + 1 - e < 3.0:
            k += 1
        e2 = max(e2, min(k + 1.0, e + 3.0, next_start - 0.05))
    return round(s2, 2), round(e2, 2)


# ---------------------------------------------------------------- scoring and decisions
PLATFORM_FIT = {"tiktok": (21, 60, 180), "instagram": (15, 45, 90), "youtube": (15, 40, 60)}


def platform_fit(dur: float, platform: str) -> float:
    lo, sweet, hi = PLATFORM_FIT[platform]
    if dur > hi:
        return 0.0
    if dur < lo:
        return 0.6
    return float(np.clip(1.0 - 0.5 * max(0.0, dur - sweet) / max(1.0, hi - sweet), 0.4, 1.0))


def _gs(gates: dict, name: str, default: float = 60.0) -> float:
    g = gates.get(name)
    return float(g["score"]) if g else default


def decide(ev: Evidence, c: dict, gates: dict, vsum: dict, crew_cfg: dict) -> dict:
    L = lens_scores(ev, c["i"], c["j"])
    dur = c["t1"] - c["t0"]
    text = ev.span_text(c["i"], c["j"])
    tf = trend_fit(ev.profile, text, dur) if ev.profile else 50.0
    short = float(np.clip((60 - dur) / 40, 0, 1))
    cat = {
        "hook": 100 * (0.7 * L["hook"] + 0.3 * vsum.get("hook_robust", L["hook"])),
        "retention": 100 * L["retention"],
        "emotional_impact": 100 * (0.6 * max(L["emotion"], L["humor"], 0.8 * L["energy"])
                                   + 0.4 * np.mean([L["emotion"], L["humor"], L["energy"]])),
        "clarity": (0.4 * _gs(gates, "subtitle_accuracy") + 0.3 * _gs(gates, "audio_quality") + 0.3 * _gs(gates, "captions"))
        * (0.7 + 0.3 * vsum.get("transcript_agreement", 1.0)),
        "originality": 100 * (0.6 * vsum.get("uniqueness", 1.0) + 0.4 * max(L["insight"], L["quotes"])),
        "usefulness": 100 * max(L["education"], L["insight"]),
        "entertainment": 100 * (0.6 * max(L["humor"], L["story"], L["controversy"], L["energy"])
                                + 0.4 * np.mean([L["humor"], L["story"], L["controversy"], L["energy"]])),
        "shareability": 100 * (0.3 * L["quotes"] + 0.3 * max(L["emotion"], L["humor"], L["story"])
                               + 0.2 * L["audience_fit"] + 0.2 * L["controversy"]),
        "replay_value": 100 * (0.4 * L["humor"] + 0.3 * L["quotes"] + 0.3 * short),
        "production_quality": np.mean([_gs(gates, n) for n in ("composition", "camera", "expressions", "audio_quality")]),
        "contextual_completeness": np.mean([_gs(gates, "context"), _gs(gates, "boundaries"),
                                            100 * vsum.get("context_ok", 1.0), 100 * vsum.get("boundary_ok", 1.0)]),
        "audience_fit": 100 * (0.6 * L["audience_fit"] + 0.4 * tf / 100),
        "platform_suitability": 100 * max(platform_fit(dur, p) for p in PLATFORM_FIT) *
        (0.8 + 0.2 * _gs(gates, "captions") / 100),
        "safety": min(_gs(gates, n, 100.0) for n in RISK_GATES),
    }
    cat = {k: round(float(np.clip(v, 0, 100)), 1) for k, v in cat.items()}
    W = {"hook": 0.18, "retention": 0.14, "emotional_impact": 0.1, "entertainment": 0.1, "shareability": 0.1,
         "audience_fit": 0.08, "clarity": 0.06, "originality": 0.05, "usefulness": 0.06, "replay_value": 0.04,
         "production_quality": 0.04, "contextual_completeness": 0.05}
    performance = sum(W[k] * cat[k] for k in W) / sum(W.values())
    # the clip's single strongest reason to exist counts: a great story is not penalised for not being funny
    best_reason = max(cat["entertainment"], cat["usefulness"], cat["emotional_impact"], cat["shareability"])
    performance = 0.75 * performance + 0.25 * best_reason

    n_sup, n_dis = c["support"], len(c["dissenters"])
    stab = vsum.get("stability_hw")
    stability = 1.0 - min(1.0, (stab if stab is not None else 0.05) / 0.08)
    conf = (0.3 * min(1.0, n_sup / 5) + 0.2 * n_sup / max(1, n_sup + n_dis) + 0.25 * stability
            + 0.15 * vsum.get("checks_ok", 1.0) + 0.1 * (1 - len(c["missing"]) / len(LENS_NAMES)))
    if c["minority"] and not vsum.get("minority_confirmed"):
        conf *= 0.8
    conf = round(float(np.clip(conf, 0, 1)), 3)
    cat["overall_confidence"] = round(100 * conf, 1)

    issues = [dict(x, gate=g) for g, r in gates.items() for x in r.get("issues", [])]
    for f in vsum.get("flags", []):
        issues.append({"sev": "block", "code": f, "detail": f"Verification found {f} on the final boundaries",
                       "gate": "verification"})
    blocks = [x for x in issues if x["sev"] == "block"]
    majors = [x for x in issues if x["sev"] == "major"]
    rejected, review = [], []
    if blocks:
        rejected += [f"{x['gate']}: {x['detail']}" for x in blocks]
    if performance < crew_cfg.get("min_score", 45):
        rejected.append(f"weak: expected performance {performance:.0f} < {crew_cfg.get('min_score', 55)}")
    if cat["contextual_completeness"] < crew_cfg.get("min_context", 50):
        rejected.append("misleading or incomplete without its context")
    if cat["production_quality"] < crew_cfg.get("min_production", 30):
        rejected.append("technically flawed source")
    if vsum.get("duplicate_of"):
        rejected.append(f"repeats {vsum['duplicate_of']}")
    if n_sup < crew_cfg.get("min_support", 4) and not c["minority"]:
        rejected.append(f"only {n_sup} reviewer(s) support it")
    if c["minority"] and not vsum.get("minority_confirmed"):
        review.append("minority discovery: one lens rates it exceptional, verification did not fully confirm it")
    review += [f"{x['gate']}: {x['detail']}" for x in majors]
    if conf < crew_cfg.get("min_confidence", 0.55):
        review.append(f"confidence {conf * 100:.0f}% is below {crew_cfg.get('min_confidence', 0.55) * 100:.0f}%")
    status = "rejected" if rejected else "review" if review else "approved"
    rank = performance * (0.7 + 0.3 * conf)
    return {"status": status, "scores": cat, "lens_scores": {k: round(v, 3) for k, v in L.items()},
            "performance": round(performance, 1), "confidence": conf, "rank_score": round(rank, 2),
            "rejected_because": rejected, "review_because": review,
            "risks": [x for x in issues if x["sev"] in ("block", "major")],
            "notes": [x for x in issues if x["sev"] in ("minor", "info")], "trend_fit": round(tf, 1)}


def why_it_works(c: dict, decision: dict) -> str:
    """A concise, faithful reason: the strongest categories and what the supporting reviewers said."""
    s = decision["scores"]
    top = sorted(((k, v) for k, v in s.items() if k not in ("overall_confidence", "safety", "clarity",
                                                           "production_quality", "contextual_completeness",
                                                           "platform_suitability")), key=lambda kv: -kv[1])[:3]
    reasons = [x["why"] for x in sorted(c["supporters"], key=lambda x: -x["score"]) if x.get("why")
               and not x["why"].startswith("rated")][:2]
    parts = ", ".join(f"{k.replace('_', ' ')} {v:.0f}" for k, v in top)
    return f"Strongest on {parts}. " + " ".join(r.rstrip(".") + "." for r in reasons)


def percentile_rank(values: list[float], v: float) -> float:
    if not values:
        return 0.5
    return float(np.mean(np.asarray(values) <= v))


def value_of(c: dict, strengths: list[float]) -> float:
    return percentile_rank(strengths, c["strength"])


def risk_of(gates: dict) -> float:
    sev = [x["sev"] for r in gates.values() for x in r.get("issues", [])]
    return 1.0 if "block" in sev else 0.6 if "major" in sev else 0.2 if "minor" in sev else 0.0


def uncertainty_of(c: dict) -> float:
    votes = c["support"] + len(c["dissenters"])
    split = len(c["dissenters"]) / votes if votes else 1.0
    return float(np.clip(0.5 * (1 - min(1.0, c["support"] / 6)) + 0.5 * math.sqrt(split) + (0.2 if c["minority"] else 0),
                         0, 1))
