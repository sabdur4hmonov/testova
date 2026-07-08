"""
Single-file extraction pipeline — THE one implementation both flows use
(single-file upload handler and the Multi-Source Builder). This code was
MOVED verbatim from handlers/upload.handle_file; handlers stay thin wrappers
that own only Telegram I/O and FSM state.

Owns: page rendering, column splitting, Gemini extraction, multi-section
refusal/collapse, image attachment, scheme ladder, marker restore,
unanswerable re-extraction, quality flags, question persistence and the
project status transitions.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from app.database import async_session_factory
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.services.ai_analyzer import (
    AIAnalyzer,
    collapse_sections,
    find_near_duplicates,
    find_siblings,
    find_unanswerable,
    flag_suspicious_questions,
    sections_confident,
    summarize_sections,
)
from app.services.file_processor import (
    attach_images_to_questions,
    docx_to_images,
    image_to_pages,
    pdf_to_images,
    restore_list_markers,
    save_debug_crops,
    split_two_column_pages,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

MAX_PAGES = 20


@dataclass
class PipelineResult:
    status: str                      # "ok" | "no_questions" | "refused_multi_section"
    questions: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    detected: dict = field(default_factory=dict)   # {number_str: letter|None}
    missing_nums: list = field(default_factory=list)
    key_max: int = 0
    refused_n: int = 0
    refused_ranges: str = ""


async def _set_project_status(
    project_id: str, status: ProjectStatus, error: str | None = None
) -> None:
    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
        p = res.scalar_one()
        p.status = status
        if error:
            p.error_message = error
        await session.commit()


async def persist_questions(project_id: str, questions: list[dict]) -> None:
    """Save extracted questions and mark the project completed."""
    async with async_session_factory() as session:
        for rq in questions:
            opts = rq.get("options", {})
            if opts.get("E"):
                # BUG FIX (#9): the questions table only has option_a..option_d
                # columns, so a 5th option cannot be persisted. Don't lose it
                # silently — full E support needs an option_e column/migration.
                logger.warning(
                    "option_e_dropped_at_persistence",
                    project_id=project_id,
                    question=rq.get("question_number"),
                )
            session.add(Question(
                project_id=uuid.UUID(project_id),
                question_number=rq.get("question_number", 0),
                question_text=rq.get("question_text", ""),
                option_a=opts.get("A"),
                option_b=opts.get("B"),
                option_c=opts.get("C"),
                option_d=opts.get("D"),
                correct_answer=rq.get("correct_answer"),
                has_image=rq.get("has_image", False),
                image_path=rq.get("image_path"),
                image_description=rq.get("image_description"),
                group_id=rq.get("group_id"),
                group_context=rq.get("group_context"),
                page_number=rq.get("page_number"),
            ))

        from sqlalchemy import select
        res = await session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
        p = res.scalar_one()
        p.status = ProjectStatus.COMPLETED
        p.question_count = len(questions)
        await session.commit()


async def process_file(
    content: bytes,
    file_type: str,
    project_id: str,
) -> PipelineResult:
    """
    Run the FULL extraction pipeline for one uploaded file and persist its
    questions. Telegram-free: callers own all messaging/FSM.
    """
    # ── Convert to page images ────────────────────────────────────────────────
    if file_type == "pdf":
        raw_pages = await asyncio.to_thread(pdf_to_images, content)
    elif file_type == "docx":
        raw_pages, _ = await asyncio.to_thread(docx_to_images, content)
    else:
        raw_pages = await asyncio.to_thread(image_to_pages, content)

    if not raw_pages:
        await _set_project_status(project_id, ProjectStatus.FAILED, "no pages")
        return PipelineResult(status="no_questions")

    src_pages = raw_pages[:MAX_PAGES]

    # ── Two-column pages → single-column halves in reading order ─────────────
    page_images, col_map = await asyncio.to_thread(split_two_column_pages, src_pages)
    images = [p.image for p in page_images]

    # ── Extract via Gemini Vision ─────────────────────────────────────────────
    analyzer = AIAnalyzer()
    all_questions = await analyzer.extract_all_questions(images=images)

    if not all_questions:
        await _set_project_status(project_id, ProjectStatus.FAILED)
        return PipelineResult(status="no_questions")

    # ── CHANGE 1: multi-test files are politely refused ───────────────────────
    sections = summarize_sections(all_questions)
    if len(sections) > 1:
        if sections_confident(sections):
            ranges = " va ".join(f"1–{m['max']}" for m in sections)
            await _set_project_status(
                project_id, ProjectStatus.FAILED, "multi-section file refused"
            )
            logger.info(
                "multi_section_refused",
                project_id=project_id, sections=len(sections),
            )
            return PipelineResult(
                status="refused_multi_section",
                refused_n=len(sections),
                refused_ranges=ranges,
            )
        all_questions = collapse_sections(all_questions)

    # ── Attach images — precise crop using PyMuPDF rects ─────────────────────
    _pdf_bytes_for_crop = content if file_type == "pdf" else None
    all_questions = await asyncio.to_thread(
        attach_images_to_questions,
        all_questions,
        page_images,
        _pdf_bytes_for_crop,
        col_map,
        src_pages,
    )

    # ── FIX 2 + FIX 3: scheme-dependent questions must carry scheme content ──
    scheme_failed = await analyzer.ensure_scheme_content(
        all_questions, images, _pdf_bytes_for_crop, col_map, src_pages
    )
    # ISSUE 1: restore a swallowed list marker ("1)") from the source words
    if _pdf_bytes_for_crop:
        await asyncio.to_thread(
            restore_list_markers, all_questions, _pdf_bytes_for_crop, col_map
        )

    # ISSUE 2: a question asking about an unknown that appears in no given
    # reaction lost a reaction — one automatic strict re-extraction attempt.
    unanswerable = find_unanswerable(all_questions)
    if unanswerable:
        nums = [u[1] for u in unanswerable]
        counts: dict[int, int] = {}
        for q in all_questions:
            counts[q.get("question_number", 0)] = counts.get(q.get("question_number", 0), 0) + 1
        try:
            fresh = await analyzer.reextract_questions(nums, all_questions, images)
        except Exception as e:
            logger.warning("unanswerable_reextract_failed", error=str(e))
            fresh = {}
        for q in all_questions:
            n = q.get("question_number")
            item = fresh.get(n)
            if item and n in nums and counts.get(n) == 1:
                q["question_text"] = item.get("question_text") or q["question_text"]
                if item.get("options"):
                    q["options"] = item["options"]
                logger.info("unanswerable_reextracted", question=n)
        unanswerable = find_unanswerable(all_questions)

    # FIX 5(c) + FIX 7: suspected near-duplicates and corrupted-content flags
    near_dups = find_near_duplicates(all_questions)
    suspicious = flag_suspicious_questions(all_questions)
    flagged_nums = {(s[0], s[1]) for s in suspicious}
    for q in all_questions:
        key = (q.get("section", 1), q.get("question_number", 0))
        if q.get("verbatim_doubt") and key not in flagged_nums:
            suspicious.append((key[0], key[1], "verbatim_doubt"))
            flagged_nums.add(key)

    # ISSUE 3: debug crops for flagged questions (paths in logs only)
    if suspicious and _pdf_bytes_for_crop:
        await asyncio.to_thread(
            save_debug_crops,
            all_questions,
            [s[1] for s in suspicious],
            _pdf_bytes_for_crop,
            col_map,
            src_pages,
        )

    # ISSUE 5: per-section OCR-correction totals for the summary
    ocr_by_sec: dict[int, int] = {}
    for q in all_questions:
        if q.get("ocr_fixes"):
            sec = q.get("section", 1)
            ocr_by_sec[sec] = ocr_by_sec.get(sec, 0) + q["ocr_fixes"]

    quality = {
        "siblings": [[s[0], list(s[1])] for s in find_siblings(all_questions)],
        "scheme_failed": [list(f) for f in scheme_failed],
        "near_dups": [[g[0], list(g[1])] for g in near_dups],
        "suspicious": [list(s) for s in suspicious],
        "unanswerable": [[u[0], u[1], ",".join(u[2])] for u in unanswerable],
        "ocr_fixes": [[sec, n] for sec, n in sorted(ocr_by_sec.items())],
    }

    # CHANGE 2: NOTHING is removed at extraction time.
    sections = summarize_sections(all_questions)

    await persist_questions(project_id, all_questions)

    detected: dict[str, str | None] = {
        str(q.get("question_number", i + 1)): q.get("correct_answer")
        for i, q in enumerate(all_questions)
    }
    missing_nums = sorted(
        [num for num, ans in detected.items() if not ans],
        key=lambda x: int(x),
    )

    return PipelineResult(
        status="ok",
        questions=all_questions,
        sections=sections,
        quality=quality,
        detected=detected,
        missing_nums=missing_nums,
        key_max=sections[0]["max"] if sections else 0,
    )
