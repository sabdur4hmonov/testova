"""
Celery tasks for background file processing.

Heavy CPU/IO work (OCR, AI calls, PDF generation) happens here,
keeping the bot responsive.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from celery import Task
from sqlalchemy import select

from app.database import async_session_factory
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.variant import Variant
from app.services import storage
from app.services.ai_analyzer import AIAnalyzer
from app.services.file_processor import preprocess_image, image_to_bytes
from app.services.ocr_pipeline import run_pipeline
from app.services.pdf_generator import build_answer_key_pdf, build_variants_pdf
from app.services.variant_generator import generate_variants
from app.tasks.celery_app import celery_app
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _run_async(coro):
    """Run a coroutine synchronously inside Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    bind=True,
    name="app.tasks.file_tasks.process_uploaded_file",
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def process_uploaded_file(
    self: Task,
    project_id: str,
    file_key: str,
    filename: str,
    telegram_chat_id: int,
) -> dict[str, Any]:
    """
    OCR + AI extraction pipeline for an uploaded document.
    Saves Question rows and updates Project status.
    """
    logger.info("task_start", task="process_file", project_id=project_id)

    async def _work() -> dict[str, Any]:
        async with async_session_factory() as session:
            # Mark as processing
            result = await session.execute(
                select(Project).where(Project.id == uuid.UUID(project_id))
            )
            project = result.scalar_one_or_none()
            if not project:
                return {"error": "project_not_found"}

            project.status = ProjectStatus.PROCESSING
            project.task_id = self.request.id
            await session.commit()

            try:
                # Read file
                file_bytes = await storage.read_file(file_key)

                # Run pipeline
                raw_questions = await run_pipeline(file_bytes, filename, project_id)

                if not raw_questions:
                    project.status = ProjectStatus.FAILED
                    project.error_message = "No questions extracted from the document."
                    await session.commit()
                    return {"error": "no_questions"}

                # Persist questions
                for rq in raw_questions:
                    opts = rq.get("options", {})
                    q = Question(
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
                        page_number=rq.get("page_number"),
                    )
                    session.add(q)

                project.status = ProjectStatus.COMPLETED
                project.question_count = len(raw_questions)
                await session.commit()

                logger.info(
                    "task_done",
                    task="process_file",
                    project_id=project_id,
                    questions=len(raw_questions),
                )
                return {"project_id": project_id, "question_count": len(raw_questions)}

            except Exception as exc:
                logger.error("task_error", task="process_file", error=str(exc), exc_info=True)
                project.status = ProjectStatus.FAILED
                project.error_message = str(exc)[:1000]
                await session.commit()
                raise

    try:
        return _run_async(_work())
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="app.tasks.file_tasks.generate_variant_pdfs",
    max_retries=2,
    default_retry_delay=30,
)
def generate_variant_pdfs(
    self: Task,
    project_id: str,
    variant_count: int,
    exam_title: str,
    telegram_chat_id: int,
) -> dict[str, Any]:
    """Generate variant PDFs and answer key PDF, store them, return keys."""

    async def _work() -> dict[str, Any]:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Project).where(Project.id == uuid.UUID(project_id))
            )
            project = result.scalar_one_or_none()
            if not project:
                return {"error": "project_not_found"}

            q_result = await session.execute(
                select(Question)
                .where(Question.project_id == uuid.UUID(project_id))
                .order_by(Question.question_number)
            )
            questions = q_result.scalars().all()

            if not questions:
                return {"error": "no_questions"}

            # Build raw question dicts
            raw_qs = [
                {
                    "question_id": str(q.id),
                    "question_number": q.question_number,
                    "question_text": q.question_text,
                    "options": {
                        "A": q.option_a,
                        "B": q.option_b,
                        "C": q.option_c,
                        "D": q.option_d,
                    },
                    "correct_answer": q.correct_answer,
                    "has_image": q.has_image,
                    "image_path": q.image_path,
                }
                for q in questions
            ]

            # Generate variants
            variants = generate_variants(raw_qs, count=variant_count)

            # Persist Variant records
            variant_records = []
            for v in variants:
                vrec = Variant(
                    project_id=uuid.UUID(project_id),
                    variant_number=v["variant_number"],
                    question_order=v["question_order"],
                    option_mapping=v["option_mapping"],
                    answer_key=v["answer_key"],
                )
                session.add(vrec)
                variant_records.append(vrec)
            await session.flush()

            # Build PDFs
            variants_pdf = build_variants_pdf(variants, exam_title=exam_title)
            key_pdf = build_answer_key_pdf(variants, exam_title=exam_title)

            # Save PDFs
            variants_key = await storage.save_file(
                variants_pdf,
                folder=f"projects/{project_id}/exports",
                filename="variants.pdf",
            )
            key_key = await storage.save_file(
                key_pdf,
                folder=f"projects/{project_id}/exports",
                filename="answer_keys.pdf",
            )

            await session.commit()

            return {
                "project_id": project_id,
                "variant_count": variant_count,
                "variants_pdf_key": variants_key,
                "answer_key_pdf_key": key_key,
                "variant_ids": [str(v.id) for v in variant_records],
            }

    try:
        return _run_async(_work())
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(name="app.tasks.file_tasks.reset_daily_usage")
def reset_daily_usage() -> None:
    """Idempotent: reset daily_projects_used for users whose last_reset is yesterday."""
    from datetime import date

    async def _work() -> None:
        from sqlalchemy import update
        from app.models.user import User
        from datetime import datetime, timezone

        today = date.today()
        async with async_session_factory() as session:
            await session.execute(
                update(User)
                .where(User.last_reset_date < datetime(today.year, today.month, today.day, tzinfo=timezone.utc))
                .values(daily_projects_used=0, last_reset_date=datetime.now(timezone.utc))
            )
            await session.commit()

    _run_async(_work())
