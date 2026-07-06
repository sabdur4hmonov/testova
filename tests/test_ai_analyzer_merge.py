"""
Tests for AIAnalyzer._merge_pages — cross-page stitching (bug #6), section
awareness (multi-test documents), renumbering, and the pre-existing
merge-by-number behavior. Pure functions, no Gemini/network.
"""
from app.services.ai_analyzer import AIAnalyzer, summarize_sections


def q(n, text="text", opts=None, frag=False, page=1, sec_title=None):
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
        "section_title": sec_title,
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


# ── Multi-test documents: section detection & (section, number) keying ───────

def test_numbering_restart_creates_second_section():
    pages = [
        [q(6, "s1q6", opts=FULL), q(7, "s1q7", opts=FULL)],
        [q(8, "s1q8", opts=FULL, page=2),
         q(1, "s2q1", opts=FULL, page=2), q(2, "s2q2", opts=FULL, page=2)],
    ]
    result = merge(pages)
    assert len(result) == 5, "both sections' questions must survive"
    assert [(x["section"], x["question_number"]) for x in result] == [
        (1, 6), (1, 7), (1, 8), (2, 1), (2, 2),
    ]
    # No merging/overwriting across sections
    assert result[0]["question_text"] == "s1q6"
    assert result[3]["question_text"] == "s2q1"


def test_small_decrease_to_one_is_not_a_restart():
    # 2 -> 1 is extraction noise, not a second test (drop < 5, no title).
    pages = [[q(2, opts=FULL), q(1, opts=FULL), q(3, opts=FULL)]]
    result = merge(pages)
    assert {x["section"] for x in result} == {1}
    assert [x["question_number"] for x in result] == [1, 2, 3]


def test_moderate_decrease_does_not_create_section():
    # n drops but not to <= 3 and no title — column noise, not a restart.
    pages = [[q(5, opts=FULL), q(4, opts=FULL)]]
    result = merge(pages)
    assert {x["section"] for x in result} == {1}
    assert len(result) == 2


def test_title_signal_triggers_section():
    # Restart flagged by Gemini title even though n=5 (> the <=3 rule).
    pages = [
        [q(10, opts=FULL)],
        [q(5, "new test", opts=FULL, page=2, sec_title="Anorganik moddalar")],
    ]
    result = merge(pages)
    assert result[0]["section"] == 1
    assert result[1]["section"] == 2
    assert result[1]["section_title"] == "Anorganik moddalar"


def test_same_number_merge_stays_within_section():
    # Section 2's q1 must NOT merge into section 1's q1.
    pages = [
        [q(1, "s1q1", opts={})],
        [q(1, "s1q1-continued", opts=FULL, page=2)],  # same section: 1 !< 1
        [q(52, "s1q52", opts=FULL, page=3),
         q(1, "s2q1", opts=FULL, page=3)],
    ]
    result = merge(pages)
    nums = [(x["section"], x["question_number"]) for x in result]
    assert nums == [(1, 1), (1, 52), (2, 1)]
    assert result[0]["options"] == FULL          # cross-page merge worked
    assert result[2]["question_text"] == "s2q1"  # not swallowed by (1,1)


def test_fragment_stitches_across_section_boundary():
    # Page ends with a cut sec-1 question; next page: fragment first, then
    # the new section starts. Fragment must stitch to the sec-1 question.
    pages = [
        [q(51, opts=FULL), q(52, "cut", opts={})],
        [q(0, "", opts=FULL, frag=True, page=2),
         q(1, "s2q1", opts=FULL, page=2, sec_title="Test 2")],
    ]
    result = merge(pages)
    assert [(x["section"], x["question_number"]) for x in result] == [
        (1, 51), (1, 52), (2, 1),
    ]
    assert result[1]["options"] == FULL


# ── summarize_sections (no merging, no renumbering) ─────────────────────────

def _sec_q(sec, n, open_ended=False):
    d = q(n, f"s{sec}q{n}", opts={} if open_ended else FULL)
    d["section"] = sec
    d["is_open_ended"] = open_ended
    return d


def test_summarize_two_sections_leaves_questions_untouched():
    qs = [_sec_q(1, n) for n in (1, 2, 3)] + [_sec_q(2, n) for n in (1, 2)]
    meta = summarize_sections(qs)
    # No renumbering: numbers stay exactly as printed
    assert [x["question_number"] for x in qs] == [1, 2, 3, 1, 2]
    assert meta[0] == {
        "section": 1, "title": None, "count": 3, "max": 3,
        "gaps": [], "open": [],
    }
    assert meta[1]["section"] == 2
    assert (meta[1]["count"], meta[1]["max"]) == (2, 2)


def test_summarize_single_section():
    meta = summarize_sections([_sec_q(1, n) for n in (1, 2, 3)])
    assert len(meta) == 1
    assert meta[0]["max"] == 3


def test_summarize_reports_gaps_and_open():
    qs = [_sec_q(1, 1), _sec_q(1, 3, open_ended=True)]
    meta = summarize_sections(qs)
    assert meta[0]["gaps"] == [2]
    assert meta[0]["open"] == [3]


def test_summarize_captures_title():
    qs = [_sec_q(1, 1), _sec_q(2, 1)]
    qs[1]["section_title"] = "Anorganik moddalar"
    meta = summarize_sections(qs)
    assert meta[1]["title"] == "Anorganik moddalar"


# ── Gap recovery: whole missing questions (33-36 block bug) ──────────────────

def test_gap_recovery_inserts_asked_questions():
    existing = [_sec_q(1, n) for n in (32, 37)]
    items = [q(n, f"recovered {n}", opts=FULL) for n in (33, 34, 35, 36)]
    for it in items:
        del it["page_number"]  # _normalize output carries no page key
    inserted = AIAnalyzer._apply_recovered_questions(
        existing, items, expected={33, 34, 35, 36}, section=1, default_page=3
    )
    assert inserted == 4
    nums = sorted(x["question_number"] for x in existing)
    assert nums == [32, 33, 34, 35, 36, 37]
    rec = next(x for x in existing if x["question_number"] == 33)
    assert rec["section"] == 1
    assert rec["page_number"] == 3
    assert rec["is_open_ended"] is False


def test_gap_recovery_rejects_unasked_numbers():
    # Hallucination guard: Gemini returns a number we didn't ask for.
    existing = [_sec_q(1, 1)]
    items = [q(99, "invented", opts=FULL)]
    inserted = AIAnalyzer._apply_recovered_questions(
        existing, items, expected={2}, section=1, default_page=1
    )
    assert inserted == 0
    assert len(existing) == 1


def test_gap_recovery_never_overwrites_existing():
    existing = [_sec_q(1, 5)]
    original_text = existing[0]["question_text"]
    items = [q(5, "different text", opts=FULL)]
    inserted = AIAnalyzer._apply_recovered_questions(
        existing, items, expected={5}, section=1, default_page=1
    )
    assert inserted == 0
    assert existing[0]["question_text"] == original_text


def test_gap_recovery_rejects_empty_text_and_single_option():
    existing = []
    items = [
        q(10, "", opts=FULL),                # empty text
        q(11, "one option", opts={"A": "a"}),  # not gradeable
        q(12, "open ended", opts={}),          # accepted as open-ended
    ]
    inserted = AIAnalyzer._apply_recovered_questions(
        existing, items, expected={10, 11, 12}, section=2, default_page=4
    )
    assert inserted == 1
    assert existing[0]["question_number"] == 12
    assert existing[0]["is_open_ended"] is True
    assert existing[0]["section"] == 2


# ── Truncated-output salvage parser (only 37-39 survived bug) ────────────────

def _analyzer():
    return AIAnalyzer()


def test_parse_salvages_truncated_array():
    # Output hit the token cap: array never closed, last object cut mid-field.
    raw = (
        '[{"n": 1, "q": "first", "A": "a", "B": "b", "C": "c", "D": "d"},'
        ' {"n": 2, "q": "second", "A": "a", "B": "b", "C": "c", "D": "d"},'
        ' {"n": 3, "q": "third cut of'
    )
    parsed = _analyzer()._parse(raw, page_num=1)
    assert [x["question_number"] for x in parsed] == [1, 2]
    assert parsed[1]["question_text"] == "second"


def test_parse_valid_json_unaffected():
    raw = '[{"n": 7, "q": "ok", "A": "a", "B": "b", "C": "c", "D": "d"}]'
    parsed = _analyzer()._parse(raw, page_num=1)
    assert len(parsed) == 1
    assert parsed[0]["question_number"] == 7


def test_parse_garbage_still_returns_empty():
    assert _analyzer()._parse("no json here at all", page_num=1) == []
