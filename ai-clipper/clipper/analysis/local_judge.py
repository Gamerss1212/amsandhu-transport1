"""Built-in clip finder and judge - works with no API key.

Every sentence of the transcript is tried as a clip start, with several lengths, and each
window is scored on what makes short clips work:
  hook        does the first sentence grab attention on its own?
  standalone  does it start cleanly (not "and...", "he said...") and end on a finished thought?
  payoff      does it end on a laugh, a punchline or a loud moment?
  intensity   emotional / shocking / funny / money words, specifics (numbers), loudness
  pace        words per second, dead air, filler words
  length      how close the length is to what goes viral right now
Fatal flaws (starting or ending mid-thought, dead air) reject a clip outright.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from ..trends.analyzer import HOOK_PATTERNS
from .signals import window_score

CONJ = {"and", "but", "so", "because", "or", "cause", "plus", "then", "also", "anyway", "anyways", "which"}
SOFT_START = {"um", "uh", "like", "yeah", "yes", "right", "okay", "ok", "well", "mm", "hmm", "oh", "no",
              "yep", "sure", "exactly", "true", "totally", "absolutely", "mhm", "mhmm", "correct", "wow"}
CONTEXT_START = {"he", "she", "they", "it", "that", "those", "these", "them", "his", "her", "their",
                 "there", "who", "him", "its", "that's", "it's", "they're", "he's", "she's"}
BACKREF = re.compile(r"\b(as i (said|mentioned)|like i said|going back to|earlier you|you (just )?mentioned|"
                     r"to your point|what you said|that point)\b")
FILLERS = {"um", "uh", "uhm", "umm", "uhh", "erm", "er", "ah", "hmm", "mm", "mhm"}
STRONG_OPEN = re.compile(
    r"^(the (biggest|worst|best|craziest|only|number one|real|truth|secret|reason|problem|mistake|thing)|"
    r"nobody|no one|never|if you|you (need|have|should|will|won't|can't|don't|are|were)|here's|here is|"
    r"what (if|most|nobody|people)|why (do|does|is|are)|how (do|does|to)|i (never|always|lost|made|quit|"
    r"got|was|had|remember|used to|nearly|almost|just)|my (dad|mom|father|mother|wife|husband|son|"
    r"daughter|first|biggest|brother|sister)|when i was|the day|one day|imagine|stop|most people|"
    r"everyone|everybody|this is (why|how|the)|let me tell you|i'll never forget|\d)")
INTENSE = re.compile(
    r"\b(died|dead|death|kill(ed)?|fired|million|billion|prison|jail|cancer|divorce|cheat(ed)?|broke|"
    r"homeless|addict(ed|ion)?|panic|scared|terrified|cry(ing)?|cried|insane|crazy|worst|best|secret|"
    r"lie|lied|lying|truth|shock(ing|ed)?|hate|war|fight|money|rich|famous|dangerous|illegal|"
    r"destroy(ed)?|never|nobody|everyone|biggest|craziest|unbelievable|shit|fuck(ing)?|damn|"
    r"hell|suicide|depress(ed|ion)|anxiety|trauma|abuse|attack(ed)?|arrest(ed)?|lawsuit|bankrupt|"
    r"quit|lost|failure|failed|regret|ashamed|embarrass(ed|ing)?|obsess(ed)?|wild|nuts|ridiculous)\b")
LAUGH = re.compile(r"\[(laughter|laughs|laughing)\]|\((laughter|laughs|laughing)\)|\bha(ha)+\b|\blol\b", re.I)
NUMBER = re.compile(r"(\$\s?\d|\b\d[\d,.]*\s?(%|percent|k|million|billion|years?|days?|months?|hours?|"
                    r"times|pounds|kilos)?\b)")
PUNCHLINE = re.compile(r"\b(that's why|that's the|and that's|turns out|the answer|which is why|the lesson|"
                       r"and i (was|said) like|never again|i'm done|that's it|the end|changed my life|"
                       r"that's what|that's how|and it worked|best decision|worst decision)\b")
CATEGORY_OF = {"funny": "funny", "shock": "shocking", "emotional": "emotional", "controversial": "controversial",
               "story": "story", "howto": "educational", "money": "insightful", "reaction": "drama",
               "question": "insightful", "pov": "story", "number_list": "educational",
               "watch_till_end": "story"}
CAPTION_PROMPT = {
    "funny": "I can't with this 😂 Who else lost it?",
    "shocking": "Wait... is this real?? 🤯",
    "emotional": "This one hit different ❤️",
    "controversial": "Agree or disagree? 👇",
    "story": "Wait for the ending 👀",
    "educational": "Save this for later 📌",
    "insightful": "Nobody talks about this 👇",
    "drama": "He really said that 😳",
    "motivational": "Needed to hear this today 🔥",
}


def _clean(text: str, keep_laughs: bool = True) -> str:
    text = re.sub(r"\[(?!laugh)[^\]]*\]" if keep_laughs else r"\[[^\]]*\]", " ", text)
    return re.sub(r"\s+", " ", text.replace(">>", " ")).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9$%']+", text.lower())


@dataclass
class Seg:
    s: float
    e: float
    text: str
    words: list[str]
    hook: float        # 0-1 strength as an opening sentence
    start_flaw: str     # why it can't open a clip ("" if fine)
    ends_open: bool     # the thought obviously continues into the next sentence
    clean_break: bool   # a pause or the other person starts talking right after


def _hook_strength(text: str, words: list[str], profile_lift: dict) -> float:
    low = text.lower()
    score = 0.25
    if STRONG_OPEN.search(low):
        score += 0.3
    if "?" in text:
        score += 0.15 if len(words) >= 7 else 0.04
    hooks = [h for h, pat in HOOK_PATTERNS.items() if re.search(pat, low)]
    for h in hooks:
        lift = profile_lift.get(h, 1.2)
        score += 0.08 * max(0.3, min(lift, 2.5))
    score += 0.06 * min(3, len(INTENSE.findall(low)))
    score += 0.06 if NUMBER.search(low) else 0.0
    n = len(words)
    if n < 4:
        score -= 0.25
    elif n > 30:
        score -= 0.1
    return float(np.clip(score, 0.0, 1.0))


def _start_flaw(words: list[str], text: str) -> str:
    if not words:
        return "empty"
    first = words[0]
    if text[:1].islower():
        return "starts mid-sentence"
    if first in CONJ:
        return f'starts mid-thought ("{first}...")'
    if first in SOFT_START and len(words) < 6:
        return f'weak opener ("{first}...")'
    if BACKREF.search(text.lower()):
        return "refers back to something earlier"
    return ""


def build_segments(segments: list[dict], profile: dict | None) -> list[Seg]:
    lift = {r["feature"]: r["lift"] for r in (profile or {}).get("hook_lift", [])}
    out = []
    for i, s in enumerate(segments):
        text = _clean(s["text"])
        words = _words(text)
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        nxt_words = _words(_clean(nxt["text"])) if nxt else []
        new_speaker = bool(nxt and nxt["text"].lstrip().startswith(">>"))
        gap = (nxt["s"] - s["e"]) if nxt else 99
        ends_open = bool(nxt_words and nxt_words[0] in CONJ and not new_speaker and gap < 1.0) or \
            text.rstrip().endswith((",", "-", "..."))
        clean_break = nxt is None or new_speaker or gap >= 0.7
        out.append(Seg(s["s"], s["e"], text, words, _hook_strength(text, words, lift),
                       _start_flaw(words, text), ends_open, clean_break))
    return out


def _length_score(dur: float, profile: dict | None) -> float:
    d = (profile or {}).get("viral_duration") or {}
    lo, mid, hi = d.get("p25") or 22.0, d.get("median") or 35.0, d.get("p75") or 55.0
    lo, hi = max(15.0, min(lo, mid)), max(hi, mid + 5)
    if lo <= dur <= hi:
        return 1.0 - 0.3 * abs(dur - mid) / max(1.0, hi - lo)
    edge = lo if dur < lo else hi
    return float(max(0.0, 0.7 - abs(dur - edge) / 40.0))


def _reaction(signals: dict, start: float, end: float) -> float | None:
    """0-1: how strong the laughter/applause is in [start, end], relative to the rest of the video."""
    raw = signals.get("raw", {}).get("reaction")
    if raw is None:
        return None
    s, e = int(max(0, start)), int(min(len(raw), end + 1))
    if e <= s:
        return 0.0
    ref = np.percentile(raw[raw > 0], 90) if np.any(raw > 0) else 0.0
    return float(min(1.0, raw[s:e].max() / ref)) if ref > 0 else 0.0


def score_window(segs: list[Seg], i: int, j: int, signals: dict, profile: dict | None) -> dict:
    """Scores the clip made of sentences i..j (inclusive)."""
    first, last = segs[i], segs[j]
    start, end = first.s, last.e
    dur = max(0.1, end - start)
    words = [w for k in range(i, j + 1) for w in segs[k].words]
    text = " ".join(segs[k].text for k in range(i, j + 1))
    low = text.lower()
    n = max(1, len(words))
    flaws = []

    # hook: the opening sentence, helped a little by the second
    hook = first.hook
    if j > i and len(first.words) < 8:
        hook = max(hook, 0.5 * first.hook + 0.5 * segs[i + 1].hook)
    if first.start_flaw:
        flaws.append(first.start_flaw)
    if first.words and first.words[0] in CONTEXT_START:
        hook -= 0.15

    # standalone + clean ending
    standalone = 1.0 - (0.25 if first.words and first.words[0] in CONTEXT_START else 0.0)
    if last.ends_open:
        flaws.append("cuts off mid-thought")
    if BACKREF.search(low):
        standalone -= 0.3

    # payoff: how it ends
    tail = " ".join(segs[k].text for k in range(max(i, j - 1), j + 1)).lower()
    mic_drop = 2 <= len(last.words) <= 10 and last.text.rstrip().endswith((".", "!"))
    payoff = 0.3 + 0.25 * bool(PUNCHLINE.search(tail)) + 0.2 * bool(LAUGH.search(tail)) + \
        0.1 * ("!" in tail) + 0.15 * last.clean_break + 0.1 * mic_drop
    energy_end = window_score(signals.get("pct", {}).get("energy"), end - min(8.0, dur / 3), end)
    if energy_end is not None:
        payoff = 0.6 * payoff + 0.4 * energy_end / 100
    # laughter / applause right after the last line is the strongest sign of a landed punchline
    react = _reaction(signals, end - 2.0, end + 3.0)
    if react is not None:
        payoff = max(payoff, 0.45 + 0.55 * react)

    # intensity
    intense = len(INTENSE.findall(low)) / n
    you = sum(w in ("you", "your", "you're") for w in words) / n
    nums = len(NUMBER.findall(low))
    laughs = len(LAUGH.findall(low))
    hook_hits = sum(1 for pat in HOOK_PATTERNS.values() if re.search(pat, low))
    intensity = 0.2 + min(0.35, intense * 12) + min(0.15, you * 3) + min(0.12, 0.04 * nums) + \
        min(0.15, 0.08 * laughs) + min(0.15, 0.03 * hook_hits)
    energy = window_score(signals.get("pct", {}).get("energy"), start, end)
    if energy is not None:
        intensity = 0.7 * intensity + 0.3 * energy / 100
    react_inside = _reaction(signals, start + 3.0, end - 2.0)
    if react_inside is not None:
        intensity += 0.15 * react_inside

    # pace and dead air
    wps = n / dur
    fillers = sum(w in FILLERS for w in words) / n
    pace = float(np.clip((wps - 1.4) / 1.6, 0.0, 1.0)) - min(0.4, fillers * 6)
    if wps < 1.5:
        flaws.append("too much dead air")

    length = _length_score(dur, profile)
    parts = {"hook": hook, "standalone": standalone, "payoff": payoff, "intensity": intensity,
             "pace": pace, "length": length}
    parts = {k: float(np.clip(v, 0.0, 1.0)) for k, v in parts.items()}
    content = 100 * (0.30 * parts["hook"] + 0.12 * parts["standalone"] + 0.16 * parts["payoff"] +
                     0.24 * parts["intensity"] + 0.10 * parts["pace"] + 0.08 * parts["length"])
    return {"start": start, "end": end, "score": round(content, 1), "parts": parts, "flaws": flaws,
            "text": text}


def find_windows(segments: list[dict], signals: dict, profile: dict | None, min_s: float, max_s: float,
                 top_n: int = 40) -> list[dict]:
    """Scores every sentence start at several lengths; returns the best non-overlapping windows."""
    segs = build_segments(segments, profile)
    targets = (min_s, (2 * min_s + max_s) / 3, (min_s + 2 * max_s) / 3, max_s)
    best: list[dict] = []
    for i, first in enumerate(segs):
        if first.start_flaw or not first.words:
            continue
        ends = []
        for j in range(i, len(segs)):
            dur = segs[j].e - first.s
            if dur > max_s:
                break
            if dur >= min_s:
                ends.append(j)
        if not ends:
            continue
        picks = {min(ends, key=lambda j: abs((segs[j].e - first.s) - t)) for t in targets}
        scored = [score_window(segs, i, j, signals, profile) for j in picks]
        scored = [w for w in scored if not w["flaws"]]
        if scored:
            best.append(max(scored, key=lambda w: w["score"]))
    best.sort(key=lambda w: -w["score"])
    kept: list[dict] = []
    for w in best:
        if all(min(w["end"], k["end"]) - max(w["start"], k["start"]) <= 0.25 * (w["end"] - w["start"])
               for k in kept):
            kept.append(w)
        if len(kept) >= top_n:
            break
    return kept


def review(text: str, start: float, end: float, segments: list[dict], signals: dict,
           profile: dict | None) -> dict:
    """Scores an arbitrary [start, end] clip (after boundary snapping)."""
    idx = [k for k, s in enumerate(segments) if s["e"] > start + 0.2 and s["s"] < end - 0.2]
    if not idx:
        return {"score": 0.0, "parts": {}, "flaws": ["empty"], "text": text}
    segs = build_segments(segments, profile)
    return score_window(segs, idx[0], idx[-1], signals, profile)


def category(text: str) -> str:
    low = text.lower()
    if LAUGH.search(text):
        return "funny"
    scores = {}
    for h, pat in HOOK_PATTERNS.items():
        hits = len(re.findall(pat, low))
        if hits and h in CATEGORY_OF:
            cat = CATEGORY_OF[h]
            scores[cat] = scores.get(cat, 0) + hits
    if re.search(r"\b(dream|discipline|never give up|work hard|believe in yourself|success|grind)\b", low):
        scores["motivational"] = scores.get("motivational", 0) + 2
    return max(scores, key=scores.get) if scores else "insightful"


def hook_text(segments: list[dict], start: float, end: float, profile: dict | None) -> str:
    """The punchiest line near the start of the clip, trimmed to overlay length."""
    lift = {r["feature"]: r["lift"] for r in (profile or {}).get("hook_lift", [])}
    limit = start + 0.6 * (end - start)
    options = [s for s in segments if s["s"] >= start - 0.3 and s["s"] <= limit] or \
        [s for s in segments if s["e"] > start][:1]
    if not options:
        return ""
    best = max(options, key=lambda s: _hook_strength(_clean(s["text"]), _words(_clean(s["text"])), lift)
               - 0.02 * options.index(s))
    text = _clean(best["text"], keep_laughs=False)
    text = re.sub(r"^((and|but|so|um|uh|like|yeah|okay|well|oh)[,.]?\s+)+", "", text, flags=re.I)
    words = text.split()
    if len(words) > 10:
        cut = next((k + 1 for k in range(4, 10) if words[k].endswith((",", ";", ":"))), None)
        text = " ".join(words[:cut]).rstrip(",;:") if cut else " ".join(words[:9]) + "..."
    return text[:1].upper() + text[1:]


def emphasis_words(text: str, profile: dict | None, limit: int = 8) -> list[str]:
    low = text.lower()
    viral_terms = set((profile or {}).get("top_viral_terms", []))
    found: list[str] = []
    for m in re.finditer(r"[a-z0-9$%']+", low):
        w = m.group(0)
        if w in found or len(w) < 3 or not w.strip("0,.$%"):
            continue
        if INTENSE.fullmatch(w) or w in viral_terms or NUMBER.fullmatch(w):
            found.append(w)
        if len(found) >= limit:
            break
    return found


def caption_and_tags(hook: str, cat: str, meta: dict, profile: dict | None) -> tuple[str, list[str]]:
    line = hook if hook.endswith(("...", "?", "!")) else hook.rstrip(".") + "..."
    caption = f"{line}\n\n{CAPTION_PROMPT.get(cat, 'Thoughts? 👇')}"
    tags: list[str] = []
    for r in (profile or {}).get("hashtag_lift", [])[:12]:
        t = r["feature"].lstrip("#")
        if t not in ("fyp", "foryou", "foryoupage", "viral", "shorts") and t not in tags:
            tags.append(t)
        if len(tags) >= 4:
            break
    channel = re.sub(r"[^a-z0-9]", "", (meta.get("channel") or "").lower())
    for t in (cat if cat != "other" else "", "podcast" if "podcast" in meta.get("title", "").lower() else "",
              channel, "fyp", "viral"):
        if t and t not in tags:
            tags.append(t)
    return caption, tags[:8]
