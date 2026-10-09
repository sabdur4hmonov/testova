"""CP24 — theme figure capture + image-based question generation wiring.

Capture reuses the upload flow's figure detection + crop primitives; these
tests pin the CP24-specific parts: the free image-free skip, the ≤384px
generation downscale, the per-theme cap, the img-index parse + resolve, the
CP14 gate bypass for image questions, and the opt-out being byte-identical.
"""
from __future__ import annotations

import json
import os

import fitz
from PIL import Image

from app.services import book_generator as bg
from app.services import book_images as bi


def _pdf(pages_with_fig: dict[int, int], n_pages: int) -> bytes:
    """A PDF of n_pages; pages_with_fig maps 1-based page -> how many separate
    figure rectangles to draw on it (well-separated so they cluster apart)."""
    doc = fitz.open()
    for i in range(1, n_pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 60), f"Page {i} has some real text content here.")
        for k in range(pages_with_fig.get(i, 0)):
            x = 80 + (k % 2) * 260
            y = 120 + (k // 2) * 180
            page.draw_rect(fitz.Rect(x, y, x + 200, y + 140), color=(0, 0, 0), width=2)
    return doc.tobytes()


# ── Capture ──────────────────────────────────────────────────────────────────

def test_image_free_theme_captures_nothing():
    pdf = _pdf({}, n_pages=2)  # text only, no figures
    assert bi.theme_has_figures(pdf, 1, 2) is False
    assert bi.capture_theme_images(pdf, 1, 2) == []


def test_theme_with_a_figure_is_detected_and_cropped():
    pdf = _pdf({2: 1}, n_pages=3)
    assert bi.theme_has_figures(pdf, 2, 2) is True
    caps = bi.capture_theme_images(pdf, 2, 2)
    assert len(caps) == 1
    c = caps[0]
    assert os.path.exists(c["image_path"]), "high-DPI crop not written"
    assert c["gen_image_path"] and os.path.exists(c["gen_image_path"])
    assert c["page_number"] == 2


def test_generation_copy_is_downscaled_to_the_384_threshold():
    pdf = _pdf({1: 1}, n_pages=1)
    caps = bi.capture_theme_images(pdf, 1, 1)
    assert caps, "no figure captured"
    hi = Image.open(caps[0]["image_path"])
    gen = Image.open(caps[0]["gen_image_path"])
    assert max(gen.size) <= bi.GEN_MAX_SIDE, f"gen image {gen.size} exceeds 384px"
    # the high-DPI crop is untouched (kept for the PDF at print quality)
    assert max(hi.size) > bi.GEN_MAX_SIDE, "high-DPI crop was wrongly shrunk"


def test_capture_is_capped_per_theme():
    pdf = _pdf({1: 6}, n_pages=1)  # 6 separated figures on one page
    caps = bi.capture_theme_images(pdf, 1, 1, cap=4)
    assert len(caps) == 4, f"cap not enforced: {len(caps)}"


# ── Generation wiring ────────────────────────────────────────────────────────

def test_img_index_parsed_only_within_range():
    raw = json.dumps([
        {"q": "Rasmda nima?", "opts": {"A": "x", "B": "y"}, "ans": "A", "img": 1},
        {"q": "text q", "opts": {"A": "p", "B": "q"}, "ans": "B"},
        {"q": "bad ref", "opts": {"A": "p", "B": "q"}, "ans": "A", "img": 9},
    ])
    out = bg.parse_generation_response(raw, n_images=2)
    assert out[0]["img_index"] == 1
    assert out[1]["img_index"] is None
    assert out[2]["img_index"] is None   # 9 > n_images → ignored


def test_image_question_bypasses_the_cp14_text_grounding_gate():
    # An image question whose answer is NOT in the theme text must still pass —
    # it is grounded in the figure, which the text-only check cannot see.
    img_q = {
        "question_text": "Rasmda ko'rsatilgan a'zo qaysi?",
        "options": {"A": "yurak", "B": "o'pka"}, "correct_answer": "A",
        "correct_answers": ["A"], "is_open_ended": False, "img_index": 1,
    }
    assert bg._question_is_valid(img_q, theme_text="butunlay boshqa mavzu matni") is True
    # the same question WITHOUT an image ref would be dropped (answer ungrounded)
    text_q = dict(img_q, img_index=None)
    assert bg._question_is_valid(text_q, theme_text="butunlay boshqa mavzu matni") is False


def test_opt_out_prompt_is_byte_identical_to_pre_cp24():
    base = bg.build_prompt(
        theme_title="T", theme_text="X", n_questions=3, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="uz",
    )
    with_zero = bg.build_prompt(
        theme_title="T", theme_text="X", n_questions=3, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="uz",
        n_images=0,
    )
    assert base == with_zero
    assert "IMAGES." not in base


def test_image_prompt_names_the_figures():
    p = bg.build_prompt(
        theme_title="T", theme_text="X", n_questions=3, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="uz",
        n_images=2,
    )
    assert "IMAGES." in p and '"img"' in p
