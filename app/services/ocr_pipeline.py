"""
Full OCR/AI pipeline: file bytes → structured Question list.

Pipeline:
  PDF/DOCX/Image → Page Images → Preprocess → Gemini Vision → Merge → Questions
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from app.services import storage
from app.services.ai_analyzer import AIAnalyzer, merge_questions
from app.services.file_processor import (
    detect_file_type,
    docx_to_images,
    image_to_bytes,
    image_to_pages,
    pdf_to_images,
    preprocess_image,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

MAX_PAGES = 60  # safety cap — very long exams are split


async def run_pipeline(
    file_bytes: bytes,
    filename: str,
    project_id: str,
) -> list[dict[str, Any]]:
    """
    Main entry point.  Returns a list of raw question dicts ready to be
    inserted into the database.
    """
    file_type = detect_file_type(filename, file_bytes)
    logger.info("pipeline_start", project_id=project_id, type=file_type, bytes=len(file_bytes))

    analyzer = AIAnalyzer()
    page_results: list[list[dict]] = []
    docx_text: str | None = None

    # ── Stage 1: Convert to page images ──────────────────────────────────────
    if file_type == "pdf":
        pages = pdf_to_images(file_bytes)
    elif file_type == "docx":
        pages, docx_text = docx_to_images(file_bytes)
    elif file_type == "image":
        pages = image_to_pages(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {file_type}")

    pages = pages[:MAX_PAGES]
    logger.info("pages_ready", count=len(pages), project_id=project_id)

    # ── Stage 2: Preprocess + AI extract per page ─────────────────────────────
    for page in pages:
        enhanced = preprocess_image(page.image)
        questions = await analyzer.extract_questions_from_image(enhanced, page.page_number)
        page_results.append(questions)
        logger.info("page_done", page=page.page_number, found=len(questions))

    # ── Stage 3: Fallback text extraction for DOCX ────────────────────────────
    if docx_text and not any(page_results):
        logger.info("docx_text_fallback", chars=len(docx_text))
        text_questions = await analyzer.extract_questions_from_text(docx_text)
        page_results.append(text_questions)

    # ── Stage 4: Merge across pages ───────────────────────────────────────────
    all_questions = merge_questions(page_results)
    logger.info("merge_done", total_questions=len(all_questions), project_id=project_id)

    # ── Stage 5: Save embedded images ────────────────────────────────────────
    all_questions = await _save_question_images(all_questions, pages, project_id)

    return all_questions


async def _save_question_images(
    questions: list[dict],
    pages: list,
    project_id: str,
) -> list[dict]:
    """
    For questions flagged has_image=True, crop and save the relevant region.
    Since Gemini doesn't return bounding boxes, we save the full page image
    as the associated image for now. A future iteration can add bbox detection.
    """
    page_map: dict[int, Image.Image] = {p.page_number: p.image for p in pages}

    for q in questions:
        if not q.get("has_image"):
            continue
        page_num = q.get("page_number", 1)
        img = page_map.get(page_num)
        if img is None:
            continue
        img_bytes = image_to_bytes(img, fmt="JPEG")
        key = await storage.save_file(
            img_bytes,
            folder=f"projects/{project_id}/images",
            filename="question.jpg",
        )
        q["image_path"] = key

    return questions
