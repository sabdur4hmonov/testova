"""
End-to-end letter preservation (KNOWN-OPEN #2): the paper's REAL option labels
(Latin, Cyrillic, gaps, any count) survive storage → shuffle → PDF → grading.
Nothing renumbers a,b,d,e into a,b,c,d; nothing drops option E; Cyrillic stays
Cyrillic for display, and grading matches via the shared canonical fold.
"""
from __future__ import annotations

from app.models.question import Question
from app.services.option_letters import canonical_letter
from app.services.pdf_generator import build_variants_pdf, build_variants_pdf_compact
from app.services.sheet_reader import _norm_letter
from app.services.variant_generator import generate_variants, validate_questions


# ── Model storage: real labels + gaps, with legacy fallback ──────────────────
def test_model_new_row_preserves_labels_and_gap():
    q = Question(
        question_number=1, question_text="x",
        options=[{"letter": "А", "text": "ta"}, {"letter": "Б", "text": "tb"},
                 {"letter": "Д", "text": "td"}, {"letter": "Е", "text": "te"}],
    )
    assert [o["letter"] for o in q.options_ordered] == ["А", "Б", "Д", "Е"]
    assert list(q.options_dict.keys()) == ["А", "Б", "Д", "Е"]  # order + gap kept


def test_model_old_row_reads_via_column_fallback():
    # options=NULL (pre-migration-007 row) → built from option_a..d, unchanged.
    q = Question(question_number=1, question_text="x",
                 option_a="ta", option_b="tb", option_c="tc", option_d="td")
    assert q.options_dict == {"A": "ta", "B": "tb", "C": "tc", "D": "td"}


def test_model_blank_entries_dropped():
    q = Question(question_number=1, question_text="x",
                 options=[{"letter": "A", "text": "ta"}, {"letter": "B", "text": ""}])
    assert list(q.options_dict.keys()) == ["A"]


# ── Persistence transform (the shape pipeline/file_tasks build) ───────────────
def _to_options_json(opts: dict) -> list:
    return [{"letter": str(k), "text": v} for k, v in opts.items() if v and str(v).strip()]


def test_persistence_shape_preserves_labels_no_e_drop():
    opts = {"A": "ta", "B": "tb", "D": "td", "E": "te"}   # gap at C, has E
    assert _to_options_json(opts) == [
        {"letter": "A", "text": "ta"}, {"letter": "B", "text": "tb"},
        {"letter": "D", "text": "td"}, {"letter": "E", "text": "te"},
    ]


# ── validate_questions: drop blanks, PRESERVE labels (no relabel) ────────────
def test_validate_preserves_gap_labels():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"A": "ta", "B": "tb", "D": "td", "E": "te"},
           "correct_answer": "E"}]
    valid, rejected = validate_questions(qs)
    assert rejected == []
    assert list(valid[0]["options"].keys()) == ["A", "B", "D", "E"]
    assert valid[0]["correct_answer"] == "E"   # real label, not remapped


# ── Shuffle: labels stay put, TEXTS permute, key = the label holding the text ─
def test_shuffle_preserves_label_set_and_gap():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"A": "TOSHKENT", "B": "SAMARQAND", "D": "BUXORO", "E": "XIVA"},
           "correct_answer": "D"}]   # correct text = BUXORO
    variants = generate_variants(qs, count=8, seed=7)
    for v in variants:
        qd = v["questions_data"][0]
        # The printed label set is exactly the paper's — no C, no relabel.
        assert set(qd["options"].keys()) == {"A", "B", "D", "E"}
        # The answer key names the label now holding BUXORO.
        key_label = v["answer_key"]["1"][0]   # answer_key values are lists now
        assert qd["options"][key_label] == "BUXORO"


def test_shuffle_cyrillic_labels_preserved():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"А": "ta", "Б": "tb", "Д": "td", "Е": "te"},
           "correct_answer": "Д"}]
    variants = generate_variants(qs, count=5, seed=3)
    for v in variants:
        qd = v["questions_data"][0]
        assert set(qd["options"].keys()) == {"А", "Б", "Д", "Е"}   # Cyrillic kept
        # key is CANONICAL (for grading); the label it points at holds td.
        key_canon = v["answer_key"]["1"][0]   # answer_key values are lists now
        real_label = next(L for L in qd["options"] if canonical_letter(L) == key_canon)
        assert qd["options"][real_label] == "td"


def test_five_option_all_preserved():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"},
           "correct_answer": "E"}]
    variants = generate_variants(qs, count=6, seed=1)
    for v in variants:
        assert set(v["questions_data"][0]["options"].keys()) == set("ABCDE")


# ── PDF builds with gaps and Cyrillic without crashing ───────────────────────
def _variant_for_pdf():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "Poytaxt?",
           "options": {"А": "Toshkent", "Б": "Samarqand", "Д": "Buxoro", "Е": "Xiva"},
           "correct_answer": "Д"},
          {"question_id": "q2", "question_number": 2, "question_text": "Gap test",
           "options": {"A": "one", "B": "two", "D": "four", "E": "five"},
           "correct_answer": "A"}]
    return generate_variants(qs, count=1, seed=2)


def test_pdf_standard_builds_with_gaps_and_cyrillic():
    pdf = build_variants_pdf(_variant_for_pdf(), exam_title="Test")
    assert isinstance(pdf, bytes) and pdf[:4] == b"%PDF"


def test_pdf_compact_builds_with_gaps_and_cyrillic():
    pdf = build_variants_pdf_compact(_variant_for_pdf(), exam_title="Test")
    assert isinstance(pdf, bytes) and pdf[:4] == b"%PDF"


# ── Grading match: canonical read == canonical key (both flows share it) ─────
def test_grading_match_canonical_across_scripts():
    # A generated Cyrillic variant's key is canonical; a student marking the real
    # label reads (via _norm_letter) to the SAME canonical → correct.
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"А": "ta", "Б": "tb", "Д": "td"}, "correct_answer": "Д"}]
    v = generate_variants(qs, count=1, seed=9)[0]
    qd = v["questions_data"][0]
    key_canon = v["answer_key"]["1"][0]   # answer_key values are lists now
    correct_label = next(L for L in qd["options"] if canonical_letter(L) == key_canon)
    # student marks that real Cyrillic label → reader canonicalises → matches key
    assert _norm_letter(correct_label) == key_canon
    # a Latin look-alike mark for the same option also matches
    assert _norm_letter(canonical_letter(correct_label)) == key_canon


# ── Stage 3 (unification): correct_answers list storage + fallback ───────────
def test_correct_answers_new_row_list():
    q = Question(question_number=1, question_text="x",
                 correct_answers=["PHONE", "TELEPHONE"])
    assert q.correct_answers_ordered == ["PHONE", "TELEPHONE"]


# ── CP22: wider Latin range (A–H) and numeric option labels ──────────────────
def test_option_letters_now_reach_h():
    from app.services.option_letters import is_option_letter
    assert is_option_letter("G") and is_option_letter("H")
    assert not is_option_letter("I"), "cap is H, not the whole alphabet"


def test_norm_letter_reads_a_numeric_option_mark():
    # A numeric-labelled test (allow_numeric=True): a single digit is a marked
    # option. Without that flag it stays a written answer (the default), so a
    # short-answer "5" is never misread as an option.
    assert _norm_letter("3", allow_numeric=True) == "3"
    assert _norm_letter("1", allow_numeric=True) == "1"
    assert _norm_letter("3") is None            # default: digit is written text
    # a multi-digit value is a written answer, not a label, either way
    assert _norm_letter("42", allow_numeric=True) is None


def test_numeric_labels_grade_by_value():
    from app.services.checker import is_correct
    # student marked "2", key is "2" → correct; "3" vs "2" → wrong.
    assert is_correct("2", ["2"]) is True
    assert is_correct("3", ["2"]) is False


def test_numeric_labelled_variant_round_trips_through_grading():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"1": "ta", "2": "tb", "3": "tc"}, "correct_answer": "2",
           "correct_answers": ["2"]}]
    v = generate_variants(qs, count=1, seed=5)[0]
    key = v["answer_key"]["1"][0]
    qd = v["questions_data"][0]
    # the label the key points at, marked by the student, reads back and matches
    correct_label = next(L for L in qd["options"] if L == key)
    assert _norm_letter(correct_label, allow_numeric=True) == key


def test_correct_answers_old_row_fallback_to_scalar():
    # options=NULL, correct_answers=NULL (pre-008 row) → falls back to the
    # legacy single-letter correct_answer, so old rows grade unchanged.
    q = Question(question_number=1, question_text="x", correct_answer="B")
    assert q.correct_answers_ordered == ["B"]


def test_correct_answers_empty_when_none_set():
    q = Question(question_number=1, question_text="x")
    assert q.correct_answers_ordered == []


def test_mc_variant_answer_key_is_one_item_list():
    qs = [{"question_id": "q1", "question_number": 1, "question_text": "t",
           "options": {"A": "ta", "B": "tb"}, "correct_answer": "A"}]
    v = generate_variants(qs, count=1, seed=1)[0]
    val = v["answer_key"]["1"]
    assert isinstance(val, list) and len(val) == 1 and val[0] in {"A", "B"}


def test_open_question_no_key_is_none():
    # An open-ended question with no accepted answers → answer_key None (ungraded).
    qs = [
        {"question_id": "q1", "question_number": 1, "question_text": "mc",
         "options": {"A": "ta", "B": "tb"}, "correct_answer": "A"},
        {"question_id": "q2", "question_number": 2, "question_text": "open",
         "options": {}, "correct_answer": None, "correct_answers": []},
    ]
    v = generate_variants(qs, count=1, seed=1)[0]
    # positions are 1..2 in some order; the open one has None, the MC one a list
    vals = list(v["answer_key"].values())
    assert None in vals
    assert any(isinstance(x, list) and len(x) == 1 for x in vals)
