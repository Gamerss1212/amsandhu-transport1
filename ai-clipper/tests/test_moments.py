import numpy as np

from clipper.analysis.moments import Clip, dedupe, fuse, select_moments, snap, windows
from clipper.analysis.signals import comment_curve, heatmap_curve, to_percentiles, window_score
from clipper.analysis.transcribe import group_segments, parse_json3


def make_words(text: str, start: float = 0.0, step: float = 0.4) -> list[dict]:
    words, t = [], start
    for w in text.split():
        words.append({"w": w, "s": round(t, 2), "e": round(t + 0.3, 2)})
        t += step + (0.8 if w.endswith(".") else 0)
    return words


SENTENCES = " ".join(f"Sentence number {i} has some words in it." for i in range(60))


def test_group_segments_splits_on_sentences():
    segs = group_segments(make_words("Hello there. How are you? Fine."))
    assert [s["text"] for s in segs] == ["Hello there.", "How are you?", "Fine."]


def test_snap_to_sentence_boundaries_and_limits():
    words = make_words(SENTENCES)
    segs = group_segments(words)
    s, e = snap(segs[10]["s"] + 1.1, segs[14]["e"] - 0.7, segs, words, min_s=10, max_s=40)
    starts = [x["s"] for x in segs]
    assert any(abs(s - (st - 0.15)) < 0.2 or abs(s - st) < 0.2 for st in starts)
    assert 10 <= e - s <= 40.5
    # too long gets trimmed to a sentence end within max length
    s2, e2 = snap(segs[0]["s"], segs[30]["e"], segs, words, min_s=10, max_s=30)
    assert e2 - s2 <= 30.5
    # too short gets extended
    s3, e3 = snap(segs[5]["s"], segs[5]["e"], segs, words, min_s=15, max_s=40)
    assert e3 - s3 >= 15


def test_windows_cover_whole_video_with_overlap():
    w = windows(3600, 1080, 90)
    assert w[0][0] == 0 and w[-1][1] == 3600
    assert all(b[0] < a[1] for a, b in zip(w, w[1:]))


def test_dedupe_keeps_best_of_overlapping():
    a = Clip(0, 30, "a", "a", fused_score=80)
    b = Clip(5, 35, "b", "b", fused_score=90)
    c = Clip(100, 130, "c", "c", fused_score=50)
    assert [x.title for x in dedupe([a, b, c])] == ["b", "c"]


def test_fuse_renormalizes_missing_signals():
    weights = {"ai": 0.45, "heatmap": 0.2, "comments": 0.1, "energy": 0.1, "trend_fit": 0.15}
    clip = Clip(10, 40, "t", "h", ai_score=90)
    fuse(clip, {"pct": {}}, None, "text", weights)
    assert clip.fused_score == 90
    heat = np.zeros(100)
    heat[10:40] = 1.0
    fuse(clip, {"pct": {"heatmap": to_percentiles(heat)}}, None, "text", weights)
    assert 85 < clip.fused_score <= 90 and "heatmap" in clip.signal_scores


def test_signal_curves():
    hm = heatmap_curve({"heatmap": [{"start_time": 0, "end_time": 10, "value": 0.1},
                                    {"start_time": 10, "end_time": 20, "value": 1.0}]}, 20)
    pct = to_percentiles(hm)
    assert window_score(pct, 10, 20) > window_score(pct, 0, 10)
    comments = [{"text": "12:30 was insane", "likes": 50}, {"text": "LOL at 12:31"}, {"text": "12:29!!"}]
    cc = comment_curve(comments, 2000)
    assert int(np.argmax(cc)) in range(745, 755)
    assert comment_curve([{"text": "no timestamps"}], 100) is None


def test_parse_youtube_json3(tmp_path):
    p = tmp_path / "subs.json3"
    p.write_text('{"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello"},'
                 '{"utf8": " world", "tOffsetMs": 500}]}, {"tStartMs": 3000, "dDurationMs": 1000,'
                 '"segs": [{"utf8": "\\n"}]}]}')
    t = parse_json3(p)
    assert [w["w"] for w in t["words"]] == ["hello", "world"]
    assert t["words"][0]["s"] == 1.0 and t["words"][1]["s"] == 1.5


class FakeLLM:
    """Stands in for Claude: proposes two clips, approves only the first."""

    def __init__(self, segs):
        self.segs = segs
        self.calls = []

    def json(self, system, content, schema, effort=None, max_tokens=0):
        self.calls.append(schema)
        if "candidates" in schema["properties"]:
            good, bad = self.segs[3], self.segs[40]
            return {"candidates": [
                {"start": good["s"], "end": self.segs[7]["e"], "title": "Great", "hook": "You won't believe",
                 "summary": "", "why_viral": "", "category": "shocking", "needs_context": False,
                 "scores": {k: 90 for k in ("hook", "payoff", "emotion", "standalone", "shareability",
                                            "trend_match")}, "overall": 92},
                {"start": bad["s"], "end": self.segs[44]["e"], "title": "Meh", "hook": "so", "summary": "",
                 "why_viral": "", "category": "other", "needs_context": False,
                 "scores": {k: 60 for k in ("hook", "payoff", "emotion", "standalone", "shareability",
                                            "trend_match")}, "overall": 75},
            ]}
        return {"verdicts": [
            {"id": 0, "approve": True, "score": 91, "fatal_flaws": [], "reasons": "strong", "start": -1,
             "end": -1, "hook_text": "HE SAID WHAT", "title": "Great clip", "caption": "Would you?",
             "hashtags": ["#podcast", "viral"], "emphasis_words": ["believe"]},
            {"id": 1, "approve": False, "score": 70, "fatal_flaws": ["weak hook"], "reasons": "boring",
             "start": 0, "end": 0, "hook_text": "", "title": "", "caption": "", "hashtags": [],
             "emphasis_words": []},
        ]}


def test_select_moments_is_strict(cfg, tmp_path):
    words = make_words(SENTENCES)
    transcript = {"words": words, "segments": group_segments(words)}
    cfg["analysis"].update(judge_with_frames=False, chunk_minutes=100, min_clip_seconds=10)
    llm = FakeLLM(transcript["segments"])
    video = tmp_path / "v" / "source.mp4"
    video.parent.mkdir()
    approved, judged = select_moments({"title": "T", "duration": words[-1]["e"]}, transcript,
                                      {"pct": {}, "raw": {}}, None, cfg, video, llm)
    assert len(judged) == 2
    assert [c.title for c in approved] == ["Great clip"]
    clip = approved[0]
    assert clip.hook == "HE SAID WHAT" and clip.hashtags == ["podcast", "viral"]
    assert clip.judge_score == 91 and clip.final_score > 85
    rejected = [c for c in judged if c.title == "Meh"][0]
    assert rejected.judge_score <= 50 and rejected.fatal_flaws == ["weak hook"]


def test_reaction_curve_finds_laughter_between_words(tmp_path):
    import wave

    from clipper.analysis.signals import reaction_curve

    sr = 16000
    t = np.arange(20 * sr) / sr
    audio = 0.2 * np.sin(2 * np.pi * 220 * t)          # someone talking the whole time...
    audio[8 * sr:11 * sr] = 0.0                          # ...except a gap at 8-11s
    rng = np.random.default_rng(0)
    audio[9 * sr:10 * sr] = rng.normal(0, 0.3, sr)       # loud laughter inside the gap
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    words = [{"w": "x", "s": s / 2, "e": s / 2 + 0.4} for s in range(0, 40) if not 16 <= s < 22]
    curve = reaction_curve(path, words, 20)
    assert curve is not None and int(np.argmax(curve)) == 9
    assert curve[:7].sum() == 0 and curve[12:].sum() == 0


def test_comedy_factor_separates_comedy_from_interviews():
    from clipper.analysis.signals import LAUGH_STRONG, comedy_factor

    minutes = 10
    comedy = np.zeros(minutes * 60)
    comedy[::30] = LAUGH_STRONG + 20     # a clear laugh every 30 s
    interview = np.zeros(minutes * 60)
    interview[::150] = LAUGH_STRONG + 20  # the odd chuckle
    assert comedy_factor(comedy, minutes * 60) == 1.0
    assert comedy_factor(interview, minutes * 60) == 0.0
    assert comedy_factor(None, 600) == 0.0
