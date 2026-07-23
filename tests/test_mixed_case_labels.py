"""
Defect 4: a mixed-CASE option-label set ("a, b, D, e") is a typo at the source
that prints verbatim, so it raises the existing `label_doubt` advisory flag.

The boundary was MEASURED against all 679 stored options-JSON rows before the
rule was written: it fires on exactly one of the eleven distinct label sets that
really occur — the typo — and stays silent on every legitimate one. All four
non-firing shapes are pinned below so the rule can never be loosened silently.

NOTHING is rewritten. Verbatim storage is correct for gapped and Cyrillic label
sets, and the flag is advisory only.
"""
from app.services.option_label_recovery import flag_mixed_case_labels

# Cyrillic option letters by CODEPOINT, not literal: on screen Cyrillic "А"
# (U+0410) is INDISTINGUISHABLE from Latin "A" (U+0041), and the mixed-script
# case below turns entirely on telling the two apart. Spelling them numerically
# keeps that difference visible to whoever reads this file next.
_A, _BE, _VE, _GHE = chr(0x410), chr(0x411), chr(0x412), chr(0x413)


def _q(labels, n=14):
    """A question shaped like the pipeline's: options is {label: text}."""
    return {"question_number": n, "options": {s: f"opt {s}" for s in labels}}


# ── fires: the real typo ─────────────────────────────────────────────────────
def test_fires_on_lone_capital_among_lowercase():
    # the live DOCX case, verbatim from the DB: a) 10;  b) 9;  D) 8;  e) 11;
    q = {"question_number": 14,
         "options": {"a": "10;", "b": "9;", "D": "8;", "e": "11;"}}
    assert flag_mixed_case_labels([q]) == 1
    assert q["label_doubt"] is True


def test_flagging_never_rewrites_the_label():
    # ADVISORY ONLY — the odd capital "D" must survive untouched
    q = {"question_number": 14,
         "options": {"a": "10;", "b": "9;", "D": "8;", "e": "11;"}}
    flag_mixed_case_labels([q])
    assert list(q["options"]) == ["a", "b", "D", "e"]
    assert q["options"]["D"] == "8;"


# ── does NOT fire: every legitimate set that occurs in the stored rows ───────
def test_gapped_lowercase_does_not_fire():
    q = _q("abde")                 # 252 stored rows — the gap at "c" is normal
    assert flag_mixed_case_labels([q]) == 0
    assert not q.get("label_doubt")


def test_uppercase_latin_does_not_fire():
    q = _q("ABCD")                 # 86 stored rows
    assert flag_mixed_case_labels([q]) == 0
    assert not q.get("label_doubt")


def test_all_cyrillic_does_not_fire():
    q = _q([_A, _BE, _VE, _GHE])   # 72 stored rows — all uppercase Cyrillic
    assert flag_mixed_case_labels([q]) == 0
    assert not q.get("label_doubt")


def test_mixed_script_does_not_fire():
    # 8 stored rows: LATIN "A" + Cyrillic "БВГ". Deliberately NOT flagged — it
    # is visually identical in print and canonical_letter folds it to the same
    # sequence as the all-Cyrillic set, so neither teacher nor student can see
    # it. Flagging it would only train teachers to ignore the flag.
    mixed = _q(["A", _BE, _VE, _GHE])
    assert mixed["options"] != _q([_A, _BE, _VE, _GHE])["options"]  # truly differs
    assert flag_mixed_case_labels([mixed]) == 0
    assert not mixed.get("label_doubt")


# ── edge cases ───────────────────────────────────────────────────────────────
def test_uncased_labels_never_fire():
    q = _q("1234")                 # numeric markers are not cased at all
    assert flag_mixed_case_labels([q]) == 0


def test_question_without_options_does_not_fire():
    assert flag_mixed_case_labels([{"question_number": 3}]) == 0
    assert flag_mixed_case_labels([{"question_number": 3, "options": {}}]) == 0


def test_already_flagged_question_is_not_double_counted():
    # the PDF backstop got there first — same teacher action, count it once
    q = _q("abDe")
    q["label_doubt"] = True
    assert flag_mixed_case_labels([q]) == 0
    assert q["label_doubt"] is True


def test_counts_only_the_mixed_case_questions_in_a_batch():
    qs = [_q("abde", n=1), _q("abDe", n=2), _q("ABCD", n=3), _q("aB", n=4)]
    assert flag_mixed_case_labels(qs) == 2
    assert [q.get("label_doubt", False) for q in qs] == [False, True, False, True]
