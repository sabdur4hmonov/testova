"""Question distribution weighted by PAGE SPAN, not by theme count.

THE BUG, and it predates multi-book: distribute_questions splits N evenly over
the number of THEMES, so a teacher who types "1-15, 40-42" gets 50/50 across a
15-page range and a 3-page one. Combining two books makes it louder - Book A
"1-15" against Book B "20-30, 40-50" gives B two thirds of the test purely for
having been typed as two ranges - but it is the same bug either way, so the fix
applies to both paths rather than only to the multi-book one.

THE RULE: largest-remainder (Hamilton) apportionment on page span.

    pages_i  = page_end - page_start + 1
    exact_i  = N * pages_i / sum(pages)
    base_i   = floor(exact_i)
    leftover = N - sum(base_i)  ->  to the largest fractional parts,
                                    ties broken by theme order, earliest first

Ties-to-earliest deliberately matches the even split's own remainder rule, so
the function stays deterministic in the same way it always was.

NO PER-THEME MINIMUM. A 1-page range beside a 40-page one can legitimately
receive zero questions. A floor of 1 would break the exact-sum property and
silently over-weight tiny ranges; the even split already allowed zeros ("the
trailing themes get 0, which is unavoidable and must not be papered over") -
proportional just moves those zeros onto the SMALLEST themes instead of the
LAST ones, which is the more defensible place for them.

weights=None keeps the even split byte-identical, so every caller that has no
page information - and every existing test - is untouched.
"""
from __future__ import annotations

import pytest

from app.services import book_generator as bg


# -- weights=None: the old behaviour, unchanged -------------------------------

@pytest.mark.parametrize("total,n_themes,expected", [
    (10, 3, [4, 3, 3]),
    (9, 3, [3, 3, 3]),
    (2, 3, [1, 1, 0]),
    (0, 3, [0, 0, 0]),
])
def test_no_weights_is_the_even_split_it_always_was(total, n_themes, expected):
    assert bg.distribute_questions(total, n_themes) == expected
    assert bg.distribute_questions(total, n_themes, weights=None) == expected


# -- the worked example from the plan ----------------------------------------

def test_the_reported_worked_example():
    """Book A "1-15" (15 pages) + Book B "20-30, 40-50" (11 + 11), N=20.

    exact  8.108, 5.946, 5.946
    base   8, 5, 5            (sum 18, leftover 2)
    frac   .108, .946, .946   -> the two largest take the leftover
    final  8, 6, 6            -> Book A 8, Book B 12

    The even split gives 7/7/6, i.e. Book A 7 and Book B 13 - the silent
    favouring of whichever book was typed as more ranges.
    """
    assert bg.distribute_questions(20, 3, weights=[15, 11, 11]) == [8, 6, 6]
    assert bg.distribute_questions(20, 3) == [7, 7, 6]


def test_a_single_book_with_uneven_ranges_is_weighted_too():
    """"1-15, 40-42" is 15 pages against 3. The same bug, the same fix - this
    path is NOT gated to multi-book."""
    assert bg.distribute_questions(18, 2, weights=[15, 3]) == [15, 3]
    assert bg.distribute_questions(18, 2) == [9, 9]


# -- invariants --------------------------------------------------------------

@pytest.mark.parametrize("total", [0, 1, 3, 7, 10, 25, 100])
@pytest.mark.parametrize("weights", [
    [15, 11, 11], [1, 40], [5, 5, 5, 5], [30, 1, 1, 1], [7], [2, 3, 5, 8, 13],
])
def test_the_split_always_sums_to_the_total(total, weights):
    got = bg.distribute_questions(total, len(weights), weights=weights)
    assert sum(got) == total
    assert len(got) == len(weights)
    assert all(n >= 0 for n in got)


@pytest.mark.parametrize("weights", [[15, 11, 11], [1, 40], [30, 1, 1, 1]])
def test_a_bigger_range_never_gets_fewer_questions_than_a_smaller_one(weights):
    got = bg.distribute_questions(50, len(weights), weights=weights)
    pairs = sorted(zip(weights, got))
    assert [n for _w, n in pairs] == sorted(n for _w, n in pairs)


def test_ties_go_to_the_earliest_theme():
    """Same rule the even split uses for its remainder, so the function stays
    deterministic in the same way."""
    assert bg.distribute_questions(7, 3, weights=[10, 10, 10]) == [3, 2, 2]
    assert bg.distribute_questions(1, 3, weights=[10, 10, 10]) == [1, 0, 0]


def test_a_tiny_range_beside_a_huge_one_can_get_nothing():
    """No floor, deliberately. 40 pages against 1, asking for 10: the one-page
    range is 2.4% of the material and rounds to zero. Papering that over with a
    minimum of 1 would break the exact sum and over-weight the tiny range."""
    assert bg.distribute_questions(10, 2, weights=[40, 1]) == [10, 0]


def test_fewer_questions_than_themes_zeroes_the_smallest_not_the_last():
    """The even split zeroes the TRAILING themes; weighted zeroes the SMALLEST
    ones, wherever they sit in the order."""
    assert bg.distribute_questions(2, 4, weights=[1, 50, 1, 50]) == [0, 1, 0, 1]
    assert bg.distribute_questions(2, 4) == [1, 1, 0, 0]


# -- degrading safely --------------------------------------------------------

def test_degenerate_weights_fall_back_to_the_even_split():
    """Nothing here may raise or return a wrong-length list: a bad weight list
    must degrade to the behaviour that was always correct, not fail a
    teacher's generation."""
    assert bg.distribute_questions(10, 3, weights=[0, 0, 0]) == [4, 3, 3]
    assert bg.distribute_questions(10, 3, weights=[]) == [4, 3, 3]
    assert bg.distribute_questions(10, 3, weights=[5, 5]) == [4, 3, 3]
    assert bg.distribute_questions(10, 3, weights=[5, 5, 5, 5]) == [4, 3, 3]
    assert bg.distribute_questions(10, 3, weights=[-4, 5, 5]) == [4, 3, 3]


def test_no_themes_is_still_an_empty_split():
    assert bg.distribute_questions(10, 0, weights=[]) == []


def test_written_and_total_use_the_same_weights_so_they_stay_aligned():
    """n_written is distributed separately from the total, then clamped per
    theme with min(). If the two used different shapes, a theme could be asked
    for more written questions than it has questions."""
    weights = [15, 11, 11]
    per_theme = bg.distribute_questions(20, 3, weights=weights)
    per_written = bg.distribute_questions(8, 3, weights=weights)
    assert sum(per_theme) == 20
    assert sum(per_written) == 8
    assert all(w <= n for w, n in zip(per_written, per_theme))


# -- the wiring: the handler must actually DELIVER the spans ------------------

async def test_run_generation_asks_each_theme_for_its_weighted_share(monkeypatch):
    """The pure function above is only half the fix. This pins that
    _run_generation turns gen_theme_pages into the per-theme ask, because a
    correct apportionment that never reaches the generator changes nothing.

    Book A "1-15" + Book B "20-30, 40-50", 20 questions: 8 / 6 / 6, not the
    even split's 7 / 7 / 6.
    """
    from app.bot.handlers import book

    asked: list[tuple[str, int, int]] = []

    def fake_generate_for_theme(*, theme_title, n_questions, n_written, **kw):
        asked.append((theme_title, n_questions, n_written))
        # Distinct STEM AND distinct OPTIONS per question: _run_generation ends
        # with dedupe_questions, whose fingerprint is stem + sorted options, so
        # a fixture that reuses one option set collapses across themes and
        # measures the deduper instead of the distribution.
        return book.book_generator.GenerationResult(
            questions=[{
                "question_text": f"{theme_title} question {i}",
                "options": {"A": f"{theme_title} a{i}", "B": f"{theme_title} b{i}"},
                "correct_answer": "A",
                "correct_answers": ["A"], "is_open_ended": False,
                "img_index": None,
            } for i in range(n_questions)],
            requested=n_questions, errored=False,
        )

    monkeypatch.setattr(
        book.book_generator, "generate_for_theme", fake_generate_for_theme
    )

    class _Status:
        async def edit_text(self, *a, **k):
            return None

    questions, errored, short, _filtered = await book._run_generation(
        _Status(), "uz",
        ["pp1-15", "pp20-30", "pp40-50"],
        ["text a", "text b", "text c"],
        None, 20, 12345,
        theme_ids=["", "", ""],
        theme_pages=[15, 11, 11],
    )

    assert [n for _t, n, _w in asked] == [8, 6, 6]
    assert sum(n for _t, n, _w in asked) == 20
    assert errored == [] and short == []
    assert len(questions) == 20


async def test_run_generation_without_spans_keeps_the_even_split(monkeypatch):
    """Any caller that has no page information must behave exactly as before."""
    from app.bot.handlers import book

    asked: list[int] = []

    def fake_generate_for_theme(*, theme_title, n_questions, n_written, **kw):
        asked.append(n_questions)
        return book.book_generator.GenerationResult(
            questions=[{
                "question_text": f"{theme_title} question {i}",
                "options": {"A": f"{theme_title} a{i}", "B": f"{theme_title} b{i}"},
                "correct_answer": "A",
                "correct_answers": ["A"], "is_open_ended": False,
                "img_index": None,
            } for i in range(n_questions)],
            requested=n_questions, errored=False,
        )

    monkeypatch.setattr(
        book.book_generator, "generate_for_theme", fake_generate_for_theme
    )

    class _Status:
        async def edit_text(self, *a, **k):
            return None

    await book._run_generation(
        _Status(), "uz",
        ["a", "b", "c"], ["ta", "tb", "tc"], None, 20, 12345,
        theme_ids=["", "", ""],
    )
    assert asked == [7, 7, 6]
