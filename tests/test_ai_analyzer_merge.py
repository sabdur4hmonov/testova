"""
Tests for AIAnalyzer._merge_pages — cross-page stitching (bug #6) and the
pre-existing merge-by-number behavior. Pure function, no Gemini/network.
"""
from app.services.ai_analyzer import AIAnalyzer


def q(n, text="text", opts=None, frag=False, page=1):
    return {
        "question_number": n,
        "question_text": text,
        "options": dict(opts or {}),
        "correct_answer": None,
        "has_image": False,
        "image_description": None,
        "image_path": None,
        "is_open_ended": False,
        "is_fragment": frag,
        "group_id": None,
        "group_context": None,
        "page_number": page,
    }


FULL = {"A": "a", "B": "b", "C": "c", "D": "d"}


def merge(pages):
    return AIAnalyzer._merge_pages(pages)


# ── Fragment stitching (bug #6) ───────────────────────────────────────────────

def test_options_on_next_page_are_stitched():
    pages = [
        [q(1, opts=FULL), q(2, "cut question", opts={})],
        [q(0, "", opts=FULL, frag=True, page=2), q(3, opts=FULL, page=2)],
    ]
    result = merge(pages)
    assert [x["question_number"] for x in result] == [1, 2, 3]
    q2 = result[1]
    assert q2["options"] == FULL
    assert q2["is_open_ended"] is False


def test_partial_options_split():
    pages = [
        [q(1, opts={"A": "a", "B": "b"})],
        [q(0, "", opts={"C": "c", "D": "d"}, frag=True, page=2)],
    ]
    result = merge(pages)
    assert result[0]["options"] == FULL


def test_text_continuation_is_appended():
    pages = [
        [q(1, "Find the", opts={})],
        [q(0, "value of x.", opts=FULL, frag=True, page=2)],
    ]
    result = merge(pages)
    assert result[0]["question_text"] == "Find the value of x."
    assert result[0]["options"] == FULL
    assert result[0]["is_open_ended"] is False


def test_fragment_on_first_page_is_dropped():
    pages = [
        [q(0, "", opts=FULL, frag=True), q(1, opts=FULL)],
    ]
    result = merge(pages)
    assert [x["question_number"] for x in result] == [1]
    assert result[0]["options"] == FULL


def test_fragment_after_empty_page_is_dropped():
    # Page 2 failed/blocked — stitching page 3's fragment onto page 1
    # would risk hitting the wrong question, so it must be dropped.
    pages = [
        [q(1, opts={})],
        [],
        [q(0, "", opts=FULL, frag=True, page=3)],
    ]
    result = merge(pages)
    assert result[0]["options"] == {}
    assert result[0]["is_open_ended"] is True


def test_stitch_never_overwrites_existing_options():
    pages = [
        [q(1, opts=FULL)],
        [q(0, "", opts={"A": "other", "B": "other"}, frag=True, page=2)],
    ]
    result = merge(pages)
    assert result[0]["options"] == FULL


def test_suspicious_text_fragment_not_appended_to_complete_question():
    # Target already has all options and the fragment brings none —
    # likely an unnumbered NEW question, must not corrupt the previous one.
    pages = [
        [q(1, "complete question", opts=FULL)],
        [q(0, "What is 2+2?", opts={}, frag=True, page=2)],
    ]
    result = merge(pages)
    assert result[0]["question_text"] == "complete question"


def test_multiple_leading_fragments_stitch_to_same_target():
    pages = [
        [q(1, "cut", opts={})],
        [
            q(0, "", opts={"A": "a", "B": "b"}, frag=True, page=2),
            q(0, "", opts={"C": "c", "D": "d"}, frag=True, page=2),
        ],
    ]
    result = merge(pages)
    assert result[0]["options"] == FULL


def test_fragment_stitches_to_last_question_of_prev_page():
    pages = [
        [q(1, opts=FULL), q(2, "last on page", opts={})],
        [q(0, "", opts=FULL, frag=True, page=2)],
    ]
    result = merge(pages)
    assert result[0]["options"] == FULL          # q1 untouched
    assert result[1]["options"] == FULL          # q2 stitched


# ── Pre-existing behavior must survive the refactor ──────────────────────────

def test_same_number_merge_still_works():
    pages = [
        [q(5, "split by number", opts={})],
        [q(5, "", opts=FULL, page=2)],
    ]
    result = merge(pages)
    assert len(result) == 1
    assert result[0]["options"] == FULL


def test_open_ended_marking_still_works():
    pages = [[q(1, "open question", opts={})]]
    result = merge(pages)
    assert result[0]["is_open_ended"] is True


def test_unnumbered_non_fragment_items_still_dropped():
    # Mid-page n=0 items (not page-leading) are not stitched — same as before.
    pages = [[q(1, opts=FULL), q(0, "stray", opts={}, frag=True)]]
    result = merge(pages)
    assert [x["question_number"] for x in result] == [1]
    assert result[0]["question_text"] == "text"


def test_sorted_output():
    pages = [
        [q(2, opts=FULL), q(1, opts=FULL)],
        [q(3, opts=FULL, page=2)],
    ]
    result = merge(pages)
    assert [x["question_number"] for x in result] == [1, 2, 3]


# ── Bug #1: zero-option recovery pass (apply step) ────────────────────────────

def test_recovery_applies_options_and_clears_open_ended():
    target = q(20, "cut question", opts={})
    target["is_open_ended"] = True
    recovered = [q(20, "", opts={"A": "2n+2", "B": "3n+3", "C": "4+n", "D": "n+2"})]
    repaired = AIAnalyzer._apply_recovered_options([target], recovered)
    assert repaired == 1
    assert target["options"] == {"A": "2n+2", "B": "3n+3", "C": "4+n", "D": "n+2"}
    assert target["is_open_ended"] is False


def test_recovery_rejects_fewer_than_two_options():
    # Hallucination guard: 0 or 1 recovered options → question stays open-ended.
    target = q(5, "truly open", opts={})
    target["is_open_ended"] = True
    recovered = [q(5, "", opts={"A": "only one"})]
    repaired = AIAnalyzer._apply_recovered_options([target], recovered)
    assert repaired == 0
    assert target["options"] == {}
    assert target["is_open_ended"] is True


def test_recovery_ignores_unknown_question_numbers():
    target = q(5, "open", opts={})
    target["is_open_ended"] = True
    recovered = [q(99, "", opts=FULL)]
    repaired = AIAnalyzer._apply_recovered_options([target], recovered)
    assert repaired == 0
    assert target["is_open_ended"] is True


def test_recovery_strips_blank_option_strings():
    target = q(7, "cut", opts={})
    target["is_open_ended"] = True
    recovered = [q(7, "", opts={"A": "a", "B": "b", "C": "", "D": "  "})]
    repaired = AIAnalyzer._apply_recovered_options([target], recovered)
    assert repaired == 1
    assert target["options"] == {"A": "a", "B": "b"}
