"""Turns an agent's raw result into one plain sentence for the live board: what it looked at and what it
concluded (a score, the best moment it found with its opening words, the problem it flagged, ...)."""
from __future__ import annotations

from ..media import fmt_ts


def _quote(ev, i: int, j: int, n: int = 70) -> str:
    try:
        t = " ".join(ev.span_text(int(i), int(j)).split())
    except Exception:
        return ""
    return f' "{t[:n].rstrip()}{"..." if len(t) > n else ""}"' if t else ""


def _at(ev, i: int, j: int) -> str:
    try:
        return f"{fmt_ts(float(ev.S[int(i)]))}-{fmt_ts(float(ev.E[int(j)]))}"
    except Exception:
        return ""


def describe(a, result, ev=None) -> str:
    """One sentence about an assignment's result. Never raises."""
    try:
        return _describe(a, result, ev)[:220]
    except Exception:
        return "Finished and submitted its sealed finding"


def _describe(a, r, ev) -> str:
    kind = a.kind
    if r is None:
        return "Found nothing worth reporting"
    if kind == "scout" and isinstance(r, dict):
        sc, I, J = r.get("score") or [], r.get("I") or [], r.get("J") or []
        if not sc:
            return f"Read section {r.get('section', '')}: {r.get('windows', 0)} windows, nothing strong enough"
        k = max(range(len(sc)), key=lambda x: sc[x])
        if sc[k] < 0.15:
            return (f"Checked {r.get('windows', len(sc))} windows in section {r.get('section', '')}: nothing strong "
                    f"enough for this lens (best {_at(ev, I[k], J[k])} only scored {sc[k] * 100:.0f})")
        return (f"Checked {r.get('windows', len(sc))} windows; best {_at(ev, I[k], J[k])} scored {sc[k] * 100:.0f}"
                + _quote(ev, I[k], J[k]))
    if kind == "plan" and isinstance(r, list):
        return f"Cut the video into {len(r)} overlapping sections so every second is read"
    if kind == "consensus" and isinstance(r, dict):
        c = r.get("candidates") or []
        top = max(c, key=lambda x: x.get("strength", 0), default=None)
        return (f"Merged {r.get('nominations', 0)} sealed nominations into {len(c)} candidates"
                + (f"; strongest {fmt_ts(top['start'])}-{fmt_ts(top['end'])} backed by {top.get('support', '?')} agents"
                   if top else ""))
    if kind == "dedupe" and isinstance(r, dict):
        return f"Kept {len(r.get('kept', []))} distinct moments, dropped {len(r.get('rejected', []))} repeats/overlaps"
    if kind == "shortlist" and isinstance(r, dict):
        return f"Shortlisted {len(r.get('shortlist', []))} finalists, turned down {len(r.get('rejected', []))}"
    if kind == "decide" and isinstance(r, list):
        st = [x.get("status", "?") for x in r if isinstance(x, dict)]
        return "Final call: " + ", ".join(f"{st.count(s)} {s}" for s in dict.fromkeys(st)) if st else "No finalists to decide"
    if kind == "coverage" and isinstance(r, dict):
        g = r.get("gaps") or []
        return ("Coverage proof: every second of the video was read by every lens" if r.get("complete")
                else f"Coverage gap: {len(g)} stretch(es) not fully read - sent back for another pass")
    if isinstance(r, dict):
        if "score" in r and "issues" in r:  # gate reviewers
            issues = r.get("issues") or []
            first = issues[0] if issues else None
            first = first.get("text") or first.get("issue") or str(first) if isinstance(first, dict) else first
            facts = r.get("facts") or {}
            f = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in list(facts.items())[:2])
            return (f"Scored {r['score']:.0f}/100" + (f" ({f})" if f else "")
                    + (f" - flagged: {first}" if first else " - no problems found"))
        if "nominations" in r:  # full-video reviewers
            n = r["nominations"]
            best = max(n, key=lambda x: x.get("score", x.get("strength", 0)), default=None) if n else None
            where = ""
            if best and "i" in best and "j" in best and ev is not None:
                where = f"; top pick {_at(ev, best['i'], best['j'])}" + _quote(ev, best["i"], best["j"], 50)
            return (r.get("note") or f"{len(n)} nominations") + where
        if "skip" in r:
            return f"Skipped: {r['skip']}"
        if "note" in r:  # verification probes
            ok = r.get("ok")
            return ("Passed: " if ok is True else "Failed: " if ok is False else "") + str(r["note"])
        if kind == "map":
            nice = {"cuts": "scene cuts", "speakers": "speakers", "topics": "topics", "zones": "story zones",
                    "chapters": "chapters", "names": "names/entities", "turns": "speaker turns"}
            parts = [f"{r[k]} {v}" for k, v in nice.items() if isinstance(r.get(k), (int, float))]
            extra = r.get("method") or r.get("note")
            return "Mapped the whole video: " + ", ".join(parts) + (f" ({extra})" if extra else "")
        nums = [f"{k.replace('_', ' ')} {v}" for k, v in r.items() if isinstance(v, (int, float, str)) and len(str(v)) < 40]
        return "Result: " + ", ".join(nums[:4]) if nums else "Submitted its sealed finding"
    if isinstance(r, list):
        return f"Submitted {len(r)} findings"
    return f"Result: {str(r)[:120]}"
