"""The edition question is a genuine choice, so its wording has to carry it.

It is asked before any photo arrives, and legacy editions print no code, so the
bot cannot work out the edition itself. The Uzbek text says in plain words why
there is a choice and exactly what to look for on the paper.
"""
from app.bot.handlers import checking


def test_the_uzbek_edition_question_is_the_approved_wording():
    assert checking._PICK_EDITION["uz"] == (
        "🗂 Bu test bir necha marta yaratilgan, har birining o'z javob kaliti bor. "
        "Qog'ozdagi «Variant 1 · K7Q2» yozuvidan kodni (masalan, K7Q2) toping va "
        "o'sha kodli tugmani bosing:"
    )


def test_every_language_still_has_the_question():
    assert set(checking._PICK_EDITION) == {"uz", "en", "ru"}
