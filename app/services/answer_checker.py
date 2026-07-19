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


@dataclass
class CheckResult:
    total: int
    correct: int
    wrong: int
    skipped: int
    score_percent: float
    question_results: list[QuestionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "skipped": self.skipped,
            "score_percent": round(self.score_percent, 1),
            "question_results": [
                {
                    "position": r.position,
                    "student_answer": r.student_answer,
                    "correct_answer": r.correct_answer,
                    "is_correct": r.is_correct,
                    "is_skipped": r.is_skipped,
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

        for r in self.question_results:
            student = r.student_answer or "-"
            if r.is_skipped:
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

        return "\n".join(lines)


def check_answers(
    student_answers: dict[str, str | None],
    answer_key: dict[str, str | None],
) -> CheckResult:
    """
    Compare student answers against the variant's answer key.

    The answer key value per position is a LIST of accepted answers (a single
    letter is a one-item list); a legacy scalar value is accepted too. Matching
    uses the SHARED checker.is_correct (casefold + collapse, no punctuation
    stripping) so a written answer / multiple accepted answers work identically
    to the manual flow. Letters grade exactly as before (one-item list).
    """
    total = max(
        (int(k) for k in answer_key if answer_key[k] is not None),
        default=0,
    )
    question_results: list[QuestionResult] = []
    correct = wrong = skipped = 0

    for pos in range(1, total + 1):
        pos_str = str(pos)
        student_ans = (student_answers.get(pos_str) or "").strip() or None
        accepted = accepted_list(answer_key.get(pos_str))
        correct_display = " / ".join(accepted) if accepted else None

        is_skipped = student_ans is None
        correct_flag = (not is_skipped) and is_correct(student_ans, accepted)

        if is_skipped:
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
            )
        )

    score_percent = (correct / total * 100) if total > 0 else 0.0

    logger.info(
        "check_complete",
        total=total,
        correct=correct,
        wrong=wrong,
        skipped=skipped,
        score=score_percent,
    )

    return CheckResult(
        total=total,
        correct=correct,
        wrong=wrong,
        skipped=skipped,
        score_percent=score_percent,
        question_results=question_results,
    )
