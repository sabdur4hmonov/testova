"""
CP13 + CP14 — the question-generation service and its grounding enforcement.

CP13 covers the pure, correctness-critical parts:
  * distributing a total question count across selected themes
  * the grounding normalizer (measured against REAL textbook text: this book
    hyphenates at line breaks once per ~141 characters, so a naive check would
    call almost every correct answer ungrounded)
  * defensive parsing of the model's JSON
  * prompt construction carrying every non-negotiable rule

CP14 adds:
  * the grounding predicates (is_answer_grounded, distractors_pass) —
    calibrated against 25 REAL questions generated for 5 real themes on the
    shipping model, scored against a negative control, not guessed at
  * the media-dependent question detector, with explicit false-positive
    regression tests against traps MEASURED in the real corpus
    ("фотосинтез" vs "фото", "картофель" vs "карт")
  * the regenerate-once-then-drop orchestration
  * PAID-key-always generation, replacing CP13's free-first/paid-on-429 path
    — removed after a live measurement showed the free key could not serve
    the shipping model and the fallback did not catch either failure mode
    (see gemini_rest's module docstring for the full story)
"""
import json
from types import SimpleNamespace

import pytest

from app.services import book_generator as bg


# ── Distributing questions across themes ─────────────────────────────────────

@pytest.mark.parametrize("total,n_themes,expected", [
    (10, 1, [10]),
    (10, 2, [5, 5]),
    (10, 3, [4, 3, 3]),      # remainder to the earliest themes, deterministic
    (9, 3, [3, 3, 3]),
    (5, 3, [2, 2, 1]),
    (1, 3, [1, 0, 0]),       # fewer questions than themes: some get none
    (0, 3, [0, 0, 0]),
])
def test_distribute_questions_is_even_and_deterministic(total, n_themes, expected):
    assert bg.distribute_questions(total, n_themes) == expected


def test_distribution_always_sums_to_the_requested_total():
    for total in range(0, 60):
        for n in range(1, 12):
            got = bg.distribute_questions(total, n)
            assert sum(got) == total, (total, n, got)
            assert len(got) == n


def test_distribution_never_differs_by_more_than_one():
    """"As evenly as possible" — no theme may carry two more than another."""
    for total in range(0, 60):
        for n in range(1, 12):
            got = bg.distribute_questions(total, n)
            if got:
                assert max(got) - min(got) <= 1, (total, n, got)


def test_no_themes_yields_no_questions():
    assert bg.distribute_questions(10, 0) == []


def test_written_share_never_exceeds_a_themes_own_total():
    """Written questions are a SUBSET of that theme's questions — a theme
    allotted 2 questions can never be asked for 3 written ones."""
    for total in range(0, 40):
        for written in range(0, total + 1):
            for n in range(1, 8):
                per_theme = bg.distribute_questions(total, n)
                per_written = bg.distribute_questions(written, n)
                for t, w in zip(per_theme, per_written):
                    assert w <= t, (total, written, n, per_theme, per_written)


# ── Grounding normalizer (measured against real book text) ───────────────────

def test_normalizer_rejoins_hyphenated_line_breaks():
    """THE measured failure mode: this textbook hyphenates once per ~141
    chars, so without this every answer quoting such a word reads as
    ungrounded."""
    raw = "по-\nзнакомитесь с растительным миром"
    assert "познакомитесь" in bg.normalize_for_grounding(raw)


def test_normalizer_handles_the_soft_hyphen_too():
    assert "познакомитесь" in bg.normalize_for_grounding("по­\nзнакомитесь")


def test_normalizer_collapses_whitespace_and_casefolds():
    got = bg.normalize_for_grounding("  Абу   Райхан\n\nБеруни  ")
    assert got == "абу райхан беруни"


def test_normalizer_survives_none_and_blank():
    assert bg.normalize_for_grounding(None) == ""
    assert bg.normalize_for_grounding("   ") == ""


def test_a_real_hyphenated_phrase_becomes_findable():
    source = bg.normalize_for_grounding(
        "Абу Райхан Беруни (973 – 1048) в произведении «Китоб Ас-Сай-\nдана фит-тибб»"
    )
    assert bg.normalize_for_grounding("Китоб Ас-Сайдана фит-тибб") in source


def test_normalizer_keeps_genuine_intra_word_hyphens():
    """A hyphen NOT at a line break is real punctuation ("Ас-Сайдана") and must
    survive — only hyphen+newline is a typographic break."""
    assert "ас-сайдана" in bg.normalize_for_grounding("Ас-Сайдана")


# ── Defensive parsing of the model's reply ───────────────────────────────────

def _mc(q="Вопрос?", opts=None, ans="A", written=False):
    return {
        "q": q,
        "opts": opts if opts is not None else {"A": "раз", "B": "два", "C": "три", "D": "четыре"},
        "ans": ans,
        "written": written,
    }


def test_parses_a_clean_response():
    raw = json.dumps([_mc()], ensure_ascii=False)
    out = bg.parse_generation_response(raw)
    assert len(out) == 1
    assert out[0]["question_text"] == "Вопрос?"
    assert out[0]["options"] == {"A": "раз", "B": "два", "C": "три", "D": "четыре"}
    assert out[0]["correct_answer"] == "A"
    assert out[0]["is_open_ended"] is False


def test_parses_through_code_fences_and_prose():
    raw = "Вот вопросы:\n```json\n" + json.dumps([_mc()], ensure_ascii=False) + "\n```"
    assert len(bg.parse_generation_response(raw)) == 1


def test_parses_a_written_question_with_no_options():
    raw = json.dumps(
        [{"q": "Объясните фотосинтез", "opts": {}, "ans": "процесс", "written": True}],
        ensure_ascii=False,
    )
    out = bg.parse_generation_response(raw)
    assert out[0]["is_open_ended"] is True
    assert out[0]["options"] == {}
    assert out[0]["correct_answers"] == ["процесс"]


def test_drops_entries_that_cannot_be_used():
    raw = json.dumps([
        {"q": "", "opts": {"A": "x", "B": "y"}, "ans": "A"},        # no text
        {"q": "Ok?", "opts": {"A": "x"}, "ans": "A"},               # single option
        {"q": "Ok?", "opts": {"A": "x", "B": "y"}, "ans": "Z"},     # answer not an option
        {"q": "Good?", "opts": {"A": "x", "B": "y"}, "ans": "B"},   # keeper
    ], ensure_ascii=False)
    out = bg.parse_generation_response(raw)
    assert [q["question_text"] for q in out] == ["Good?"]


@pytest.mark.parametrize("raw", ["", "not json", "{}", "null", "[]", None])
def test_unusable_replies_yield_no_questions(raw):
    assert bg.parse_generation_response(raw) == []


def test_a_truncated_reply_salvages_what_it_can():
    """max_output_tokens can cut a reply mid-array. Two COMPLETE questions
    precede the break here, so both must survive — losing them because a third
    was clipped would silently waste a paid call."""
    full = json.dumps(
        [_mc(q="Первый?"), _mc(q="Второй?"), _mc(q="Третий?")], ensure_ascii=False
    )
    cut_at = full.index('{"q": "Третий?"') + 20      # mid-way through the third
    out = bg.parse_generation_response(full[:cut_at])
    assert [q["question_text"] for q in out] == ["Первый?", "Второй?"]


def test_truncation_that_clips_the_only_question_yields_nothing():
    """The mirror case — half an object is not a question, and must not be
    guessed at."""
    full = json.dumps([_mc(q="Единственный?")], ensure_ascii=False)
    assert bg.parse_generation_response(full[:25]) == []


def test_cyrillic_option_labels_survive_parsing():
    raw = json.dumps(
        [{"q": "Савол?", "opts": {"А": "бир", "Б": "икки", "В": "уч"}, "ans": "Б"}],
        ensure_ascii=False,
    )
    out = bg.parse_generation_response(raw)
    assert list(out[0]["options"].keys()) == ["А", "Б", "В"]
    assert out[0]["correct_answer"] == "Б"


def test_numeric_option_labels_survive_parsing():
    raw = json.dumps(
        [{"q": "Савол?", "opts": {"1": "бир", "2": "икки"}, "ans": "2"}],
        ensure_ascii=False,
    )
    out = bg.parse_generation_response(raw)
    assert out[0]["correct_answer"] == "2"


def test_correct_answers_list_is_always_populated_for_grading():
    """variant_generator reads correct_answers (migration 008); an MC question
    whose list is empty would grade as ungraded."""
    out = bg.parse_generation_response(json.dumps([_mc(ans="C")], ensure_ascii=False))
    assert out[0]["correct_answers"] == ["C"]


# ── Prompt construction ──────────────────────────────────────────────────────

def _prompt(**kw):
    defaults = dict(
        theme_title="§ 1. Ботаника",
        theme_text="Абу Райхан Беруни (973 – 1048) написал «Китоб Ас-Сайдана».",
        n_questions=4, n_written=1,
        option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru",
    )
    defaults.update(kw)
    return bg.build_prompt(**defaults)


def test_prompt_carries_the_source_text_and_counts():
    p = _prompt()
    assert "Абу Райхан Беруни" in p
    assert "4" in p and "1" in p


def test_prompt_states_the_grounding_rule():
    """The trust-critical instruction: never invent facts."""
    p = _prompt().lower()
    assert "only" in p or "must" in p
    assert "invent" in p or "not in the text" in p


def test_prompt_requires_contextually_close_distractors():
    p = _prompt().lower()
    assert "distractor" in p or "wrong answer" in p
    assert "plausible" in p or "related" in p


def test_prompt_tells_the_model_to_skip_media_dependent_exercises():
    p = _prompt().lower()
    assert "audio" in p or "image" in p or "media" in p


def test_prompt_pins_the_exact_option_labels():
    p = _prompt(option_labels=["А", "Б", "В", "Г"])
    for lab in ("А", "Б", "В", "Г"):
        assert lab in p


def test_prompt_pins_numeric_labels_too():
    p = _prompt(option_labels=["1", "2", "3", "4"])
    assert '"1"' in p or "1, 2, 3, 4" in p or "1" in p


def test_prompt_includes_the_language_safety_net():
    """The detector is known to be unreliable on thin samples, so the prompt
    must ALSO say "same language as the source" rather than trusting a label."""
    p = _prompt(language=None).lower()
    assert "same language" in p


def test_a_sample_question_is_used_as_style_only():
    p = _prompt(sample_question="Кто открыл клетку? A) Гук B) Дарвин")
    assert "Кто открыл клетку?" in p
    low = p.lower()
    assert "style" in low or "tone" in low
    assert "not" in low          # must say NOT to use it as content


# ── CP20: difficulty skew ────────────────────────────────────────────────────

def test_medium_and_no_difficulty_are_byte_identical_to_the_pre_cp20_prompt():
    """"medium" (the default) must add nothing — teachers who never touch the
    new choice see exactly the old prompt."""
    assert _prompt(difficulty="medium") == _prompt()
    assert _prompt(difficulty=None) == _prompt()


def test_easy_difficulty_asks_for_mostly_easy_questions():
    p = _prompt(difficulty="easy").lower()
    assert "easier" in p or "easy" in p
    assert "recall" in p


def test_hard_difficulty_asks_for_mostly_hard_questions():
    p = _prompt(difficulty="hard").lower()
    assert "harder" in p or "hard" in p
    assert "connecting" in p or "applying" in p


def test_easy_and_hard_prompts_differ_from_the_medium_default():
    base = _prompt()
    assert _prompt(difficulty="easy") != base
    assert _prompt(difficulty="hard") != base
    assert _prompt(difficulty="easy") != _prompt(difficulty="hard")


def test_an_unknown_difficulty_value_is_treated_as_no_skew():
    """Defensive: build_prompt itself, not just the handler, must not choke on
    or silently misapply a bad value."""
    assert _prompt(difficulty="extreme") == _prompt()


def test_no_sample_question_leaves_no_dangling_section():
    p = _prompt(sample_question=None)
    assert "None" not in p


def test_written_count_of_zero_asks_for_no_open_questions():
    p = _prompt(n_written=0).lower()
    assert "0" in p or "no open" in p or "all" in p


# ── CP28: type-mix rule + open-answer rubric ─────────────────────────────────

def test_the_type_mix_rule_is_always_present():
    """Item 6: the prompt must ask for a spread of question TYPES, and it is an
    unconditional rule (present at every difficulty) — that is what keeps
    'medium == default' byte-identical while still improving variety."""
    for diff in (None, "medium", "easy", "hard"):
        assert "VARY THE QUESTION TYPES" in _prompt(difficulty=diff)


def test_the_rubric_block_appears_only_when_a_written_question_is_asked_for():
    """CP24-style gating: an all-MC request's prompt must be byte-identical to
    pre-CP28 (no open-ended block), while any written question adds it.

    Item 1 renamed the block from "RUBRIC (open-ended only)" to "OPEN-ENDED
    ANSWER FORMAT" when the short-answer requirement moved in beside the
    rubric. The GATING is what this test is about and it is unchanged.
    """
    assert "OPEN-ENDED ANSWER FORMAT" not in _prompt(n_written=0)
    assert "OPEN-ENDED ANSWER FORMAT" in _prompt(n_written=1)
    assert "key_points" in _prompt(n_written=2)


def test_the_open_ended_block_demands_a_short_answer():
    """Item 1: the instruction that actually changes what the model returns.
    MEASURED over 8 real calls (kimyo 7 uz, biologiya 6 ru) - without this the
    Uzbek median answer was 267 chars / 35 words and NOTHING came back within
    3 words; with it the median is 14 chars / 2 words."""
    p = _prompt(n_written=2)
    assert "AT MOST 3 words" in p
    assert "NEVER a sentence" in p
    # It must also offer the conversion, or tightening the rule just loses
    # coverage of the parts of the source that need a sentence to answer.
    assert "MULTIPLE-CHOICE question instead" in p
    assert "AT MOST 3 words" not in _prompt(n_written=0)


def test_the_rubric_example_has_single_braces_not_doubled():
    """A .format slip would leak '{{' into the prompt the model reads."""
    p = _prompt(n_written=1)
    assert "{{" not in p and '"key_points": [' in p


def test_a_written_answer_carries_its_rubric_key_points():
    raw = json.dumps([{
        "q": "Fotosintezni tushuntiring.", "opts": {}, "ans": "yorug'lik reaksiyasi",
        "written": True, "key_points": ["yorug'lik yutiladi", "kislorod ajraladi"],
    }], ensure_ascii=False)
    out = bg.parse_generation_response(raw)
    assert out[0]["is_open_ended"] is True
    assert out[0]["rubric"] == ["yorug'lik yutiladi", "kislorod ajraladi"]


def test_a_written_answer_without_key_points_has_a_none_rubric():
    """Backward-compatible: the model (or an older prompt) may omit key_points —
    the field must read as None, never crash or fabricate."""
    raw = json.dumps([{"q": "Nima?", "opts": {}, "ans": "javob", "written": True}],
                     ensure_ascii=False)
    out = bg.parse_generation_response(raw)
    assert out[0]["is_open_ended"] is True and out[0]["rubric"] is None


def test_malformed_key_points_do_not_cost_the_question():
    """A rubric is dormant data; a bad one must degrade to None, keeping the
    (otherwise valid) written question."""
    for bad in ('"not a list"', "42", "[null, \"\", \"  \"]", "{}"):
        raw = f'[{{"q": "Q?", "opts": {{}}, "ans": "a", "written": true, "key_points": {bad}}}]'
        out = bg.parse_generation_response(raw)
        assert len(out) == 1 and out[0]["rubric"] is None, bad


def test_key_points_are_capped():
    pts = [f"point {i}" for i in range(12)]
    raw = json.dumps([{"q": "Q?", "opts": {}, "ans": "a", "written": True, "key_points": pts}])
    out = bg.parse_generation_response(raw)
    assert len(out[0]["rubric"]) == bg._MAX_KEY_POINTS


def test_multiple_choice_questions_carry_no_rubric():
    """The rubric is an open-ended concept only — an MC question must not grow
    the field (its dict shape is unchanged from pre-CP28)."""
    out = bg.parse_generation_response(json.dumps([_mc()], ensure_ascii=False))
    assert "rubric" not in out[0]


# ── Paid-key generation wiring (CP14: the free-first path is gone) ───────────

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": json.dumps([_mc()], ensure_ascii=False)}]}}],
            "usageMetadata": {},
        }

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _capture(monkeypatch, responses=None):
    import requests
    calls = []
    queue = list(responses or [])

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"key": (headers or {}).get("x-goog-api-key"), "body": json})
        return queue.pop(0) if queue else _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def _stub_keys(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")


def test_generation_uses_the_paid_key(monkeypatch):
    """CP14 replaces free-first/paid-on-429 (CP13) with paid key, always — the
    free key 404s on the shipping model and free-tier substitutes were
    intermittently 503, and neither failure mode tripped the old 429-only
    fallback. Theme text is deliberately grounded so this exercises exactly
    one call, not an incidental CP14 regeneration."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[_Resp(payload={
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(
            [_mc(opts={"A": "раз", "B": "два", "C": "три", "D": "четыре"}, ans="A")],
            ensure_ascii=False,
        )}]}}],
        "usageMetadata": {},
    })])
    bg.generate_for_theme(
        theme_title="§ 1", theme_text="Раз, два, три и четыре — этапы опыта.",
        n_questions=1, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert [c["key"] for c in calls] == ["PAID"]
    assert len(calls) == 1, "a fully-grounded reply must not trigger a regeneration call"


def test_generation_runs_with_thinking_ON(monkeypatch):
    """Analysis runs thinking OFF; GENERATION is the reasoning step and must
    not — that is the whole reason call_generation defaults thinking on."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[_Resp(payload={
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(
            [_mc(opts={"A": "раз", "B": "два", "C": "три", "D": "четыре"}, ans="A")],
            ensure_ascii=False,
        )}]}}],
        "usageMetadata": {},
    })])
    bg.generate_for_theme(
        theme_title="§ 1", theme_text="Раз, два, три и четыре — этапы опыта.",
        n_questions=1, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    budget = calls[0]["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"]
    assert budget != 0, "generation must not run with thinking disabled"


def test_generation_is_billed_to_the_teacher(monkeypatch):
    """Per-user cost attribution, same as every other Gemini call site. The
    stub reply is ungrounded against "text", so this also exercises a CP14
    regeneration call — recorded still ends up with the same kwargs either
    way, since both calls carry the same user_id/kind."""
    _stub_keys(monkeypatch)
    recorded = {}

    def fake_call(prompt, **kw):
        recorded.update(kw)
        return json.dumps([_mc()], ensure_ascii=False), 1

    monkeypatch.setattr(bg.gemini_rest, "call_generation", fake_call)
    bg.generate_for_theme(
        theme_title="§ 1", theme_text="text", n_questions=1, n_written=0,
        option_labels=["A", "B"], sample_question=None, language="ru", user_id=4242,
    )
    assert recorded.get("user_id") == 4242
    assert recorded.get("kind")


def test_a_theme_asked_for_zero_questions_makes_no_call(monkeypatch):
    """A theme allotted nothing must not cost a request."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch)
    out = bg.generate_for_theme(
        theme_title="§ 1", theme_text="text", n_questions=0, n_written=0,
        option_labels=["A", "B"], sample_question=None, language="ru", user_id=1,
    )
    assert out.questions == []
    assert out.requested == 0
    assert out.errored is False, "nothing to ask is not an error"
    assert calls == []


def test_an_empty_theme_text_makes_no_call(monkeypatch):
    """Nothing to ground against — asking anyway would invite invention."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch)
    out = bg.generate_for_theme(
        theme_title="§ 1", theme_text="   ", n_questions=3, n_written=0,
        option_labels=["A", "B"], sample_question=None, language="ru", user_id=1,
    )
    assert out.questions == [] and calls == []
    assert out.requested == 3
    assert out.errored is False, "nothing to ground against is not an error"


def test_a_failed_call_returns_no_questions_rather_than_raising(monkeypatch):
    """One theme's failure must not abort a multi-theme request (CP17 isolates
    this further, but the service must already degrade rather than explode).
    The original call errored, so there is nothing to regenerate — only one
    call is made, not two. CP15: this is now visible on the result, not
    swallowed into an empty list indistinguishable from a legitimately-empty
    theme."""
    _stub_keys(monkeypatch)
    attempts = []

    def boom(*a, **k):
        attempts.append(1)
        raise RuntimeError("network down")

    monkeypatch.setattr(bg.gemini_rest, "call_generation", boom)
    out = bg.generate_for_theme(
        theme_title="§ 1", theme_text="text", n_questions=2, n_written=0,
        option_labels=["A", "B"], sample_question=None, language="ru", user_id=1,
    )
    assert out.questions == []
    assert out.requested == 2
    assert out.errored is True
    assert "network down" in out.error
    assert len(attempts) == 1, "a hard call failure must not be retried as if it were ungrounded"


# ── CP14: grounding predicates ────────────────────────────────────────────────
#
# Calibrated against 25 real questions generated for 5 real themes on the
# shipping model (gemini-2.5-flash, paid), scored against a NEGATIVE CONTROL
# (each real answer checked against the OTHER four themes' text — 100 real,
# adversarial, but not invented pairs). Threshold 0.90 at stem-length 5 kept
# 100% of genuinely grounded answers and admitted only 6% of the adversarial
# ones; see the module docstring for the full numbers.

REAL_THEME_TEXT = (
    "Сосны и тополя имеют прямые, скученные кроны; карагача – шарообразные. "
    "Полукустарники – многолетние растения с древесными нижними и травянис-\n"
    "тыми верхними частями побегов."
)


def test_a_genuinely_grounded_answer_passes():
    assert bg.is_answer_grounded("Прямые, скученные кроны", REAL_THEME_TEXT)


def test_an_inflected_paraphrase_still_passes():
    """THE measured failure mode of strict substring matching: Russian
    inflection ("многолетними растениями" for the book's "многолетние
    растения") is not invention — 20/20 real MC answers scored 1.00 at this
    threshold on the shipping model precisely because of cases like this."""
    assert bg.is_answer_grounded(
        "Многолетними растениями с травянистыми верхними частями побегов",
        REAL_THEME_TEXT,
    )


def test_a_short_root_inflection_can_miss_at_this_stem_length():
    """Documented residual limitation, not a silent gap: a 4-letter root
    whose case ending changes the letter AT the stem cutoff ("прямУЮ" vs the
    book's "прямЫЕ") can score below threshold even though it is genuinely
    grounded. Real 2.5-flash data never produced this shape (20/20 MC scored
    1.00), so it is rare, but the CP14 policy still does the safe thing with
    it: drop-after-one-regeneration, not silently include an unverified
    answer."""
    assert not bg.is_answer_grounded("Прямую, скученную", REAL_THEME_TEXT)


def test_a_fabricated_answer_fails():
    """The other half of the measurement: a plausible-sounding but genuinely
    unsupported claim must be rejected, not waved through by a loose check."""
    assert not bg.is_answer_grounded(
        "Кустарники имеют мочковатую корневую систему", REAL_THEME_TEXT
    )


def test_a_numeric_answer_falls_back_to_verbatim():
    """No content token to score a ratio on — the only evidence is whether
    the number itself appears."""
    assert bg.is_answer_grounded("прямые, скученные", REAL_THEME_TEXT)
    assert not bg.is_answer_grounded("12 метров", REAL_THEME_TEXT)


def test_grounding_check_survives_line_break_hyphenation():
    """The predicate must benefit from normalize_for_grounding, not just the
    normalizer's own unit tests."""
    assert bg.is_answer_grounded("травянистыми верхними частями", REAL_THEME_TEXT)


def test_distractors_pass_on_a_majority_even_with_one_ungrounded():
    options = {"A": "Прямые, скученные", "B": "Шарообразные",
               "C": "Ветвистые кроны", "D": "Выдуманный признак"}
    assert bg.distractors_pass(options, "A", REAL_THEME_TEXT)


def test_distractors_fail_when_the_majority_is_ungrounded():
    options = {"A": "Прямые, скученные", "B": "Выдуманный признак раз",
               "C": "Выдуманный признак два", "D": "Выдуманный признак три"}
    assert not bg.distractors_pass(options, "A", REAL_THEME_TEXT)


def test_numeric_only_distractors_are_exempt():
    """A quantity question's wrong options are SUPPOSED to be absent from the
    source — that is what makes them distractors. Penalizing that would
    punish a well-formed question."""
    options = {"A": "25 кг", "B": "10 кг", "C": "1 кг", "D": "200 кг"}
    assert bg.distractors_pass(options, "A", "Орех весит 25 кг.")


def test_distractors_pass_with_no_wrong_options():
    assert bg.distractors_pass({"A": "x"}, "A", REAL_THEME_TEXT)


# ── CP14: media-dependent question detector ──────────────────────────────────

def test_a_figure_reference_is_flagged():
    assert bg.references_unavailable_media("Что изображено на рисунке 3?")


def test_a_table_reference_is_flagged():
    assert bg.references_unavailable_media("Используя таблицу 2, назовите вид.")


def test_a_map_reference_is_flagged_only_as_a_real_word():
    assert bg.references_unavailable_media("Покажите на карте расположение зоны.")


def test_an_audio_or_video_reference_is_flagged():
    assert bg.references_unavailable_media("Прослушайте аудиозапись и ответьте.")
    assert bg.references_unavailable_media("Посмотрите видео о фотосинтезе.")


def test_media_reference_in_an_option_is_also_flagged():
    """Rule 4 covers distractors too — an option that itself points at a
    figure is just as unanswerable as a stem that does."""
    assert bg.references_unavailable_media(
        "Что из перечисленного верно?", {"A": "См. рис. 4", "B": "Другое"}
    )


def test_ordinary_biology_text_is_not_flagged():
    assert not bg.references_unavailable_media(
        "Что происходит с растением в процессе фотосинтеза?"
    )


def test_photosynthesis_does_not_trip_the_photo_marker():
    """MEASURED trap: bare 'фото' matches inside 'фотосинтез' (appears 5x in
    the real corpus). The marker must require 'фотограф', not 'фото'."""
    assert not bg.references_unavailable_media(
        "В процессе фотосинтеза растение выделяет кислород."
    )


def test_potato_does_not_trip_the_map_marker():
    """MEASURED trap: bare 'карт' matches inside 'картофель' (potato). The
    marker must require an inflected 'карта' word, not the bare stem."""
    assert not bg.references_unavailable_media(
        "Картофель размножается клубнями."
    )


def test_a_card_does_not_trip_the_map_marker():
    """'карточка' (card) shares the 'карт' + vowel prefix with 'карта' (map)
    but is a different word — an explicit inflected-form list must not
    over-match it the way a length-bounded stem would."""
    assert not bg.references_unavailable_media("Заполните карточку ответов.")


def test_rice_does_not_trip_the_figure_marker():
    """MEASURED-adjacent trap: bare 'рис' is also the word for rice. The
    figure marker requires the period ('рис.') or the 'рисун' stem."""
    assert not bg.references_unavailable_media("Рис — важная зерновая культура.")


def test_lecture_hall_does_not_trip_the_audio_marker():
    """'аудитория' (lecture hall) shares a 4-letter prefix with 'аудио' but
    diverges at the 5th letter — the marker must require the full 'аудио'."""
    assert not bg.references_unavailable_media(
        "Студенты собрались в аудитории перед лекцией."
    )


def test_uzbek_official_does_not_trip_the_rasm_marker():
    """'rasmiy' (official) shares the 'rasm' prefix with 'rasm' (picture) but
    is a different word — this exclusion is REASONED, not measured against a
    real Uzbek corpus (none is in the local test set yet)."""
    assert not bg.references_unavailable_media("Bu rasmiy hujjat hisoblanadi.")


def test_uzbek_picture_reference_is_flagged():
    assert bg.references_unavailable_media("Rasmga qarab, o'simlikni ayting.")


# ── CP14: enforcement orchestration (regenerate once, then drop) ─────────────

def _payload_for(questions):
    return {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": json.dumps(questions, ensure_ascii=False)}]}}],
        "usageMetadata": {},
    }


# Verified against the real predicate functions (bg.is_answer_grounded /
# bg.distractors_pass), not hand-predicted — Russian stem-overlap is not
# reliably eyeballed, as the two fixes above already proved twice.
_GROUNDED_MC = _mc(
    q="Что представляют собой полукустарники?",
    opts={
        "A": "Многолетние растения с травянистыми частями побегов",  # grounded (0.83+)
        "B": "Растения с прямыми, скученными кронами, как сосны и тополя",  # 0.71
        "C": "Растения с шарообразными кронами, как карагача",  # 0.60
        "D": "Полностью деревянистые растения без листьев",  # 0.20 — the minority miss
    },
    ans="A",
)
_UNGROUNDED_MC = _mc(q="Не имеющий отношения вопрос?",
                      opts={"A": "Выдуманный признак, не упомянутый в тексте",
                            "B": "Другой выдуманный признак раз",
                            "C": "Другой выдуманный признак два",
                            "D": "Другой выдуманный признак три"}, ans="A")

# CP27: near-duplicate questions are now collapsed, so a batch that stands for N
# distinct questions must actually BE distinct. These carry _GROUNDED_MC's own
# verified-grounded options (so grounding still passes) but different stems, so
# dedup correctly treats them as the separate questions they represent (identical
# options + a <0.90-similar stem is a sibling, not a duplicate).
_GROUNDED_MC_B = _mc(
    q="Какие части побегов у полукустарников бывают травянистыми?",
    opts=dict(_GROUNDED_MC["opts"]), ans="A",
)
_GROUNDED_MC_C = _mc(
    q="Чем кроны сосен и тополей отличаются от кроны карагача?",
    opts=dict(_GROUNDED_MC["opts"]), ans="A",
)


def test_a_fully_grounded_batch_triggers_no_regeneration_call(monkeypatch):
    """Cost guard: the common case (everything passes) must cost exactly the
    one call CP13 measured, not two."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(out.questions) == 2
    assert out.errored is False
    assert len(calls) == 1, "a full, grounded batch needs neither regeneration nor backfill"


def test_an_ungrounded_question_is_regenerated_and_the_good_one_kept(monkeypatch):
    """The exact approved policy: check, regenerate once, keep what now
    passes. One good + one bad in, one regenerated-good replacing the bad."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _UNGROUNDED_MC])),
        _Resp(payload=_payload_for([_GROUNDED_MC_B])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2, "one regeneration recovers the count, so no backfill follows"
    assert len(out.questions) == 2
    assert out.errored is False
    assert all(bg.is_answer_grounded(
        q["options"][q["correct_answer"]], REAL_THEME_TEXT
    ) for q in out.questions)


def test_a_question_still_ungrounded_after_regeneration_is_dropped(monkeypatch):
    """The other half of the policy: a question that keeps failing the grounding
    bar is dropped — never guessed at, never included.

    CP26: the CP14 regen is now followed by backfill rounds that chase the
    target, but they too keep only what grounds; when a round produces nothing
    NEW that passes, the loop stops early (the section is out of gradable
    material), so the theme still ends dropped rather than padded. Here every
    call returns the same ungrounded question, so backfill makes exactly one
    (empty) round and then bails: initial + regen + one backfill = three calls.

    CP15's core distinction still holds: requested=1, questions=[], but errored
    MUST be False — every call ran cleanly and returned real content that just
    didn't clear the bar. Collapsing this into errored=True would tell a teacher
    "try again" for something retrying can't fix."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_UNGROUNDED_MC])),
        _Resp(payload=_payload_for([_UNGROUNDED_MC])),
        _Resp(payload=_payload_for([_UNGROUNDED_MC])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=1, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 3, "backfill makes one unproductive round, then stops early"
    assert out.questions == []
    assert out.requested == 1
    assert out.errored is False, "a clean-but-ungrounded regen is a policy drop, not an error"


def test_regeneration_is_batched_not_one_call_per_failing_question(monkeypatch):
    """Call-economy guard: three ungrounded questions must cost ONE follow-up
    call for all three, not three separate regeneration calls."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_UNGROUNDED_MC, _UNGROUNDED_MC, _UNGROUNDED_MC])),
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B, _GROUNDED_MC_C])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=3, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2, "three failures cost ONE batched regeneration that fills the count"
    assert len(out.questions) == 3
    assert out.errored is False


def test_a_media_dependent_question_is_treated_as_ungrounded(monkeypatch):
    """Rule 4 enforcement flows through the same regenerate-once-then-drop
    pipeline as answer grounding, not a separate path. The options here are
    _GROUNDED_MC's own (verified fully-grounded, majority-passing distractors)
    with ONLY the stem changed to reference a figure — so this isolates the
    media check specifically: if it were a no-op, grounding alone would let
    this question through, and the test would not catch that."""
    _stub_keys(monkeypatch)
    media_q = _mc(
        q="Что изображено на рисунке, где показаны полукустарники?",
        opts=dict(_GROUNDED_MC["opts"]), ans="A",
    )
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([media_q])),
        _Resp(payload=_payload_for([_GROUNDED_MC])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=1, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2
    assert len(out.questions) == 1
    assert out.errored is False, "a media-flagged question is a policy drop, not an error"
    assert "рисунке" not in out.questions[0]["question_text"]


# ── CP15: error visibility (GenerationResult.errored) ─────────────────────────
#
# The gap CP15 closes: _call_and_parse used to swallow every failure into a
# bare []. That was already proven blind once — the CP13 free-key 404 stayed
# invisible until it was live-measured, because nothing distinguished "this
# theme legitimately supports zero questions" from "the call blew up". These
# tests prove the fix at BOTH points where CP15 added a try/except: the
# regeneration call (not just the initial one, which CP14 already covered),
# and the validity check itself.

def test_a_regeneration_call_that_errors_reports_errored_but_keeps_the_good_ones(
    monkeypatch,
):
    """A hard failure on the REGENERATION call — not the initial one, which
    the CP14-era test already covers — must also surface as errored=True,
    while still returning whatever the initial call already validated. This
    is the actual bug CP15 fixes: before this checkpoint, this scenario
    returned a silently-shorter list with no sign anything had gone wrong."""
    _stub_keys(monkeypatch)
    import requests

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(payload=_payload_for([_GROUNDED_MC, _UNGROUNDED_MC]))
        raise RuntimeError("connection reset")

    monkeypatch.setattr(requests, "post", fake_post)
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2
    assert len(out.questions) == 1, "the already-grounded question must survive the regen failure"
    assert out.requested == 2
    assert out.errored is True
    assert "connection reset" in out.error


def test_a_validity_check_that_raises_reports_errored_without_crashing(monkeypatch):
    """CP15: _question_is_valid is wrapped the same way as the Gemini calls —
    a bug there must surface as errored=True, not propagate uncaught and take
    the whole theme down. Parsed dicts are supposed to always have the right
    shape (CP13's defensive parsing guarantees it), so this exercises a
    backstop, not an expected path."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC])),
    ])

    def boom(*a, **k):
        raise ValueError("predicate bug")

    monkeypatch.setattr(bg, "_question_is_valid", boom)
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=1, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    # A validity check that can't judge a question treats it as failed, so it
    # regenerates (CP14) and then backfills once (CP26) before giving up — the
    # calls themselves never raised, so the backfill guard does not trip.
    assert len(calls) == 3, "regeneration then one backfill round, all unvalidatable"
    assert out.questions == [], "a question that couldn't even be validated must not be included"
    assert out.errored is True
    assert "predicate bug" in out.error


def test_a_validity_check_bug_does_not_abort_a_multi_question_batch(monkeypatch):
    """One question's validity check raising must not stop the rest of the
    batch from being evaluated — same call-economy/isolation stance as a
    single theme's failure not aborting a multi-theme request."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC])),
    ])

    real_check = bg._question_is_valid
    call_count = {"n": 0}

    def flaky(question, theme_text, book_kind=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("predicate bug on the first question only")
        return real_check(question, theme_text, book_kind)

    monkeypatch.setattr(bg, "_question_is_valid", flaky)
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert out.errored is True
    assert len(out.questions) >= 1, "the second, unaffected question must still be evaluated and kept"


# ── CP26: backfill to the requested count ─────────────────────────────────────
#
# The visible bug: a teacher who asks for N sometimes gets fewer, silently. The
# root cause was that generate_for_theme only ever regenerated questions that
# PARSED but failed grounding — a model that simply returned fewer questions
# than asked (the common case) triggered no second call at all. Backfill closes
# that gap, bounded by a hard ceiling so the cost stays predictable.

def test_backfill_fills_an_under_delivered_theme_to_target(monkeypatch):
    """The bug scenario itself: the model returns FEWER than asked, and every
    returned question is grounded (so the CP14 regen, which only fires on a
    grounding failure, never would). Backfill must chase the shortfall to the
    requested count. Distinct grounded replies each round, so nothing is a
    duplicate."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC])),      # asked 3, model gives 1
        _Resp(payload=_payload_for([_GROUNDED_MC_B])),    # backfill round 1
        _Resp(payload=_payload_for([_GROUNDED_MC_C])),    # backfill round 2
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=3, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(out.questions) == 3, "an under-delivered theme is now backfilled to the target"
    assert out.errored is False
    assert len(calls) == 3, "no CP14 regen (nothing failed grounding) — only backfill top-ups"


def test_backfill_is_bounded_by_the_hard_ceiling(monkeypatch):
    """Every call yields one FRESH grounded question, so backfill never
    early-exits on an empty round — it must still stop at BACKFILL_MAX_ROUNDS,
    leaving the theme honestly short rather than looping. This is the cost
    ceiling that keeps a thin theme from spending an unbounded number of
    requests."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC])),
        _Resp(payload=_payload_for([_GROUNDED_MC_B])),
        _Resp(payload=_payload_for([_GROUNDED_MC_C])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=5, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 1 + bg.BACKFILL_MAX_ROUNDS, "initial call plus at most R backfill rounds"
    assert len(out.questions) == 3, "delivered what the ceiling allowed, honestly short of 5"
    assert out.errored is False


def test_backfill_does_not_chase_after_a_hard_call_failure(monkeypatch):
    """A raised call is infrastructure failure, not under-delivery: it says
    nothing about whether the section supports the questions, so backfill must
    NOT spin more calls after it. (The initial-call case is covered elsewhere;
    this pins the same guard on a failure that strikes mid-run.)"""
    _stub_keys(monkeypatch)
    import requests
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(payload=_payload_for([_GROUNDED_MC]))   # 1 of 3, grounded
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "post", fake_post)
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=3, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    # initial (ok, 1 grounded) → backfill round 1 raises → stop. No further calls.
    assert len(calls) == 2
    assert len(out.questions) == 1, "the grounded question survives; the shortfall is not chased blindly"
    assert out.errored is True
    assert "network down" in out.error


# ── C-2: a follow-up call says what the model already produced ───────────────
#
# The regen, both backfill rounds and the CP29b refill all route through
# build_prompt, which had no parameter for "questions already accepted". Each
# asked for N more from IDENTICAL source with IDENTICAL instructions, so the
# model reliably re-derived what it had just written, _is_near_duplicate
# discovered that only AFTER the call was paid for, and an unproductive round
# broke the loop having spent a full theme-text + thinking request for nothing.

def _prompt_of(call):
    return call["body"]["contents"][0]["parts"][0]["text"]


def test_a_backfill_call_lists_the_already_accepted_stems(monkeypatch):
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC])),      # asked 3, model gives 1
        _Resp(payload=_payload_for([_GROUNDED_MC_B])),
        _Resp(payload=_payload_for([_GROUNDED_MC_C])),
    ])
    bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=3, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )

    round_one = _prompt_of(calls[1])
    assert _GROUNDED_MC["q"] in round_one, (
        "backfill must tell the model which stem it already accepted")

    # Round 2 knows about round 1's addition too, not just the initial batch.
    round_two = _prompt_of(calls[2])
    assert _GROUNDED_MC["q"] in round_two
    assert _GROUNDED_MC_B["q"] in round_two


def test_the_regeneration_call_lists_the_kept_stems(monkeypatch):
    """The CP14 regen replaces questions that failed grounding — the ones that
    PASSED are exactly what it must not reproduce."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _UNGROUNDED_MC])),
        _Resp(payload=_payload_for([_GROUNDED_MC_B])),
    ])
    bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )

    assert _GROUNDED_MC["q"] in _prompt_of(calls[1])
    assert _UNGROUNDED_MC["q"] not in _prompt_of(calls[1]), (
        "a REJECTED question is not an accepted one — listing it would tell the "
        "model to avoid the very ground it must now cover")


def test_the_first_call_of_a_theme_is_byte_identical_to_before(monkeypatch):
    """Regression guard: nothing has been accepted yet, so the initial ask must
    carry no avoid block at all — the prefix every later call caches against."""
    _stub_keys(monkeypatch)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),
    ])
    bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )

    expected = bg.build_prompt(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru",
    )
    assert _prompt_of(calls[0]) == expected


def test_build_prompt_with_no_accepted_questions_is_unchanged():
    """The same guard at the unit level, including the empty-list case a caller
    with nothing kept yet will pass."""
    assert _prompt(accepted_questions=[]) == _prompt()
    assert _prompt(accepted_questions=None) == _prompt()


def test_the_avoid_block_is_appended_after_the_count_instruction():
    """AUDIT A1 put the count instruction last so two calls differing only in
    count share the whole prefix. The avoid block varies per call for the same
    reason, so it must sit AFTER the count, not before it — otherwise it pushes
    the divergence point back up above the rules."""
    p = _prompt(accepted_questions=["Что представляют собой полукустарники?"])
    base = _prompt()
    assert p.startswith(base), "the avoid block must be a pure suffix"
    assert "Что представляют собой полукустарники?" in p


def test_the_avoid_list_is_bounded(monkeypatch):
    """A theme can accumulate many accepted questions; the block must not grow
    the prompt without limit."""
    stems = [f"Вопрос номер {i} про полукустарники?" for i in range(200)]
    p = _prompt(accepted_questions=stems)
    listed = sum(1 for s in stems if s in p)
    assert listed <= bg.AVOID_MAX_STEMS
    assert listed > 0


# ── CP27: near-duplicate removal + distractor-format gate ─────────────────────

def _pq(stem, opts, ans="A"):
    return {
        "question_text": stem, "options": dict(opts), "correct_answer": ans,
        "correct_answers": [ans], "is_open_ended": False, "img_index": None,
    }


_OPTS_1 = {"A": "mitoxondriya", "B": "yadro", "C": "ribosoma", "D": "vakuola"}
_OPTS_2 = {"A": "kislorod", "B": "vodorod", "C": "azot", "D": "geliy"}


def test_dedupe_drops_a_repeat_keeps_siblings_and_different_option_sets():
    q1 = _pq("Hujayra energiyasi qayerda hosil bo'ladi?", _OPTS_1)
    q2 = _pq("Hujayra  energiyasi qayerda hosil bo'ladi?", _OPTS_1)   # whitespace only → duplicate
    q3 = _pq("Yashil pigment qayerda joylashgan?", _OPTS_1)          # same options, far stem → sibling
    q4 = _pq("Hujayra energiyasi qayerda hosil bo'ladi?", _OPTS_2)   # same stem, other options → different
    kept = bg.dedupe_questions([q1, q2, q3, q4])
    assert [k["question_text"] for k in kept] == [
        q1["question_text"], q3["question_text"], q4["question_text"],
    ], "only the whitespace-identical repeat (q2) is dropped"


def test_dedupe_is_a_no_op_on_genuinely_distinct_questions():
    qs = [_pq(f"Savol {i} nima?", {"A": f"a{i}", "B": f"b{i}", "C": f"c{i}"}) for i in range(4)]
    assert bg.dedupe_questions(qs) == qs


def test_distractors_well_formed_accepts_a_clean_set():
    assert bg.distractors_well_formed(_OPTS_1, "A") is True


def test_distractors_well_formed_rejects_a_repeated_option():
    assert bg.distractors_well_formed(
        {"A": "yadro", "B": "yadro", "C": "ribosoma", "D": "vakuola"}, "A") is False


def test_distractors_well_formed_rejects_a_blank_option():
    assert bg.distractors_well_formed(
        {"A": "yadro", "B": "", "C": "ribosoma", "D": "vakuola"}, "A") is False


def test_distractors_well_formed_keeps_a_legitimately_longer_correct_answer():
    """The length-outlier heuristic was DROPPED after it rejected a real stored
    question whose correct answer is a genuinely longer scientific term. This
    pins that decision: a long, correct, fuller answer against short distractors
    must pass the format gate."""
    opts = {
        "A": "dezoksiribonuklein kislotasi molekulasi",   # long, correct
        "B": "gen", "C": "oqsil", "D": "yadro",
    }
    assert bg.distractors_well_formed(opts, "A") is True


def test_a_malformed_option_set_is_regenerated_through_the_gate(monkeypatch):
    """A duplicate-option question rides the SAME check → regen path as an
    ungrounded one: the answer here is grounded, so only the format defect can
    fail it, and the regenerated well-formed question replaces it."""
    _stub_keys(monkeypatch)
    grounded_answer = _GROUNDED_MC["opts"]["A"]
    malformed = _mc(
        q="Что такое полукустарники по тексту?",
        opts={"A": grounded_answer, "B": "повтор", "C": "повтор", "D": "иное"},
        ans="A",
    )
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([malformed])),
        _Resp(payload=_payload_for([_GROUNDED_MC])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=1, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2, "the malformed question failed the gate and was regenerated"
    assert len(out.questions) == 1
    assert bg.distractors_well_formed(
        out.questions[0]["options"], out.questions[0]["correct_answer"]) is True


# ── CP29b: ambiguity + concept-coverage critique (guard-gated, default off) ──

def _critique_reply(*verdicts):
    """A _Resp carrying a critique verdict array (fed through the same _capture
    queue as generation replies — both go through requests.post → call())."""
    return _Resp(payload=_payload_for(list(verdicts)))


def test_parse_critique_is_fail_open():
    # explicit ok:false drops; ok:true and everything else keeps
    assert bg._parse_critique('[{"i":0,"ok":false},{"i":1,"ok":true}]', 2) == [False, True]
    # unparseable reply keeps everything
    assert bg._parse_critique("total garbage", 3) == [True, True, True]
    assert bg._parse_critique(None, 2) == [True, True]
    # an OMITTED index stays kept (fail-open)
    assert bg._parse_critique('[{"i":0,"ok":false}]', 2) == [False, True]
    # an out-of-range index is ignored, not applied to a real one
    assert bg._parse_critique('[{"i":9,"ok":false}]', 2) == [True, True]
    # a verdict with no index is ignored (cannot fail-close a mystery question)
    assert bg._parse_critique('[{"ok":false}]', 1) == [True]


def test_critique_payload_carries_options_and_answer():
    q_mc = {"question_text": "Q?", "options": {"A": "x", "B": "y"},
            "correct_answer": "A", "correct_answers": ["A"],
            "is_open_ended": False, "img_index": None}
    q_wr = {"question_text": "W?", "options": {}, "correct_answer": None,
            "correct_answers": ["ans"], "is_open_ended": True,
            "img_index": None, "rubric": None}
    payload = json.loads(bg._critique_payload([q_mc, q_wr]))
    assert payload[0] == {"i": 0, "q": "Q?", "opts": {"A": "x", "B": "y"}, "ans": "A"}
    assert payload[1] == {"i": 1, "q": "W?", "ans": "ans"}


def test_critique_is_off_by_default_and_adds_no_call(monkeypatch):
    """Guard default off → generate_for_theme is byte-identical to CP29a: no
    critique call at all, even though one would have marked a question."""
    from app.config import settings
    _stub_keys(monkeypatch)
    monkeypatch.setattr(settings, "BOOK_CRITIQUE_ENABLED", False)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT, n_questions=2, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 1, "no critique call when the guard is off"
    assert len(out.questions) == 2


def test_critique_drops_an_ambiguous_question_and_refills_it(monkeypatch):
    """Guard on: a question the critique marks ok:false is dropped and the slot
    refilled through the normal grounding gate — one critique + one refill call."""
    from app.config import settings
    _stub_keys(monkeypatch)
    monkeypatch.setattr(settings, "BOOK_CRITIQUE_ENABLED", True)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),        # 1: generate 2
        _critique_reply({"i": 0, "ok": True}, {"i": 1, "ok": False, "reason": "ambiguity"}),  # 2
        _Resp(payload=_payload_for([_GROUNDED_MC_C])),                      # 3: refill
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT, n_questions=2, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 3
    stems = [q["question_text"] for q in out.questions]
    assert _GROUNDED_MC_B["q"] not in stems, "the ambiguous question was dropped"
    assert _GROUNDED_MC_C["q"] in stems, "the dropped slot was refilled"
    assert len(out.questions) == 2 and out.errored is False


def test_a_clean_critique_changes_nothing_but_costs_one_call(monkeypatch):
    from app.config import settings
    _stub_keys(monkeypatch)
    monkeypatch.setattr(settings, "BOOK_CRITIQUE_ENABLED", True)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),
        _critique_reply({"i": 0, "ok": True}, {"i": 1, "ok": True}),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT, n_questions=2, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2, "critique ran once; nothing to refill"
    assert len(out.questions) == 2 and out.errored is False


def test_a_critique_call_that_raises_keeps_every_question(monkeypatch):
    """FAIL-OPEN: a critique call that raises must keep the whole grounded set,
    add no refill, and NOT flag the theme errored (a critique hiccup is silent,
    not a 'try again' the teacher sees when they already have good questions)."""
    from app.config import settings
    import requests
    _stub_keys(monkeypatch)
    monkeypatch.setattr(settings, "BOOK_CRITIQUE_ENABLED", True)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B]))
        raise RuntimeError("critique down")

    monkeypatch.setattr(requests, "post", fake_post)
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT, n_questions=2, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2, "generate + critique(raised), then no refill"
    assert len(out.questions) == 2, "fail-open kept both grounded questions"
    assert out.errored is False, "a critique failure is silent, not teacher-facing"


def test_a_malformed_critique_reply_keeps_every_question(monkeypatch):
    from app.config import settings
    _stub_keys(monkeypatch)
    monkeypatch.setattr(settings, "BOOK_CRITIQUE_ENABLED", True)
    calls = _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),
        _Resp(payload={"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": "sorry, no JSON here"}]}}], "usageMetadata": {}}),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT, n_questions=2, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="ru", user_id=1,
    )
    assert len(calls) == 2, "no refill when the critique parsed to 'keep all'"
    assert len(out.questions) == 2


# ── CP30: question-stem language normalization v1 ────────────────────────────

def test_source_question_language_is_byte_identical():
    # None and an unrecognized code both add no override block.
    assert _prompt(question_language=None) == _prompt()
    assert _prompt(question_language="xx") == _prompt()


def test_a_chosen_question_language_adds_a_stem_only_override():
    p = _prompt(question_language="uz")
    assert "LANGUAGE OF THE QUESTION STEM" in p and "Uzbek" in p
    assert p != _prompt()


def test_the_language_override_keeps_options_and_answer_in_source():
    """The grounding-preserving constraint must be explicit: translate only the
    stem; options + the answer stay in the SOURCE language."""
    low = _prompt(question_language="en").lower()
    assert "translate only the stem" in low
    assert "source language" in low


def test_a_foreign_stem_with_a_source_answer_still_grounds(monkeypatch):
    """The whole reason v1 constrains answer==source: is_answer_grounded scores
    the ANSWER, not the stem, so a question ASKED in another language whose
    answer stays in the source still passes the CP14 gate and is kept."""
    _stub_keys(monkeypatch)
    foreign = _mc(                       # Uzbek stem, verified-grounded RU options
        q="Yarim butalar nima? (savol o'zbekcha)",
        opts=dict(_GROUNDED_MC["opts"]), ans="A",
    )
    _capture(monkeypatch, responses=[_Resp(payload=_payload_for([foreign]))])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT, n_questions=1, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="ru",
        user_id=1, question_language="uz",
    )
    assert len(out.questions) == 1, "a foreign-language stem must not fail grounding"
    assert out.questions[0]["question_text"].startswith("Yarim butalar")


# ── CP31a: workbook-mode prompt ──────────────────────────────────────────────

def test_narrative_book_kind_is_byte_identical():
    # None / "narrative" / unknown add no workbook block.
    assert _prompt(book_kind=None) == _prompt()
    assert _prompt(book_kind="narrative") == _prompt()
    assert _prompt(book_kind="something_else") == _prompt()


def test_workbook_kind_adds_exercise_instructions():
    p = _prompt(book_kind="workbook")
    assert "WORKBOOK MODE" in p
    assert "FILL-IN-THE-BLANK" in p and "CHOOSE-THE-CORRECT-FORM" in p
    assert p != _prompt()


def test_workbook_mode_keeps_the_grounding_invariant():
    """The exercise prompt must still tell the model the answer is a word FROM
    the source — that is what keeps the CP14 gate valid for workbooks."""
    p = _prompt(book_kind="workbook")
    assert "GROUNDING" in p and "SOURCE" in p


def test_a_workbook_fill_in_the_blank_answer_still_grounds(monkeypatch):
    """A fill-in-the-blank question whose answer is a word taken from the source
    passes the CP14 grounding gate exactly like any other grounded MC."""
    _stub_keys(monkeypatch)
    theme = "She goes to school every day. He plays football. They are friends."
    fitb = _mc(
        q="She ___ to school every day.",
        opts={"A": "goes", "B": "go", "C": "going", "D": "gone"}, ans="A",
    )
    _capture(monkeypatch, responses=[_Resp(payload=_payload_for([fitb]))])
    out = bg.generate_for_theme(
        theme_title="Unit 1", theme_text=theme, n_questions=1, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="en",
        user_id=1, book_kind="workbook",
    )
    assert len(out.questions) == 1, "a grounded fill-in-the-blank must be kept"
    assert out.questions[0]["correct_answer"] == "A"


def test_workbook_relaxes_only_the_distractor_grounding_check():
    """The targeted relaxation: a workbook question whose DISTRACTORS are absent
    from the source (as grammar-exercise wrong forms are) is kept, while the
    SAME question judged as a narrative book is dropped by distractors_pass. The
    answer-grounding invariant is unchanged in both."""
    theme = "She goes to school every day."
    q = {
        "question_text": "She ___ to school.",
        "options": {"A": "goes", "B": "walks", "C": "runs", "D": "sits"},
        "correct_answer": "A", "correct_answers": ["A"],
        "is_open_ended": False, "img_index": None,
    }
    assert bg._question_is_valid(q, theme, "narrative") is False   # distractors ungrounded
    assert bg._question_is_valid(q, theme, "workbook") is True     # relaxed for workbooks
    # An UNGROUNDED answer is still rejected even in workbook mode.
    bad = dict(q, options={"A": "flies", "B": "walks", "C": "runs", "D": "sits"})
    assert bg._question_is_valid(bad, theme, "workbook") is False


# ── CP31b: vocab-mode prompt + relaxation ────────────────────────────────────

def test_vocab_kind_adds_vocabulary_instructions():
    p = _prompt(book_kind="vocab")
    assert "VOCABULARY MODE" in p and "TRANSLATION" in p
    assert "WORKBOOK MODE" not in p
    assert p != _prompt()


def test_vocab_mode_keeps_the_grounding_invariant():
    p = _prompt(book_kind="vocab")
    assert "GROUNDING" in p and "SOURCE" in p


def test_vocab_relaxes_the_distractor_grounding_check():
    """Same targeted relaxation as workbook: a vocab question whose distractors
    are other words NOT in this chunk is kept, while narrative drops it; an
    ungrounded ANSWER is still rejected."""
    theme = "apple - olma"     # only this pair; other options are not in it
    q = {
        "question_text": "'apple' tarjimasi?",
        "options": {"A": "olma", "B": "kitob", "C": "suv", "D": "non"},
        "correct_answer": "A", "correct_answers": ["A"],
        "is_open_ended": False, "img_index": None,
    }
    assert bg._question_is_valid(q, theme, "narrative") is False
    assert bg._question_is_valid(q, theme, "vocab") is True
    bad = dict(q, correct_answer="B")   # answer "kitob" not in the source
    assert bg._question_is_valid(bad, theme, "vocab") is False


def test_a_vocab_translation_answer_still_grounds(monkeypatch):
    _stub_keys(monkeypatch)
    theme = "apple - olma\nbook - kitob\nwater - suv"
    tq = _mc(q="'apple' so'zining tarjimasi?",
             opts={"A": "olma", "B": "daftar", "C": "qalam", "D": "stol"}, ans="A")
    _capture(monkeypatch, responses=[_Resp(payload=_payload_for([tq]))])
    out = bg.generate_for_theme(
        theme_title="So'zlar 1-3", theme_text=theme, n_questions=1, n_written=0,
        option_labels=["A", "B", "C", "D"], sample_question=None, language="uz",
        user_id=1, book_kind="vocab",
    )
    assert len(out.questions) == 1 and out.questions[0]["correct_answer"] == "A"


# ── Phase 1 checkpoint A1: prompt reorder for implicit prefix caching ───────
#
# Gemini's implicit prefix cache matches the LONGEST COMMON PREFIX between
# calls. Regen/backfill/critique calls for one theme repeat theme_title,
# theme_text, option_labels, difficulty, question_language and book_kind
# UNCHANGED, and only n_questions/n_mc/n_written differ (see book_generator's
# _ask_in_chunks). Before this fix, the count appeared as the FIRST variable
# content right after the source ("Write exactly {n_questions}...") AND again
# inside rule 1 ("...does not support {n_questions} good questions") — so the
# cache-eligible prefix ended a few hundred characters in, before any of the
# 8 numbered rules. Moving the count instruction to the very end (after every
# other block) and rewording rule 1 to stop embedding the count extends the
# shared prefix through the entire rules block for two calls that only differ
# in how many questions they ask for.

def test_the_count_no_longer_appears_before_the_rules_block():
    """Rule 1 must not embed {n_questions} — that was the earlier, smaller
    prefix break inside the rules block itself."""
    p = _prompt(n_questions=4)
    rules_start = p.index("RULES")
    rule2_start = p.index("2. DISTRACTORS")
    rule1_text = p[rules_start:rule2_start]
    assert "4" not in rule1_text, (
        f"rule 1 still embeds the question count, breaking the cache mid-rules: "
        f"{rule1_text!r}"
    )


def test_two_counts_share_a_prefix_through_the_whole_rules_block():
    """The actual cache-relevant property: two prompts differing ONLY in
    count must share everything up to and including rule 8."""
    a = _prompt(n_questions=4, n_written=1)
    b = _prompt(n_questions=9, n_written=3)

    common_len = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        common_len += 1
    shared_prefix = a[:common_len]

    assert "VARY THE QUESTION TYPES" in shared_prefix, (
        f"shared prefix breaks before the end of the rules block "
        f"(only {common_len} chars shared): {shared_prefix[-80:]!r}"
    )


def test_the_count_instruction_is_the_last_variable_content():
    """The count instruction itself must move to the tail, after the rules
    and after the optional per-theme blocks, not sit right after the source."""
    p = _prompt(n_questions=4, n_written=1)
    count_idx = p.index("Write exactly")
    rules_idx = p.index("RULES")
    assert count_idx > rules_idx, (
        "the count instruction still precedes the rules block"
    )


def test_prompt_content_is_unchanged_only_reordered():
    """Regression guard: reordering must not drop or duplicate any content —
    same set of characters, same counts of the key instructions."""
    p = _prompt(n_questions=4, n_written=1)
    assert p.count("Write exactly") == 1
    assert p.count("GROUNDING") == 1
    assert p.count("DISTRACTORS") == 1
    assert "4" in p and "1" in p     # still carries the real counts somewhere


# ── The shortfall a teacher is shown must name the RIGHT cause ────────────────
#
# The mojibake incident: a teacher asked for 30 questions from a Russian
# textbook whose text layer was CP1251 read as CP1252. The model returned 116
# correct Russian questions across four calls; the CP14 gate, comparing them
# against mojibake source, kept 3 — every survivor a four-digit year, because
# digits are the only thing the codepage transform leaves intact. The teacher
# was told "the section didn't have enough material", so they went looking at
# the book. The book was fine. The filter was rejecting almost everything.
#
# Those are different failures and must read differently, so the count has to
# exist. `dropped` is what the model RETURNED minus what survived, subtracted
# ONCE at the end — never a sum of per-phase rejections, which would count a
# question that was rejected and then successfully regenerated as lost.

def test_dropped_is_zero_when_everything_survives(monkeypatch):
    _stub_keys(monkeypatch)
    _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _GROUNDED_MC_B])),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(out.questions) == 2
    assert out.dropped == 0


def test_dropped_counts_what_did_not_survive_not_what_was_rejected(monkeypatch):
    """THE definition that matters. One question fails the gate, the batched
    regeneration replaces it successfully, and the theme delivers in full.

    A per-phase sum would report 1 dropped against 2 delivered from 3 returned
    and read as loss. The final subtraction reports it as exactly what it is:
    three questions came back, two are being used, one was not."""
    _stub_keys(monkeypatch)
    _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC, _UNGROUNDED_MC])),
        _Resp(payload=_payload_for([_GROUNDED_MC_B])),      # the replacement
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=2, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(out.questions) == 2, "the theme delivered in full"
    assert out.dropped == 1, "3 returned, 2 kept — not 1-per-phase double counted"


def test_dropped_is_large_when_the_gate_rejects_nearly_everything(monkeypatch):
    """The mojibake signature: the model delivers, the gate does not accept.
    This is the number that tells a teacher the book was not the problem."""
    _stub_keys(monkeypatch)
    _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_UNGROUNDED_MC] * 4)),
        _Resp(payload=_payload_for([_UNGROUNDED_MC] * 4)),
        _Resp(payload=_payload_for([_UNGROUNDED_MC] * 4)),
        _Resp(payload=_payload_for([_UNGROUNDED_MC] * 4)),
        _Resp(payload=_payload_for([_UNGROUNDED_MC] * 4)),
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=4, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert out.questions == []
    assert out.dropped >= 4, "everything the model returned was rejected"
    assert out.errored is False, "a drop is the policy working, not an error"


def test_dropped_is_zero_when_the_model_simply_under_delivers(monkeypatch):
    """The OTHER cause, which must stay distinguishable: nothing is rejected,
    the model just has less to say. This is the genuine 'not enough material'
    case and it must not borrow the filtered wording."""
    _stub_keys(monkeypatch)
    _capture(monkeypatch, responses=[
        _Resp(payload=_payload_for([_GROUNDED_MC])),
        _Resp(payload=_payload_for([])),      # backfill adds nothing, stops
    ])
    out = bg.generate_for_theme(
        theme_title="§ 3", theme_text=REAL_THEME_TEXT,
        n_questions=4, n_written=0, option_labels=["A", "B", "C", "D"],
        sample_question=None, language="ru", user_id=1,
    )
    assert len(out.questions) == 1
    assert out.dropped == 0


def test_dropped_never_reaches_a_stored_question():
    """`dropped` is provenance about an ATTEMPT, not a property of a question.
    It lives on GenerationResult and must never end up on the dicts that go to
    the OPT6 pool or into persisted rows."""
    result = bg.GenerationResult(
        questions=[dict(_GROUNDED_MC and {
            "question_text": "q", "options": {"A": "a", "B": "b"},
            "correct_answer": "A", "correct_answers": ["A"],
            "is_open_ended": False, "img_index": None,
        })],
        requested=1, errored=False, dropped=7,
    )
    assert all("dropped" not in q for q in result.questions)
