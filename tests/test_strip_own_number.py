"""
_strip_own_number: remove a stem's OWN leading number, tolerating a short run of
leading markdown/code artifacts Gemini occasionally leaks (a stray backtick, a
``` fence, ** ). It must still strip ONLY the question's own number — a list
marker "1)" (1 != question_number) stays, and real content is never eaten.

Reproduces the real DB row: qn=11, stem "`11.Hisoblang: (33-22)*(42-4)=".
"""
from app.services.ai_analyzer import _strip_own_number


def _strip(n, text):
    q = {"question_number": n, "question_text": text}
    _strip_own_number(q)
    return q["question_text"]


# ── the real failure: single leading backtick before the own number ─────────
def test_single_backtick_before_number():
    assert _strip(11, "`11.Hisoblang: (33-22)*(42-4)=") == "Hisoblang: (33-22)*(42-4)="


# ── a RUN of markdown (your adjustment): ``` fence and ** must both work ─────
def test_triple_backtick_fence():
    assert _strip(11, "```11.Hisoblang:") == "Hisoblang:"


def test_double_asterisk_bold():
    assert _strip(11, "**11. Compute x") == "Compute x"


def test_mixed_markdown_run():
    assert _strip(7, "~*`7) Solve") == "Solve"


# ── a clean number still strips (unchanged behaviour) ───────────────────────
def test_clean_number_still_strips():
    assert _strip(11, "11. Compute x") == "Compute x"
    assert _strip(3, "3) Compute x") == "Compute x"


# ── only the OWN number: a list marker (n != qn) is preserved ───────────────
def test_list_marker_preserved():
    # question 5, stem opens with "1) ... 2) ..." reactions — NOT the own number
    stem = "1) Al + H2O 2) KClO3 qaysi?"
    assert _strip(5, stem) == stem


def test_leading_markdown_but_wrong_number_preserved():
    # backtick + "12." but the question is 11 → not its own number → untouched
    assert _strip(11, "`12. Compute x") == "`12. Compute x"


# ── bounded: a long run of junk is NOT stripped (guards against over-eating) ─
def test_run_is_bounded():
    # 6 backticks exceeds the 0-4 bound → left as-is (never a greedy match)
    assert _strip(11, "``````11. x") == "``````11. x"


# ── no number set → no-op ────────────────────────────────────────────────────
def test_no_number_noop():
    assert _strip(0, "`11. x") == "`11. x"


# ── caret own-number: the DOCX superscript→caret bleed (real DB row qn=11) ────
# _para_scripted_text turns a superscripted source number into "^11", and Gemini
# transcribes the stem as "^11.Hisoblang:". The strip must see through the caret
# — but ONLY when the caret number is the question's own number.
def test_caret_own_number_stripped():
    # the real bug row: qn=11, "^11.Hisoblang: (3^3-2-2^2)*(4^2-4)="
    assert _strip(11, "^11.Hisoblang: (3^3-2-2^2)*(4^2-4)=") \
        == "Hisoblang: (3^3-2-2^2)*(4^2-4)="


def test_caret_own_number_with_paren_terminator():
    assert _strip(3, "^3) Compute x") == "Compute x"


def test_caret_wrong_number_preserved():
    # "^11." but the question is 13 → not its own number → untouched
    assert _strip(13, "^11.Hisoblang: 2+2") == "^11.Hisoblang: 2+2"


def test_leading_superscript_isotope_preserved():
    # real DB rows qn 28/31/32/49: a leading isotope mass number is REAL stem
    # content. Both gates protect it — the number is not the question's own AND
    # "^210" is followed by "_", never a . / ) terminator.
    iso = "^210_82 Pb + x^1_0p → ^205_81 Tl + y^4_2α yadro reaksiyasi"
    assert _strip(28, iso) == iso


def test_leading_superscript_that_matches_own_but_is_real_formula_preserved():
    # the adversarial case: the own number appears as a leading superscript but
    # is real content (x² + x = 5), so there is no . / ) terminator → untouched.
    assert _strip(2, "^2 + x = 5 tenglamani yeching") == "^2 + x = 5 tenglamani yeching"


def test_internal_carets_never_touched():
    # only the LEADING own-number token goes; exponents inside the stem stay.
    assert _strip(5, "5. 2^3 + 3^2 ni hisoblang") == "2^3 + 3^2 ni hisoblang"
