"""
Pure grading functions for the manual answer-sheet checker.

No I/O, no Gemini, no DB — just the comparison and the grade scale. This is
THE single source of truth for the 5/4/3/2 grade; nothing else defines its own.

A question's key is a LIST of accepted answers; the student is correct if their
answer matches ANY item. A multiple-choice letter is just a one-item list
(["A"]), so letters and written short answers share one rule. A legacy scalar
key ({q: "A"}) is still accepted and behaves exactly as before.

Matching is done HERE, in Python — Gemini is never asked to judge correctness.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.services.option_letters import canonical_letter

# A PLAIN number: optional sign, digits, and ONE decimal separator (comma OR dot).
# A slash ("2/3"), a second comma ("1,2,3"), letters or spaces make it NOT plain,
# so fractions/ratios/lists/words stay literal text and match exactly.
_NUM_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


# ── CP22: a number written as a WORD ─────────────────────────────────────────
#
# A student who writes "besh" (or "беш", or "пять") where the key holds "5" used to
# be marked wrong: _as_number returned None for a word, so the Decimal branch in
# is_correct was never entered and the comparison fell through to exact match.
# Item 1's short-answer constraint made short numeric answers far more common,
# which moved this from an edge case to a real one.
#
# The tables are keyed on the NORMALIZED form (see normalize above): casefolded,
# whitespace-collapsed, with every Uzbek apostrophe spelling already folded to
# ASCII "'". So B-19's work is REUSED - "oʻn", "o‘n", "o’n" and "o'n" all arrive
# here as "o'n" and need one entry, not four.
#
# SCOPE is compositional over 0-9999 rather than a flat table, measured not
# guessed: 60 written answers sampled across kimyo 7 (uz), biologiya 6 (ru) and
# grammarway (en) held two bare numbers, both YEARS (1811, 1926). Nobody
# hand-writes a year as words, so what this actually serves is small numbers,
# and composition covers those plus anything larger for free.
#
# Uzbek needs BOTH scripts: sheet_reader deliberately preserves what the student
# wrote ("do NOT transliterate between scripts"), so Cyrillic-writing students
# reach this comparator in Cyrillic.
_NUMBER_WORDS: dict[str, int] = {
    # ---- Uzbek, Latin ----
    "nol": 0, "bir": 1, "ikki": 2, "uch": 3, "to'rt": 4, "besh": 5,
    "olti": 6, "yetti": 7, "etti": 7, "sakkiz": 8, "to'qqiz": 9,
    "o'n": 10, "yigirma": 20, "o'ttiz": 30, "qirq": 40, "ellik": 50,
    "oltmish": 60, "yetmish": 70, "sakson": 80, "to'qson": 90,
    # ---- Uzbek, Cyrillic ----
    "нол": 0, "бир": 1, "икки": 2, "уч": 3, "тўрт": 4, "беш": 5,
    "олти": 6, "етти": 7, "саккиз": 8, "тўққиз": 9,
    "ўн": 10, "йигирма": 20, "ўттиз": 30, "қирқ": 40, "эллик": 50,
    "олтмиш": 60, "етмиш": 70, "саксон": 80, "тўқсон": 90,
    # ---- Russian (feminine/neuter variants included: a spoken answer may
    #      agree with the noun it counts) ----
    "ноль": 0, "нуль": 0,
    "один": 1, "одна": 1, "одно": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
    # ---- English ----
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Multipliers rather than values. A bare scale word IS accepted as its own value
# ("yuz" -> 100), which is a DELIBERATE, documented trade:
#
#   Uzbek "yuz" means both "hundred" and "face", so a vocabulary answer "yuz"
#   will accept a student who writes "100". MEASURED before choosing this: zero
#   of the 905 stored option texts and zero stored written answers collide with
#   any number word in any of the three languages, so the collision is
#   theoretical. The alternative - refusing a standalone scale word - is WORSE:
#   a student writing "yuz" against the key "100" would then be SILENTLY wrong,
#   because similarity_ratio("yuz", "100") is 0.0 and written_confirm_needed
#   would not even ask the teacher. This direction can only misfire if a student
#   writes a DIGIT for a vocabulary word, which is not a thing students do.
_SCALE_WORDS: dict[str, int] = {
    "yuz": 100, "ming": 1000,
    "юз": 100, "минг": 1000,
    "сто": 100, "тысяча": 1000, "тысячи": 1000, "тысяч": 1000,
    "hundred": 100, "thousand": 1000,
}

# Joiners that carry no value ("one hundred AND five"). Ignored inside a number
# phrase; a string made only of these is not a number.
_NUMBER_FILLERS = frozenset({"and"})

# Hyphens join number words in English ("twenty-five") and Uzbek ordinals; split
# on them so each part is looked up separately. normalize() has already
# collapsed whitespace, so this is the only extra separator needed.
_NUMBER_SPLIT = re.compile(r"[\s\-‐‑‒–—]+")


def _words_to_number(s: str) -> Decimal | None:
    """Decimal value of a string that is ENTIRELY number words, else None.

    Compositional, the standard accumulate/multiply walk:
      * a value word adds into the current group ("yigirma" + "besh" -> 25);
      * a HUNDRED scale multiplies the current group ("bir yuz" -> 100);
      * a THOUSAND scale flushes the current group into the total
        ("ikki ming" -> 2000), so "тысяча девятьсот двадцать шесть" -> 1926.

    ANY unknown token makes the whole string not-a-number. That guard is what
    keeps a real short answer intact: "besh kitob" and "0,2 gramm" stay literal
    text and are still compared exactly, instead of being silently reduced to
    a number they are not.
    """
    if not s:
        return None
    tokens = [t for t in _NUMBER_SPLIT.split(s) if t]
    total = 0
    group = 0
    seen_value = False
    for token in tokens:
        if token in _NUMBER_FILLERS:
            continue
        if token in _NUMBER_WORDS:
            group += _NUMBER_WORDS[token]
            seen_value = True
            continue
        scale = _SCALE_WORDS.get(token)
        if scale is None:
            return None            # not a number phrase at all
        seen_value = True
        if scale >= 1000:
            total += (group or 1) * scale
            group = 0
        else:
            group = (group or 1) * scale
    if not seen_value:
        return None                # e.g. "and" on its own
    return Decimal(total + group)


def _as_number(s: str) -> Decimal | None:
    """Decimal value of a plain number OR of a number written as words.

    Used ONLY for the safe notation-equivalence in is_correct and for
    written_confirm_needed's "numbers always ask": when BOTH sides resolve to a
    number, compare their Decimal values so "2,3" == "2.3" == "2,30",
    "5" == "5.0", and (CP22) "besh" == "пять" == "5". Sign is meaning-bearing
    (Decimal(-5) != Decimal(5)), so -5 never equals 5. NOT a math evaluator: no
    fractions, no rounding policy.

    Digits take a fast path, so the overwhelmingly common case costs one regex
    and nothing else. `s` is expected ALREADY NORMALIZED (see normalize) - both
    call sites pass normalized text - which is what lets the word tables carry
    one entry per word instead of one per apostrophe spelling and letter case.
    """
    if _NUM_RE.match(s):
        try:
            return Decimal(s.replace(",", "."))
        except InvalidOperation:
            return None
    return _words_to_number(s)


def grade_for(percent: float) -> int:
    """The ONE grading scale (Uzbek 5-point). percent is 0..100.

    AUDIT B-18. The rounding happens HERE, not in the callers. It used to be
    done by two of the three call sites and not the third, so 24/28 = 85.714%
    was a ⭐5 in the manual flow and a ⭐4 in the saved flow: a student's
    official grade depended on which button the teacher pressed. Measured over
    every attainable score on tests up to 50 questions, 20 of 1325 (score,
    total) pairs graded differently — 6/7, 12/14, 18/21, 5/9, 12/17 and others.

    Standardising on the ROUNDED behaviour is deliberate:
      * it can only ever RAISE a grade relative to grading the exact percent
        (rounding crosses an integer threshold upward, never downward), so no
        student who earns a grade today gets a lower one after this;
      * it matches the percent the teacher is SHOWN — the manual report prints
        the rounded value, and displaying "86%" beside a ⭐4 is the one
        combination that cannot be explained to a student.

    The alternative — grade the exact percent and drop the callers' rounding —
    would have turned existing ⭐5s into ⭐4s on the manual path, which is a
    change to already-issued grades rather than a bug fix.
    """
    # HALF-UP, not Python's round(): the builtin is banker's rounding, so
    # round(70.5) == 70 while round(85.5) == 86 — the scale would tip a
    # different way at each threshold, which is indefensible when the number
    # decides a student's grade. 141/200 = 70.5% is attainable, so this is not
    # hypothetical. Half-up can only round a .5 case UP, so it too never
    # lowers a grade relative to the old behaviour.
    percent = int(Decimal(str(percent)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if percent >= 86:
        return 5
    if percent >= 71:
        return 4
    if percent >= 56:
        return 3
    return 2


# ── Answer matching ──────────────────────────────────────────────────────────

def accepted_list(value: Any) -> list[str]:
    """A question's accepted answers, always as a list. A legacy scalar key
    value is treated as a one-item list, so old callers keep working."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


# AUDIT B-19. Every character Unicode offers for "apostrophe", folded to one.
# Uzbek Latin needs this constantly (oʻzbek / o'zbek / o‘zbek / oʼzbek are the
# SAME word), and the two sides of a comparison come from different keyboards:
# the sheet reader is told to transcribe EXACTLY as written, while a
# book-generated key carries whatever the typesetter used. MEASURED: 42 of 49
# apostrophe-variant pairs failed to match before this.
_APOSTROPHES = "‘’ʼʻ`´′"
_APOS_MAP = {ord(c): "'" for c in _APOSTROPHES}


def normalize(value: Any) -> str:
    """
    Case-insensitive + whitespace-collapsed + apostrophe/unicode-folded.

    Deliberately no punctuation stripping beyond that: "-5" must not equal "5"
    and "x=5" must stay intact (meaning-bearing symbols survive — same rule the
    standalone probe validated, and consistent with the ban on regex-on-math).

    NFC first (AUDIT B-19, found by sweeping rather than cited): "café" typed
    as e + U+0301 and as U+00E9 are the same word and must grade the same.
    Then every apostrophe spelling folds to ASCII "'", so only the SPELLING of
    a separator is normalised — never a character that carries meaning.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    return " ".join(text.translate(_APOS_MAP).split()).casefold()


# ── A decimal separator inside a PHRASE ──────────────────────────────────────
#
# "0,2 gramm" vs "0.2 gramm" used to grade WRONG. The bare-number case already
# worked (_as_number handles "2,3" == "2.3"), but a number sitting inside a
# phrase never reaches that path: the string is not plain digits, and "gramm" is
# not a number word, so both sides return None and the comparison falls through
# to exact match, which differs by one character.
#
# SEVERITY, measured and worth stating: 1 occurrence in 100 post-fix generated
# written answers, and ZERO across all 1,169 real strings in the DB (264 question
# texts, 905 option texts, every stored answer). It is also NOT silently wrong -
# similarity is 0.889, well over CONFIRM_SIMILARITY_THRESHOLD, so
# written_confirm_needed already sends it to the teacher. This removes a tap; it
# does not rescue a lost mark.
#
# That is exactly why the rule is narrow. A false ACCEPT here would be SILENT and
# unreviewable - strictly worse than the behaviour being replaced - so this
# compares STRINGS, never values, and relaxes nothing except the separator
# character itself. Same digits, same words, same units, same order, or no match.
#
# NOT folded into _as_number, deliberately: that maps a whole string to a VALUE,
# and teaching it to pull a number out of a phrase would make "0,2 gramm" equal a
# bare "0.2" - a real false accept, since one names a unit and the other does not.

# A separator that is genuinely INSIDE a number: a digit on both sides. A date
# ("23.oktyabr"), an ordinal ("23-oktyabr") or a sentence stop has a non-digit on
# one side and is never touched.
_DIGIT_SEP = re.compile(r"(?<=\d)[.,](?=\d)")

# ...unless exactly three digits follow with no further digit. That is the
# universal signature of a THOUSANDS separator, and "1,000" vs "1.000" denote
# DIFFERENT values under different conventions, so such a string is refused
# outright rather than guessed at.
#
# MEASURED cost of that guard: it fires on one real stored answer, "proton
# massasi 1,673 * 10-24 g", where 1,673 genuinely IS a decimal. Refusing keeps
# the existing teacher review for that case. A reviewed false-negative beats a
# silent false-accept, which is the whole trade this rule is built around.
_GROUP_SEP = re.compile(r"(?<=\d)[.,](?=\d{3}(?!\d))")


def _fold_decimal_separators(value: str) -> str | None:
    """`value` with every digit-internal separator rewritten to ".", or None if
    the string carries a thousands-group separator and is therefore ambiguous.

    Pure string surgery: it rewrites separators, it never deletes one (so "2,5"
    never becomes "25") and it never moves, reorders or drops anything else.
    """
    if _GROUP_SEP.search(value):
        return None
    return _DIGIT_SEP.sub(".", value)


def is_correct(student: Any, accepted: list[str]) -> bool:
    """True if the student's answer matches ANY accepted answer.

    Exact (casefold + whitespace) match first. If that fails and BOTH sides are
    SINGLE letters, they match on their canonical form, so a Latin letter and its
    Cyrillic look-alike grade equal (Cyrillic "В" == Latin "B"). The fold happens
    HERE, at comparison time, so the key and the read can each STORE the real
    label the teacher/student actually used — the report shows "В", not "B", and
    a genuine Б-vs-В difference stays visible instead of being hidden by storage.
    Applied to BOTH sides, so it can never make two DIFFERENT options equal:
    Б folds to Б, В folds to B — they stay distinct.

    If that fails and BOTH sides are plain numbers, they match on Decimal value —
    so comma/dot decimals and trailing zeros are equal ("2,3" == "2.3" == "2,30",
    "5" == "5.0"). Anything that isn't a plain number (fractions "2/3", lists
    "1000 g, 400 g, 600 g", words, "-5" vs "5") stays exact-match-only.
    """
    s = normalize(student)
    if not s:
        return False
    s_num = _as_number(s)
    s_letter = canonical_letter(s).casefold() if len(s) == 1 else None
    # Computed once: the folded form of the student answer, and whether it
    # actually carried a decimal separator at all.
    s_fold = _fold_decimal_separators(s)
    s_has_sep = _DIGIT_SEP.search(s) is not None
    for a in accepted:
        a_norm = normalize(a)
        if s == a_norm:
            return True
        if s_letter is not None and len(a_norm) == 1:
            if s_letter == canonical_letter(a_norm).casefold():
                return True
        if s_num is not None:
            a_num = _as_number(a_norm)
            if a_num is not None and s_num == a_num:
                return True
        # LAST, and only once exact match has already failed: identical except
        # for a decimal separator inside a number. Gated on a separator being
        # PRESENT on one side or the other, so with no separator anywhere this
        # reduces to the exact check above and can never widen it.
        if s_fold is not None and (s_has_sep or _DIGIT_SEP.search(a_norm)):
            a_fold = _fold_decimal_separators(a_norm)
            if a_fold is not None and s_fold == a_fold:
                return True
    return False


# ── Confirm-worthiness of a WRONG written answer ──────────────────────────────
#
# A written answer that isn't is_correct has TWO possible causes the system
# can't otherwise tell apart: the student really wrote the wrong thing, OR the
# camera/AI misread a correct one. So the teacher is asked To'g'ri/Xato. But a
# far-off answer ("APPLE" vs key "BANAN") can't be a misread of the key, so it
# is a genuine wrong the teacher need not confirm. This decides which band a
# wrong answer is in, using normalized Levenshtein similarity.
#
# THRESHOLD justified by the real stored answer distribution (do not move
# without re-measuring — the test pins the split): an empty band separates
# plausible misreads (similarity >= 0.50: BANONA/BANANA, BRVAN/BANAN, 12/13)
# from genuine wrongs (similarity <= 0.30: "R"/"12", mangled gibberish). 0.40
# sits in that gap, below the 0.50 misread floor with margin. Lower = more
# different. BIASED TOWARD ASKING: a false ask costs one tap; a false
# auto-reject silently mis-grades a misread.
CONFIRM_SIMILARITY_THRESHOLD = 0.40


def _levenshtein(a: str, b: str) -> int:
    """Edit distance (insert/delete/substitute each cost 1)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity_ratio(a: str, b: str) -> float:
    """Normalized Levenshtein similarity in [0, 1]; 1.0 = identical."""
    m = max(len(a), len(b))
    return 1.0 if m == 0 else 1.0 - _levenshtein(a, b) / m


def written_confirm_needed(student: Any, accepted: list[str]) -> bool:
    """True = ask the teacher To'g'ri/Xato (close, possibly a misread);
    False = far enough to mark wrong without asking.

    Call ONLY for answers is_correct already rejected. Two rules, both biased
    toward ASKING:
      * NUMBERS ALWAYS ASK — if the student answer and ANY accepted answer are
        both plain numbers, return True unconditionally. A digit misread
        ("12" vs "120") can look far by magnitude yet be a plausible camera
        error, so a numeric mismatch is never auto-rejected.
      * Otherwise ask when the similarity to the CLOSEST accepted answer is
        >= CONFIRM_SIMILARITY_THRESHOLD; only skip the ask when EVERY accepted
        answer is far.
    """
    s = normalize(student)
    if not s:
        return False  # blank isn't a written answer (never in texts); defensive
    s_num = _as_number(s)
    best = 0.0
    for a in accepted:
        a_norm = normalize(a)
        if s_num is not None and _as_number(a_norm) is not None:
            return True  # numeric vs numeric → always ask
        best = max(best, similarity_ratio(s, a_norm))
    return best >= CONFIRM_SIMILARITY_THRESHOLD


def _display(accepted: list[str]) -> str:
    """How the key is shown in the wrong-answer report ('PHONE / TELEPHONE')."""
    return " / ".join(accepted)


def compare(student: dict[int, str], key: dict[int, str]) -> dict[str, Any]:
    """
    Compare a student's read answers against the correct key.

    Args:
      student — {question_number: "A".."D"} confidently-read answers.
      key     — {question_number: "A".."D"} correct answers (defines the total
                and which questions count).
      Note: questions the reader marked ambiguous are passed separately via the
      caller's `unclear` list and injected here through `student` being absent
      for them; see the handler. `compare` treats any question in `key` that is
      missing from `student` as wrong (unanswered).

    Returns:
      {
        "score":  int,   # correct count
        "total":  int,   # len(key)
        "wrong":  [{"q": n, "student": s|None, "correct": c}],  # definite wrongs
        "unclear": [n],  # ambiguous questions (subset of wrong total)
      }

    `unclear` is populated by the caller passing ambiguous question numbers; a
    question listed as unclear is EXCLUDED from `wrong` detail but still counts
    against the score. See `compare_with_unclear` for the combined entry point.
    """
    return compare_with_unclear(student, key, unclear=[])


def compare_with_unclear(
    student: dict[int, str],
    key: dict[int, str],
    unclear: list[int],
) -> dict[str, Any]:
    """
    Full comparison. `unclear` = questions the sheet-reader could not read
    confidently ("?"). An unclear question counts as WRONG (not correct) but is
    reported separately so the teacher can grade it by hand — it never appears
    in the `wrong` detail list.
    """
    total = len(key)
    unclear_set = {q for q in unclear if q in key}

    score = 0
    wrong: list[dict[str, Any]] = []
    for q in sorted(key):
        accepted = accepted_list(key[q])
        if q in unclear_set:
            continue  # counts against score (not added to `score`), listed apart
        s = student.get(q)
        if is_correct(s, accepted):
            score += 1
        else:
            wrong.append({"q": q, "student": s, "correct": _display(accepted)})

    return {
        "score": score,
        "total": total,
        "wrong": wrong,
        "unclear": sorted(unclear_set),
    }
