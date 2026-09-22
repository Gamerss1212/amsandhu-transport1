from viralforge.utils.text import title_case_label, trim_hook, trim_to_words


def test_never_cuts_mid_word():
    long = "I lost forty thousand dollars in my first year because I waited for someone"
    for limit in range(10, 70, 7):
        out = trim_to_words(long, 20, limit)
        assert out == "" or long.startswith(out), out
        assert not out.endswith(" ")


def test_hook_prefers_a_natural_break():
    assert trim_hook("Here is what changed it. I stopped asking.") == "Here is what changed it"
    assert trim_hook("Stop building an MVP") == "Stop building an MVP"


def test_empty_input_is_empty_output():
    assert trim_hook("") == "" and trim_hook("   ") == "" and title_case_label("") == ""
