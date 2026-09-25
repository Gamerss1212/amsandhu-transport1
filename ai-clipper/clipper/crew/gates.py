"""Gate reviewers: 16 independent checks of quality, integrity and risk for each shortlisted clip.

Every gate returns a 0-100 score, a list of issues and, for boundary and context gates, a proposal to move
a boundary. Issue severity decides what happens to the clip:
  block  - the clip cannot be posted as is (personal data, slurs, cut mid-sentence, misleading cut...)
  major  - a person must look at it before it is posted (health claim, accusation, music bed...)
  minor  - worth knowing, handled automatically (fast captions, nobody in frame...)
  info   - context for the report
Gates never see each other's verdicts; the ranking coordinator combines them afterwards.
"""
from __future__ import annotations

import re
import subprocess
import wave

import numpy as np

from ..media import ffmpeg_exe, fmt_ts
from . import lexicon as lx
from .evidence import Evidence, norm_words


def issue(sev: str, code: str, detail: str, t: float | None = None) -> dict:
    d = {"sev": sev, "code": code, "detail": detail}
    if t is not None:
        d["at"] = round(t, 1)
    return d


def _result(score: float, issues: list[dict], **extra) -> dict:
    return {"score": round(float(np.clip(score, 0, 100)), 1), "issues": issues, **extra}


def _text(ev: Evidence, c: dict) -> str:
    return ev.span_text(c["i"], c["j"])


def audio_slice(ev: Evidence, a: float, b: float) -> np.ndarray | None:
    """16 kHz mono samples of [a, b] from the analysis audio."""
    if ev.wav is None:
        return None
    a = max(0.0, a)
    try:
        if str(ev.wav).lower().endswith(".wav"):
            with wave.open(str(ev.wav), "rb") as w:
                sr = w.getframerate()
                w.setpos(min(w.getnframes(), int(a * sr)))
                raw = w.readframes(int((b - a) * sr))
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if w.getnchannels() > 1:
                    x = x.reshape(-1, w.getnchannels()).mean(1)
                return x if sr == 16000 else x[:: max(1, sr // 16000)]
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{a:.2f}", "-i", str(ev.wav),
                              "-t", f"{b - a:.2f}", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                             capture_output=True, timeout=60).stdout
        return np.frombuffer(out[: len(out) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return None


def _frames_db(x: np.ndarray, hop: int = 1600) -> np.ndarray:
    n = len(x) // hop
    if n == 0:
        return np.zeros(0)
    return 10 * np.log10(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-10)


def _sample_times(c: dict, n: int, lo: float = 0.08, hi: float = 0.92) -> list[float]:
    d = c["end"] - c["start"]
    return [c["start"] + d * (lo + (hi - lo) * k / max(1, n - 1)) for k in range(n)]


# ---------------------------------------------------------------- technical
def g_visual(ev: Evidence, c: dict) -> dict:
    if ev.video is None:
        return _result(65, [issue("info", "audio_only", "Very long video: only audio was downloaded - the picture "
                                                        "is checked by the export inspector after the clip is fetched")])
    import cv2

    black = faces = slides = dark = 0
    times = _sample_times(c, 5)
    sizes = []
    for t in times:
        img = ev.frame(t)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        m, sd = float(gray.mean()), float(gray.std())
        if m < 18 and sd < 12:
            black += 1
            continue
        dark += m < 45
        f = ev.faces(t) or []
        if f:
            faces += 1
            sizes.append(max(x[2] for x in f))
        edges = float((cv2.Canny(gray, 80, 160) > 0).mean())
        slides += (not f) and edges > 0.12
    n = len(times)
    issues = []
    if black >= 2:
        issues.append(issue("major", "black", f"{black}/{n} sampled frames are black or empty"))
    if faces == 0:
        issues.append(issue("minor", "no_face", "Nobody in shot in the sampled frames - the full-frame layout is used"))
    if slides >= 2:
        issues.append(issue("minor", "slides", "Looks like slides or on-screen text for much of the clip"))
    if dark >= 3:
        issues.append(issue("minor", "dark", "The picture is dark; the colour grade will lift it"))
    face_frac = faces / n
    size = float(np.median(sizes)) if sizes else 0.0
    score = 100 * (0.45 * face_frac + 0.15 * min(1.0, size / 0.12) + 0.2 * (1 - black / n) + 0.2 * (1 - slides / n))
    return _result(score, issues, facts={"face_frames": f"{faces}/{n}", "face_size": round(size, 3)})


def g_camera(ev: Evidence, c: dict) -> dict:
    if ev.scenes is None or ev.video is None:
        return _result(70, [issue("info", "no_scene_map", "Scene changes are checked on the downloaded clip")])
    cuts = [t for t in ev.scenes if c["start"] < t < c["end"]]
    per_min = len(cuts) / max(0.1, (c["end"] - c["start"]) / 60)
    issues = []
    if cuts and c["end"] - cuts[-1] < 0.6:
        issues.append(issue("minor", "cut_at_end", "A new shot starts in the last half second", cuts[-1]))
    if per_min > 15:
        issues.append(issue("minor", "busy", f"{per_min:.0f} shot changes a minute - reframing works harder"))
    score = 70 if not cuts else 92 if per_min <= 8 else 80 if per_min <= 15 else 60
    return _result(score, issues, facts={"cuts": len(cuts), "per_minute": round(per_min, 1)})


def g_faces(ev: Evidence, c: dict) -> dict:
    if ev.video is None:
        return _result(65, [issue("info", "audio_only", "Faces are checked on the downloaded clip")])
    import cv2

    seen = moving = 0
    times = _sample_times(c, 6)
    for t in times:
        f = ev.faces(t) or []
        if not f:
            continue
        seen += 1
        a, b = ev.frame(t), ev.frame(t + 0.4)
        if a is None or b is None:
            continue
        h, w = a.shape[:2]
        cx, cy, fw = max(f, key=lambda x: x[2])
        x0, x1 = int(max(0, (cx - fw / 2) * w)), int(min(w, (cx + fw / 2) * w))
        y0, y1 = int(max(0, (cy - fw / 2) * h)), int(min(h, (cy + fw / 1.2) * h))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        diff = cv2.absdiff(cv2.cvtColor(a[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY),
                           cv2.cvtColor(b[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY))
        moving += float(diff.mean()) > 4.0
    frac = seen / len(times)
    issues = [] if frac >= 0.3 else [issue("minor", "faces_rare", "Faces are rarely visible")]
    score = 100 * (0.6 * frac + 0.4 * (moving / max(1, seen)))
    return _result(score, issues, facts={"face_frames": f"{seen}/{len(times)}", "animated": moving})


def g_audio(ev: Evidence, c: dict) -> dict:
    x = audio_slice(ev, c["start"], c["end"])
    if x is None or len(x) < 16000:
        return _result(60, [issue("info", "no_audio_sample", "Audio could not be sampled - the export inspector "
                                                             "measures the final file")])
    db = _frames_db(x)
    speech, floor = float(np.percentile(db, 90)), float(np.percentile(db, 10))
    snr = speech - floor
    clipped = float(np.mean(np.abs(x) > 0.985))
    issues = []
    if clipped > 0.005:
        issues.append(issue("major", "clipping", f"{clipped * 100:.1f}% of samples are clipped (distorted source)"))
    if snr < 10:
        issues.append(issue("minor", "noisy", f"Voice only {snr:.0f} dB above the background - noise reduction on"))
    if speech < -38:
        issues.append(issue("minor", "quiet", "Quiet source - loudness is normalised to -14 LUFS in the edit"))
    low = db < -55
    run = best = 0
    for k, v in enumerate(low):
        run = run + 1 if v else 0
        if 5 < k < len(low) - 5:
            best = max(best, run)
    if best * 0.1 > 2.0:
        issues.append(issue("major", "dropout", f"Audio drops out for {best * 0.1:.1f}s"))
    score = 100 * (0.5 * min(1.0, snr / 30) + 0.3 * (1 - min(1.0, clipped * 50)) + 0.2 * (speech > -38))
    return _result(score, issues, facts={"snr_db": round(snr, 1), "clipped": round(clipped, 4)})


def _music_ratio(ev: Evidence, c: dict) -> tuple[float, str]:
    """Share of the pauses between words that are filled with sustained sound (a music bed)."""
    tags = lx.count("music_tag", " ".join(ev.segments[k]["text"] for k in range(c["i"], c["j"] + 1)))
    x = audio_slice(ev, c["start"], c["end"])
    if x is None or len(x) < 16000:
        return (0.8 if tags else 0.0), "transcript tags" if tags else "unknown"
    db = _frames_db(x)
    speaking = np.zeros(len(db), dtype=bool)
    for w in ev.words:
        if w["e"] < c["start"] or w["s"] > c["end"]:
            continue
        a, b = int((w["s"] - c["start"]) / 0.1), int((w["e"] - c["start"]) / 0.1) + 1
        speaking[max(0, a - 1):min(len(db), b + 1)] = True
    gaps = db[~speaking]
    if len(gaps) < 8:
        return (0.8 if tags else 0.0), "too little silence between words to tell"
    speech = float(np.percentile(db[speaking], 70)) if speaking.any() else float(np.percentile(db, 90))
    filled = float(np.mean(gaps > speech - 16))
    steady = float(np.std(gaps)) < 4.0
    ratio = min(1.0, filled * (1.3 if steady else 0.8) + (0.4 if tags else 0))
    return ratio, "sound between words"


def g_music(ev: Evidence, c: dict) -> dict:
    ratio, how = _music_ratio(ev, c)
    issues = [issue("minor", "music_bed", f"Background music likely ({ratio * 100:.0f}% of pauses filled; {how})")] \
        if ratio >= 0.5 else []
    return _result(100 - 60 * ratio, issues, facts={"music_ratio": round(ratio, 2)})


def g_captions(ev: Evidence, c: dict) -> dict:
    text = _text(ev, c)
    words = norm_words(text)
    wps = len(words) / max(0.1, c["end"] - c["start"])
    long = sum(len(w) > 12 for w in words)
    starred = lx.count("profanity", text)
    issues = []
    if wps > 4.2:
        issues.append(issue("minor", "fast", f"{wps:.1f} words/s - captions show fewer words at a time"))
    if starred:
        issues.append(issue("info", "starred", f"{starred} explicit word(s) are starred out in on-screen text"))
    score = 100 - max(0, wps - 3.2) * 18 - long * 2
    return _result(score, issues, facts={"words_per_s": round(wps, 2)})


def g_subtitles(ev: Evidence, c: dict) -> dict:
    ws = [w for w in ev.words if c["start"] - 0.05 <= w["s"] <= c["end"] + 0.05]
    if not ws:
        return _result(0, [issue("block", "no_words", "No transcript words inside the clip")])
    zero = sum(w["e"] - w["s"] < 0.02 for w in ws)
    overlap = sum(b["s"] < a["e"] - 0.2 for a, b in zip(ws, ws[1:]))
    toks = [re.sub(r"[^\w']", "", w["w"].lower()) for w in ws]
    loops = 0
    for k in range(len(toks) - 8):
        g = tuple(toks[k:k + 3])
        if g == tuple(toks[k + 3:k + 6]) == tuple(toks[k + 6:k + 9]) and any(g):
            loops += 1
    junk = sum(not re.search(r"[a-z0-9]", t) for t in toks)
    issues = []
    if loops:
        issues.append(issue("major", "loop", "Speech recognition repeated itself (a transcription loop) - check the words"))
    if zero > 0.1 * len(ws):
        issues.append(issue("minor", "timing", f"{zero} words have no length - caption timing is approximate"))
    if ev.source.startswith("youtube"):
        issues.append(issue("info", "auto_captions", "Words come from YouTube captions"))
    score = 100 - 25 * loops - 40 * zero / len(ws) - 30 * overlap / len(ws) - 30 * junk / len(ws)
    return _result(score, issues, facts={"words": len(ws), "source": ev.source})


# ---------------------------------------------------------------- integrity
def g_boundary(ev: Evidence, c: dict) -> dict:
    i, j = c["i"], c["j"]
    issues, proposal = [], {}
    prev_open = i > 0 and ev.ends_open[i - 1]
    if ev.start_flaw[i]:
        issues.append(issue("block", "start", f"Starts {ev.start_flaw[i]}", ev.S[i]))
    elif prev_open and ev.S[i] - ev.E[i - 1] < 0.3:
        issues.append(issue("major", "start_joined", "The line before runs straight into the first line", ev.S[i]))
    if ev.ends_open[j]:
        nxt = next((k for k in range(j + 1, min(ev.n, j + 4)) if not ev.ends_open[k]), None)
        if nxt is not None and ev.E[nxt] - ev.S[i] <= ev.max_s * 1.1:
            proposal["j"] = nxt
            issues.append(issue("minor", "end_extended", "Ends mid-thought - extended to the end of the thought", ev.E[j]))
        else:
            issues.append(issue("block", "end", "Ends mid-thought and cannot be extended within the length limit",
                                ev.E[j]))
    # a short punchline right after the end, followed by a laugh, belongs in the clip
    k = j + 1
    if k < ev.n and ev.S[k] - ev.E[j] < 0.6 and len(ev.sents[k].words) <= 7 and \
            ((ev.cmax("laughs", ev.E[k], ev.E[k] + 3) or 0) > 0 or ev.count["laugh"][k] > 0) and \
            ev.E[k] - ev.S[i] <= ev.max_s * 1.1:
        proposal["j"] = max(proposal.get("j", 0), k)
        issues.append(issue("minor", "punchline", f"The punchline {ev.text[k]!r} comes right after - included", ev.S[k]))
    tail = ev.cmax("laughs", ev.E[j], ev.E[j] + 3)
    if tail:
        proposal["land"] = True
        issues.append(issue("info", "reaction", "A laugh follows the last line - the clip runs on to let it land"))
    score = 100 - sum({"block": 60, "major": 25, "minor": 6, "info": 0}[x["sev"]] for x in issues)
    return _result(score, issues, proposal=proposal)


def _referent_gap(ev: Evidence, i: int, j: int, lookback: float = 90.0) -> tuple[str, float] | None:
    """A clip opening on "he"/"they" whose name is only said before the clip."""
    if not ev.ctx_start[i]:
        return None
    inside = ev.span_text(i, j)
    for name, times in ev.names.items():
        before = [t for t in times if ev.S[i] - lookback <= t < ev.S[i]]
        if before and name not in inside:
            return name, before[-1]
    return None


def g_context(ev: Evidence, c: dict) -> dict:
    i, j = c["i"], c["j"]
    issues, proposal = [], {}
    budget = ev.max_s * 1.1
    # the clip opens on an answer: the question just before it belongs in the clip
    if i > 0 and ev.is_q[i - 1] and ev.S[i] - ev.E[i - 1] < 3.0:
        if ev.E[j] - ev.S[i - 1] <= budget:
            proposal["i"] = i - 1
            issues.append(issue("minor", "question_added", f"Opens on an answer - the question {ev.text[i - 1]!r} is included",
                                ev.S[i - 1]))
        else:
            issues.append(issue("major", "answer_only", "Opens on an answer; the question does not fit in the clip",
                                ev.S[i]))
    # the line just before sets the moment up (same breath, a strong opener itself): it belongs in the clip
    k = proposal.get("i", i) - 1
    if k >= 0 and ev.start_ok[k] and ev.S[k + 1] - ev.E[k] < 1.0 and ev.hook[k] >= max(0.55, ev.hook[k + 1] - 0.15) and \
            not ev.is_q[k] and ev.speaker[k] == ev.speaker[k + 1] and ev.E[j] - ev.S[k] <= budget:
        proposal["i"] = k
        issues.append(issue("minor", "setup_added", f"The setup line {ev.text[k]!r} is included", ev.S[k]))
    ref = _referent_gap(ev, proposal.get("i", i), j)
    if ref:
        name, t = ref
        k = ev.idx_at(t)
        if ev.E[j] - ev.S[k] <= budget and ev.start_ok[k]:
            proposal["i"] = min(proposal.get("i", i), k)
            issues.append(issue("minor", "referent_added", f"Opens on '{ev.first[i]}' - starts earlier so {name} is named", t))
        else:
            issues.append(issue("major", "referent", f"Opens on '{ev.first[i]}' - who? ({name} is only named at {fmt_ts(t)})",
                                ev.S[i]))
    if ev.wsum("backref", [i], [j])[0] > 0:
        issues.append(issue("major", "backref", "Refers back to something said earlier"))
    before = " ".join(ev.text[k] for k in range(max(0, i - 4), i) if ev.S[i] - ev.S[k] <= 25)
    if lx.count("framing", before):
        issues.append(issue("major", "framing", "Said as a hypothetical / devil's advocate just before the clip"))
    for k in range(j + 1, min(ev.n, j + 5)):
        if ev.S[k] - ev.E[j] > 20:
            break
        if lx.count("sarcasm", ev.text[k]):  # the speaker takes it back right after: keep that in, or drop the clip
            if ev.E[k] - ev.S[proposal.get("i", i)] <= budget:
                proposal["j"] = k
                issues.append(issue("minor", "reversal_kept", "The speaker takes it back right after - kept in the clip",
                                    ev.S[k]))
            else:
                issues.append(issue("block", "reversed", "The speaker takes it back right after the clip ends "
                                                         "('just kidding') and it does not fit in the clip"))
            break
    score = 100 - sum({"block": 60, "major": 25, "minor": 5, "info": 0}[x["sev"]] for x in issues)
    return _result(score, issues, proposal=proposal)


def g_claims(ev: Evidence, c: dict) -> dict:
    issues, claims = [], []
    for k in range(c["i"], c["j"] + 1):
        t = ev.text[k]
        if not lx.count("claim", t):
            continue
        hedged = lx.count("hedge", t) > 0
        domain = next((d for d in ("health", "finance", "election") if lx.count(d, t)), "")
        claims.append({"at": round(float(ev.S[k]), 1), "text": t[:160], "domain": domain, "hedged": hedged})
        if domain and not hedged:
            issues.append(issue("major", f"{domain}_claim", f"Unverified {domain} claim: {t[:100]!r} - check before posting",
                                ev.S[k]))
        else:
            issues.append(issue("minor" if not hedged else "info", "claim", f"Claim: {t[:100]!r}", ev.S[k]))
    score = 100 - 25 * sum(x["sev"] == "major" for x in issues) - 6 * sum(x["sev"] == "minor" for x in issues)
    return _result(score, issues, claims=claims)


def g_misinfo(ev: Evidence, c: dict) -> dict:
    text = _text(ev, c)
    issues = []
    hits = lx.hits("misinfo", text)
    consp = lx.hits("conspiracy", text)
    if hits and consp:
        issues.append(issue("block", "misinfo", f"Repeats known misinformation ({hits[0]!r}) with conspiracy framing"))
    elif hits:
        issues.append(issue("major", "misinfo", f"Mentions a known misinformation claim ({hits[0]!r}) - check how it is framed"))
    elif consp:
        issues.append(issue("minor", "conspiracy", f"Conspiracy framing ({consp[0]!r})"))
    absolute_health = re.search(r"\b(guaranteed|100 percent|always works|cures?)\b", text, re.I) and \
        (lx.count("health", text) or lx.count("finance", text))
    if absolute_health:
        issues.append(issue("major", "absolute_claim", "Absolute health or money promise"))
    score = 100 - sum({"block": 70, "major": 30, "minor": 10, "info": 0}[x["sev"]] for x in issues)
    return _result(score, issues)


def g_privacy(ev: Evidence, c: dict) -> dict:
    text = _text(ev, c)
    issues = []
    for key, what in (("pii_phone", "a phone number"), ("pii_email", "an email address"),
                      ("pii_address", "a street address"), ("pii_secret", "account / identity numbers")):
        if lx.count(key, text):
            issues.append(issue("block", key, f"Contains {what}"))
    if lx.count("minor", text) and lx.count("family", text):
        issues.append(issue("minor", "child", "Talks about a child - make sure nothing identifies them"))
    score = 100 - 70 * sum(x["sev"] == "block" for x in issues) - 8 * sum(x["sev"] == "minor" for x in issues)
    return _result(score, issues)


def g_brand(ev: Evidence, c: dict) -> dict:
    text = _text(ev, c)
    n = max(1, len(norm_words(text)))
    issues = []
    prof = lx.count("profanity", text)
    if lx.count("hate", text):
        issues.append(issue("block", "hate", "Contains a slur or a statement against a whole group"))
    if prof / n > 0.04:
        issues.append(issue("major", "profanity", f"Heavy profanity ({prof} words) - many brands avoid this"))
    elif prof:
        issues.append(issue("minor", "profanity", f"{prof} explicit word(s) (starred on screen)"))
    for key, label in (("sexual", "sexual content"), ("drugs", "drug use"), ("violence", "violence"),
                       ("self_harm", "self-harm")):
        k = lx.count(key, text)
        if k >= 2 or (key == "self_harm" and k):
            issues.append(issue("major", key, f"Talks about {label} ({k} mentions)"))
        elif k:
            issues.append(issue("info", key, f"Mentions {label}"))
    score = 100 - sum({"block": 70, "major": 22, "minor": 6, "info": 2}[x["sev"]] for x in issues)
    return _result(score, issues)


def g_copyright(ev: Evidence, c: dict) -> dict:
    meta = ev.meta
    issues = []
    third_party = bool(meta.get("webpage_url") or re.fullmatch(r"[\w-]{11}", str(meta.get("id") or "")))
    blob = f"{meta.get('title', '')} {meta.get('channel', '')}"
    if lx.BROADCAST.search(blob):
        issues.append(issue("major", "broadcast", "Looks like broadcast / licensed footage (sports, TV, trailer, music video)"))
    # an independent music check: long loud stretches with no words inside the clip
    e = ev.curves.get("energy")
    quiet_words = 0
    if e is not None:
        spoken = np.zeros(int(c["end"] - c["start"]) + 2, dtype=bool)
        for w in ev.words:
            if c["start"] <= w["s"] <= c["end"]:
                spoken[int(w["s"] - c["start"])] = True
        loud = e[int(c["start"]):int(c["start"]) + len(spoken)] > 0.6
        run = best = 0
        for s, l in zip(spoken, loud):
            run = run + 1 if (l and not s) else 0
            best = max(best, run)
        quiet_words = best
    tags = lx.count("music_tag", " ".join(ev.segments[k]["text"] for k in range(c["i"], c["j"] + 1)))
    if quiet_words >= 5 or tags:
        issues.append(issue("major", "music", "Music without speech inside the clip - copyrighted audio risk"))
    if third_party:
        issues.append(issue("info", "third_party", f"Content from {meta.get('channel') or 'another creator'}: post only with "
                                                   "permission or under the creator's clipping program"))
    score = 100 - 30 * sum(x["sev"] == "major" for x in issues) - (10 if third_party else 0)
    return _result(score, issues, third_party=third_party)


def g_policy(ev: Evidence, c: dict) -> dict:
    text = _text(ev, c)
    issues = []
    platforms = {"tiktok": "ok", "instagram": "ok", "youtube": "ok"}
    if lx.count("hate", text):
        issues.append(issue("block", "hate", "Hate speech rules (all platforms)"))
        platforms = dict.fromkeys(platforms, "removed")
    if lx.count("minor", text) and lx.count("sexual", text):
        issues.append(issue("block", "minors", "Minor safety rules (all platforms)"))
        platforms = dict.fromkeys(platforms, "removed")
    if lx.count("dangerous", text):
        issues.append(issue("major", "dangerous", "Dangerous acts - may be limited or age-restricted"))
    if lx.count("self_harm", text):
        issues.append(issue("major", "self_harm", "Suicide / self-harm talk - platforms add resources or limit reach"))
    if lx.count("accuse", text):
        issues.append(issue("major", "harassment", "Accuses a person - harassment / defamation rules"))
    if lx.count("drugs", text) >= 2 or lx.count("weapons", text) >= 2 or lx.count("gambling", text) >= 2:
        issues.append(issue("major", "regulated", "Drugs / weapons / gambling - regulated-goods rules"))
        platforms["tiktok"] = platforms["instagram"] = "limited"
    if lx.count("sexual", text) >= 2:
        issues.append(issue("major", "sexual", "Sexual talk - often limited on TikTok and Instagram"))
        platforms["tiktok"] = platforms["instagram"] = "limited"
    if lx.count("misinfo", text):
        issues.append(issue("major", "misinfo_policy", "Misinformation rules may apply"))
    score = 100 - sum({"block": 70, "major": 22, "minor": 6, "info": 0}[x["sev"]] for x in issues)
    return _result(score, issues, platforms=platforms)


def g_reputation(ev: Evidence, c: dict) -> dict:
    i, j = c["i"], c["j"]
    text = _text(ev, c)
    issues, proposal = [], {}
    if lx.count("accuse", text):
        issues.append(issue("major", "accusation", "Accuses someone of a crime or wrongdoing - defamation risk"))
    if lx.count("hate", text):
        issues.append(issue("block", "slur", "A slur would follow the speaker and the account"))
    for k in range(j + 1, min(ev.n, j + 4)):
        if ev.S[k] - ev.E[j] > 15:
            break
        if lx.count("sarcasm", ev.text[k]):
            if ev.E[k] - ev.S[i] <= ev.max_s * 1.1:
                proposal["j"] = k
                issues.append(issue("minor", "sarcasm_kept", "The 'just kidding' right after is kept in the clip", ev.S[k]))
            else:
                issues.append(issue("block", "sarcasm_cut", "Cutting before 'just kidding' would misrepresent the speaker",
                                    ev.S[k]))
            break
    if lx.count("debate", text) and lx.count("absolute", text) >= 2 and lx.count("profanity", text) >= 2:
        issues.append(issue("minor", "heated", "Heated, absolute take on a divisive subject"))
    score = 100 - sum({"block": 70, "major": 28, "minor": 6, "info": 0}[x["sev"]] for x in issues)
    return _result(score, issues, proposal=proposal)


GATES = {"g_visual": ("composition", g_visual), "g_camera": ("camera", g_camera), "g_faces": ("expressions", g_faces),
         "g_audio": ("audio_quality", g_audio), "g_music": ("music", g_music), "g_captions": ("captions", g_captions),
         "g_subtitles": ("subtitle_accuracy", g_subtitles), "g_boundary": ("boundaries", g_boundary),
         "g_context": ("context", g_context), "g_claims": ("claims", g_claims), "g_misinfo": ("misinformation", g_misinfo),
         "g_privacy": ("privacy", g_privacy), "g_brand": ("brand_safety", g_brand), "g_copyright": ("copyright", g_copyright),
         "g_policy": ("platform_policy", g_policy), "g_reputation": ("reputation", g_reputation)}
RISK_GATES = ("claims", "misinformation", "privacy", "brand_safety", "copyright", "platform_policy", "reputation")
