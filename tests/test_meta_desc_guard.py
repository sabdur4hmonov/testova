"""
img_desc shape guard: a scheme-transcription/recovery call can return PROSE
answering the prompt's implicit question ("does this contain a scheme?") instead
of a figure description — e.g. "Question 7 does not contain a scheme/diagram of
transformations". That meta-sentence must never be stored as image_description
(it renders as a "[Rasm]: ..." box on the exam). Real descriptions are kept.
"""
from app.services.ai_analyzer import _is_meta_desc, clean_question


# ── the observed leak strings are caught ─────────────────────────────────────
def test_catches_observed_leak():
    assert _is_meta_desc("Question 7 does not contain a scheme/diagram of transformations.")
    assert _is_meta_desc("This question does not contain a diagram.")
    assert _is_meta_desc("There is no scheme in this question.")
    assert _is_meta_desc("No diagram is present.")
    assert _is_meta_desc("Question 12 has no transformation chain.")


# ── real figure descriptions are kept ────────────────────────────────────────
def test_keeps_real_descriptions():
    for good in (
        "A number line with points A, B, C marked below it.",
        "A right triangle with sides 3, 4, 5.",
        "A stylized green tree on a light blue background.",
        "SiS2 -> (H2O) X1 -> (NaOH) X2",
        "A diagram showing the reaction of CO2 with H2O.",
        "A figure with no labels on the axis.",   # 'no' not about scheme/diagram
    ):
        assert not _is_meta_desc(good), good


def test_none_and_empty():
    assert not _is_meta_desc(None)
    assert not _is_meta_desc("")


# ── the normalizer drops a meta description end-to-end ───────────────────────
def test_clean_question_drops_meta_desc():
    q = {"question_text": "Compute x", "options": {},
         "image_description": "Question 7 does not contain a scheme/diagram."}
    clean_question(q)
    assert q["image_description"] is None


def test_clean_question_keeps_real_desc():
    q = {"question_text": "Look at the figure", "options": {},
         "image_description": "A number line with A, B, C."}
    clean_question(q)
    assert q["image_description"] == "A number line with A, B, C."
