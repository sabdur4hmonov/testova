"""
Variant generation engine — group-aware anti-cheating shuffle.

Rules:
  - Reading-comprehension groups (shared context_text) move as ONE unit.
    Their internal question order stays fixed; only option letters are shuffled.
  - Single questions: both question order and option letters are shuffled.
  - No two adjacent variants share the same unit arrangement (where possible).
"""
from __future__ import annotations

import random
import uuid
from typing import Any

from app.utils.logging import get_logger

logger = get_logger(__name__)

OPTION_LETTERS = ["A", "B", "C", "D"]


# ── Pre-export validation ───────────────────────────────────────────────────────

def validate_questions(
    questions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    BUG FIX: validate questions BEFORE variants/PDFs are built.
    Previously nothing checked question integrity, so broken questions
    (blank options, missing text) went straight into the printed exam.

    Rules:
      - empty question_text                      → rejected
      - 0 non-empty options                      → kept, marked is_open_ended
      - exactly 1 non-empty option               → rejected (not gradeable MC)
      - blank option strings among 2-4 filled    → blanks dropped pre-shuffle
      - correct_answer points at a dropped blank → rejected (key unrecoverable)

    Returns (valid_questions, rejected) where each rejected entry is
    {"question_number": int, "reason": str}.
    """
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for q in questions:
        q = {**q}  # don't mutate caller's dicts
        num = q.get("question_number", "?")

        if not str(q.get("question_text") or "").strip():
            rejected.append({"question_number": num, "reason": "empty_text"})
            continue

        opts = q.get("options") or {}
        filled = {
            k: v for k, v in opts.items()
            if v is not None and str(v).strip()
        }

        if len(filled) == 0:
            # Genuinely open-ended — keep, but mark it so the PDF renders
            # a write-in line instead of an empty options block.
            q["options"] = {}
            q["is_open_ended"] = True
            valid.append(q)
            continue

        if len(filled) == 1:
            rejected.append({"question_number": num, "reason": "single_option"})
            continue

        ca = q.get("correct_answer")
        if ca and ca in opts and ca not in filled:
            # The correct option itself was blank — the answer key would
            # point at nothing after blanks are dropped.
            rejected.append({"question_number": num, "reason": "blank_correct_option"})
            continue

        # Drop blank strings AND compact the remaining letters so the printed
        # question never shows a gap (A, B, D → A, B, C). The correct-answer
        # letter is remapped alongside its content.
        source_order = [k for k in "ABCDE" if k in filled]
        compact: dict[str, str] = {}
        remap: dict[str, str] = {}
        for new_letter, old_letter in zip("ABCDE", source_order):
            compact[new_letter] = filled[old_letter]
            remap[old_letter] = new_letter
        q["options"] = compact
        if ca and ca in remap:
            q["correct_answer"] = remap[ca]
        q["is_open_ended"] = False
        valid.append(q)

    if rejected:
        logger.warning(
            "questions_rejected",
            count=len(rejected),
            details=rejected,
        )
    return valid, rejected


# ── Option shuffler ─────────────────────────────────────────────────────────────

def _shuffle_options(
    question: dict[str, Any], rng: random.Random
) -> tuple[dict[str, str], str | None]:
    original = question.get("options", {})
    available = [k for k in OPTION_LETTERS if original.get(k) is not None]

    if len(available) < 2:
        return original, question.get("correct_answer")

    positions = available[:]
    rng.shuffle(positions)

    shuffled: dict[str, str] = {}
    option_map: dict[str, str] = {}  # original → new letter

    for new_letter, orig_letter in zip(available, positions):
        shuffled[new_letter] = original[orig_letter]
        option_map[orig_letter] = new_letter

    orig_correct = question.get("correct_answer")
    new_correct = option_map.get(orig_correct) if orig_correct else None
    return shuffled, new_correct


# ── Unit builder ────────────────────────────────────────────────────────────────

def _build_units(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group questions into shuffleable units.
    Reading-comprehension questions with the same group_id form one unit.
    All others are individual units.
    """
    units: list[dict[str, Any]] = []
    seen: dict[str, int] = {}  # group_id → unit index

    for q in questions:
        gid = q.get("group_id")
        if gid and gid in seen:
            units[seen[gid]]["questions"].append(q)
        else:
            unit: dict[str, Any] = {
                "group_id": gid,
                "group_context": q.get("group_context"),
                "is_reading_group": bool(gid),
                "questions": [q],
            }
            if gid:
                seen[gid] = len(units)
            units.append(unit)

    return units


# ── Core generator ──────────────────────────────────────────────────────────────

def generate_variants(
    questions: list[dict[str, Any]],
    count: int,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Generate `count` variants from questions.

    Returns list of variant dicts:
    {
        "variant_number": int,
        "question_order": [question_id, ...],
        "option_mapping": {question_id: {orig_letter: new_letter}},
        "answer_key": {"1": "B", "2": "A", ...},
        "questions_data": [...],   # fully resolved, for PDF
    }
    """
    if not questions:
        raise ValueError("Cannot generate variants from empty question list")

    # BUG FIX: never export unvalidated questions. Callers (upload handler)
    # should run validate_questions themselves to report rejects to the
    # teacher; this pass is defense-in-depth for any other entry point.
    questions, _rejected = validate_questions(questions)
    if not questions:
        raise ValueError("No valid questions left after validation")

    units = _build_units(questions)
    base_rng = random.Random(seed)
    seen_orders: set[tuple] = set()
    variants: list[dict[str, Any]] = []

    for variant_num in range(1, count + 1):
        rng = random.Random(base_rng.randint(0, 2 ** 32))

        # Shuffle units (reading groups move as one block)
        order = list(range(len(units)))
        for _ in range(200):
            rng.shuffle(order)
            key = tuple(order)
            if key not in seen_orders:
                seen_orders.add(key)
                break

        shuffled_units = [units[i] for i in order]

        option_mapping: dict[str, dict[str, str]] = {}
        answer_key: dict[str, str | None] = {}
        resolved: list[dict[str, Any]] = []
        pos = 0

        for unit in shuffled_units:
            unit_questions = unit["questions"]

            # For reading groups: keep internal order, only shuffle options
            # For single questions: shuffle options (order already handled at unit level)
            for q in unit_questions:
                pos += 1
                q_id = str(q.get("question_id", q.get("id", uuid.uuid4())))
                shuffled_opts, new_correct = _shuffle_options(q, rng)

                # Build original→new letter map
                orig_opts = q.get("options", {})
                fwd_map: dict[str, str] = {}
                for new_k, text in shuffled_opts.items():
                    for orig_k, orig_text in orig_opts.items():
                        if text == orig_text:
                            fwd_map[orig_k] = new_k
                            break

                option_mapping[q_id] = fwd_map
                answer_key[str(pos)] = new_correct

                resolved.append({
                    **q,
                    "position_in_variant": pos,
                    "options": shuffled_opts,
                    "correct_answer": new_correct,
                    # Only the first question in a reading group carries context
                    # so the PDF prints the passage once before the block.
                    "group_context": unit["group_context"] if q is unit["questions"][0] else None,
                })

        variants.append({
            "variant_number": variant_num,
            "question_order": [
                str(q.get("question_id", q.get("id", "")))
                for unit in shuffled_units
                for q in unit["questions"]
            ],
            "option_mapping": option_mapping,
            "answer_key": answer_key,
            "questions_data": resolved,
        })

        logger.debug("variant_generated", variant=variant_num, units=len(units))

    logger.info("variants_complete", total=len(variants))
    return variants
