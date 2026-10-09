"""Online test rules (online/service.py) against the real per-run database."""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.online_test import (
    AQ_ACTIVE, AQ_REPLACED, ATTEMPT_ANNULLED, ATTEMPT_CAUGHT, ATTEMPT_EXPIRED,
    ATTEMPT_FINISHED, OnlineAttemptQuestion, OnlineEvent,
)
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from online import service as svc
from sqlalchemy import select

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def make_project(session, user, n_mc: int, n_written: int = 0, multi_every: int = 0):
    project = Project(id=uuid.uuid4(), user_id=user.id, name="Algebra",
                      status=ProjectStatus.COMPLETED, question_count=n_mc + n_written)
    session.add(project)
    await session.flush()
    for i in range(1, n_mc + 1):
        correct = ["A", "C"] if multi_every and i % multi_every == 0 else ["B"]
        session.add(Question(
            project_id=project.id, question_number=i, question_text=f"Savol {i}?",
            options=[{"letter": L, "text": f"{L}-javob {i}"} for L in "ABCD"],
            correct_answers=correct, correct_answer=correct[0],
        ))
    for j in range(n_written):
        session.add(Question(project_id=project.id, question_number=n_mc + j + 1,
                             question_text=f"Yozma {j}", options=[]))
    await session.flush()
    return project


async def make_test(session, user, *, n_mc=45, n=30, written=0, total=None, per_q=None,
                    allow_back=True, multi_every=0):
    project = await make_project(session, user, n_mc, written, multi_every)
    test, info = await svc.create_test(
        session, project_id=project.id, user_id=user.id, title="Algebra",
        question_count=n, total_seconds=total, per_question_seconds=per_q,
        allow_back=allow_back,
    )
    return test, info


async def start(session, test, name="Ali", rng_seed=1, now=T0):
    return await svc.start_attempt(session, test, first_name=name, last_name="Valiyev",
                                   class_name="9-A", now=now, rng=random.Random(rng_seed))


async def rows(session, attempt, status=AQ_ACTIVE):
    return list((await session.execute(
        select(OnlineAttemptQuestion).where(OnlineAttemptQuestion.attempt_id == attempt.id,
                                            OnlineAttemptQuestion.status == status)
        .order_by(OnlineAttemptQuestion.position)
    )).scalars().all())


async def answer_shown(session, attempt, test, position, choices, now):
    """What the page does: open the question, then answer it."""
    await svc.goto(session, attempt, test, position, now)
    return await svc.answer(session, attempt, test, position, choices, now)


async def correct_choice(session, aq) -> list[int]:
    q = await session.get(Question, aq.question_id)
    return [aq.option_order.index(L) for L in q.correct_answers_ordered]


# ── pool and selection ──────────────────────────────────────────────────────

def test_required_pool():
    assert svc.required_pool(30) == 45
    assert svc.required_pool(7) == 11


async def test_pool_excludes_written_and_checks_reserve(session, sample_user):
    test, info = await make_test(session, sample_user, n_mc=45, n=30, written=3)
    assert info.written_excluded == 3 and len(info.pool_ids) == 45
    assert info.replacement_enabled and test.replacement_enabled

    test2, info2 = await make_test(session, sample_user, n_mc=40, n=30)
    assert not info2.replacement_enabled          # 40 < 45: order shuffled only

    test3, info3 = await make_test(session, sample_user, n_mc=10, n=30)
    assert info3.question_count == 10             # N clamps to the pool


async def test_code_is_long_and_unambiguous(session, sample_user):
    test, _ = await make_test(session, sample_user)
    assert len(test.code) >= 8
    assert not set(test.code) & set("01OIL")


async def test_each_student_gets_n_random_questions_with_shuffled_options(session, sample_user):
    test, _ = await make_test(session, sample_user, n_mc=45, n=30)
    a1, tok1 = await start(session, test, "Ali", 1)
    a2, tok2 = await start(session, test, "Vali", 2)
    r1, r2 = await rows(session, a1), await rows(session, a2)
    assert [r.position for r in r1] == list(range(1, 31))
    ids1 = [str(r.question_id) for r in r1]
    ids2 = [str(r.question_id) for r in r2]
    assert len(set(ids1)) == 30 and set(ids1) <= set(test.pool_question_ids)
    assert ids1 != ids2                                  # different draw/order
    assert all(sorted(r.option_order) == list("ABCD") for r in r1)
    assert any(r.option_order != list("ABCD") for r in r1)
    assert tok1 != tok2 and a1.token_hash == svc.hash_token(tok1)


async def test_same_name_cannot_start_twice_until_annulled(session, sample_user):
    test, _ = await make_test(session, sample_user)
    a1, _ = await start(session, test, "Ali")
    with pytest.raises(svc.OnlineError) as e:
        await svc.start_attempt(session, test, first_name=" ali ", last_name="VALIYEV",
                                class_name="9-a", now=T0)
    assert e.value.code == "already_started"
    await svc.annul(session, a1, at(60))
    assert a1.status == ATTEMPT_ANNULLED
    a2, _ = await start(session, test, "Ali", now=at(70))
    assert a2.id != a1.id


async def test_closed_test_refuses_start(session, sample_user):
    test, _ = await make_test(session, sample_user)
    await svc.close_test(session, test, T0)
    with pytest.raises(svc.OnlineError) as e:
        await start(session, test)
    assert e.value.code == "test_closed"


# ── what reaches the browser ────────────────────────────────────────────────

async def test_question_view_never_contains_the_answer(session, sample_user):
    test, _ = await make_test(session, sample_user, multi_every=2)
    attempt, _ = await start(session, test)
    for aq in await rows(session, attempt):
        view = await svc.question_view(session, attempt, test, aq, T0)
        assert set(view) == {"position", "text", "context", "options", "multi",
                             "has_image", "chosen", "seconds_left"}
        dumped = json.dumps(view, ensure_ascii=False)
        assert str(aq.question_id) not in dumped
        assert "correct" not in dumped.lower()
        # options are bare texts; their letters are not sent
        assert all(isinstance(o, str) for o in view["options"])
    state = await svc.attempt_view(session, attempt, test, T0)
    assert "score" not in state                    # not before the end
    assert "B" not in json.dumps(state["answered"])


# ── answers and score ───────────────────────────────────────────────────────

async def test_score_counts_exact_answers(session, sample_user):
    test, _ = await make_test(session, sample_user, n_mc=45, n=10, multi_every=3)
    attempt, _ = await start(session, test)
    aqs = await rows(session, attempt)
    for i, aq in enumerate(aqs):
        good = await correct_choice(session, aq)
        if i < 6:
            await answer_shown(session, attempt, test, aq.position, good, at(10 + i))
        elif i < 8:
            wrong = [k for k in range(4) if k not in good][:1]
            await answer_shown(session, attempt, test, aq.position, wrong, at(10 + i))
        # 8, 9 unanswered
    await svc.finish(session, attempt, at(100))
    assert attempt.status == ATTEMPT_FINISHED
    assert (attempt.score, attempt.max_score) == (6, 10)


async def test_multi_answer_needs_the_whole_set():
    assert svc.is_correct(["A", "C"], ["C", "A"])
    assert not svc.is_correct(["A"], ["A", "C"])
    assert not svc.is_correct(["A", "B", "C"], ["A", "C"])
    assert svc.is_correct(["А"], ["A"])            # Cyrillic А folds to Latin A
    assert not svc.is_correct(None, ["A"])


async def test_bad_choice_index_refused(session, sample_user):
    test, _ = await make_test(session, sample_user)
    attempt, _ = await start(session, test)
    with pytest.raises(svc.OnlineError) as e:
        await svc.answer(session, attempt, test, 1, [7], T0)
    assert e.value.code == "bad_choice"


async def test_no_going_back(session, sample_user):
    test, _ = await make_test(session, sample_user, allow_back=False)
    attempt, _ = await start(session, test)
    await svc.goto(session, attempt, test, 2, at(5))
    with pytest.raises(svc.OnlineError) as e:
        await svc.goto(session, attempt, test, 1, at(6))
    assert e.value.code == "no_going_back"
    with pytest.raises(svc.OnlineError):
        await svc.answer(session, attempt, test, 1, [0], at(6))


async def test_per_question_time_is_server_side(session, sample_user):
    test, _ = await make_test(session, sample_user, per_q=30)
    attempt, _ = await start(session, test)              # question 1 shown at T0
    await svc.answer(session, attempt, test, 1, [0], at(29))
    with pytest.raises(svc.OnlineError) as e:
        await svc.answer(session, attempt, test, 1, [1], at(40))
    assert e.value.code == "question_time_over"


async def test_total_time_expires_and_grades(session, sample_user):
    test, _ = await make_test(session, sample_user, n=5, n_mc=10, total=600)
    attempt, _ = await start(session, test)
    aq = (await rows(session, attempt))[0]
    await svc.answer(session, attempt, test, 1, await correct_choice(session, aq), at(10))
    await svc.refresh(session, attempt, test, at(601))
    assert attempt.status == ATTEMPT_EXPIRED and attempt.score == 1
    with pytest.raises(svc.OnlineError):
        await svc.answer(session, attempt, test, 2, [0], at(602))


# ── violations ──────────────────────────────────────────────────────────────

async def test_short_absence_is_not_counted(session, sample_user):
    test, _ = await make_test(session, sample_user)
    attempt, _ = await start(session, test)
    await svc.report(session, attempt, test, "hidden", at(10))
    out = await svc.report(session, attempt, test, "visible", at(11.5))
    assert not out.counted and attempt.violations == 0
    ev = (await session.execute(select(OnlineEvent).where(
        OnlineEvent.attempt_id == attempt.id, OnlineEvent.kind == "away_end"))).scalar_one()
    assert ev.duration_ms == 1500 and ev.counted is False


async def test_first_violation_warns_and_swaps_five_unanswered(session, sample_user):
    test, _ = await make_test(session, sample_user, n_mc=45, n=30)
    attempt, _ = await start(session, test)
    before = await rows(session, attempt)
    answered = {aq.position for aq in before[:10]}
    for aq in before[:10]:
        await answer_shown(session, attempt, test, aq.position, [0], at(5))
    await svc.report(session, attempt, test, "hidden", at(10))
    out = await svc.report(session, attempt, test, "visible", at(15))
    assert out.counted and out.violations == 1 and out.remaining_chances == 2
    assert len(out.replaced_positions) == 5
    assert not set(out.replaced_positions) & answered
    assert attempt.current_position not in out.replaced_positions
    after = await rows(session, attempt)
    assert [r.position for r in after] == list(range(1, 31))      # still N, same slots
    old = {r.position: str(r.question_id) for r in before}
    new = {r.position: str(r.question_id) for r in after}
    seen_before = set(old.values())
    for pos in out.replaced_positions:
        assert new[pos] != old[pos] and new[pos] not in seen_before
    assert len(await rows(session, attempt, AQ_REPLACED)) == 5


async def test_second_violation_never_brings_back_an_old_question(session, sample_user):
    test, _ = await make_test(session, sample_user, n_mc=45, n=30)
    attempt, _ = await start(session, test)
    for k in (1, 2):
        await svc.report(session, attempt, test, "hidden", at(k * 100))
        await svc.report(session, attempt, test, "visible", at(k * 100 + 5))
    everything = list((await session.execute(select(OnlineAttemptQuestion).where(
        OnlineAttemptQuestion.attempt_id == attempt.id))).scalars().all())
    ids = [str(r.question_id) for r in everything]
    assert len(ids) == len(set(ids)) == 40                    # 30 + 5 + 5, no repeats
    assert attempt.violations == 2


async def test_third_violation_closes_with_zero(session, sample_user):
    test, _ = await make_test(session, sample_user, n_mc=45, n=10)
    attempt, _ = await start(session, test)
    for aq in await rows(session, attempt):
        await answer_shown(session, attempt, test, aq.position, await correct_choice(session, aq), at(1))
    outs = []
    for k in (1, 2, 3):
        await svc.report(session, attempt, test, "hidden", at(k * 100))
        outs.append(await svc.report(session, attempt, test, "visible", at(k * 100 + 3)))
    assert [o.remaining_chances for o in outs] == [2, 1, 0]
    assert outs[-1].caught and attempt.status == ATTEMPT_CAUGHT
    assert attempt.score == 0
    with pytest.raises(svc.OnlineError):
        await svc.answer(session, attempt, test, 1, [0], at(400))
    res = await svc.results(session, test)
    assert res[0]["caught"] is True and res[0]["violations"] == 3


async def test_no_reserve_means_no_swap(session, sample_user):
    test, _ = await make_test(session, sample_user, n_mc=30, n=30)
    assert not test.replacement_enabled
    attempt, _ = await start(session, test)
    await svc.report(session, attempt, test, "hidden", at(10))
    out = await svc.report(session, attempt, test, "visible", at(20))
    assert out.counted and out.replaced_positions == []
    assert attempt.violations == 1


async def test_closed_tab_counts_at_next_contact(session, sample_user):
    test, _ = await make_test(session, sample_user)
    attempt, _ = await start(session, test)
    await svc.report(session, attempt, test, "hidden", at(10))
    # no "visible" ever arrives; the student reopens the link a minute later
    await svc.refresh(session, attempt, test, at(70))
    assert attempt.violations == 1 and attempt.away_since is None


async def test_repeated_hidden_reports_are_one_absence(session, sample_user):
    test, _ = await make_test(session, sample_user)
    attempt, _ = await start(session, test)
    await svc.report(session, attempt, test, "hidden", at(10))
    await svc.report(session, attempt, test, "hidden", at(11))   # blur + visibilitychange
    out = await svc.report(session, attempt, test, "visible", at(13))
    assert out.counted and attempt.violations == 1


# ── teacher side ────────────────────────────────────────────────────────────

async def test_close_test_grades_running_attempts(session, sample_user):
    test, _ = await make_test(session, sample_user, n=5, n_mc=10)
    a, _ = await start(session, test)
    n = await svc.close_test(session, test, at(30))
    assert n == 1 and a.status == ATTEMPT_FINISHED and a.score == 0


async def test_results_rows(session, sample_user):
    test, _ = await make_test(session, sample_user, n=5, n_mc=10)
    a, _ = await start(session, test)
    await svc.report(session, a, test, "hidden", at(10))
    await svc.report(session, a, test, "visible", at(10.5))
    await svc.finish(session, a, at(90))
    [r] = await svc.results(session, test)
    assert r["name"] == "Valiyev Ali" and r["class_name"] == "9-A"
    assert r["seconds"] == 90 and r["score"] == 0 and r["max_score"] == 5
    assert r["away"] == [{"at": at(10.5).isoformat(), "ms": 500, "counted": False}]
