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
