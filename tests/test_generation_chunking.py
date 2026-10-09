"""AUDIT B-65 - chunk a large single-theme generation ask.

book_generator does ONE CALL PER THEME, which is right for grounding. But
handle_generate_count accepts up to 100 questions, and a page-range book can
legitimately have exactly ONE theme, so all 100 went into a single call with
nothing chunking them.

MEASURED end to end before the fix: asking 100 from one theme truncated at 62
salvaged, then spent a regeneration and both backfill rounds chasing the
shortfall - 4 calls, 14,627 output tokens, 94 of 100 delivered.

The mechanism is that maxOutputTokens bounds THINKING + OUTPUT together
(measured: budgets 2048/512/0 all truncate at sum ~8,180), so the effective
content budget is maxOutputTokens minus whatever thinking consumes. Raising the
cap was tested and rejected: at 16384 it still truncated, and at 32768 the model
returned 151 questions for a 100 ask - paying for over-generation.

Chunking instead keeps every call inside the budget, which is the same shape as
the backfill loop ("ask only for the shortfall") and extraction's continuation
rounds.
"""
from __future__ import annotations

import pytest

from app.services import book_generator as bg


# _question_is_valid checks BOTH is_answer_grounded (the answer) and
# distractors_pass (the wrong options must be real terms from the section), so
# every option value used below has to appear in this text.
_TERMS = [f"{1300 + i}" for i in range(60)] + [
    "Samarqand", "Buxoro", "Shahrisabz", "Termiz", "Kesh", "Xiva",
    "Registon", "Bibixonim", "Guri Amir", "Oqsaroy",
]

THEME = (
    "Amir Temur davri tarixi. Quyidagi sanalar va joylar manbalarda uchraydi: "
    + ", ".join(_TERMS)
    + ". Bu davrda savdo, me'morchilik va hunarmandchilik rivojlangan. "
) * 6


def _distinct_question(idx: int) -> dict:
    """A question that is distinct from its neighbours on BOTH axes dedupe
    uses: a >=0.90-similar stem AND an identical option set are required
    together for a collapse, and real generated questions differ in both.

    A fixture that varied only the stem would have every question share one
    option set, which is not what generation produces and would make this test
    measure similarity rather than chunking.
    """
    import random

    rnd = random.Random(idx)
    opts = rnd.sample(_TERMS, 4)
    words = rnd.sample(
        ["savdo", "me'morchilik", "hunarmandchilik", "kutubxona", "madrasa",
         "bozor", "qal'a", "dehqonchilik", "zargarlik", "tibbiyot",
         "astronomiya", "matematika", "shoirlar", "elchilar", "kemasozlik",
         "suv", "bog'lar", "karvon", "ustaxona", "rasadxona"], 6)
    # NB: "xaritalar" (maps) is deliberately absent - it trips
    # references_unavailable_media, which is the checker doing its job.
    return {
        "question_text": f"{' '.join(words)} — {idx}?",
        "options": dict(zip(["A", "B", "C", "D"], opts)),
        "correct_answer": "A",
        "is_open_ended": False,
        "has_image": False,
        "image_description": None,
        "image_path": None,
    }


@pytest.fixture
def spy_calls(monkeypatch):
    """Record every _call_and_parse, and answer with well-formed questions
    grounded in THEME so they survive validation."""
    calls = []

    def fake(*, theme_title, theme_text, n_questions, n_written, option_labels,
             sample_question, language, user_id, difficulty=None,
             theme_images=None, question_language=None, book_kind=None,
             accepted_questions=None):
        calls.append({"n": n_questions, "written": n_written,
                      "images": theme_images,
                      "accepted": list(accepted_questions or [])})
        base = len(calls) * 1000
        out = [_distinct_question(base + i) for i in range(n_questions)]
        return out, False, None

    monkeypatch.setattr(bg, "_call_and_parse", fake)
    return calls


def _generate(n, n_written=0, images=None):
    return bg.generate_for_theme(
        theme_title="T", theme_text=THEME, n_questions=n, n_written=n_written,
        option_labels=["A", "B", "C", "D"], sample_question=None,
        language=None, user_id=None, theme_images=images,
    )


def _initial(spy_calls, requested):
    """The chunks of the FIRST ask, excluding the regeneration/backfill calls
    that legitimately follow when dedupe or grounding drops some."""
    import math

    k = math.ceil(requested / bg.MAX_QUESTIONS_PER_CALL)
    return [c["n"] for c in spy_calls[:k]]


def test_a_large_ask_is_split_into_chunks(spy_calls):
    """100 questions in one call is what truncates. Split it."""
    _generate(100)

    initial = _initial(spy_calls, 100)
    assert len(initial) > 1, "a 100-question ask still went out as one call"
    assert all(n <= bg.MAX_QUESTIONS_PER_CALL for n in initial), (
        f"a chunk exceeded the per-call ceiling: {initial}")
    assert sum(initial) == 100, f"chunks did not cover the ask: {initial}"


def test_no_single_call_ever_exceeds_the_ceiling(spy_calls):
    """Including the regeneration and backfill calls, not just the first ask."""
    _generate(100)

    over = [c["n"] for c in spy_calls if c["n"] > bg.MAX_QUESTIONS_PER_CALL]
    assert over == [], f"calls asked for more than the ceiling: {over}"


def test_the_chunks_cover_the_whole_request(spy_calls):
    res = _generate(100)
    assert res.requested == 100
    assert len(res.questions) >= 94, (
        f"delivered {len(res.questions)}/100; the measured pre-fix baseline "
        f"was 94, so this must not regress")


def test_a_small_ask_is_not_split(spy_calls):
    """The common case must stay one call per theme. (A later backfill call is
    pre-existing behaviour when dedupe drops some - not chunking.)"""
    _generate(10)
    assert _initial(spy_calls, 10) == [10]


def test_an_ask_exactly_at_the_ceiling_is_not_split(spy_calls):
    n = bg.MAX_QUESTIONS_PER_CALL
    _generate(n)
    assert _initial(spy_calls, n) == [n]


def test_written_questions_are_distributed_across_chunks(spy_calls):
    """n_written must not be asked for in full from every chunk, or a
    100/50-written request would ask for 50 written five times over."""
    _generate(100, n_written=40)

    import math

    k = math.ceil(100 / bg.MAX_QUESTIONS_PER_CALL)
    total_written = sum(c["written"] for c in spy_calls[:k])
    assert total_written == 40, (
        f"initial chunks asked for {total_written} written, wanted exactly 40")
    assert all(c["written"] <= c["n"] for c in spy_calls), (
        "a chunk asked for more written questions than questions")


def test_theme_images_are_sent_only_once(spy_calls):
    """CP24 figures are a bounded per-theme resource. Re-sending them on every
    chunk multiplies image tokens and invites duplicate figure questions."""
    imgs = [{"image_path": "a.png"}, {"image_path": "b.png"}]
    _generate(100, images=imgs)

    with_images = [c for c in spy_calls if c["images"]]
    assert len(with_images) == 1, (
        f"images went out on {len(with_images)} calls, expected 1")


def test_a_later_chunk_is_told_what_the_earlier_chunks_returned(spy_calls):
    """AUDIT C-2. Chunk 2 asks the SAME source with the SAME instructions as
    chunk 1, so without this it re-derives chunk 1's questions and
    test_cross_chunk_duplicates_are_collapsed's dedup then throws them away —
    after the call was paid for."""
    _generate(100)

    assert spy_calls[0]["accepted"] == [], "the first chunk has nothing to avoid"
    assert len(spy_calls[1]["accepted"]) == spy_calls[0]["n"], (
        "chunk 2 must carry every stem chunk 1 returned")
    assert len(spy_calls[2]["accepted"]) == spy_calls[0]["n"] + spy_calls[1]["n"]


def test_cross_chunk_duplicates_are_collapsed(monkeypatch):
    """Two chunks asked the same source can return the same question."""
    calls = []

    def fake(*, n_questions, **kw):
        calls.append(n_questions)
        q = _distinct_question(7)      # the SAME question every time
        return [dict(q) for _ in range(n_questions)], False, None

    monkeypatch.setattr(bg, "_call_and_parse", fake)
    res = _generate(60)

    assert len(res.questions) == 1, (
        f"identical questions across chunks were not deduped: {len(res.questions)}")


def test_a_raising_chunk_stops_further_chunks(monkeypatch):
    """An infrastructure failure must not be retried as if it were merely
    under-delivery - the same rule the regen and backfill already follow."""
    calls = []

    def fake(*, n_questions, **kw):
        calls.append(n_questions)
        if len(calls) == 2:
            return [], True, "boom"
        return [], False, None

    monkeypatch.setattr(bg, "_call_and_parse", fake)
    res = _generate(100)

    assert len(calls) == 2, f"kept calling after a hard failure: {len(calls)}"
    assert res.errored is True
