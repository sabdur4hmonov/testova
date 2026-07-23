"""Deterministic option-label backstop for TEXT-BASED PDF sources.

Gemini non-deterministically relabels a paper's printed option markers
(a, b, d, e) into a fresh sequential A, B, C, D — PER PAGE, per call (the same
prompt drifts on the very next upload). The PDF text layer contains the REAL
printed markers as actual characters, so we read them back and OVERWRITE
Gemini's labels. Code reading characters is deterministic; a model reading a
picture is not.

SAFETY MODEL — never guess a mapping:
  A question's labels are overwritten ONLY when the number of printed markers
  found in its region equals the number of options Gemini returned (and the
  markers form a clean, duplicate-free run). ANY other outcome — question not
  locatable in the text layer, marker count mismatch, duplicated markers —
  leaves Gemini's labels untouched and sets q["label_doubt"] = True so the
  question is surfaced (suspicious report) for the teacher to eyeball. A
  question the backstop cannot see at all is as visible as one it declined.

SCOPE: PDF only. DOCX (rendered to image here — no text layer) stays prompt-only
(backlog follow-up). Scanned/image PDFs (no text layer at all) no-op entirely and
fall through to today's behaviour.
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF

from app.services.option_letters import canonical_letter
from app.utils.logging import get_logger

logger = get_logger(__name__)

# A printed option marker: ONE letter (Latin or Cyrillic) immediately followed
# by ")" — "a)", "e)", "г)", and the glued inline form "a)1)14sm,2)48sm". The
# strict "<one char>)" shape means inner numbering ("1)", "2)") never matches
# (those start with a DIGIT) and a word like "ab)" never matches (2nd char "b").
_MARKER_RE = re.compile(r'^([A-Za-zА-Яа-яЁё])\)')

# Question numbers sit at the LEFT text margin. We deliberately allow a number
# GLUED to a digit-starting stem ("2.4...", "4.2*8<...", "8.345...") — which the
# figure-crop _QNUM_WORD_RE rejects via its (?!\d) decimal guard — and instead
# disambiguate by POSITION: a real question number hugs the left edge, a mid-line
# "2.5" does not. (Own locator here; _QNUM_WORD_RE / figure-crop untouched.)
_LEFT_MARGIN_FRAC = 0.22   # a number candidate's x0 must be within this * width
_MIN_TEXT_LEN = 40         # total text below this ⇒ scan-only PDF ⇒ no-op


def _num_pat(n: int) -> re.Pattern:
    return re.compile(rf'^{n}[.)]')


def _locate_numbers(doc, qnums: list[int]) -> dict[int, tuple[int, float]]:
    """qnum -> (page_index, y0). Margin-aware, document-wide. The earliest-page,
    top-most left-margin candidate wins."""
    located: dict[int, tuple[int, float]] = {}
    for pi in range(len(doc)):
        page = doc[pi]
        margin_x = page.rect.width * _LEFT_MARGIN_FRAC
        words = page.get_text("words")
        for n in qnums:
            if n in located:
                continue
            pat = _num_pat(n)
            best_y = None
            for w in words:
                if w[0] > margin_x:            # not at the left margin
                    continue
                if pat.match(w[4].strip()):
                    if best_y is None or w[1] < best_y:
                        best_y = w[1]
            if best_y is not None:
                located[n] = (pi, best_y)
    return located


def _band_end(located: dict[int, tuple[int, float]], page_idx: int,
              y0: float, page_height: float) -> float:
    """Bottom of a question's region: the next located number below it on the
    SAME page, else the page bottom."""
    same = [y for (p, y) in located.values() if p == page_idx and y > y0 + 1]
    return min(same) if same else page_height


def _markers_in(page, y0: float, y1: float) -> list[str]:
    """Printed option-marker letters within [y0, y1), in reading order."""
    found: list[tuple[float, float, str]] = []
    for w in page.get_text("words"):
        cy = (w[1] + w[3]) / 2
        if not (y0 - 1 <= cy < y1):
            continue
        m = _MARKER_RE.match(w[4].strip())
        if m:
            found.append((w[1], w[0], m.group(1)))
    found.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in found]


def recover_pdf_option_labels(pdf_bytes: bytes | None,
                              questions: list[dict]) -> int:
    """Correct Gemini's option labels from the PDF text layer, in place.

    Returns the number of questions whose labels were confirmed/overwritten
    from the text layer. Sets q["label_doubt"] = True on every options-bearing
    question that could NOT be confirmed. No-op (returns 0) when there is no
    text layer or no PDF bytes.
    """
    if not pdf_bytes:
        return 0
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.warning("label_recovery_open_failed", error=str(e))
        return 0
    try:
        total_text = sum(len(doc[i].get_text().strip()) for i in range(len(doc)))
        if total_text < _MIN_TEXT_LEN:
            logger.info("label_recovery_skip_no_text_layer", chars=total_text)
            return 0

        opt_questions = [q for q in questions if q.get("options")]
        qnums = sorted({
            q["question_number"] for q in opt_questions
            if q.get("question_number")
        })
        located = _locate_numbers(doc, qnums)

        applied = 0
        flagged = 0
        for q in opt_questions:
            n = q.get("question_number")
            opts: dict = q["options"]
            if not n or n not in located:
                q["label_doubt"] = True
                flagged += 1
                logger.info("label_doubt", question=n, reason="not_located")
                continue

            pi, y0 = located[n]
            page = doc[pi]
            markers = _markers_in(page, y0, _band_end(located, pi, y0, page.rect.height))
            canon = [canonical_letter(m) for m in markers]

            clean_run = (
                markers
                and len(markers) == len(opts)
                and len(set(canon)) == len(markers)   # no duplicated labels
            )
            if not clean_run:
                q["label_doubt"] = True
                flagged += 1
                logger.info(
                    "label_doubt", question=n, reason="count_mismatch",
                    markers="".join(markers), gemini="".join(map(str, opts.keys())),
                )
                continue

            old_labels = list(opts.keys())
            texts = list(opts.values())
            new_opts = {markers[i]: texts[i] for i in range(len(markers))}
            # remap a correct-answer LABEL (if any) by position
            ca = q.get("correct_answer")
            if ca and ca in old_labels:
                q["correct_answer"] = markers[old_labels.index(ca)]
            if list(new_opts.keys()) != old_labels:
                logger.info(
                    "label_recovered", question=n,
                    old="".join(map(str, old_labels)), new="".join(markers),
                )
            q["options"] = new_opts
            applied += 1

        logger.info(
            "label_recovery_done",
            applied=applied, flagged=flagged, total=len(opt_questions),
        )
        return applied
    finally:
        doc.close()
