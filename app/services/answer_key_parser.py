"""
Answer-key parser for the manual "Javob orqali tekshirish" flow.

Turns a teacher's free-typed answer key into {question_number: [accepted, ...]}.
EVERY answer is a LIST of accepted strings — a multiple-choice letter is simply
a one-item list (["A"]) — so ONE matching rule covers both kinds and a test may
freely mix multiple-choice and written short answers.

Accepted shapes (freely mixable across lines):
  * labelled  — "1A 2B 3C 4D", any separators ("1) A, 2) B. 3-C")
  * bare      — "ABCDABCD" (or "abcd abcd"): letters numbered from 1
  * written   — "5: TOSHKENT"                        (one accepted answer)
  * multi     — "22: PHONE / TELEPHONE / SMARTPHONE" (ANY of these is correct)
  * one-line  — "1: banan 2: apple 3: peach": several written answers on ONE
                line, split ONLY when the numbers are strictly consecutive.

A written line is one whose FIRST token is `<number>:`. Within it, a new answer
starts at `<number>:` that is NOT immediately followed by a digit — so a ratio
"2:3", a time "14:30", or a scale "1:100" stays INSIDE the current answer and is
never treated as a new question. If a one-line split is not confident (numbers
not consecutive, or an empty segment), we REJECT with a clear message rather
than guess — never silently drop or mis-parse an answer.

The teacher's REAL label is stored, in the script they typed: a Cyrillic "В"
stays "В" and is reported back as "В", never as Latin "B". Cross-script matching
(Cyrillic "В" == Latin "B") happens at COMPARISON time in checker.is_correct,
which folds both sides — so storing the real label costs nothing in matching and
keeps a genuine Б-vs-В difference visible instead of hiding it behind one symbol.
Words are never transliterated either — Cyrillic "ТОШКЕНТ" stays Cyrillic.
"""
from __future__ import annotations

import re

from app.services.option_letters import CYRILLIC_OPTION_LETTERS

# A labelled token: a question number followed by ONE letter, e.g. "12A",
# "12) A", "12 - a", "12Б". Separators are optional. Matches ANY single letter
# (Latin or Cyrillic); option-membership is validated downstream where the
# test's real options are known (upload/photo flow), never by a fixed set here.
_LABELLED_RE = re.compile(r"(\d+)\s*[).\-:]?\s*([^\W\d_])", re.UNICODE)

# A WRITTEN line starts with "<number>:" (that routes it to the written path).
_STARTS_WRITTEN = re.compile(r"^\s*(\d+)\s*:")
# An INTERNAL new-answer boundary inside a written line: whitespace, a number,
# a colon that is NOT immediately followed by a digit. The `(?!\d)` is the whole
# ratio/time/scale guard — "2:3" / "14:30" / "1:100" never start a new answer.
_BOUNDARY_RE = re.compile(r"\s+(\d+)\s*:(?!\d)")
# A leftover (letter-path) line that still hides a written entry ("1A 2B 3: cat")
# — number, colon, then 2+ non-digit/non-space chars = a word. Warn, never drop.
_WRITTEN_ON_LETTER_LINE = re.compile(r"\d+\s*:\s*[^\d\s]{2,}")

# BUG A (silent loss): a written NUMERIC answer typed WITHOUT a colon —
# "19 8,23", "19- 1/2", "19-8,23", "19. 8,23", "19) 8,23" — used to fall through
# to the letter parser (its value "8,23" has no letter) and be dropped. Detect a
# single leading number, a space/dash/dot/paren separator, then a value whose
# FIRST char is a DIGIT (a written numeric answer — never an MC letter, whose
# value would be a letter), and normalise it to colon form so the tested written
# path handles it. Letter answers ("19 a", "1-A 2-B"), colon lines and a bare
# "19-" skip are left exactly as typed.
_WRITTEN_NONCOLON = re.compile(r"^(\s*)(\d+)[\s.\-)]+(\d.*?)\s*$")

# BUG A (word truncation): a written WORD typed with a non-colon separator —
# "13.Temurbek", "13-Temurbek", "13)Temurbek" — used to hit the letter parser,
# whose single-letter capture truncated it to "13T" and then rejected it. Detect
# a single leading number, a dot/dash/paren separator, then a SINGLE multi-char
# token whose first char is a LETTER (2+ chars, no internal space → not a bare
# option letter like "13.F", not a multi-answer line, not a numeric). Bare SPACE
# is deliberately EXCLUDED here so "1 A" stays a letter answer and a spaced
# numeric like "20 8,23" is handled by _WRITTEN_NONCOLON, never mis-split.
_WRITTEN_NONCOLON_WORD = re.compile(r"^(\s*)(\d+)[.\-)]\s*([^\W\d]\S+)\s*$", re.UNICODE)


def _to_colon_written(text: str) -> str:
    """Rewrite non-colon written answers (numeric OR single word) to
    '<num>: <value>' so the tested written path handles them. Idempotent — colon
    lines and letter runs ("1A 2B") match neither pattern and pass through."""
    out: list[str] = []
    for line in text.splitlines():
        m = _WRITTEN_NONCOLON.match(line) or _WRITTEN_NONCOLON_WORD.match(line)
        out.append(
            f"{m.group(1)}{m.group(2)}: {m.group(3).strip()}" if m else line
        )
    return "\n".join(out)

_MAX_ITEM = 100  # matches CheckResult.student_name / display_name width

# Trilingual "put each answer on its own line" — the ONLY per-lang message
# (existing reasons stay Uzbek; their tests pin Uzbek substrings).
_AMBIGUOUS = {
    "uz": ("Bir qatordagi bir nechta javobni aniq ajratib bo'lmadi (raqamlar "
           "ketma-ket emas). Har bir javobni ALOHIDA QATORGA yozing. Masalan:\n"
           "5: TOSHKENT\n6: SMARTFON"),
    "en": ("Couldn't reliably separate multiple answers on one line (the numbers "
           "aren't consecutive). Put each answer on its OWN LINE, e.g.:\n"
           "5: TOSHKENT\n6: SMARTPHONE"),
    "ru": ("Не удалось надёжно разделить несколько ответов в одной строке "
           "(номера не по порядку). Пишите каждый ответ на ОТДЕЛЬНОЙ СТРОКЕ, "
           "напр.:\n5: TOSHKENT\n6: SMARTFON"),
}


def _norm_item(s: str) -> str:
    """
    One accepted answer: upper-cased + whitespace-collapsed, capped.

    Cyrillic is folded ONLY when the whole item is a single letter (a
    multiple-choice answer). Multi-character answers keep their script exactly.
    """
    s = " ".join(s.split()).upper()
    # The REAL label is stored, never the folded form: a teacher who typed
    # Cyrillic "В" must see "В" in the report, not "B". Cross-script matching is
    # done at COMPARISON time by checker.is_correct, which folds both sides — so
    # storing the real label costs nothing in matching and keeps a genuine
    # Б-vs-В difference visible instead of hiding it behind a shared symbol.
    return s[:_MAX_ITEM]


def _parse_letters(text: str) -> tuple[dict[int, str], str]:
    """LEGACY letter parsing — rules unchanged. Labelled wins over bare.

    Case is normalised but the SCRIPT is never converted: a Cyrillic "В" stays
    "В" (see _norm_item). Matching folds both sides at comparison time.
    """
    upper = text.strip().upper()

    # ── Shape 1: labelled "1A 2B ..." — wins so "1A 2B" is never misread as a
    # bare run of letters.
    labelled = _LABELLED_RE.findall(upper)
    if labelled:
        # Accept ANY single letter as a candidate option label — do NOT gate on a
        # hardcoded A–E set (that wrongly rejected F and gapped-set letters). The
        # manual flow has no stored options to validate against, so it accepts any
        # letter and lets grading decide; the upload/photo flow re-validates each
        # letter against the question's REAL options downstream (_resolve_saved_key).
        key = {int(num_s): letter for num_s, letter in labelled}
        if not key:
            return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C"
        return key, ""

    # ── Shape 2: bare "ABCDABCD" — letters only, numbered from 1. Any Latin
    # letter is accepted (option-membership is re-checked downstream where the
    # test's real options are known); no hardcoded A–E wall here. Cyrillic option
    # letters are KEPT too — stripping to [A-Z] silently dropped a bare "АБВГ"
    # run's Б and Г (they have no Latin look-alike to fold to).
    letters_only = re.sub(r"[^A-Z" + CYRILLIC_OPTION_LETTERS + r"]", "", upper)
    if not letters_only:
        return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C yoki ABCD"

    return {i + 1: c for i, c in enumerate(letters_only)}, ""


def _items(segment: str) -> list[str]:
    """One answer segment → accepted list.

    Multi-accept splits ONLY on a SPACED slash (" / "), matching the documented
    "PHONE / TELEPHONE" format. A bare slash inside a token — a fraction "1/2", a
    ratio "a/b" — is NOT a separator, so the answer stays a single literal value
    (this is the Bug-A fix: "1/2" used to silently become ["1","2"]). An item that
    is only punctuation/slashes (no alphanumerics) is dropped, so "5: /" is still
    an EMPTY answer and gets rejected rather than stored as "/"."""
    parts = re.split(r"\s+/\s+", segment)
    return [
        i for i in (_norm_item(p) for p in parts)
        if i and any(ch.isalnum() for ch in i)
    ]


def _split_written_line(line: str) -> tuple[dict[int, list[str]] | None, str]:
    """
    Parse ONE written line (starts with `<number>:`) into {num: [accepted, ...]}.

    Returns (entries, status):
      * status "ok"        → entries is the parsed dict.
      * status "ambiguous" → a one-line multi-answer split that isn't confident
                             (numbers not consecutive). entries is None.
      * status "empty"     → some question's answer segment was blank. None.

    A new answer starts only at a `<number>:` NOT immediately followed by a digit
    (so ratios/times/scales stay inside the current answer). Multiple entries are
    accepted ONLY when their numbers are strictly consecutive.
    """
    first = _STARTS_WRITTEN.match(line)
    # Boundaries: the leading header, then every internal non-ratio "<num>:".
    heads: list[tuple[int, int, int]] = [  # (question_number, hdr_start, hdr_end)
        (int(first.group(1)), first.start(), first.end())
    ]
    for m in _BOUNDARY_RE.finditer(line):
        if m.start(1) > first.end():          # strictly after the first header
            heads.append((int(m.group(1)), m.start(), m.end()))

    # Slice each answer from its header-end to the next header-start.
    entries: list[tuple[int, str]] = []
    for i, (num, _hs, he) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(line)
        entries.append((num, line[he:end].strip()))

    if len(entries) > 1:
        nums = [n for n, _ in entries]
        if nums != list(range(nums[0], nums[0] + len(nums))):
            return None, "ambiguous"          # not strictly consecutive → don't guess

    out: dict[int, list[str]] = {}
    for num, seg in entries:
        items = _items(seg)
        if not items:
            return None, "empty"
        out[num] = items
    return out, "ok"


def parse_answer_key(text: str, lang: str = "uz") -> tuple[dict[int, list[str]], str]:
    """
    Parse a typed answer key.

    Returns (key, reason):
      * key    — {question_number: ["ACCEPTED", ...]} (1-indexed), {} on failure.
                 A letter answer is a one-item list.
      * reason — "" on success, else a short human-readable explanation. Only the
                 one-line ambiguity message is translated (`lang`); other reasons
                 are Uzbek.
    """
    if not text or not text.strip():
        return {}, "Javob kaliti bo'sh."

    # Normalise non-colon numeric written answers ("19 8,23") to colon form so a
    # teacher who separates with a space/dash/dot/paren doesn't silently lose them.
    text = _to_colon_written(text)

    key: dict[int, list[str]] = {}
    leftover: list[str] = []

    # A line starting with "<number>:" is a WRITTEN line (single or one-line
    # multi). Everything else falls through to the legacy letter parser.
    for line in text.splitlines():
        if not line.strip():
            continue
        if _STARTS_WRITTEN.match(line):
            entries, status = _split_written_line(line)
            if status == "ambiguous":
                return {}, _AMBIGUOUS.get(lang, _AMBIGUOUS["uz"])
            if status == "empty":
                return {}, "Har bir savol uchun javob yozing (bo'sh javob bor)."
            key.update(entries)
        else:
            leftover.append(line)

    if leftover:
        joined = "\n".join(leftover)
        # A letter-path line that still hides a written entry ("1A 2B 3: cat")
        # would silently drop the word — warn instead.
        if _WRITTEN_ON_LETTER_LINE.search(joined):
            return {}, _AMBIGUOUS.get(lang, _AMBIGUOUS["uz"])
        letters, reason = _parse_letters(joined)
        if reason:
            return {}, reason
        for num, letter in letters.items():
            key.setdefault(num, [letter])

    if not key:
        return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C yoki 5: TOSHKENT"
    return key, ""
