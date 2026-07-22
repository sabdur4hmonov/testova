"""
_strip_leading_backticks: drop a leading run of stray backticks (markdown /
code-fence bleed) opening a stem with no number after — the residual case that
_strip_own_number (which needs a number) does not cover. Backticks only; bounded;
inline mid-stem code is untouched.
"""
from app.services.ai_analyzer import _strip_leading_backticks


def _strip(text, n=1):
    q = {"question_number": n, "question_text": text}
    _strip_leading_backticks(q)
    return q["question_text"]


def test_single_leading_backtick_no_number():
    assert _strip("`Hisoblang: (3-2)*(4-4)=") == "Hisoblang: (3-2)*(4-4)="


def test_backtick_run():
    assert _strip("```Compute the value") == "Compute the value"


def test_no_backtick_unchanged():
    assert _strip("Compute the value") == "Compute the value"


def test_inline_code_mid_stem_preserved():
    # a backtick that is not leading must stay
    stem = "Compute `x` for the value"
    assert _strip(stem) == stem


def test_long_run_fully_stripped():
    # a leading backtick is never real content → the whole run goes
    assert _strip("`````Compute") == "Compute"


def test_composes_with_own_number_strip():
    # end-to-end order: own-number strip first (handles `11.), then this is a
    # no-op — proving no double-processing on the number case.
    from app.services.ai_analyzer import _strip_own_number
    q = {"question_number": 11, "question_text": "`11.Hisoblang:"}
    _strip_own_number(q)
    _strip_leading_backticks(q)
    assert q["question_text"] == "Hisoblang:"
