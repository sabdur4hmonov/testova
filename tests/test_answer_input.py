"""
Format matrix for the teacher answer-key parser — different teachers type
keys in different styles; fixing one style must never break another.
"""
from app.bot.handlers.upload import _labels_hint, _parse_answer_input


def test_labels_hint_shows_real_gapped_labels():
    # Stage 1: the key-entry hint must show each question's REAL labels (gaps
    # and all), not a contiguous A-D example — this is what made a teacher type
    # C on an a,b,d,e paper. Open-ended questions show a write-in marker.
    qs = [
        {"question_number": 1, "options": {"a": "x", "b": "y", "d": "z", "e": "w"}},
        {"question_number": 16, "options": {"b": "x", "d": "y", "e": "z"}},
        {"question_number": 19, "options": {}},
    ]
    hint = _labels_hint(qs)
    assert "1) abde" in hint          # gap at c preserved
    assert "16) bde" in hint          # gap at a,c
    assert "19)" in hint and "✍️" in hint  # open-ended → write-in
    assert "1A 2B 5C 10D" not in hint  # the stale contiguous example is gone


def test_plain_pairs():
    assert _parse_answer_input("1A 2B 3C", 10) == {"1": "A", "2": "B", "3": "C"}


def test_dash_separated():
    assert _parse_answer_input("1-A 2-B", 10) == {"1": "A", "2": "B"}


def test_space_separated():
    assert _parse_answer_input("1 A 2 B", 10) == {"1": "A", "2": "B"}


def test_paren_and_dot_separators():
    assert _parse_answer_input("1)A 2.B 3:C", 10) == {"1": "A", "2": "B", "3": "C"}


def test_newline_separated_dash_format():
    text = "37-A\n38-B\n39-C"
    assert _parse_answer_input(text, 52) == {"37": "A", "38": "B", "39": "C"}


def test_spaced_dash():
    assert _parse_answer_input("1 - A", 10) == {"1": "A"}


def test_skip_marker():
    assert _parse_answer_input("47-", 52) == {"47": "-"}


def test_skip_marker_mixed_with_pairs():
    text = "45-A\n46-B\n47-\n48-D"
    assert _parse_answer_input(text, 52) == {
        "45": "A", "46": "B", "47": "-", "48": "D",
    }


def test_pair_overrides_earlier_skip():
    assert _parse_answer_input("5-\n5C", 10) == {"5": "C"}


def test_cyrillic_letters_preserved():
    # Letter preservation (KNOWN-OPEN #2): the REAL Cyrillic label is kept, not
    # force-mapped to Latin. Validation + canonical-matching against the
    # question's real options resolves it at grade time. Explicit codepoints
    # avoid the source-ambiguity between Cyrillic С (U+0421) and Latin C.
    cyr = {"1": "А", "2": "В", "3": "С", "4": "Д", "5": "Е"}
    text = " ".join(f"{n}{L}" for n, L in cyr.items())
    assert _parse_answer_input(text, 10) == cyr


def test_cyrillic_key_validates_via_canonical():
    # A Cyrillic label canonicalises to match a Cyrillic-labelled question's
    # real options (the letters_by_num check in the handler uses this).
    from app.services.option_letters import canonical_letter
    real_labels = ["А", "Б", "Д", "Е"]  # А Б Д Е (gap at В,Г)
    canon_to_real = {canonical_letter(L): L for L in real_labels}
    # teacher types Cyrillic Д → canonical D → resolves to the real Cyrillic Д
    assert canon_to_real.get(canonical_letter("Д")) == "Д"
    # a Latin "A" also resolves to the Cyrillic А (look-alike)
    assert canon_to_real.get(canonical_letter("A")) == "А"
    # a letter not on this question (В) does not resolve
    assert canon_to_real.get(canonical_letter("В")) is None


def test_lowercase_input():
    assert _parse_answer_input("1a 2b", 10) == {"1": "A", "2": "B"}


def test_out_of_range_numbers_ignored():
    assert _parse_answer_input("0A 5B 99C", 10) == {"5": "B"}


def test_letters_only_sequence():
    assert _parse_answer_input("ABCD", 10) == {
        "1": "A", "2": "B", "3": "C", "4": "D",
    }


def test_letters_only_respects_count():
    assert _parse_answer_input("ABCDE", 3) == {"1": "A", "2": "B", "3": "C"}


def test_empty_and_garbage():
    assert _parse_answer_input("", 10) == {}
    assert _parse_answer_input("???", 10) == {}
