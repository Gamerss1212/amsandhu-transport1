from clipper.analysis.boundaries import snap


def _w(text, t0=0.0, step=0.4):
    return [{"w": w, "s": round(t0 + i * step, 2), "e": round(t0 + i * step + 0.35, 2)} for i, w in enumerate(text.split())]


def test_zero_length_word_on_the_edge_counts_as_inside():
    words = _w("We should do that, actually. Next.")
    words[4] = {"w": "actually.", "s": 1.7, "e": 1.7}
    s, e, notes = snap(words, 0.0, 1.7, 1, 60)             # the crew's end is exactly that word's time
    assert not notes and e >= 1.95


def test_zero_length_last_word_is_kept():
    words = _w("We should do that, actually. Let's do another question.")
    words[4] = {"w": "actually.", "s": 1.7, "e": 1.7}   # the transcript gave it no length
    s, e, _ = snap(words, 0.0, 1.69, 1, 60)
    assert e >= 1.7 + 0.25 and e < words[5]["s"]         # the word plays out, the next one does not start


def test_finishes_the_sentence_or_steps_back():
    words = _w("One two three. " + " ".join(f"w{k}" for k in range(20)) + " end.")
    s, e, notes = snap(words, 0.0, words[17]["e"], 1, 60)    # a long unfinished sentence: finish it
    assert words[-1]["e"] <= e and "extended" in notes[0]
    s, e, notes = snap(words, 0.0, words[17]["e"], 1, 6.0)   # no room: back to "three."
    assert words[2]["e"] <= e < words[3]["s"] and "trimmed" in notes[0]
    s, e, notes = snap(words, 0.0, words[4]["e"], 1, 60)     # a short fragment of the next sentence: dropped
    assert words[2]["e"] <= e < words[3]["s"] and "trimmed" in notes[0]


def test_starts_at_a_sentence_start():
    words = _w("First sentence here. Second one starts and goes on for a while.")
    s, e, notes = snap(words, words[4]["s"], words[-1]["e"], 1, 60)   # starts mid "Second one..."
    assert s <= words[3]["s"] and s > words[2]["e"]


def test_nothing_to_do_and_empty():
    words = _w("Hello there. General Kenobi.")
    s, e, notes = snap(words, 0.0, words[1]["e"], 0.1, 60)
    assert not notes and e >= words[1]["e"]
    assert snap([], 1.0, 2.0, 1, 2) == (1.0, 2.0, [])
