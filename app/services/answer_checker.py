"""
Answer checking engine.

Given:
  - A student's extracted answers: {question_position: letter}
  - The variant's answer key: {question_position: correct_letter}

Produces a detailed result report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.checker import accepted_list, is_correct
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class QuestionResult:
    position: int
    student_answer: str | None
    correct_answer: str | None
    is_correct: bool
    is_skipped: bool
    # AUDIT B-5. The sheet reader emits "?" for a mark it could not read (an
    # 8-vs-5 smudge and friends) and routes it to `unclear`, never to an
    # answer. Without this flag the saved flow saw only an ABSENT position and
    # reported "No answer", which is what a blank looks like — so the teacher
    # was never told to check the paper. Distinct from is_skipped: nobody
    # marked nothing, we just could not read what they marked.
    is_unclear: bool = False
    # AUDIT B-6. This question has NO answer key at all (None, [], or the
    # position is absent). It cannot be graded automatically: accepted==[] made
    # is_correct() always False, so every student was marked WRONG for it.
    # Distinct from is_unclear, where a key exists and the student's mark could
    # not be read.
    is_unkeyed: bool = False


# Sanity-backstop threshold (S1.4b): below this many questions, a perfect
# score is unremarkable (trivial to reach by real ability or plain luck on a
# short quiz) and flagging it would just be noise. At/above it, a perfect
# score is worth one advisory line for the teacher to glance at — never a
# blocked or altered grade, since a genuinely excellent student scoring 100%
# is the normal, expected case this must not cry wolf over.
SUSPICIOUS_PERFECT_MIN_QUESTIONS = 10


@dataclass
class CheckResult:
    total: int
    correct: int
    wrong: int
    skipped: int
    score_percent: float
    question_results: list[QuestionResult] = field(default_factory=list)
    # AUDIT B-5: positions the reader flagged "?" — counted against the score,
    # kept OUT of the wrong-answer detail, listed separately for a hand check.
    # Same contract the manual flow's compare_with_unclear already proved.
    unclear: list[int] = field(default_factory=list)
    # AUDIT B-6: positions with no key. Reported for a hand grade and EXCLUDED
    # from `total` — see check_answers for why they are excluded rather than
    # counted the way `unclear` is.
    unkeyed: list[int] = field(default_factory=list)
    # Advisory only (S1.4b post-processing sanity check) — never affects
    # correct/wrong/score_percent above. One plausible cause among several
    # (a genuinely great student is the common one); surfaced so a teacher
    # can glance at it, not proof of anything on its own.
    flagged_suspicious: bool = field(default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "skipped": self.skipped,
            "score_percent": round(self.score_percent, 1),
            "flagged_suspicious": self.flagged_suspicious,
            "unclear": list(self.unclear),
            "unkeyed": list(self.unkeyed),
            "question_results": [
                {
                    "position": r.position,
                    "student_answer": r.student_answer,
                    "correct_answer": r.correct_answer,
                    "is_correct": r.is_correct,
                    "is_skipped": r.is_skipped,
                    "is_unclear": r.is_unclear,
                    "is_unkeyed": r.is_unkeyed,
                }
                for r in self.question_results
            ],
        }

    def format_telegram_report(self, lang: str = "en") -> str:
        """Return a concise Telegram-formatted answer report."""
        lines: list[str] = []

        if lang == "uz":
            header = f"📊 Natija: {self.correct}/{self.total} ({self.score_percent:.1f}%)"
        elif lang == "ru":
            header = f"📊 Результат: {self.correct}/{self.total} ({self.score_percent:.1f}%)"
        else:
            header = f"📊 Result: {self.correct}/{self.total} ({self.score_percent:.1f}%)"

        lines.append(header)
        lines.append("")

        hand = {
            "uz": "Aniqlanmadi — qo'lda tekshiring",
            "ru": "Не распознано — проверьте вручную",
        }.get(lang, "Unreadable — check by hand")

        nokey = {
            "uz": "Kalit yo'q — qo'lda tekshiring",
            "ru": "Нет ключа — проверьте вручную",
        }.get(lang, "No answer key — grade by hand")

        for r in self.question_results:
            student = r.student_answer or "-"
            if r.is_unkeyed:
                icon = "✍"
                detail = f"{student}  |  {nokey}"
            elif r.is_unclear:
                icon = "❓"
                detail = f"{hand}  |  Correct: {r.correct_answer}"
            elif r.is_skipped:
                icon = "⬜"
                detail = f"❌ No answer  |  Correct: {r.correct_answer}"
            elif r.is_correct:
                icon = "✅"
                detail = f"{student}"
            else:
                icon = "❌"
                detail = f"{student}  |  Correct: {r.correct_answer}"

            lines.append(f"{icon} {r.position}. {detail}")

        lines.append("")
        if lang == "uz":
            lines.append(f"✅ To'g'ri: {self.correct}  ❌ Noto'g'ri: {self.wrong}  ⬜ Javob yo'q: {self.skipped}")
        elif lang == "ru":
            lines.append(f"✅ Правильно: {self.correct}  ❌ Неправильно: {self.wrong}  ⬜ Нет ответа: {self.skipped}")
        else:
            lines.append(f"✅ Correct: {self.correct}  ❌ Wrong: {self.wrong}  ⬜ Skipped: {self.skipped}")

        if self.flagged_suspicious:
            if lang == "uz":
                lines.append("ℹ️ Barcha javoblar to'g'ri — natijani ko'zdan kechiring.")
            elif lang == "ru":
                lines.append("ℹ️ Все ответы верны — рекомендуем проверить результат.")
            else:
                lines.append("ℹ️ All answers correct — worth a quick double-check.")

        return "\n".join(lines)


def check_answers(
    student_answers: dict[str, str | None],
    answer_key: dict[str, str | None],
    unclear: list[int] | None = None,
) -> CheckResult:
    """
    Compare student answers against the variant's answer key.

    The answer key value per position is a LIST of accepted answers (a single
    letter is a one-item list); a legacy scalar value is accepted too. Matching
    uses the SHARED checker.is_correct (casefold + collapse, no punctuation
    stripping) so a written answer / multiple accepted answers work identically
    to the manual flow. Letters grade exactly as before (one-item list).
    """
    # AUDIT B-6. Two DIFFERENT numbers were conflated here. The loop needs the
    # LAST position so every question still renders; the score needs a COUNT of
    # the questions that can actually be graded. Using one `max(... is not
    # None)` for both meant an unkeyed question at the END shrank the test
    # (total 3 -> 2) while the same question mid-test cost the student a point.
    last_position = max((int(k) for k in answer_key), default=0)

    def _keyed(pos: int) -> bool:
        # A BLANK accepted answer is no key at all: accepted_list keeps ""
        # (it is not None), but is_correct can never match it, so [""] grades
        # exactly like None and belongs in the same bucket.
        return any(
            str(a).strip() for a in accepted_list(answer_key.get(str(pos)))
        )

    # `total` counts only gradeable questions, which is what the manual flow has
    # always done (compare_with_unclear: `total = len(key)`), so the two flows
    # agree. An unkeyed question is EXCLUDED rather than counted the way
    # `unclear` is: with an unclear mark the key exists and the student's
    # writing could not be read, so counting it against them pending a hand
    # check is defensible; with no key there is no ground truth at all — the
    # gap is ours, not the student's — and counting it would guarantee a point
    # nobody can ever earn.
    total = sum(1 for pos in range(1, last_position + 1) if _keyed(pos))
    # Intersect with the key, exactly as compare_with_unclear does: a reader
    # flagging a position this test does not have must never invent one. An
    # unkeyed position is excluded too — there is nothing to be unclear about.
    unclear_set = {
        int(q) for q in (unclear or [])
        if str(q) in answer_key and _keyed(int(q))
    }
    question_results: list[QuestionResult] = []
    unkeyed: list[int] = []
    correct = wrong = skipped = 0

    for pos in range(1, last_position + 1):
        pos_str = str(pos)
        student_ans = (student_answers.get(pos_str) or "").strip() or None
        accepted = accepted_list(answer_key.get(pos_str))
        correct_display = " / ".join(accepted) if accepted else None

        # No key -> not gradeable. Never correct, never wrong, never skipped,
        # and never in `total`; the teacher grades it by hand.
        unkeyed_flag = not any(str(a).strip() for a in accepted)
        unclear_flag = (not unkeyed_flag) and pos in unclear_set
        is_skipped = (
            (not unkeyed_flag) and (not unclear_flag) and student_ans is None
        )
        correct_flag = (
            (not unkeyed_flag) and (not unclear_flag) and (not is_skipped)
            and is_correct(student_ans, accepted)
        )

        if unkeyed_flag:
            unkeyed.append(pos)
        elif unclear_flag:
            pass          # counts against the score; reported separately
        elif is_skipped:
            skipped += 1
        elif correct_flag:
            correct += 1
        else:
            wrong += 1

        question_results.append(
            QuestionResult(
                position=pos,
                student_answer=student_ans,
                correct_answer=correct_display,
                is_correct=correct_flag,
                is_skipped=is_skipped,
                is_unclear=unclear_flag,
                is_unkeyed=unkeyed_flag,
            )
        )

    score_percent = (correct / total * 100) if total > 0 else 0.0
    flagged_suspicious = (
        total >= SUSPICIOUS_PERFECT_MIN_QUESTIONS and correct == total
    )

    logger.info(
        "check_complete",
        total=total,
        correct=correct,
        wrong=wrong,
        skipped=skipped,
        unclear=len(unclear_set),
        unkeyed=len(unkeyed),
        score=score_percent,
        flagged_suspicious=flagged_suspicious,
    )

    return CheckResult(
        total=total,
        correct=correct,
        wrong=wrong,
        skipped=skipped,
        score_percent=score_percent,
        question_results=question_results,
        unclear=sorted(unclear_set),
        unkeyed=unkeyed,
        flagged_suspicious=flagged_suspicious,
    )
