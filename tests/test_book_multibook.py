"""Multi-book generation ("Bir nechta kitobdan"), rebuilt on page ranges.

CP25 built this on TOPIC accumulation and it was retired with the TOC flow.
The rebuild accumulates the SYNTHETIC is_page_range themes each book's own
page-range prompt produces, so every book contributes its own page ranges to
one combined test.

CP25 found two gaps that would recur verbatim on a naive rebuild. Both are
re-verified here rather than re-discovered:

  (a) image capture was hardcoded to ONE book_id. _get_or_capture_theme_images
      already resolves each theme's own book via BookTheme.book_id - that
      survived the retirement - and test_image_capture_resolves_each_themes_own_book
      pins it.
  (b) per-book text prep leaked page-boundary titles across books. That is gone
      BY CONSTRUCTION now: a page range is taken WHOLE (own_title and
      next_theme_title are both None in _run_page_range_generation), so there is
      no cross-book title state left to leak. Pinned below.

And one gap CP25 could NOT have seen: books.kind (CP31) landed AFTER CP25, and
it selects the prompt shape. A single run-wide kind would apply one book's shape
to another's text, and the real library is mixed - grammarway and Enterprise 2
are workbooks, kimyo 7 and literatura 9 are narrative.
"""
from __future__ import annotations

import pytest

from app.bot.handlers import book
from app.bot.keyboards.inline import (
    book_entry_menu_keyboard, book_multibook_hub_keyboard,
)
from app.bot.states.forms import BookStates
from app.config import settings


def _btn_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# -- the entry point ---------------------------------------------------------

def test_the_entry_is_hidden_until_a_teacher_has_two_books():
    """One book cannot be combined with anything, so the button would be a dead
    end - same has_books convention the rest of this menu uses."""
    one = book_entry_menu_keyboard(
        "uz", has_books=True, has_public=False, has_multiple_books=False
    )
    assert "book:multilibrary" not in _btn_data(one)

    two = book_entry_menu_keyboard(
        "uz", has_books=True, has_public=False, has_multiple_books=True
    )
    assert "book:multilibrary" in _btn_data(two)


def test_the_public_library_button_survives_the_new_entry():
    """The multi-book label was inserted into the middle of the label tuple, so
    the public-library button is the one that would silently take the wrong
    callback if the indexes slipped."""
    kb = book_entry_menu_keyboard(
        "uz", has_books=True, has_public=True, has_multiple_books=True
    )
    assert _btn_data(kb) == [
        "book:add", "book:library", "book:multilibrary", "book:publib:open",
    ]


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
def test_the_hub_offers_add_and_done_in_every_language(lang):
    assert _btn_data(book_multibook_hub_keyboard(lang)) == [
        "book:multi:add", "book:multi:done",
    ]


# -- accumulation ------------------------------------------------------------

class _State:
    def __init__(self, data=None):
        self.state = None
        self.data = dict(data or {})

    async def set_state(self, s):
        self.state = s

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}


class _Msg:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))
        return self

    # A message returned by .answer() is used as a live status message by the
    # generation path, which edits and deletes it.
    async def edit_text(self, *a, **k):
        return self

    async def delete(self):
        return None


class _CB:
    def __init__(self, data, state_data=None):
        self.data = data
        self.message = _Msg()

    async def answer(self, *a, **k):
        return None


async def test_starting_a_multibook_run_clears_any_earlier_selection():
    """A fresh run must not inherit themes from a previous one, or a teacher
    who abandoned a selection silently gets it folded into the next test."""
    state = _State({
        "gen_theme_titles": ["stale"], "gen_theme_ids": ["x"],
        "gen_theme_pages": [9], "gen_theme_kinds": ["narrative"],
        "gen_theme_book_ids": ["b"],
    })

    async def fake_picker(message, st, user_id, lang):
        return True

    import app.bot.handlers.book as bk
    original = bk._show_book_picker
    bk._show_book_picker = fake_picker
    try:
        class _U:
            class language:
                value = "uz"
            id = "11111111-1111-1111-1111-111111111111"
        await bk.handle_multibook_start(_CB("book:multilibrary"), state, _U())
    finally:
        bk._show_book_picker = original

    assert state.data["multibook"] is True
    assert state.data["gen_theme_titles"] == []
    assert state.data["gen_theme_ids"] == []
    assert state.data["gen_theme_pages"] == []
    assert state.data["gen_theme_kinds"] == []


async def test_done_refuses_an_empty_selection():
    state = _State({"multibook": True, "gen_theme_titles": []})
    cb = _CB("book:multi:done")

    class _U:
        class language:
            value = "uz"
        id = "1"
    await book.handle_multibook_done(cb, state, _U())
    assert state.state is None          # cleared, not advanced


async def test_done_advances_to_the_shared_generation_chain():
    """The multi-book flow must join the EXISTING count -> written -> ... chain
    rather than growing a parallel one."""
    state = _State({
        "multibook": True,
        "gen_theme_titles": ["kimyo 7: 20-27-betlar", "grammarway: 10-15-betlar"],
        "gen_theme_pages": [8, 6],
    })
    cb = _CB("book:multi:done")

    class _U:
        class language:
            value = "uz"
        id = "1"
    await book.handle_multibook_done(cb, state, _U())
    assert state.state == BookStates.waiting_for_generate_count


async def test_done_refuses_a_run_over_the_combined_page_ceiling():
    """The per-range caps ran per book and cannot see the total: three books at
    the scanned cap is 90 pages and 15 sequential Vision calls, and no
    per-range check would notice."""
    over = settings.MAX_MULTIBOOK_TOTAL_PAGES + 1
    state = _State({
        "multibook": True,
        "gen_theme_titles": ["a", "b"],
        "gen_theme_pages": [over - 10, 10],
    })
    cb = _CB("book:multi:done")

    class _U:
        class language:
            value = "uz"
        id = "1"
    await book.handle_multibook_done(cb, state, _U())

    # Refused, and the selection is KEPT so the teacher can drop ranges rather
    # than re-preparing every book from scratch.
    assert state.state != BookStates.waiting_for_generate_count
    assert state.data["gen_theme_titles"] == ["a", "b"]
    assert str(over) in cb.message.answers[0][0]


async def test_exactly_at_the_ceiling_is_allowed():
    state = _State({
        "multibook": True,
        "gen_theme_titles": ["a"],
        "gen_theme_pages": [settings.MAX_MULTIBOOK_TOTAL_PAGES],
    })
    cb = _CB("book:multi:done")

    class _U:
        class language:
            value = "uz"
        id = "1"
    await book.handle_multibook_done(cb, state, _U())
    assert state.state == BookStates.waiting_for_generate_count


# -- the CP31 gap CP25 could not have seen -----------------------------------

async def test_each_theme_is_generated_with_its_own_books_kind(monkeypatch):
    """A workbook's prompt block and a narrative's are different. Mixing books
    must not apply one book's shape to the other's text."""
    seen: list[tuple[str, str]] = []

    def fake_generate_for_theme(*, theme_title, n_questions, book_kind, **kw):
        seen.append((theme_title, book_kind))
        return book.book_generator.GenerationResult(
            questions=[{
                "question_text": f"{theme_title} q{i}",
                "options": {"A": f"{theme_title}a{i}", "B": f"{theme_title}b{i}"},
                "correct_answer": "A", "correct_answers": ["A"],
                "is_open_ended": False, "img_index": None,
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
        ["grammarway: 10-15", "kimyo 7: 20-27"],
        ["workbook text", "narrative text"],
        None, 6, 12345,
        theme_ids=["", ""],
        theme_pages=[6, 8],
        theme_kinds=["workbook", "narrative"],
        book_kind="workbook",       # the run-wide value must NOT win
    )
    assert dict(seen) == {
        "grammarway: 10-15": "workbook",
        "kimyo 7: 20-27": "narrative",
    }


async def test_a_run_without_per_theme_kinds_falls_back_to_the_run_wide_one(monkeypatch):
    """Every pre-existing caller passes no theme_kinds and must be unaffected."""
    seen: list[str] = []

    def fake_generate_for_theme(*, theme_title, n_questions, book_kind, **kw):
        seen.append(book_kind)
        return book.book_generator.GenerationResult(
            questions=[], requested=n_questions, errored=False,
        )

    monkeypatch.setattr(
        book.book_generator, "generate_for_theme", fake_generate_for_theme
    )

    class _Status:
        async def edit_text(self, *a, **k):
            return None

    await book._run_generation(
        _Status(), "uz", ["a", "b"], ["ta", "tb"], None, 4, 12345,
        theme_ids=["", ""], book_kind="vocab",
    )
    assert seen == ["vocab", "vocab"]


# -- CP25 gap (a), re-verified rather than re-discovered ---------------------

def test_image_capture_resolves_each_themes_own_book():
    """_get_or_capture_theme_images must take theme ids ALONE - the moment it
    takes a single book_id, a multi-book run crops every theme's figures out of
    one book's PDF. This is the CP25 gap; it survived the retirement, and this
    pins the signature that makes it impossible."""
    import inspect
    params = inspect.signature(book._get_or_capture_theme_images).parameters
    assert list(params) == ["theme_ids"]


def test_run_generation_no_longer_uses_book_id_for_anything():
    """book_id stays on the signature for callers, but a multi-book run spans
    several books, so nothing inside may key off it."""
    import inspect
    src = inspect.getsource(book._run_generation)
    body = src.split(") -> tuple[list[dict], list[str], list[str]]:", 1)[1]
    # Only the docstring/comments may mention it; no executable use.
    code_lines = [
        ln.strip() for ln in body.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert not any(
        "book_id" in ln and "=" in ln and not ln.startswith('"')
        for ln in code_lines
    ), "book_id must not drive behaviour inside _run_generation"


# -- CP25 gap (b), gone by construction --------------------------------------

def test_page_ranges_are_taken_whole_so_no_boundary_state_can_leak():
    """CP25's second gap was per-book text prep leaking next_theme_title across
    books. A page-range theme is extracted WHOLE, so the boundary-trimming
    arguments are unconditionally None and there is nothing left to leak."""
    import inspect
    src = inspect.getsource(book._run_page_range_generation)
    assert "own_title=None, next_theme_title=None" in src


# -- the accumulation rules --------------------------------------------------

def _acc(data, **kw):
    base = dict(
        book_title="kimyo 7", book_id="book-a", book_kind="narrative",
        book_language="uz",
        titles=["20-27-betlar"], texts=["kimyo text"],
        theme_ids=["t1"], pages=[8],
    )
    base.update(kw)
    return book.accumulate_multibook(data, **base)


def test_the_first_book_seeds_the_selection():
    out = _acc({})
    assert out["gen_theme_titles"] == ["kimyo 7: 20-27-betlar"]
    assert out["gen_theme_ids"] == ["t1"]
    assert out["gen_theme_pages"] == [8]
    assert out["gen_theme_kinds"] == ["narrative"]
    assert out["gen_theme_book_ids"] == ["book-a"]
    assert out["gen_book_title"] == "kimyo 7"


def test_a_second_book_appends_rather_than_overwriting():
    first = _acc({})
    out = _acc(
        first, book_title="grammarway", book_id="book-b", book_kind="workbook",
        book_language="en",
        titles=["10-15-betlar", "40-44-betlar"],
        texts=["gw a", "gw b"], theme_ids=["t2", "t3"], pages=[6, 5],
    )
    assert out["gen_theme_titles"] == [
        "kimyo 7: 20-27-betlar",
        "grammarway: 10-15-betlar",
        "grammarway: 40-44-betlar",
    ]
    assert out["gen_theme_ids"] == ["t1", "t2", "t3"]
    assert out["gen_theme_pages"] == [8, 6, 5]
    assert out["gen_theme_book_ids"] == ["book-a", "book-b", "book-b"]


def test_every_per_theme_list_stays_index_aligned():
    """Distribution, image capture and the prompt shape all zip against these,
    so a length drift silently pairs a theme with another theme's data."""
    out = _acc(
        _acc({}), book_title="grammarway", book_id="book-b",
        book_kind="workbook",
        titles=["10-15", "40-44"], texts=["a", "b"],
        theme_ids=["t2", "t3"], pages=[6, 5],
    )
    n = len(out["gen_theme_titles"])
    for key in (
        "gen_theme_texts", "gen_theme_ids", "gen_theme_pages",
        "gen_theme_kinds", "gen_theme_book_ids",
    ):
        assert len(out[key]) == n, key


def test_each_books_kind_is_recorded_against_its_own_themes():
    out = _acc(
        _acc({}), book_title="grammarway", book_id="book-b",
        book_kind="workbook",
        titles=["10-15"], texts=["gw"], theme_ids=["t2"], pages=[6],
    )
    assert out["gen_theme_kinds"] == ["narrative", "workbook"]


def test_re_picking_the_same_book_and_ranges_adds_nothing_twice():
    """create_page_range_themes serves an already-materialised range from cache
    and returns the SAME theme row, so a teacher who re-picks a book they
    already added must not double it."""
    first = _acc({})
    again = _acc(first)
    assert again["gen_theme_ids"] == ["t1"]
    assert again["gen_theme_titles"] == ["kimyo 7: 20-27-betlar"]
    assert again["gen_theme_pages"] == [8]


def test_a_partial_overlap_keeps_only_the_new_ranges():
    first = _acc({})
    out = _acc(
        first, titles=["20-27-betlar", "50-55-betlar"],
        texts=["kimyo text", "kimyo more"], theme_ids=["t1", "t9"],
        pages=[8, 6],
    )
    assert out["gen_theme_ids"] == ["t1", "t9"]
    assert out["gen_theme_pages"] == [8, 6]


def test_the_first_books_title_and_language_are_kept():
    """Both are informational for a mixed run - build_prompt deliberately
    ignores `language` and pins each theme to its own source text - so the
    first book's values simply persist rather than being overwritten book by
    book."""
    out = _acc(
        _acc({}), book_title="grammarway", book_id="book-b",
        book_language="en", titles=["10-15"], texts=["gw"],
        theme_ids=["t2"], pages=[6],
    )
    assert out["gen_book_title"] == "kimyo 7"
    assert out["gen_language"] == "uz"


def test_the_page_totals_the_hub_reports_are_the_ones_the_cap_checks():
    """The hub shows sum(gen_theme_pages) and handle_multibook_done caps on the
    same list, so what the teacher is shown is what gets enforced."""
    out = _acc(
        _acc({}), book_title="grammarway", book_id="book-b",
        titles=["10-15"], texts=["gw"], theme_ids=["t2"], pages=[6],
    )
    assert sum(out["gen_theme_pages"]) == 14


# -- quota: ONE test charge for the whole combined run -----------------------

async def test_a_combined_run_consumes_exactly_one_test(monkeypatch):
    """The expected answer, VERIFIED rather than assumed: quota.TEST is consumed
    once per ATTEMPT in _generate_one_attempt_inner, not once per book and not
    once per theme. Books are already owned, so nothing is charged for them.

    Driven with four themes spanning three books - if the consume had drifted
    into the per-theme or per-book loop this would count 4 or 3.
    """
    consumed: list[str] = []

    async def fake_consume(factory, telegram_id, kind):
        consumed.append(kind)
        return True

    async def fake_budget(factory, telegram_id):
        return True

    async def fake_run_generation(*a, **kw):
        return [], [], [], []

    async def fake_review(*a, **kw):
        return None

    monkeypatch.setattr(book.quota, "check_and_consume", fake_consume)
    monkeypatch.setattr(book.quota, "within_token_budget", fake_budget)
    monkeypatch.setattr(book.plans, "is_trial", lambda u: False)
    monkeypatch.setattr(book, "_run_generation", fake_run_generation)
    monkeypatch.setattr(book, "_show_generation_review", fake_review)

    class _U:
        telegram_id = 4242

        class language:
            value = "uz"

    await book._generate_one_attempt_inner(
        _Msg(), _State(), "uz", _U(), 1,
        titles=["kimyo: a", "kimyo: b", "grammarway: c", "roundup: d"],
        texts=["ta", "tb", "tc", "td"],
        language="uz", total=20, difficulty=None,
        option_labels=["A", "B", "C", "D"], n_written=0,
        include_images=False, question_language=None,
        book_kind="narrative", book_id=None,
        theme_ids=["t1", "t2", "t3", "t4"],
        theme_pages=[8, 6, 6, 5],
        theme_kinds=["narrative", "narrative", "workbook", "workbook"],
    )

    assert consumed == [book.quota.TEST]
    assert book.quota.TEST == book.quota.VARIANT   # TEST is aliased onto variants
