"""The trimming guarantee through the real handler path (_run_generation, the
review screen, partial-accept), with generate_for_theme stubbed.

Pins:
  * BOOK_TRIM_TO_REQUESTED_ENABLED (default True) trims to the exact per-type
    counts; False restores the old path exactly (over-delivery kept, the
    shortfall footer compares totals only);
  * a trim is silent to the teacher and logged as book_generation_trimmed;
  * the shortfall footer names the per-type counts, pinned verbatim for every
    UI language (the middle dot is U+00B7; this project has had a real
    mojibake bug, so the rendered bytes are pinned, not paraphrased);
  * partial-accept logs per-type requested/accepted counts;
  * a clamp of the per-theme written target is logged as a warning.
"""
from __future__ import annotations

import itertools

import pytest

from app.bot.handlers import book
from app.config import settings
from app.services.book_extractor import looks_like_codepage_mojibake
from app.services.book_generator import GenerationResult

_SEQ = itertools.count()
TEXT = ("Suv vodorod va kisloroddan iborat. Kislorod yonishni quvvatlaydi. "
        "Vodorod eng yengil gaz. Azot havoning asosiy qismi.")


def _mc():
    i = next(_SEQ)
    return {"question_text": f"MC savol {i} {i * 7919 % 997}?",
            "options": {"A": "vodorod", "B": f"x{i}b", "C": f"x{i}c", "D": f"x{i}d"},
            "correct_answer": "A", "correct_answers": ["A"],
            "is_open_ended": False, "img_index": None}


def _op():
    i = next(_SEQ)
    return {"question_text": f"Ochiq savol {i} {i * 104729 % 991}?", "options": {},
            "correct_answer": None, "correct_answers": [f"vodorod {i}"],
            "is_open_ended": True, "img_index": None}


def _stub(monkeypatch, per_theme):
    """per_theme: {title: (n_mc, n_open)} the stub returns for that theme."""
    def fake(*, theme_title, n_questions, **kw):
        m, w = per_theme[theme_title]
        return GenerationResult(questions=[_mc() for _ in range(m)] + [_op() for _ in range(w)],
                                requested=n_questions, errored=False)
    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake)


class _Status:
    async def edit_text(self, *a, **k):
        return None


class _Msg:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class _State:
    def __init__(self, **data):
        self.data, self.state = dict(data), None

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data, self.state = {}, None


def _events(monkeypatch):
    seen = []
    for level in ("info", "warning"):
        monkeypatch.setattr(book.logger, level,
                            lambda ev, _l=level, **kw: seen.append((_l, ev, kw)))
    return seen


async def _run(titles, total, written, pages=None):
    qs, *_ = await book._run_generation(
        _Status(), "uz", titles, [TEXT] * len(titles), "uz", total, 12345,
        n_written_total=written, theme_ids=[""] * len(titles), theme_pages=pages,
    )
    return qs


def _types(qs):
    w = sum(1 for q in qs if q["is_open_ended"])
    return len(qs) - w, w


# ── the switch ───────────────────────────────────────────────────────────────

def test_the_switch_ships_on():
    assert settings.BOOK_TRIM_TO_REQUESTED_ENABLED is True


async def test_mcq_over_delivery_is_trimmed_and_the_open_count_kept(monkeypatch):
    _stub(monkeypatch, {"T": (40, 4)})
    qs = await _run(["T"], 40, 4)
    assert _types(qs) == (36, 4)
    assert [q["question_number"] for q in qs] == list(range(1, 41))


async def test_switched_off_the_old_path_is_restored_exactly(monkeypatch):
    monkeypatch.setattr(settings, "BOOK_TRIM_TO_REQUESTED_ENABLED", False)
    _stub(monkeypatch, {"T": (40, 4)})
    qs = await _run(["T"], 40, 4)
    assert _types(qs) == (40, 4), "over-delivery kept, as before"
    assert [q["question_number"] for q in qs] == list(range(1, 45))


async def test_a_trim_is_logged_with_per_type_counts(monkeypatch):
    seen = _events(monkeypatch)
    _stub(monkeypatch, {"T": (40, 5)})
    await _run(["T"], 40, 4)
    kw = next(kw for _l, ev, kw in seen if ev == "book_generation_trimmed")
    assert kw["requested"] == {"mc": 36, "written": 4}
    assert kw["before"] == {"mc": 40, "written": 5}
    assert kw["after"] == {"mc": 36, "written": 4}
    assert kw["dropped"] == {"mc": 4, "written": 1}


async def test_no_trim_no_log(monkeypatch):
    seen = _events(monkeypatch)
    _stub(monkeypatch, {"T": (36, 4)})
    await _run(["T"], 40, 4)
    assert not [ev for _l, ev, _ in seen if ev == "book_generation_trimmed"]


async def test_multi_theme_surplus_fills_the_other_theme_same_type(monkeypatch):
    _stub(monkeypatch, {"A": (13, 0), "B": (8, 0)})
    qs = await _run(["A", "B"], 20, 0, pages=[10, 10])
    assert _types(qs) == (20, 0)


async def test_an_all_blank_mcq_converted_to_open_counts_by_the_open_rule(monkeypatch):
    """validate_questions (now run before trimming) turns an MC whose options
    are all blank into an open question; its answer is the MC's key letter."""
    def fake(*, theme_title, n_questions, **kw):
        broken = _mc()
        broken["options"] = {"A": " ", "B": ""}
        broken["correct_answers"] = ["vodorod va kislorod birikmasidan iborat bo'ladi"]
        return GenerationResult(questions=[_mc(), _mc(), broken], requested=n_questions,
                                errored=False)
    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake)
    qs = await _run(["T"], 3, 1)
    assert _types(qs) == (2, 0), "a long converted answer is not a written question"


# ── the review screen ────────────────────────────────────────────────────────

FOOTER_LINES = {
    "uz": "\nYozma: 2/4 · Test: 36/36",
    "en": "\nWritten: 2/4 · Multiple-choice: 36/36",
    "ru": "\nПисьменные: 2/4 · С вариантами: 36/36",
}


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
async def test_the_per_type_line_is_rendered_verbatim(lang):
    assert book.t("generation_shortfall_by_type", lang,
                  got_w=2, req_w=4, got_mc=36, req_mc=36) == FOOTER_LINES[lang]


def test_every_ui_language_has_the_per_type_line():
    from app.models.user import Language
    assert set(book.T["generation_shortfall_by_type"]) == {l.value for l in Language}


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
def test_the_per_type_line_is_clean_utf8_not_mojibake(lang):
    line = FOOTER_LINES[lang]
    assert "·" in line and "Â" not in line   # "Â·" is the UTF-8-as-cp1252 form
    assert line.encode("utf-8").decode("utf-8") == line
    assert looks_like_codepage_mojibake(line * 20) is False


async def _review(monkeypatch, lang, per_theme, total, written):
    _stub(monkeypatch, per_theme)
    titles = list(per_theme)
    qs = await _run(titles, total, written)
    state = _State(gen_total=total, gen_written=written, gen_theme_titles=titles,
                   gen_theme_pages=[])
    msg = _Msg()
    await book._show_generation_review(msg, state, lang, qs, [], [], 1)
    return msg.answers[-1], state


async def test_open_short_mcq_over_shows_the_per_type_shortfall(monkeypatch):
    (text, kw), state = await _review(monkeypatch, "uz", {"T": (38, 2)}, 40, 4)
    assert "Siz 40 ta so'radingiz" in text and "faqat 38 ta" in text
    assert text.endswith(FOOTER_LINES["uz"])
    assert "book:gen:accept_partial" in str(kw["reply_markup"])


async def test_exact_counts_show_no_footer(monkeypatch):
    (text, kw), _ = await _review(monkeypatch, "uz", {"T": (40, 5)}, 40, 4)
    assert "Yozma:" not in text and "so'radingiz" not in text


async def test_no_written_requested_no_per_type_line(monkeypatch):
    (text, _kw), _ = await _review(monkeypatch, "uz", {"T": (30, 0)}, 40, 0)
    assert "so'radingiz" in text and "Yozma:" not in text


async def test_switched_off_the_footer_compares_totals_only(monkeypatch):
    monkeypatch.setattr(settings, "BOOK_TRIM_TO_REQUESTED_ENABLED", False)
    (text, _kw), _ = await _review(monkeypatch, "uz", {"T": (38, 2)}, 40, 4)
    assert "so'radingiz" not in text, "40 delivered for 40: the old total-only check is blind"


async def test_partial_accept_logs_per_type_counts(monkeypatch):
    (_text, _kw), state = await _review(monkeypatch, "uz", {"T": (38, 2)}, 40, 4)
    seen = _events(monkeypatch)

    class _User:
        telegram_id = 12345

        class language:
            value = "uz"

    class _Cb:
        message = _Msg()

        async def answer(self, *a, **k):
            return None

    await book.handle_generation_accept_partial(_Cb(), state, _User())
    kw = next(kw for _l, ev, kw in seen if ev == "book_generation_partial_accepted")
    assert (kw["requested"], kw["accepted"]) == (40, 38)
    assert (kw["requested_written"], kw["accepted_written"]) == (4, 2)
    assert (kw["requested_mc"], kw["accepted_mc"]) == (36, 36)


async def test_a_clamped_written_target_is_logged(monkeypatch):
    real = book.book_generator.distribute_questions
    # Force a split where one theme is allotted more written than questions.
    # total 3 -> [3, 0]; written 2 -> [1, 1]: theme B is allotted 1 written of 0.
    monkeypatch.setattr(book.book_generator, "distribute_questions",
                        lambda total, n, weights=None: [3, 0] if total == 3 else [1, 1])
    seen = _events(monkeypatch)
    _stub(monkeypatch, {"A": (2, 1), "B": (0, 0)})
    await _run(["A", "B"], 3, 2)
    kw = next(kw for l, ev, kw in seen if ev == "book_generation_type_targets_clamped")
    assert kw["requested_written"] == 2 and kw["effective_written"] == 1
    monkeypatch.setattr(book.book_generator, "distribute_questions", real)
