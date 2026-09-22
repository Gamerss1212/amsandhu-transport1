"""Turn a transcript into candidate clip windows.

The unit of work is a *sentence* - reconstructed from punctuation where the
transcriber provides it and from pauses where it does not.  Candidates are then
whole runs of sentences, which is what makes clips start and end on a complete
thought instead of mid-word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..config import Config
from ..models import AudioAnalysis, ClipCandidate, ClipScores, Transcript, Word
from ..utils.text import title_case_label, trim_hook

SENTENCE_END = re.compile(r"[.!?]['\")\]]?$")
CLAUSE_END = re.compile(r"[,;:]$")

# Openers that guarantee the clip starts mid-thought.
DANGLING_STARTS = {
    "and", "but", "so", "because", "which", "that", "then", "or", "yet", "nor",
    "however", "although", "though", "while", "whereas", "plus", "also", "anyway",
    "therefore", "thus", "hence", "meanwhile", "otherwise", "besides",
}
# Words that carry a stake, a number, or a claim - cheap proxy for "interesting".
SALIENT = {
    "never", "always", "everyone", "nobody", "worst", "best", "secret", "mistake",
    "wrong", "truth", "actually", "literally", "million", "billion", "thousand",
    "dollars", "money", "free", "proof", "reason", "why", "how", "stop", "start",
    "problem", "answer", "first", "biggest", "hardest", "fastest", "you", "your",
    "i", "me", "my", "we", "us", "they", "nobody", "everybody", "most", "least",
    "died", "failed", "quit", "fired", "lost", "won", "changed", "realized",
}
FILLERS = {"um", "uh", "erm", "hmm", "mm", "mhm", "like", "yeah", "okay", "right",
           "you know", "i mean", "sort of", "kind of", "basically", "literally"}


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: List[Word]

    @property
    def duration(self) -> float:
        return self.end - self.start


def split_sentences(transcript: Transcript, max_gap: float = 0.62) -> List[Sentence]:
    """Rebuild sentences from word timings plus whatever punctuation exists."""
    words = transcript.words
    if not words:
        return []

    sentences: List[Sentence] = []
    current: List[Word] = []
    for i, word in enumerate(words):
        current.append(word)
        text = word.text.strip()
        gap = words[i + 1].start - word.end if i + 1 < len(words) else 999.0
        long_enough = (word.end - current[0].start) >= 1.2
        ends_sentence = bool(SENTENCE_END.search(text))
        big_pause = gap >= max_gap
        # A comma plus a real pause is a sentence boundary for our purposes:
        # auto-captions rarely punctuate, so pauses do most of the work.
        soft_break = bool(CLAUSE_END.search(text)) and gap >= max_gap * 0.7

        if (ends_sentence or big_pause or soft_break) and long_enough:
            sentences.append(_make_sentence(current))
            current = []
        elif (word.end - current[0].start) > 22.0:      # runaway monologue guard
            sentences.append(_make_sentence(current))
            current = []

    if current:
        sentences.append(_make_sentence(current))
    return [s for s in sentences if s.text]


def _make_sentence(words: Sequence[Word]) -> Sentence:
    text = " ".join(w.text.strip() for w in words if w.text.strip())
    text = re.sub(r"\s+([,.!?;:])", r"\1", text).strip()
    return Sentence(start=words[0].start, end=words[-1].end, text=text, words=list(words))


# --------------------------------------------------------------------------- #


def build_candidates(transcript: Transcript, audio: Optional[AudioAnalysis],
                     cfg: Config) -> List[ClipCandidate]:
    sentences = split_sentences(transcript)
    if not sentences:
        return []

    cc = cfg.candidates
    candidates = _sweep(sentences, transcript, audio, cfg, skip_dangling=cc.snap_to_sentence)

    # Snapping to clean sentence starts is a preference, not a rule.  Some
    # speakers open almost every sentence with "and" or "so"; enforcing it
    # there would return nothing at all, which is worse than a clip that needs
    # a second of trimming.  The structure score still penalises them.
    if cc.snap_to_sentence and len(candidates) < 6:
        candidates = _sweep(sentences, transcript, audio, cfg, skip_dangling=False)

    candidates.sort(key=lambda c: c.scores.total, reverse=True)
    trimmed: List[ClipCandidate] = []
    for cand in candidates:
        if any(_overlap_fraction(cand, kept) > 0.82 for kept in trimmed):
            continue
        trimmed.append(cand)
        if len(trimmed) >= cc.max_candidates:
            break
    trimmed.sort(key=lambda c: c.start)
    return trimmed


def _sweep(sentences: Sequence[Sentence], transcript: Transcript,
           audio: Optional[AudioAnalysis], cfg: Config,
           skip_dangling: bool) -> List[ClipCandidate]:
    cc = cfg.candidates
    candidates: List[ClipCandidate] = []
    seen: set = set()

    for i, first in enumerate(sentences):
        if skip_dangling and _starts_dangling(first):
            continue
        end_index = i
        while end_index < len(sentences):
            span_end = sentences[end_index].end
            duration = span_end - first.start
            if duration < cc.min_duration:
                end_index += 1
                continue
            if duration > cc.max_duration:
                break
            key = (round(first.start, 1), round(span_end, 1))
            if key not in seen:
                seen.add(key)
                candidates.append(_candidate(
                    first.start, span_end, sentences[i:end_index + 1], transcript, audio, cfg))
            end_index += 1
    return candidates


def _candidate(start: float, end: float, sentences: Sequence[Sentence],
               transcript: Transcript, audio: Optional[AudioAnalysis],
               cfg: Config) -> ClipCandidate:
    text = " ".join(s.text for s in sentences).strip()
    scores = ClipScores(
        structure=_structure_score(sentences),
        delivery=_delivery_score(start, end, audio),
    )
    scores.total = 0.5 * scores.structure + 0.5 * scores.delivery
    opener = sentences[0].text if sentences else text
    return ClipCandidate(
        start=round(start, 3),
        end=round(end, 3),
        transcript=text,
        scores=scores,
        # Placeholders until the model supplies better ones; they are what the
        # --no-llm path ships with, so they have to be presentable.
        hook_line=trim_hook(opener),
        title=title_case_label(opener),
    )


def _starts_dangling(sentence: Sentence) -> bool:
    first = re.sub(r"[^a-z']", "", sentence.text.strip().lower().split(" ")[0] if sentence.text else "")
    return first in DANGLING_STARTS


def _structure_score(sentences: Sequence[Sentence]) -> float:
    if not sentences:
        return 0.0
    score = 62.0
    first, last = sentences[0], sentences[-1]
    if _starts_dangling(first):
        score -= 28.0
    if SENTENCE_END.search(last.text.strip()):
        score += 14.0
    else:
        score -= 10.0

    words = [w for s in sentences for w in s.words]
    if not words:
        return max(0.0, score)

    lowered = [re.sub(r"[^a-z']", "", w.text.lower()) for w in words]
    filler_rate = sum(1 for w in lowered if w in FILLERS) / len(lowered)
    score -= min(18.0, filler_rate * 120.0)

    salient_rate = sum(1 for w in lowered if w in SALIENT) / len(lowered)
    score += min(22.0, salient_rate * 110.0)

    # Front-loading: a clip whose most interesting words are at the start holds.
    head = lowered[: max(6, len(lowered) // 6)]
    if head and sum(1 for w in head if w in SALIENT) / len(head) > salient_rate:
        score += 6.0

    # Numbers are specific, and specific travels.
    if any(re.search(r"\d", w.text) for w in words):
        score += 5.0
    return max(0.0, min(100.0, score))


def _delivery_score(start: float, end: float, audio: Optional[AudioAnalysis]) -> float:
    if audio is None or not audio.rms_db:
        return 55.0
    import numpy as np
    i0, i1 = int(start / audio.hop), int(end / audio.hop)
    window = np.array(audio.rms_db[i0:i1]) if i1 > i0 else np.array([])
    if window.size < 5:
        return 50.0
    dynamic_range = float(np.percentile(window, 95) - np.percentile(window, 20))
    loudness = float(np.percentile(window, 70))
    dead_air = float(np.mean(window < (np.percentile(window, 70) - 22.0)))

    score = 48.0
    score += min(26.0, dynamic_range * 1.7)          # varied delivery reads as energy
    score += max(-14.0, min(14.0, (loudness + 26.0) * 1.4))
    score -= min(26.0, dead_air * 85.0)
    return max(0.0, min(100.0, score))


def _overlap_fraction(a: ClipCandidate, b: ClipCandidate) -> float:
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = min(a.duration, b.duration)
    return overlap / shortest if shortest > 0 else 0.0


def words_per_minute(candidate: ClipCandidate) -> float:
    words = len([w for w in candidate.transcript.split() if w.strip()])
    return words / (candidate.duration / 60.0) if candidate.duration > 0 else 0.0
