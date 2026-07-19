"""
Stage 4 (unification): the saved-flow key entry accepts written + multi-accept
answers by reusing parse_answer_key, and validates each entry against the
question's real type via the pure _resolve_saved_key. MC letters are canonical-
matched to real (possibly gapped/Cyrillic) labels; open questions take written
answers verbatim; a word for an MC question (or a letter not on the paper) is
rejected — never silently dropped.
"""
from __future__ import annotations

from app.bot.handlers.upload import _resolve_saved_key


# labels: MC gapped (a,b,d,e), MC full, open (no options)
LABELS = {1: ["a", "b", "d", "e"], 2: ["a", "b", "c", "d"], 19: []}


def test_mc_letter_matches_gapped_real_label():
    good, bad = _resolve_saved_key({1: ["A"]}, set(), LABELS)
    assert good == {"1": ["a"]}          # Latin A → real lowercase gapped label a
    assert bad == []


def test_mc_letter_not_on_paper_rejected():
    good, bad = _resolve_saved_key({1: ["C"]}, set(), LABELS)  # q1 has no c
    assert good == {}
    assert bad == [(1, "bad_letter")]


def test_open_question_takes_written_answer():
    good, bad = _resolve_saved_key({19: ["8,23"]}, set(), LABELS)
    assert good == {"19": ["8,23"]}
    assert bad == []


def test_open_question_multi_accept():
    good, bad = _resolve_saved_key({19: ["PHONE", "TELEPHONE"]}, set(), LABELS)
    assert good == {"19": ["PHONE", "TELEPHONE"]}
    assert bad == []


def test_word_for_mc_question_rejected():
    good, bad = _resolve_saved_key({2: ["TOSHKENT"]}, set(), LABELS)  # q2 is MC
    assert good == {}
    assert bad == [(2, "bad_letter")]


def test_mc_multi_accept_letters():
    good, bad = _resolve_saved_key({2: ["A", "B"]}, set(), LABELS)
    assert good == {"2": ["a", "b"]}     # both accepted, canonical→real
    assert bad == []


def test_skip_marker():
    good, bad = _resolve_saved_key({}, {1}, LABELS)
    assert good == {"1": "-"} and bad == []


def test_unknown_question_rejected():
    good, bad = _resolve_saved_key({99: ["A"]}, set(), LABELS)
    assert good == {} and bad == [(99, "no_question")]


def test_cyrillic_letter_matches_cyrillic_labels():
    labels = {1: ["А", "Б", "Д"]}       # Cyrillic gapped labels
    good, bad = _resolve_saved_key({1: ["Д"]}, set(), labels)
    assert good == {"1": ["Д"]} and bad == []


def test_mixed_key_letters_written_and_skip():
    parsed = {1: ["A"], 19: ["temurbek"]}
    good, bad = _resolve_saved_key(parsed, {2}, LABELS)
    assert good == {"2": "-", "1": ["a"], "19": ["temurbek"]}
    assert bad == []
