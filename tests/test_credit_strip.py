# -*- coding: utf-8 -*-
"""
FIX 3: a teacher who uploads someone else's test must not republish that
author's name or channel. The source carries them as an author-credit line
("Tuzuvchi: NAME  Telegram kanalimiz:@handle") and a diagonal watermark of the
same name. strip_source_credits removes them deterministically; math content
(subscripts, emails) and normal prose are left untouched.

Proven on the REAL credit line from the uploaded test (project 64ad06c6):
  "Tuzuvchi: Biloliddinov  Muxammadzoir   Telegram  kanalimiz:@_maxsus_maktablarga_tayyorlov"
"""
from app.services.ai_analyzer import (
    harvest_credit_name_tokens,
    strip_credits_from_questions,
    strip_source_credits,
)

REAL = ("Tuzuvchi: Biloliddinov  Muxammadzoir               "
        "Telegram  kanalimiz:@_maxsus_maktablarga_tayyorlov")


def test_harvest_real_name():
    q = [{"question_text": REAL}]
    assert harvest_credit_name_tokens(q) == {"Biloliddinov", "Muxammadzoir"}


def test_real_credit_line_stripped_whole():
    assert strip_source_credits(REAL, {"Biloliddinov", "Muxammadzoir"}) == ""


def test_credit_after_question_keeps_question():
    text = ("Uchburchak tomonini toping? Tuzuvchi: Biloliddinov Muxammadzoir "
            "Telegram kanalimiz:@_maxsus_maktablarga_tayyorlov")
    assert strip_source_credits(text, {"Biloliddinov", "Muxammadzoir"}) \
        == "Uchburchak tomonini toping?"


def test_watermark_underscore_reversed_stripped():
    # the diagonal watermark: underscore-joined, order reversed vs the credit.
    text = "Muxammadzoir_Biloliddinov Bu masalada 5 ta son bor."
    assert strip_source_credits(text, {"Biloliddinov", "Muxammadzoir"}) \
        == "Bu masalada 5 ta son bor."


def test_channel_only_line_stripped():
    assert strip_source_credits("Telegram kanalimiz: @_maxsus_maktablarga_tayyorlov") == ""


def test_bare_handle_stripped_inline():
    assert strip_source_credits("Obuna bo'ling @_maxsus_maktablarga_tayyorlov kanaliga") \
        == "Obuna bo'ling  kanaliga".replace("  ", " ")


def test_normal_question_untouched():
    # "muallifi" has no colon → not a credit; "a@b" is too short for a handle.
    q = "Asarning muallifi kim? a@b elektron pochta orqali."
    assert strip_source_credits(q, {"Biloliddinov"}) == q


def test_math_subscripts_survive():
    q = "x_1 + x_2 = 5 va x_1*x_2 = 6"
    assert strip_source_credits(q, {"Biloliddinov", "Muxammadzoir"}) == q


def test_in_place_pass_nulls_emptied_description():
    questions = [
        {"question_number": 1,
         "question_text": "Sonni toping? Tuzuvchi: Biloliddinov Muxammadzoir",
         "image_description": "Muxammadzoir_Biloliddinov"},
        {"question_number": 2,
         "question_text": "Muxammadzoir_Biloliddinov ni hisoblang: 2+2",
         "image_description": None},
    ]
    strip_credits_from_questions(questions)
    assert questions[0]["question_text"] == "Sonni toping?"
    # description was ONLY the watermark → nulled, no empty [Rasm] box
    assert questions[0]["image_description"] is None
    assert questions[1]["question_text"] == "ni hisoblang: 2+2"


def test_no_credit_is_noop():
    questions = [{"question_number": 1, "question_text": "2 + 2 = ?",
                  "image_description": "A red dice."}]
    strip_credits_from_questions(questions)
    assert questions[0]["question_text"] == "2 + 2 = ?"
    assert questions[0]["image_description"] == "A red dice."
