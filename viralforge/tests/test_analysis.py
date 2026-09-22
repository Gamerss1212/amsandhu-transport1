import pytest

from viralforge.analyze.candidates import build_candidates, split_sentences, words_per_minute
from viralforge.analyze.scoring import select_clips
from viralforge.analyze.transcribe import load_transcript_file, TranscriptionError
from viralforge.config import Config
from viralforge.models import ClipCandidate, ClipScores, Transcript, TranscriptSegment, Word


def transcript_from(sentences, gap=0.8, word_time=0.32) -> Transcript:
    words, t = [], 0.0
    for sentence in sentences:
        for token in sentence.split():
            words.append(Word(text=token, start=t, end=t + word_time * 0.8))
            t += word_time
        t += gap
    seg = TranscriptSegment(start=0.0, end=t, text=" ".join(sentences), words=words)
    return Transcript(language="en", segments=[seg])


def test_sentences_split_on_punctuation_and_pauses():
    t = transcript_from(["This is the first thing I want to say.",
                         "And this is the second thing entirely."])
    assert len(split_sentences(t)) == 2


def test_candidates_respect_the_duration_bounds():
    cfg = Config()
    cfg.candidates.min_duration, cfg.candidates.max_duration = 10.0, 30.0
    sentences = [f"Sentence number {i} with a reasonable amount of words in it."
                 for i in range(30)]
    for c in build_candidates(transcript_from(sentences), None, cfg):
        assert cfg.candidates.min_duration <= c.duration <= cfg.candidates.max_duration


def test_clips_starting_mid_thought_score_worse():
    cfg = Config()
    cfg.candidates.min_duration, cfg.candidates.max_duration = 4.0, 30.0
    good = transcript_from(["Nobody tells you this about starting a business today.",
                            "You will lose money for the first eighteen months."])
    bad = transcript_from(["and then he said the thing about the money which was odd.",
                           "and it kept going like that for a while after."])
    g = build_candidates(good, None, cfg)
    b = build_candidates(bad, None, cfg)
    assert g and b, "a video of windups must still yield candidates, just worse ones"
    assert max(c.scores.structure for c in g) > max(c.scores.structure for c in b)


def test_words_per_minute():
    c = ClipCandidate(start=0.0, end=60.0, transcript=" ".join(["word"] * 180))
    assert words_per_minute(c) == pytest.approx(180.0)


def test_selection_never_overlaps_and_honours_the_gap():
    cands = [
        ClipCandidate(start=0.0, end=30.0, scores=ClipScores(total=90)),
        ClipCandidate(start=25.0, end=55.0, scores=ClipScores(total=88)),   # overlaps
        ClipCandidate(start=35.0, end=60.0, scores=ClipScores(total=70)),   # inside the gap
        ClipCandidate(start=100.0, end=130.0, scores=ClipScores(total=60)),
    ]
    chosen = select_clips(cands, count=4, min_gap=10.0)
    assert [c.start for c in chosen] == [0.0, 100.0]


def test_selection_prefers_higher_scores():
    cands = [ClipCandidate(start=i * 100.0, end=i * 100.0 + 30, scores=ClipScores(total=i))
             for i in range(6)]
    chosen = select_clips(cands, count=2, min_gap=5.0)
    assert {c.scores.total for c in chosen} == {5.0, 4.0}


def test_srt_import(tmp_path):
    path = tmp_path / "t.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:03,500\nHello there friend\n\n"
                    "2\n00:00:04,000 --> 00:00:06,000\nSecond cue here\n", encoding="utf-8")
    t = load_transcript_file(str(path))
    assert t.source == "imported"
    assert [w.text for w in t.words][:3] == ["Hello", "there", "friend"]
    assert t.words[0].start == pytest.approx(1.0, abs=0.01)
    assert t.words[-1].end == pytest.approx(6.0, abs=0.01)


def test_json3_import_keeps_word_offsets(tmp_path):
    path = tmp_path / "t.json3"
    path.write_text(
        '{"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":['
        '{"utf8":"Hello","tOffsetMs":0},{"utf8":"there","tOffsetMs":600}]}]}',
        encoding="utf-8")
    t = load_transcript_file(str(path))
    assert [w.text for w in t.words] == ["Hello", "there"]
    assert t.words[1].start == pytest.approx(1.6, abs=0.01)


def test_unreadable_transcript_raises_clearly(tmp_path):
    path = tmp_path / "t.xyz"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(TranscriptionError):
        load_transcript_file(str(path))
    with pytest.raises(TranscriptionError):
        load_transcript_file(str(tmp_path / "missing.srt"))
