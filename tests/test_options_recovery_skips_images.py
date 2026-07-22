"""
Options-recovery must NOT target figure-based write-in questions. A question with
has_image / image_description is legitimately option-less; sending it to the
options-recovery Gemini call wastes a call and risks hallucinating MC options off
the diagram (fabricating a bogus answer key). Only genuinely option-lost
questions get a recovery call.
"""
import asyncio

from app.services.ai_analyzer import AIAnalyzer


def _analyzer():
    # skip __init__ (no Gemini client needed — _call_multi is replaced)
    return object.__new__(AIAnalyzer)


def _run(questions):
    a = _analyzer()
    calls = []

    async def fake_call_multi(prompt, imgs):
        calls.append(prompt)
        return ("[]", 0)     # empty recovery result

    a._call_multi = fake_call_multi
    asyncio.run(a._recover_missing_options(questions, [object()]))   # 1 page
    return calls


def test_has_image_question_not_sent():
    q = {"question_number": 1, "is_open_ended": True, "options": {},
         "page_number": 1, "has_image": True}
    assert _run([q]) == []          # skipped → no recovery call


def test_image_description_question_not_sent():
    q = {"question_number": 1, "is_open_ended": True, "options": {},
         "page_number": 1, "image_description": "A number line with A, B, C"}
    assert _run([q]) == []


def test_plain_option_less_question_is_sent():
    q = {"question_number": 2, "is_open_ended": True, "options": {},
         "page_number": 1}
    assert len(_run([q])) == 1      # genuinely option-lost → recovery attempted


def test_mixed_only_plain_one_sent():
    qs = [
        {"question_number": 1, "is_open_ended": True, "options": {},
         "page_number": 1, "has_image": True},          # skipped
        {"question_number": 2, "is_open_ended": True, "options": {},
         "page_number": 1},                              # sent
    ]
    calls = _run(qs)
    assert len(calls) == 1
    assert "2" in calls[0] and "1" not in calls[0].split("recover")[-1][:40]
