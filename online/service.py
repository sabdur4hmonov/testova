"""Online test rules — every decision is taken here, on the server.

Used by the browser service (online/app.py) and by the bot (creating a test,
closing it, reading results, allowing a retake). Pure helpers first, then the
database operations; each operation takes an AsyncSession and the current
time, so tests drive the clock.

Rules (docs/YANGI_FUNKSIYALAR.md, B3/B4):
* each student gets N random questions from the test's pool, questions and
  options shuffled per student;
* the browser never receives a correct answer, an original option letter or a
  question id — options are addressed by their index in the shuffled list;
* time is counted on the server (deadline_at, shown_at);
* leaving the page (hidden/blur) starts an "away" period; returning closes
  it; an away period of 2 s or more is a violation. A period never closed
  (tab closed) is judged at the next contact;
* violations 1 and 2: a warning, and up to 5 UNANSWERED questions are
  swapped for reserve questions; violation 3: the attempt is closed with
  score 0 and marked "caught".
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.online_test import (
    AQ_ACTIVE, AQ_REPLACED, ATTEMPT_ACTIVE, ATTEMPT_ANNULLED, ATTEMPT_CAUGHT,
    ATTEMPT_EXPIRED, ATTEMPT_FINISHED, TEST_CLOSED, TEST_OPEN,
    OnlineAttempt, OnlineAttemptQuestion, OnlineEvent, OnlineTest,
)
from app.models.question import Question
from app.services.option_letters import canonical_letter

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # no 0/O, 1/I/L
CODE_LENGTH = 10
POOL_FACTOR = 1.5
AWAY_GRACE = timedelta(seconds=2)
MAX_VIOLATIONS = 3
REPLACE_PER_VIOLATION = 5
# A per-question timer gets this much slack for network latency before the
# server refuses an answer as late.
LATE_SLACK = timedelta(seconds=3)

FINAL_STATUSES = {ATTEMPT_FINISHED, ATTEMPT_EXPIRED, ATTEMPT_CAUGHT, ATTEMPT_ANNULLED}


class OnlineError(Exception):
    """A refusal the browser shows as a message; `code` is a stable key."""

    def __init__(self, code: str, **info) -> None:
        super().__init__(code)
        self.code = code
        self.info = info


# ── pure helpers ────────────────────────────────────────────────────────────

def new_code(rng: random.Random | None = None) -> str:
    pick = (rng.choice if rng else secrets.choice)
    return "".join(pick(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_SPACES = re.compile(r"\s+")
_APOS = re.compile(r"['ʻʼ‘’`]")


def clean_name(s: str, limit: int) -> str:
    return _SPACES.sub(" ", (s or "").strip())[:limit]


def name_key(first: str, last: str, class_name: str) -> str:
    def norm(s: str) -> str:
        return _APOS.sub("'", _SPACES.sub(" ", (s or "").strip().lower()))
    return f"{norm(last)}|{norm(first)}|{norm(class_name)}"


def required_pool(n: int) -> int:
    """Pool size that allows replacements: 1.5 x N, rounded up (30 -> 45)."""
    return math.ceil(n * POOL_FACTOR)


def is_mc(q: Question) -> bool:
    """Gradable online: has options and at least one correct answer."""
    return bool(q.options_ordered) and bool(q.correct_answers_ordered) and not q.is_deleted


def is_correct(selected: list[str] | None, correct: list[str]) -> bool:
    """All-or-nothing: the chosen set must equal the correct set."""
    if not selected:
        return False
    return {canonical_letter(s) for s in selected} == {canonical_letter(c) for c in correct}




# ── creating a test (bot side) ──────────────────────────────────────────────

@dataclass
class PoolInfo:
    pool_ids: list[str]
    written_excluded: int
    replacement_enabled: bool
    question_count: int


async def pool_for_project(session: AsyncSession, project_id: uuid.UUID, n: int) -> PoolInfo:
    rows = (await session.execute(
        select(Question).where(Question.project_id == project_id, Question.is_deleted.is_(False))
        .order_by(Question.question_number)
    )).scalars().all()
    mc = [q for q in rows if is_mc(q)]
    n_eff = min(n, len(mc))
    return PoolInfo(
        pool_ids=[str(q.id) for q in mc],
        written_excluded=len(rows) - len(mc),
        replacement_enabled=len(mc) >= required_pool(n_eff) and n_eff > 0,
        question_count=n_eff,
    )


async def create_test(
    session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID, title: str,
    question_count: int, total_seconds: int | None, per_question_seconds: int | None,
    allow_back: bool, lang: str = "uz",
) -> tuple[OnlineTest, PoolInfo]:
    info = await pool_for_project(session, project_id, question_count)
    if info.question_count == 0:
        raise OnlineError("no_mc_questions")
    for _ in range(20):
        code = new_code()
        taken = (await session.execute(select(OnlineTest.id).where(OnlineTest.code == code))).first()
        if taken is None:
            break
    else:  # pragma: no cover — 31^10 codes
        raise RuntimeError("no free online test code")
    test = OnlineTest(
        code=code, project_id=project_id, user_id=user_id, title=title[:512],
        question_count=info.question_count, total_seconds=total_seconds,
        per_question_seconds=per_question_seconds, allow_back=allow_back,
        pool_question_ids=info.pool_ids, replacement_enabled=info.replacement_enabled,
        lang=lang if lang in ("uz", "ru", "en") else "uz", status=TEST_OPEN,
    )
    session.add(test)
    await session.flush()
    return test, info


# ── loading ─────────────────────────────────────────────────────────────────

async def test_by_code(session: AsyncSession, code: str) -> OnlineTest | None:
    code = (code or "").strip().upper()
    if len(code) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in code):
        return None
    return (await session.execute(select(OnlineTest).where(OnlineTest.code == code))).scalar_one_or_none()


async def attempt_by_token(session: AsyncSession, test: OnlineTest, token: str | None,
                           *, lock: bool = False) -> OnlineAttempt | None:
    """The attempt this browser's token belongs to. `lock=True` takes the
    row lock (SELECT ... FOR UPDATE) for the rest of the transaction and
    re-reads the row, so two requests of one student — a "visible" fired by
    both blur and visibilitychange, an answer racing a finish — are applied
    one after the other: a violation is never counted twice, two swaps never
    pick the same position, and nothing is answered after grading."""
    if not token:
        return None
    stmt = select(OnlineAttempt).where(
        OnlineAttempt.test_id == test.id, OnlineAttempt.token_hash == hash_token(token),
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def active_questions(session: AsyncSession, attempt: OnlineAttempt) -> list[OnlineAttemptQuestion]:
    return list((await session.execute(
        select(OnlineAttemptQuestion).where(
            OnlineAttemptQuestion.attempt_id == attempt.id,
            OnlineAttemptQuestion.status == AQ_ACTIVE,
        ).order_by(OnlineAttemptQuestion.position)
    )).scalars().all())


async def _questions_by_id(session: AsyncSession, ids) -> dict[str, Question]:
    ids = [uuid.UUID(str(i)) for i in ids]
    if not ids:
        return {}
    rows = (await session.execute(select(Question).where(Question.id.in_(ids)))).scalars().all()
    return {str(q.id): q for q in rows}


def _event(attempt: OnlineAttempt, kind: str, now: datetime, *, duration: timedelta | None = None,
           counted: bool = False, detail: str | None = None) -> OnlineEvent:
    return OnlineEvent(
        attempt_id=attempt.id, kind=kind, at=now, counted=counted, detail=detail,
        duration_ms=int(duration.total_seconds() * 1000) if duration is not None else None,
    )


# ── starting ────────────────────────────────────────────────────────────────

def _shuffled_letters(q: Question, rng: random.Random) -> list[str]:
    letters = [o["letter"] for o in q.options_ordered]
    rng.shuffle(letters)
    return letters


async def start_attempt(
    session: AsyncSession, test: OnlineTest, *, first_name: str, last_name: str,
    class_name: str, now: datetime, rng: random.Random | None = None,
) -> tuple[OnlineAttempt, str]:
    """(attempt, raw token for the cookie). Raises OnlineError."""
    if test.status != TEST_OPEN:
        raise OnlineError("test_closed")
    first, last, cls = clean_name(first_name, 64), clean_name(last_name, 64), clean_name(class_name, 32)
    if not first or not last or not cls:
        raise OnlineError("name_required")
    rng = rng or random.SystemRandom()
    qmap = await _questions_by_id(session, test.pool_question_ids)
    pool = [qid for qid in test.pool_question_ids if qid in qmap and is_mc(qmap[qid])]
    n = min(test.question_count, len(pool))
    if n == 0:
        raise OnlineError("no_mc_questions")
    chosen = rng.sample(pool, n)   # random subset, already in random order

    token = new_token()
    attempt = OnlineAttempt(
        test_id=test.id, first_name=first, last_name=last, class_name=cls,
        name_key=name_key(first, last, cls), token_hash=hash_token(token),
        status=ATTEMPT_ACTIVE, started_at=now, current_position=1, violations=0,
        deadline_at=now + timedelta(seconds=test.total_seconds) if test.total_seconds else None,
        max_score=n,
    )
    try:
        # A savepoint: a duplicate name must undo only this insert, not the
        # caller's whole transaction.
        async with session.begin_nested():
            session.add(attempt)
            await session.flush()
    except IntegrityError as e:
        raise OnlineError("already_started") from e
    for pos, qid in enumerate(chosen, start=1):
        session.add(OnlineAttemptQuestion(
            attempt_id=attempt.id, position=pos, question_id=uuid.UUID(qid),
            option_order=_shuffled_letters(qmap[qid], rng), status=AQ_ACTIVE,
            shown_at=now if pos == 1 else None,
        ))
    session.add(_event(attempt, "start", now))
    await session.flush()
    return attempt, token


# ── time ────────────────────────────────────────────────────────────────────

async def refresh(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest, now: datetime,
                  *, judge_away: bool = True) -> None:
    """Everything that happens by itself with time, decided at each contact:
    an away period left open (tab closed) and the total deadline.

    `judge_away=False` for the visibility report itself: its "visible" closes
    the away period and must be the one that counts it (and tells the page)."""
    if attempt.status != ATTEMPT_ACTIVE:
        return
    if test.status == TEST_CLOSED:
        await finish(session, attempt, now, ATTEMPT_FINISHED, reason="test_closed")
        return
    if judge_away and attempt.away_since is not None and now - attempt.away_since >= AWAY_GRACE:
        # Came back without a "visible" (page reload / reopened tab).
        await _close_away(session, attempt, test, now)
        if attempt.status != ATTEMPT_ACTIVE:
            return
    if attempt.deadline_at is not None and now >= attempt.deadline_at:
        await finish(session, attempt, now, ATTEMPT_EXPIRED)


def remaining_seconds(attempt: OnlineAttempt, now: datetime) -> int | None:
    if attempt.deadline_at is None:
        return None
    return max(0, int((attempt.deadline_at - now).total_seconds()))


def question_seconds_left(test: OnlineTest, aq: OnlineAttemptQuestion, now: datetime) -> int | None:
    if not test.per_question_seconds or aq.shown_at is None:
        return test.per_question_seconds or None
    left = test.per_question_seconds - (now - aq.shown_at).total_seconds()
    return max(0, int(left))


def _question_expired(test: OnlineTest, aq: OnlineAttemptQuestion, now: datetime) -> bool:
    if not test.per_question_seconds or aq.shown_at is None:
        return False
    return now > aq.shown_at + timedelta(seconds=test.per_question_seconds) + LATE_SLACK


# ── navigation and answers ──────────────────────────────────────────────────

def _require_active(attempt: OnlineAttempt) -> None:
    if attempt.status != ATTEMPT_ACTIVE:
        raise OnlineError("attempt_over", status=attempt.status)


async def _aq_at(session: AsyncSession, attempt: OnlineAttempt, position: int) -> OnlineAttemptQuestion:
    aq = (await session.execute(
        select(OnlineAttemptQuestion).where(
            OnlineAttemptQuestion.attempt_id == attempt.id,
            OnlineAttemptQuestion.position == position,
            OnlineAttemptQuestion.status == AQ_ACTIVE,
        )
    )).scalar_one_or_none()
    if aq is None:
        raise OnlineError("bad_position")
    return aq


async def goto(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
               position: int, now: datetime) -> OnlineAttemptQuestion:
    """Move to `position`. Without allow_back only forward moves are allowed;
    with a per-question timer, a question whose time ran out cannot be reopened."""
    _require_active(attempt)
    if not 1 <= position <= (attempt.max_score or 0):
        raise OnlineError("bad_position")
    if not test.allow_back and position < attempt.current_position:
        raise OnlineError("no_going_back")
    aq = await _aq_at(session, attempt, position)
    if position != attempt.current_position and _question_expired(test, aq, now):
        raise OnlineError("question_time_over")
    if aq.shown_at is None:
        aq.shown_at = now
    attempt.current_position = position
    await session.flush()
    return aq


async def answer(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                 position: int, choices: list[int], now: datetime) -> OnlineAttemptQuestion:
    """Save the student's choice at once. `choices` are indexes into the
    shuffled option list this student was shown."""
    _require_active(attempt)
    if not test.allow_back and position != attempt.current_position:
        raise OnlineError("no_going_back")
    aq = await _aq_at(session, attempt, position)
    if aq.shown_at is None:
        # Never opened: answering it blind would also dodge its own timer,
        # which only starts when the question is shown.
        raise OnlineError("not_shown")
    if _question_expired(test, aq, now):
        raise OnlineError("question_time_over")
    order = list(aq.option_order or [])
    if any(not isinstance(i, int) or not 0 <= i < len(order) for i in choices):
        raise OnlineError("bad_choice")
    aq.answer = [order[i] for i in sorted(set(choices))] or None
    aq.answered_at = now if aq.answer else None
    await session.flush()
    return aq


# ── violations ──────────────────────────────────────────────────────────────

@dataclass
class EventOutcome:
    counted: bool = False
    violations: int = 0
    remaining_chances: int = MAX_VIOLATIONS
    replaced_positions: list[int] = field(default_factory=list)
    caught: bool = False


async def report(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                 kind: str, now: datetime) -> EventOutcome:
    """A focus/visibility report from the page: kind "hidden" or "visible"."""
    out = EventOutcome(violations=attempt.violations,
                       remaining_chances=MAX_VIOLATIONS - attempt.violations)
    if attempt.status != ATTEMPT_ACTIVE:
        out.caught = attempt.status == ATTEMPT_CAUGHT
        return out
    if kind == "hidden":
        if attempt.away_since is None:
            attempt.away_since = now
            session.add(_event(attempt, "away_start", now))
            await session.flush()
        return out
    if kind != "visible":
        raise OnlineError("bad_event")
    if attempt.away_since is None:
        return out
    return await _close_away(session, attempt, test, now)


async def _close_away(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                      now: datetime) -> EventOutcome:
    away = now - attempt.away_since
    attempt.away_since = None
    counted = away >= AWAY_GRACE
    session.add(_event(attempt, "away_end", now, duration=away, counted=counted))
    out = EventOutcome(counted=counted, violations=attempt.violations,
                       remaining_chances=MAX_VIOLATIONS - attempt.violations)
    if counted:
        out = await _violation(session, attempt, test, now)
        out.counted = True
    await session.flush()
    return out


async def _violation(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                     now: datetime) -> EventOutcome:
    attempt.violations += 1
    out = EventOutcome(counted=True, violations=attempt.violations,
                       remaining_chances=max(0, MAX_VIOLATIONS - attempt.violations))
    session.add(_event(attempt, "violation", now, counted=True, detail=str(attempt.violations)))
    if attempt.violations >= MAX_VIOLATIONS:
        await finish(session, attempt, now, ATTEMPT_CAUGHT)
        out.caught = True
        return out
    out.replaced_positions = await replace_unanswered(session, attempt, test, now)
    return out


async def replace_unanswered(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                             now: datetime, rng: random.Random | None = None,
                             limit: int = REPLACE_PER_VIOLATION) -> list[int]:
    """Swap up to `limit` unanswered questions (not the one on screen) for
    reserve questions this student has never had. Returns swapped positions."""
    if not test.replacement_enabled:
        session.add(_event(attempt, "replace_skipped", now, detail="replacement disabled"))
        return []
    rng = rng or random.SystemRandom()
    all_rows = list((await session.execute(
        select(OnlineAttemptQuestion).where(OnlineAttemptQuestion.attempt_id == attempt.id)
    )).scalars().all())
    ever = {str(r.question_id) for r in all_rows}
    qmap = await _questions_by_id(session, test.pool_question_ids)
    reserve = [qid for qid in test.pool_question_ids
               if qid not in ever and qid in qmap and is_mc(qmap[qid])]
    rng.shuffle(reserve)
    candidates = [r for r in all_rows
                  if r.status == AQ_ACTIVE and not r.answer and r.position != attempt.current_position]
    rng.shuffle(candidates)
    swapped: list[int] = []
    for row, qid in zip(candidates[:limit], reserve):
        row.status = AQ_REPLACED
        await session.flush()   # free the position before the partial unique index sees the new row
        session.add(OnlineAttemptQuestion(
            attempt_id=attempt.id, position=row.position, question_id=uuid.UUID(qid),
            option_order=_shuffled_letters(qmap[qid], rng), status=AQ_ACTIVE,
        ))
        swapped.append(row.position)
    session.add(_event(attempt, "replaced", now, detail=",".join(map(str, sorted(swapped))) or "none"))
    await session.flush()
    return sorted(swapped)


# ── finishing ───────────────────────────────────────────────────────────────

async def finish(session: AsyncSession, attempt: OnlineAttempt, now: datetime,
                 status: str = ATTEMPT_FINISHED, reason: str | None = None) -> OnlineAttempt:
    if attempt.status != ATTEMPT_ACTIVE:
        return attempt
    rows = await active_questions(session, attempt)
    qmap = await _questions_by_id(session, [r.question_id for r in rows])
    score = 0
    for r in rows:
        q = qmap.get(str(r.question_id))
        r.is_correct = bool(q) and is_correct(r.answer, q.correct_answers_ordered)
        score += int(r.is_correct)
    attempt.status = status
    attempt.finished_at = now
    attempt.away_since = None
    attempt.max_score = len(rows)
    attempt.score = 0 if status == ATTEMPT_CAUGHT else score
    session.add(_event(attempt, status, now, detail=reason))
    await session.flush()
    return attempt


async def close_test(session: AsyncSession, test: OnlineTest, now: datetime) -> int:
    """Close the link; attempts still running are graded as they stand."""
    test.status = TEST_CLOSED
    test.closed_at = now
    running = (await session.execute(
        select(OnlineAttempt).where(OnlineAttempt.test_id == test.id,
                                    OnlineAttempt.status == ATTEMPT_ACTIVE)
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    for a in running:
        await finish(session, a, now, ATTEMPT_FINISHED, reason="test_closed")
    await session.flush()
    return len(running)


async def annul(session: AsyncSession, attempt: OnlineAttempt, now: datetime) -> None:
    """The teacher allows a retake: this attempt stays on record as annulled,
    and the same student may start again."""
    if attempt.status == ATTEMPT_ACTIVE:
        await finish(session, attempt, now, ATTEMPT_FINISHED, reason="annulled")
    attempt.status = ATTEMPT_ANNULLED
    attempt.annulled_at = now
    session.add(_event(attempt, "annulled", now))
    await session.flush()


# ── what the browser may see ────────────────────────────────────────────────

def option_texts(q: Question, order: list[str]) -> list[str]:
    by_letter = {o["letter"]: o["text"] for o in q.options_ordered}
    return [by_letter.get(letter, "") for letter in order]


async def question_view(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                        aq: OnlineAttemptQuestion, now: datetime) -> dict:
    """One question as the browser gets it. No letter, no id, no answer key:
    options are a list of texts; the student's own choice is given back as
    indexes into that list."""
    q = (await _questions_by_id(session, [aq.question_id]))[str(aq.question_id)]
    order = list(aq.option_order or [])
    chosen = [order.index(letter) for letter in (aq.answer or []) if letter in order]
    return {
        "position": aq.position,
        "text": q.question_text,
        "context": q.group_context or None,
        "options": option_texts(q, order),
        "multi": len(q.correct_answers_ordered) > 1,
        "has_image": bool(q.has_image and q.image_path),
        "chosen": chosen,
        "seconds_left": question_seconds_left(test, aq, now),
    }


async def attempt_view(session: AsyncSession, attempt: OnlineAttempt, test: OnlineTest,
                       now: datetime) -> dict:
    rows = await active_questions(session, attempt) if attempt.status == ATTEMPT_ACTIVE else []
    view = {
        "status": attempt.status,
        "student": f"{attempt.last_name} {attempt.first_name}",
        "class_name": attempt.class_name,
        "total": attempt.max_score,
        "current": attempt.current_position,
        "answered": [r.position for r in rows if r.answer],
        "seconds_left": remaining_seconds(attempt, now),
        "violations": attempt.violations,
        "remaining_chances": max(0, MAX_VIOLATIONS - attempt.violations),
        "allow_back": test.allow_back,
        "per_question_seconds": test.per_question_seconds,
        "now": now.isoformat(),
    }
    if attempt.status in FINAL_STATUSES:
        view["score"] = attempt.score
    return view


# ── results (bot side) ──────────────────────────────────────────────────────

async def results(session: AsyncSession, test: OnlineTest, now: datetime | None = None) -> list[dict]:
    """Every attempt for the teacher. With `now`, attempts whose time ran out
    while the student stayed silent are graded first (the caller commits),
    so the list never shows "in progress" for a test that is long over."""
    if now is not None:
        stale = (await session.execute(
            select(OnlineAttempt).where(OnlineAttempt.test_id == test.id,
                                        OnlineAttempt.status == ATTEMPT_ACTIVE)
            .with_for_update().execution_options(populate_existing=True)
        )).scalars().all()
        for a in stale:
            await refresh(session, a, test, now)
    attempts = (await session.execute(
        select(OnlineAttempt).where(OnlineAttempt.test_id == test.id)
        .order_by(OnlineAttempt.started_at)
    )).scalars().all()
    out = []
    for a in attempts:
        events = (await session.execute(
            select(OnlineEvent).where(OnlineEvent.attempt_id == a.id,
                                      OnlineEvent.kind.in_(("away_end", "violation")))
            .order_by(OnlineEvent.at)
        )).scalars().all()
        out.append({
            "attempt_id": str(a.id),
            "name": f"{a.last_name} {a.first_name}",
            "class_name": a.class_name,
            "status": a.status,
            "score": a.score,
            "max_score": a.max_score,
            "violations": a.violations,
            "caught": a.status == ATTEMPT_CAUGHT,
            "seconds": int(((a.finished_at or a.started_at) - a.started_at).total_seconds()),
            "away": [
                {"at": e.at.isoformat(), "ms": e.duration_ms, "counted": e.counted}
                for e in events if e.kind == "away_end"
            ],
        })
    return out
