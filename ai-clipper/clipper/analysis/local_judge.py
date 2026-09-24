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
from .signals import LAUGH_STRONG, window_score

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
    r"everyone|everybody|this is (why|how|the)|let me tell you|i'll never forget|you're a|"
    r"you (said|were|went|did|told|knew|lied|had|claim|admitted)|\d)")
_INTENSE_WORDS = """
died dead death dying kill killed killing killer murder murdered shot shooting gun stabbed war bomb attack
attacked blood violent violence weapon suicide overdose funeral genocide holocaust nazi nazis terror terrorist
hostage torture tortured massacre executed
prison jail jailed arrested cops police fbi cia lawsuit sued illegal crime criminal fraud scam scammed stole
stolen steal robbed cheat cheated cheating bribe corrupt corruption hacked leaked exposed conspiracy
evil guilt guilty shame ashamed regret regrets betrayed betrayal lied lie lies lying liar hate hated revenge
jealous furious angry rage scared afraid terrified fear panic anxiety depressed depression lonely heartbroken
cry crying cried tears trauma traumatized abused abuse bullied humiliated embarrassed embarrassing disgusting
insane crazy wild nuts unbelievable shocking shocked horrible horrific terrible worst best greatest ridiculous
dangerous destroyed destroy ruined obsessed obsession nightmare miracle
million millions billion billions rich broke bankrupt debt fired famous fame mansion fortune
sex affair divorce divorced pregnant breakup dumped
drugs cocaine heroin meth drunk addicted addiction rehab alcoholic
shit fuck fucking fucked damn hell bitch
never nobody everyone secret truth biggest craziest
"""
INTENSE = re.compile(r"\b(" + "|".join(sorted(set(_INTENSE_WORDS.split()), key=len, reverse=True)) + r")\b")
SHOW_BREAK = re.compile(r"\b(we'?ll be (right )?back|we are back|we're back|welcome back|after the break|"
                        r"stay tuned|don'?t go anywhere|right after this)\b")
PROMO = re.compile(r"\b(subscribe|patreon|link in|promo code|use code|sponsored|brought to you by|tickets|tour dates|"
                   r"on tour|on the road|catch (him|her|me|us|them) live|check out (my|our|his|her)|"
                   r"follow (me|us|him|her)|dot com|i'?ll be in|i'?m going to be in|merch)\b")
WEAK_END = {"the", "a", "an", "in", "of", "to", "and", "or", "but", "that", "this", "for", "on", "at", "with",
            "from", "by", "is", "was", "are", "were", "be", "my", "your", "his", "her", "their", "our", "its",
            "it", "i", "you", "he", "she", "we", "they", "so", "if", "as", "about", "like", "just", "um", "uh"}
BLUNT_START = {"no", "nope", "never", "not", "yes", "yeah", "absolutely", "none", "zero", "nothing", "of",
               "exactly", "definitely", "correct", "wrong"}
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
GRAVE = re.compile(r"\b(holocaust|nazis?|genocide|died|death|killed|murder(ed)?|suicide|cancer|abuse[d]?|war|"
                   r"funeral|overdose|massacre|terminal|passed away)\b")
CATEGORY_TAG = {  # the hashtag people actually search for each kind of clip
    "funny": "funny", "shocking": "crazy", "emotional": "emotional", "controversial": "debate",
    "story": "storytime", "educational": "learnontiktok", "insightful": "mindset", "drama": "drama",
    "motivational": "motivation", "serious": "truestory",
}
CAPTION_PROMPT = {
    "serious": "Let that sink in.",
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
    question: bool      # ends with a question mark
    blunt: bool         # a short, flat answer: "No.", "Not at all.", "Never."


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
    stumbles = sum(a == b for a, b in zip(words, words[1:])) + len(re.findall(r"\bthe that\b|\bi was like\b", low))
    score -= 0.1 * min(3, stumbles)
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
        blunt = 1 <= len(words) <= 6 and words[0] in BLUNT_START
        out.append(Seg(s["s"], s["e"], text, words, _hook_strength(text, words, lift),
                       _start_flaw(words, text), ends_open, clean_break, text.rstrip().endswith("?"), blunt))
    return out


def _length_score(dur: float, profile: dict | None) -> float:
    """Length matters, but less than content: a gentle preference for what's working now."""
    d = (profile or {}).get("viral_duration") or {}
    lo, mid, hi = d.get("p25") or 25.0, d.get("median") or 40.0, d.get("p75") or 65.0
    lo, hi = max(15.0, min(lo, mid)), max(hi, mid + 5)
    if lo <= dur <= hi:
        return 1.0 - 0.2 * abs(dur - mid) / max(1.0, hi - lo)
    edge = lo if dur < lo else hi
    return float(max(0.3, 0.8 - abs(dur - edge) / 60.0))


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


def _laughs(signals: dict, start: float, end: float) -> tuple[float, float] | None:
    """(overall 0-1, strength of a laugh in the first 10 s) - only for videos that run on laughs."""
    raw = signals.get("raw", {}).get("reaction")
    comedy = signals.get("comedy", 0.0)
    if raw is None or comedy <= 0:
        return None
    ref = 1.5 * LAUGH_STRONG
    s, e = int(max(0, start)), int(min(len(raw), end + 3))
    seg = raw[s:e]
    if not len(seg):
        return None
    strong = seg >= LAUGH_STRONG
    bursts = int(np.sum(np.diff(np.r_[0, strong.astype(np.int8)]) == 1))
    top = float(min(1.0, np.mean(np.sort(seg)[::-1][:3]) / ref))
    overall = 0.5 * min(1.0, bursts / 3) + 0.5 * top
    early = float(min(1.0, raw[s:min(e, s + 10)].max(initial=0) / ref))
    return comedy * overall, comedy * early


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
    laughs = _laughs(signals, start, end)
    if laughs and laughs[1] >= 0.5:  # a laugh in the first seconds means the opening already works
        hook = max(hook, 0.55 + 0.35 * laughs[1])

    # standalone + clean ending
    standalone = 1.0 - (0.25 if first.words and first.words[0] in CONTEXT_START else 0.0)
    if last.ends_open:
        flaws.append("cuts off mid-thought")
    if BACKREF.search(low):
        standalone -= 0.3
    inner = " ".join(segs[k].text for k in range(i, j)).lower()  # everything but the last line
    if SHOW_BREAK.search(inner):
        flaws.append("runs across a show break")
    promos = len(PROMO.findall(low))
    if promos >= 2:
        flaws.append("promo / plug talk")
    standalone -= 0.2 * min(1, promos)

    # payoff: how it ends
    tail = " ".join(segs[k].text for k in range(max(i, j - 1), j + 1)).lower()
    mic_drop = (2 <= len(last.words) <= 10 and last.text.rstrip().endswith((".", "!"))) or last.blunt
    # ends on a flat answer to a pointed question ("No feeling of guilt?" "None of that.")
    answered = last.blunt and any(segs[k].question for k in range(max(i, j - 3), j))
    payoff = 0.3 + 0.25 * bool(PUNCHLINE.search(tail)) + 0.2 * bool(LAUGH.search(tail)) + \
        0.1 * ("!" in tail) + 0.15 * last.clean_break + 0.1 * mic_drop + 0.2 * answered + \
        0.1 * bool(INTENSE.search(tail))
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
    laugh_tags = len(LAUGH.findall(low))
    hook_hits = sum(1 for pat in HOOK_PATTERNS.values() if re.search(pat, low))
    # confrontation: a pointed question answered flatly within the next couple of lines
    pressed = sum(1 for k in range(i, j) if segs[k].question and
                  any(segs[m].blunt for m in range(k + 1, min(j, k + 3) + 1)))
    intensity = 0.2 + min(0.35, intense * 12) + min(0.15, you * 3) + min(0.12, 0.04 * nums) + \
        min(0.15, 0.08 * laugh_tags) + min(0.15, 0.03 * hook_hits) + min(0.25, 0.12 * pressed)
    energy = window_score(signals.get("pct", {}).get("energy"), start, end)
    if energy is not None:
        intensity = 0.7 * intensity + 0.3 * energy / 100
    react_inside = _reaction(signals, start + 3.0, end - 2.0)
    if react_inside is not None:
        intensity += 0.15 * react_inside

    # pace and dead air
    wps = n / dur
    fillers = sum(w in FILLERS for w in words) / n
    pace = float(np.clip((wps - 1.5) / 0.9, 0.0, 1.0)) - min(0.4, fillers * 6)  # 2.4+ words/s = full marks
    if wps < 1.5:
        flaws.append("too much dead air")

    length = _length_score(dur, profile)
    parts = {"hook": hook, "standalone": standalone, "payoff": payoff, "intensity": intensity,
             "pace": pace, "length": length}
    parts = {k: float(np.clip(v, 0.0, 1.0)) for k, v in parts.items()}
    content = 100 * (0.30 * parts["hook"] + 0.12 * parts["standalone"] + 0.16 * parts["payoff"] +
                     0.24 * parts["intensity"] + 0.10 * parts["pace"] + 0.08 * parts["length"])
    if laughs:  # real laughter is the strongest evidence a moment works; no laughter changes nothing
        parts["laughs"] = laughs[0]
        content = min(100.0, content + 14 * laughs[0])
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


def category(text: str, laughs: float = 0.0) -> str:
    low = text.lower()
    if len(GRAVE.findall(low)) >= 2:  # heavy subject: never a jokey caption
        return "serious"
    if LAUGH.search(text) or laughs >= 0.5:
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


def _pause_trim(seg: dict, words: list[dict], max_words: int = 12) -> str | None:
    """A segment with no full stop may run into the next sentence ("I was ahead of his time come on"):
    end it at the clearest pause in speech instead."""
    ws = [w for w in words if seg["s"] - 0.01 <= w["s"] <= seg["e"] + 0.01]
    if len(ws) < 5 or re.search(r"[.!?]['\"]?$", seg["text"].strip()):
        return None
    gaps = [(ws[k + 1]["s"] - ws[k]["e"], k) for k in range(3, min(len(ws) - 1, max_words))]
    if not gaps:
        return None
    gap, k = max(gaps)
    return " ".join(w["w"] for w in ws[:k + 1]) if gap >= 0.2 else None


def hook_text(segments: list[dict], start: float, end: float, profile: dict | None,
              words: list[dict] | None = None) -> str:
    """The punchiest line near the start of the clip, trimmed to overlay length."""
    lift = {r["feature"]: r["lift"] for r in (profile or {}).get("hook_lift", [])}
    limit = start + 0.6 * (end - start)
    options = [s for s in segments if s["s"] >= start - 0.3 and s["s"] <= limit] or \
        [s for s in segments if s["e"] > start][:1]
    if not options:
        return ""
    best = max(options, key=lambda s: _hook_strength(_clean(s["text"]), _words(_clean(s["text"])), lift)
               - 0.02 * options.index(s))
    text = _clean((_pause_trim(best, words) if words else None) or best["text"], keep_laughs=False)
    text = re.sub(r"^((and|but|so|um|uh|like|yeah|okay|well|oh)[,.]?\s+)+", "", text, flags=re.I)
    words = text.split()
    # a clause that can stand alone reads better on screen: "I was 14 years old and I would say..." ->
    # "I was 14 years old..."
    joint = next((k for k in range(4, min(len(words), 10)) if words[k].lower() in ("and", "but", "because")), None)
    if joint and len(words) > 8:
        text = " ".join(words[:joint]).rstrip(",;:") + "..."
    elif len(words) > 10:
        cut = next((k + 1 for k in range(4, 10) if words[k].endswith((",", ";", ":"))), None)
        if cut:
            text = " ".join(words[:cut]).rstrip(",;:")
        else:
            # never end an overlay on "the", "in", "of"...: back up to the last meaningful word
            keep = 9
            while keep > 4 and re.sub(r"[^\w']", "", words[keep - 1]).lower() in WEAK_END:
                keep -= 1
            text = " ".join(words[:keep]).rstrip(",;:") + "..."
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
    for t in (CATEGORY_TAG.get(cat, ""), "podcast" if "podcast" in meta.get("title", "").lower() else "",
              channel, "fyp", "viral"):
        if t and t not in tags:
            tags.append(t)
    return caption, tags[:8]
