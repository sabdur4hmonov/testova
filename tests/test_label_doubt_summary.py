"""
Piece 2: the option-label backstop's `label_doubt` flag is surfaced in the
post-extraction summary the teacher sees — and kept SEPARATE from `suspicious`
(so it never drives the Gemini re-read).
"""
from app.bot.handlers.upload import _summary_message


def _meta(sec: int = 1) -> dict:
    return {"section": sec, "count": 20, "max": 20, "gaps": [], "open": []}


def test_label_doubt_listed_in_summary():
    q = {"label_doubt": [[1, 10], [1, 11], [1, 14]], "suspicious": []}
    msg = _summary_message(_meta(), q, "uz")
    assert "10, 11, 14" in msg
    assert "harflar" in msg.lower()          # label_doubt_info wording


def test_label_doubt_is_not_the_suspicious_line():
    # label_doubt present, suspicious empty → the "suspicious" re-read line must
    # NOT appear (they are independent; label_doubt never triggers a re-read)
    q = {"label_doubt": [[1, 7]], "suspicious": []}
    msg = _summary_message(_meta(), q, "en")
    assert "7" in msg
    assert "option letters" in msg.lower()
    assert "Suspicious questions" not in msg


def test_no_label_doubt_no_line():
    q = {"label_doubt": [], "suspicious": []}
    msg = _summary_message(_meta(), q, "en")
    assert "option letters" not in msg.lower()


def test_label_doubt_filtered_by_section():
    q = {"label_doubt": [[1, 7], [2, 9]], "suspicious": []}
    msg = _summary_message(_meta(sec=1), q, "en")
    assert "7" in msg
    assert "9" not in msg
