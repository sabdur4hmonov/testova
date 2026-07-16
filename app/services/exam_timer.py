"""
In-process exam timer built on APScheduler (AsyncIOScheduler).

Runs INSIDE the bot process — no Celery, no broker, no worker, no Redis. Jobs
live in memory only, which is exactly why a running exam's jobs are re-loaded
from the database on startup (`reload_pending`): a restart mid-exam must never
lose a timer.

Design split:
  * `plan_jobs` is a PURE function — given an end time and "now" it decides which
    jobs to schedule (a 10-minutes-before warning + an at-end notice), skipping
    the warning when the exam is <10 minutes out and returning nothing when the
    end time is already in the past. Fully unit-testable without a scheduler.
  * everything else (start/stop, add jobs, reload) is thin glue around it.

Robustness: the scheduler is optional. If APScheduler cannot start, the bot must
still run — callers wrap `init_scheduler` in try/except and every other function
here degrades to a safe no-op when the scheduler is absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select

from app.models.project import Project
from app.models.user import User
from app.services.notify import send_text
from app.utils.logging import get_logger

logger = get_logger(__name__)

WARNING_MINUTES = 10  # how long before the end the "wrap up" nudge fires

# uz/en/ru copy for the two proactive messages.
MESSAGES = {
    "warn": {
        "uz": "⚠️ Imtihon tugashiga 10 daqiqa qoldi. Javob varaqalarini yig'ishni boshlang.",
        "en": "⚠️ 10 minutes left until the exam ends. Start collecting the answer sheets.",
        "ru": "⚠️ До конца экзамена осталось 10 минут. Начинайте собирать листы ответов.",
    },
    "end": {
        "uz": "🔔 Imtihon vaqti tugadi!",
        "en": "🔔 The exam time is over!",
        "ru": "🔔 Время экзамена вышло!",
    },
}


@dataclass(frozen=True)
class PlannedJob:
    kind: str        # "warn" | "end"
    run_at: datetime  # UTC-aware


def plan_jobs(end_time: datetime, now: datetime) -> list[PlannedJob]:
    """
    PURE. Decide which timer jobs to schedule.

    * end_time already in the past (<= now) → [] (caller should reject).
    * exam < WARNING_MINUTES away          → [end] only (warning skipped).
    * otherwise                            → [warn, end].
    """
    if end_time <= now:
        return []
    jobs: list[PlannedJob] = []
    warn_at = end_time - timedelta(minutes=WARNING_MINUTES)
    if warn_at > now:
        jobs.append(PlannedJob("warn", warn_at))
    jobs.append(PlannedJob("end", end_time))
    return jobs


def is_end_in_future(end_time: datetime, now: datetime) -> bool:
    """Thin predicate the handler uses to reject a past end time up front."""
    return end_time > now


def _msg(kind: str, lang: str) -> str:
    table = MESSAGES[kind]
    return table.get(lang, table["uz"])


async def _fire(bot: Bot, chat_id: int, text: str) -> None:
    """Job body — a coroutine AsyncIOScheduler runs on the bot's event loop."""
    await send_text(bot, chat_id, text)


# ── Scheduler lifecycle ───────────────────────────────────────────────────────
# Module-level singleton. None until init succeeds; back to None after shutdown.
_scheduler = None  # type: ignore[var-annotated]


def init_scheduler():
    """
    Create and start the AsyncIOScheduler. Idempotent. Returns the scheduler, or
    None if APScheduler is unavailable/fails — callers treat None as "no timers".

    Must be called from within a running asyncio loop (AsyncIOScheduler binds to
    the current loop). NOTE: main.py wraps this in try/except so any failure here
    leaves the bot fully functional, just without timers.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    # APScheduler 3.x wants a pytz timezone for its scheduler tz; pytz ships as
    # an APScheduler dependency. Fall back to stdlib UTC if it's somehow absent.
    try:
        import pytz
        tz = pytz.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.start()
    _scheduler = scheduler
    logger.info("exam_scheduler_started")
    return _scheduler


def shutdown_scheduler() -> None:
    """
    Stop the scheduler cleanly. Safe no-op if it was never started (e.g. startup
    failed early). Never raises — a shutdown hook must not hang or throw on exit.
    """
    global _scheduler
    scheduler = _scheduler
    _scheduler = None
    if scheduler is None:
        return
    try:
        if getattr(scheduler, "running", False):
            scheduler.shutdown(wait=False)
            logger.info("exam_scheduler_stopped")
    except Exception as exc:  # noqa: BLE001
        logger.warning("exam_scheduler_shutdown_error", error=str(exc))


def _job_id(project_id: str, kind: str) -> str:
    return f"exam:{project_id}:{kind}"


def schedule_exam(
    bot: Bot,
    project_id: str,
    chat_id: int,
    end_time: datetime,
    lang: str = "uz",
    now: datetime | None = None,
) -> int:
    """
    Schedule the warn/end jobs for one exam. Returns how many jobs were added.

    Safe no-op (returns 0) when the scheduler is not running. Job ids are keyed
    by project + kind with replace_existing=True, so re-scheduling or a startup
    reload never duplicates jobs.
    """
    if _scheduler is None:
        logger.warning("schedule_exam_no_scheduler", project_id=project_id)
        return 0

    if now is None:
        now = datetime.now(timezone.utc)

    planned = plan_jobs(end_time, now)
    added = 0
    for job in planned:
        try:
            _scheduler.add_job(
                _fire,
                trigger="date",
                run_date=job.run_at,
                args=[bot, chat_id, _msg(job.kind, lang)],
                id=_job_id(project_id, job.kind),
                replace_existing=True,
                misfire_grace_time=3600,  # tolerate a brief delay before firing
            )
            added += 1
        except Exception as exc:  # noqa: BLE001 — one bad job must not abort the rest
            logger.warning(
                "schedule_exam_job_failed",
                project_id=project_id, kind=job.kind, error=str(exc),
            )
    if added:
        logger.info(
            "exam_scheduled", project_id=project_id, jobs=added,
            end_time=end_time.isoformat(),
        )
    return added


async def reload_pending(bot: Bot, session_factory, now: datetime | None = None) -> int:
    """
    On startup: re-schedule timers for every project whose exam has not yet
    ended. REQUIRED for restart safety. Returns the number of jobs (re)scheduled.

    Safe no-op when the scheduler is not running. Never raises — a reload failure
    must not stop the bot from starting.
    """
    if _scheduler is None:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)

    total = 0
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(
                    Project.id, Project.exam_end_time,
                    User.telegram_id, User.language,
                )
                .join(User, User.id == Project.user_id)
                .where(
                    Project.exam_end_time.is_not(None),
                    Project.exam_end_time > now,
                )
            )
            for pid, end_time, telegram_id, language in result.all():
                lang = getattr(language, "value", None) or "uz"
                total += schedule_exam(
                    bot, str(pid), telegram_id, end_time, lang, now
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("exam_reload_failed", error=str(exc))
        return total

    logger.info("exam_reload_done", jobs=total)
    return total
