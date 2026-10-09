from __future__ import annotations

from functools import lru_cache
from typing import Optional

import re

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secrets in a Settings repr end up in error traces: pytest's monkeypatch puts
# repr(target) in its AttributeError, and that printed every field - bot token,
# DB and Redis passwords, admin secrets - into test output (FOUND 2026-09-21).
# The SecretStr fields below mask themselves; these are masked by
# Settings.__repr_args__ because app/admin/ (out of scope) and many modules read
# them as plain str.
_MASKED_STR_FIELDS = frozenset({"BOT_TOKEN", "ADMIN_PASSWORD_HASH", "ADMIN_SESSION_SECRET"})
_URL_FIELDS = frozenset({"DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"})
_URL_PASSWORD = re.compile(r"(://[^:/@]*:)[^@]*@")


def secret_value(value) -> str:
    """The plain value of a secret setting: SecretStr, plain str (tests assign
    those) or None -> ""."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value or ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Bot ───────────────────────────────────────────────────────────────────
    BOT_TOKEN: str
    WEBHOOK_URL: Optional[str] = None
    WEBHOOK_PATH: str = "/webhook"
    WEBHOOK_SECRET: Optional[SecretStr] = None   # read via secret_value()

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://testova:password@localhost:5432/testova_db"
    # AUDIT F09: Postgres's max_connections (default 100) must cover EVERY
    # client at once. Budget: bot 20 + 20, admin 5 + 5 (compose), each
    # process's usage writer 2, and 20 left for psql / pg_dump / alembic.
    # tests/test_connection_budget.py pins the sum. (Was 25 + 50 = 75 for the
    # bot alone, plus one unpooled connection per concurrent Gemini call.)
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_ECHO: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    # AUDIT F10: FSM keys expire after this long WITHOUT activity (every state
    # or data write restarts the clock). Redis runs noeviction, so this is what
    # bounds the memory abandoned conversations can hold.
    FSM_TTL_DAYS: int = 30
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── AI ────────────────────────────────────────────────────────────────────
    GEMINI_API_KEY: SecretStr = SecretStr("")   # read via secret_value()
    # gemini-2.0-flash-exp was shut down 2026-06-01; keep the fallback valid.
    # (.env overrides this; live behavior is unchanged.)
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ---- Model per task role ------------------------------------------------
    #
    # None = GEMINI_MODEL, which is how this ships: every call site keeps
    # today's model until a role is set in .env. Resolved at CALL time by
    # gemini_models_for() below, so nothing freezes a model at import.
    #
    # Why per role: the jobs do not move together. Generation and Vision are
    # where the cost is; grading is where a misread becomes a wrong grade, so
    # it moves only when its model is retired and only after its own A/B.
    GEMINI_MODEL_GENERATION: Optional[str] = None   # questions + critique
    GEMINI_MODEL_VISION: Optional[str] = None       # scanned page-range transcription
    GEMINI_MODEL_EXTRACTION: Optional[str] = None   # test-paper upload extraction
    GEMINI_MODEL_GRADING: Optional[str] = None      # answer-sheet reading
    GEMINI_MODEL_TOC: Optional[str] = None          # contents-page reads (unreachable today)
    # Models to try, in order and on the same key, when the role's model
    # answers HTTP 404 NOT_FOUND. Comma-separated; empty = none, how this ships.
    # 404 only: a 429 or 5xx is not a missing model. Put a model here only
    # after it passed that role's evaluation. There is deliberately NO grading
    # entry: grading must fail loudly, not switch model on its own.
    GEMINI_FALLBACKS_GENERATION: str = ""
    GEMINI_FALLBACKS_VISION: str = ""
    GEMINI_FALLBACKS_EXTRACTION: str = ""
    GEMINI_FALLBACKS_TOC: str = ""
    # thinkingLevel per role, used ONLY for a model that takes levels (Gemini
    # 3.x, see GEMINI_MODEL_THINKING). A budget model (2.5) keeps using the
    # thinking budgets, so none of these change anything today. Set
    # explicitly because an omitted thinkingConfig is not "off": PROBED
    # 2026-09-21, 3.6/3.8-flash spent 77-79 thinking tokens replying "OK".
    # "minimal" is not guaranteed zero; the gemini_call log shows what it cost.
    GEMINI_THINKING_LEVEL_GENERATION: str = "low"
    GEMINI_THINKING_LEVEL_VISION: str = "minimal"
    GEMINI_THINKING_LEVEL_EXTRACTION: str = "minimal"
    GEMINI_THINKING_LEVEL_GRADING: str = "minimal"
    GEMINI_THINKING_LEVEL_TOC: str = "minimal"

    GEMINI_MAX_RETRIES: int = 3
    # Transport-level retry budget for gemini_rest.call (AUDIT B-15). SEPARATE
    # from GEMINI_MAX_RETRIES, which counts whole extraction attempts one layer
    # up: this one counts HTTP attempts inside a single such attempt, so the two
    # multiply. TOTAL attempts (1 initial + up to 2 retries), retried only on a
    # transient transport failure — connection reset, socket timeout, 5xx — and
    # never on 429 (retrying an exhausted quota is guaranteed failures with
    # backoff in between). Deliberately small: this is the shared lowest layer
    # and its callers block a thread while it runs, so the worst case is
    # 3 x DEFAULT_TIMEOUT + 2 x 1s backoff.
    GEMINI_REST_MAX_RETRIES: int = 3

    # ---- HTTP 429: bounded backoff per role (gemini_throttle) --------------
    #
    # A 429 used to raise on the first attempt (grading: a flat 1s retry that
    # used up a read attempt). A per-minute limit clears in seconds, so one
    # burst failed a whole theme or asked for a retake of a readable photo.
    #
    # Wait = max(server hint, BASE * 2**n) + jitter of up to JITTER_FRACTION,
    # clamped to the role's MAX_WAIT, and no wait at all once the next one
    # would pass the role's BUDGET (total seconds of 429 sleep per call). A
    # hint longer than MAX_WAIT, or a per-day quota, fails at once: waiting
    # would be minutes at best. A 429 retry does not use up a transport
    # attempt (GEMINI_REST_MAX_RETRIES) or a grading read attempt.
    #
    # Sized against the measured call profile, not against tier limits (those
    # are unknown; the rate-limit page defers to AI Studio):
    #   * grading is interactive. MEASURED p50 7.4s / p90 11.1s per read, and
    #     the worst case was already ~77s (3 x 25s + 2 x 1s). 2 retries within
    #     12s keeps it a short pause, and a longer hint means "retry later".
    #   * generation and Vision run in the background after the teacher has
    #     been told to wait, and a failure costs the whole theme: 4 retries
    #     within 60s covers a full per-minute window.
    #   * extraction runs under ai_analyzer's 90s wait_for per call, so its 40s
    #     budget leaves room for the successful call itself.
    #   * toc is unreachable today; it mirrors extraction.
    GEMINI_429_BACKOFF_BASE_SECONDS: float = Field(2.0, gt=0)
    GEMINI_429_JITTER_FRACTION: float = Field(0.25, ge=0, le=1)
    GEMINI_429_MAX_RETRIES_GRADING: int = Field(2, ge=0)
    GEMINI_429_MAX_WAIT_SECONDS_GRADING: float = Field(8.0, gt=0)
    GEMINI_429_BUDGET_SECONDS_GRADING: float = Field(12.0, gt=0)
    GEMINI_429_MAX_RETRIES_GENERATION: int = Field(4, ge=0)
    GEMINI_429_MAX_WAIT_SECONDS_GENERATION: float = Field(30.0, gt=0)
    GEMINI_429_BUDGET_SECONDS_GENERATION: float = Field(60.0, gt=0)
    GEMINI_429_MAX_RETRIES_VISION: int = Field(4, ge=0)
    GEMINI_429_MAX_WAIT_SECONDS_VISION: float = Field(30.0, gt=0)
    GEMINI_429_BUDGET_SECONDS_VISION: float = Field(60.0, gt=0)
    GEMINI_429_MAX_RETRIES_EXTRACTION: int = Field(3, ge=0)
    GEMINI_429_MAX_WAIT_SECONDS_EXTRACTION: float = Field(20.0, gt=0)
    GEMINI_429_BUDGET_SECONDS_EXTRACTION: float = Field(40.0, gt=0)
    GEMINI_429_MAX_RETRIES_TOC: int = Field(3, ge=0)
    GEMINI_429_MAX_WAIT_SECONDS_TOC: float = Field(20.0, gt=0)
    GEMINI_429_BUDGET_SECONDS_TOC: float = Field(40.0, gt=0)

    # ---- Process-wide concurrent Gemini requests per role ------------------
    #
    # A slot is held around ONE HTTP attempt only, never across a backoff
    # sleep. Waiting for a slot is bounded by the call's own HTTP timeout,
    # then raises GeminiSlotTimeout (a GeminiQuotaExceeded). Grading has no
    # setting here: its process-wide limit already exists, MAX_CONCURRENT_GRADING.
    #
    # Today's per-request ceilings: generation 4 themes in parallel
    # (book.py GENERATION_CONCURRENCY), extraction 4 pages per upload
    # (ai_analyzer MAX_CONCURRENT), Vision 1 (batches run in order). The
    # defaults admit four such requests at once (Vision: eight), so a normal
    # day sees no change; the limit starts to bind only in a multi-teacher
    # burst. All of them share the 32-thread pool (THREAD_POOL_MAX_WORKERS).
    # Lower these once the tier's RPM/TPM are known.
    GEMINI_MAX_CONCURRENCY_GENERATION: int = Field(16, ge=1)
    GEMINI_MAX_CONCURRENCY_VISION: int = Field(8, ge=1)
    GEMINI_MAX_CONCURRENCY_EXTRACTION: int = Field(16, ge=1)
    GEMINI_MAX_CONCURRENCY_TOC: int = Field(4, ge=1)
    # Thinking-token ceiling for question generation (AUDIT C-1).
    #
    # call_generation used to pass thinkingBudget=-1 ("model decides"), which no
    # call site overrode. Thinking bills at the OUTPUT rate (2.50 vs 0.30 /M),
    # so an unbounded budget was the largest single cost lever on the book path.
    #
    # MEASURED, not assumed. 39 live generations over 2 real textbooks
    # (10 questions/theme, 16-19k char themes), 13 trials per budget:
    #
    #   budget   mean thinking   mean $/call   vs uncapped   zero-parse
    #   -1             3720        0.01368         --           0/13
    #   2048           1783        0.00897       -34.4%         1/13
    #   1024            843        0.00663       -51.5%         0/13
    #
    # Questions parsed was 10/10 in 38 of the 39 runs and grounding held at
    # 9-10/10 at every budget, so the cap removes spend, not quality.
    #
    # The model SCALES thinking to fill whatever budget it is given (3720 ->
    # 1783 -> 843) rather than spending what a theme inherently needs, which is
    # what makes this such a strong lever.
    #
    # The single zero-parse run (budget 2048, finish_reason=STOP, so not an
    # output truncation) did NOT reproduce: 6 further trials at 2048 were clean,
    # including two that hit the cap exactly. It reads as a sporadic model
    # formatting error rather than a cap effect — but 13 trials cannot prove
    # that, so it is recorded here rather than explained away.
    #
    # 2048 ships as the conservative default. 1024 measured cheaper still
    # (-51.5%) with zero failures and never actually reached its own ceiling
    # (max observed 902) — a reasonable tightening once watched in production.
    #
    # Set to -1 to restore the old uncapped behavior.
    GEMINI_THINKING_BUDGET_GENERATION: int = 2048
    # Thinking-token ceiling for question EXTRACTION (AUDIT NEW-1).
    #
    # Extraction used to run through google-generativeai, whose 0.8.3 release
    # cannot set thinkingConfig at all (see gemini_rest's module docstring), so
    # every page was extracted with the model's DEFAULT unbounded thinking —
    # on the highest-volume path in the product (1+ call per page, up to
    # MAX_PAGES pages per upload, x retries x continuations).
    #
    # MEASURED on real uploaded test papers (5 pages x 3 trials per variant,
    # real VISION_PROMPT, real renderer, real parser):
    #
    #   variant     questions  usable  thinking   $/call    vs SDK   latency
    #   SDK (old)      13.91   13.64      2796   0.01459      --      39.5s
    #   budget 0       13.91   13.64         0   0.00757    -48%      29.4s
    #   budget 512     13.82   13.55       500   0.00850    -42%      23.6s
    #   budget 1024    14.36   14.18       968   0.00995    -32%      21.8s
    #
    # Question-number agreement with the old SDK output was 100% at every
    # budget. Budget 0 reproduces the SDK result EXACTLY (same questions, same
    # usable count) for half the cost, which is the same conclusion the grading
    # path reached independently (sheet_reader: thinkingBudget=0 measured
    # cheaper, faster AND more accurate on real sheets).
    #
    # Extraction is a structural read — transcribe what is on the page — not a
    # reasoning task, which is why thinking buys nothing here.
    GEMINI_THINKING_BUDGET_EXTRACTION: int = 0

    # ---- Output-token ceilings, per call type (AUDIT item j) ---------------
    #
    # These do NOT reduce the price of a normal call: billing is on tokens
    # ACTUALLY GENERATED, not on reserved capacity. They bound the DISASTER
    # case - a model that runs away and emits the full ceiling repeatedly.
    #
    # Sized against the WORST LEGAL REQUEST, not merely observed traffic -
    # observed maxima understate paths whose output scales with user input.
    #
    #   book_extract  observed max 8192 -> 8192 UNCHANGED (cap is BINDING: 3
    #     real calls hit it exactly, and hitting it sets MAX_TOKENS which
    #     triggers continuation round-trips, so a lower cap buys MORE calls)
    #   book_generate observed max 1469 -> 8192 UNCHANGED. Output scales with
    #     the teacher's question count, which book.py:2795 allows up to 100.
    #     MEASURED at ~100 output tokens per generated question (mean 99.9,
    #     max 124.1, n=9 live 10-question runs), so a 4096 cap would truncate
    #     any request above ~41 questions and lose the whole batch. See the
    #     related finding in AUDIT.md: even 8192 only covers ~66-82 questions.
    #   book_toc      observed max 2949 -> 4096. TOC output is a bounded
    #     structure, not user-scalable, so observed max is the real ceiling.
    #   grade         observed max  285 -> 2048. Output scales with the number
    #     of answers on the sheet (~8 tokens each), so a 100-question sheet
    #     needs ~800; 2048 keeps ~2.5x headroom over that worst case.
    MAX_OUTPUT_TOKENS_EXTRACTION: int = 8192
    MAX_OUTPUT_TOKENS_GENERATION: int = 8192

    # ---- Open-answer ("yozma javob") length ceiling ------------------------
    #
    # A written question's reference answer is graded by checker.is_correct,
    # which is an EXACT match after normalize (NFC + casefold + whitespace +
    # apostrophe folding) with equivalences only for single letters and plain
    # numbers. There is no fuzzy path in the correct/incorrect decision, so an
    # answer longer than a short phrase can never be matched against what a
    # student wrote by hand — and written_confirm_needed then puts every one of
    # them in front of the teacher as a manual tap.
    #
    # THE DEFECT, measured in the dev DB: of 40 stored book-generated written
    # questions only 10 had an answer <= 40 chars; the longest ran 465 (two
    # paragraphs about "Slovo o polku Igoreve", literatura 9 pp4-17).
    #
    # THE CEILING, measured over 8 real generation calls on kimyo 7 (uz) and
    # biologiya 6 (ru), 10 written questions each:
    #
    #   prompt     lang  chars med/max  words med/max  <=3 words
    #   current    uz       267 / 546       35 / 63         0%
    #   current    ru       134 / 271       15 / 39         0%
    #   tightened  uz        14 /  37        2 /  5        90%
    #   tightened  ru        10 /  23        1 /  3       100%
    #
    # WORDS is the primary filter and 3 is what a complying model actually
    # produces. CHARS is the backstop for a pathological three-"word" answer,
    # NOT a second filter: Uzbek is agglutinative — a SINGLE Uzbek word reaches
    # 17 chars ("Yelimlanmaganligi") where the Russian single-word maximum
    # measured 12 — so a char cap set near the word cap would reject legitimate
    # Uzbek while passing the same answer in Russian. 50 sits ~2.2x above the
    # widest measured <=3-word answer (23 chars) and just under three
    # worst-case Uzbek words plus spaces (~53), so on the measured corpus it
    # fires on nothing the word cap has not already caught.
    #
    # Enforced in book_generator._question_is_valid, which is the SAME gate
    # grounding rides — so a too-long answer regenerates and then backfills
    # exactly like an ungrounded one, and the OPT6 pool's read-time
    # re-validation catches pre-fix rows with no migration.
    MAX_WRITTEN_ANSWER_WORDS: int = 3
    MAX_WRITTEN_ANSWER_CHARS: int = 50
    MAX_OUTPUT_TOKENS_ANALYSIS: int = 4096
    MAX_OUTPUT_TOKENS_GRADING: int = 2048

    # ---- Per-teacher 30-day token budget (runaway backstop) ----------------
    #
    # 0 = DISABLED, which is how this ships. It is a backstop against a runaway
    # (a script, a loop, an abusive account), not a routine limiter, so it
    # should be set far above what any real teacher uses in a month.
    #
    # Enforced at the EXISTING quota gates (book generation, sheet checking) so
    # it costs no extra per-call query, and it reads the (user_id, created_at)
    # index added in migration 025.
    #
    # FAILS OPEN on any error, like every other gate in app/services/quota.py:
    # "a quota hiccup must never block a teacher mid-exam". Turn it on only
    # after the cost alerts (ALERT_USER_USD_30D) have been watched for a while,
    # so an admin is warned before any teacher is blocked.
    USER_TOKEN_BUDGET_30D: int = 0

    # ---- Quota shadow mode -------------------------------------------------
    #
    # True = the PAID caps (tests, checks, books owned) LOG a would-have-
    # blocked event and let the action through; False = they enforce.
    #
    # On by default for the first cycle because no baseline exists: book
    # generation was never metered (book_generation_count_this_period is 0
    # for every user in the DB), so the real split between book-generated and
    # variant-generated tests is unknown and any cap chosen today is a guess.
    # One cycle of quota_would_block rows turns that guess into a measurement.
    #
    # Shadow start: 2026-09-04. Review the cycle on or after 2026-10-04.
    #
    # It does NOT cover Bepul: the free trial is enforced from day one (see
    # quota.try_consume). Reading the results needs the fixture rows filtered
    # out - the test suite writes into this same DB; see
    # docs/CP_NEXT_HANDOFF.md.
    QUOTA_SHADOW_MODE: bool = True

    # ---- Scanned page-range extraction (AUDIT C-3) -------------------------
    #
    # extract_theme_text_vision transcribes a SCANNED page range in ONE Gemini
    # call, with no page-count cap and sharing call_book_analysis's output cap
    # with the unrelated TOC-title path. Both were latent until Phase 1 item 4
    # accidentally exposed the first: giving call_book_analysis a shared
    # 4096-token default (right for TOC, wrong for full-page transcription)
    # made a real 10-page scanned range truncate (finish_reason=MAX_TOKENS)
    # where it previously did not.
    #
    # The cap on how many pages ONE range (= one synthetic theme) may cover.
    # A teacher who needs more can still submit several ranges ("1-50, 51-100")
    # — each is capped independently and becomes its own theme regardless.
    #
    # RE-DERIVED post-JPEG, and SPLIT, because the flat 15 was carrying two
    # different ceilings at once and matching neither:
    #
    #   * INPUT bytes — scanned only, and no longer binding. MEASURED on the
    #     real books in storage at BOOK_VISION_DPI/quality: 318-326 KB/page
    #     base64 for the two scans and 814 KB/page for the densest digital
    #     book, i.e. 25-64 pages before the API's 20MB inline limit. The PNG
    #     era's 31MB 14-page payload is gone.
    #   * OUTPUT tokens — the ceiling that actually bites, and 15 was already
    #     past it. MEASURED from gemini_usage (call_site='book.theme_vision'):
    #     an ELEVEN-page range drew 16,383 then 16,384 output tokens — two
    #     truncated MAX_TOKENS draws — before a third succeeded at 6,284. At
    #     the worst observed ~1,183 output tokens/page, 16,384 runs out around
    #     13 pages.
    #
    # BOOK_VISION_PAGES_PER_CALL removes the output ceiling entirely by
    # batching, so what is left to cap is cost and latency, not truncation.
    # DIGITAL extraction spends NO Gemini tokens at all (pure fitz), and its
    # only downstream cost is prompt size: MEASURED, a 50-page digital range
    # compresses to 18-35k prompt tokens (~$0.01), so 50 is comfortable.
    # SCANNED stays tighter because every page is a real billed Vision page and
    # 30 pages is already 5 sequential calls.
    #
    # Chosen per RANGE from that range's own text layer — see
    # book_extractor.validate_span_by_text_layer.
    MAX_PAGE_RANGE_SPAN_DIGITAL: int = 50
    MAX_PAGE_RANGE_SPAN_SCANNED: int = 30

    # The COMBINED ceiling for a multi-book run. The per-range caps above are
    # still enforced per range and are what protect extraction; this is the
    # separate question of how much material one test may be built from in
    # total, which per-range caps cannot see: three books at 30 scanned pages
    # each is 15 sequential Vision calls, and no per-range check would notice.
    #
    # 120 is deliberately generous — it is a runaway guard, not a curation
    # rule. It sits above two full digital books at the 50-page cap (100) so the
    # realistic case never meets it, and below the point where one test's
    # preparation runs for many minutes.
    MAX_MULTIBOOK_TOTAL_PAGES: int = 120

    # A DEDICATED cap for vision transcription, independent of
    # MAX_OUTPUT_TOKENS_ANALYSIS (TOC reads). MEASURED: clean transcription of
    # a real scanned book runs ~2,700-3,300 chars/page (~700-900 output
    # tokens/page). At BOOK_VISION_PAGES_PER_CALL=6 pages per call that is at
    # most ~5,400 tokens; 16384 keeps headroom without inviting the
    # over-generation seen
    # when raising the GENERATION cap (B-65) — transcription is bounded by
    # actual page content, not by an open-ended count, so it does not rambles
    # the way question generation did at a raised cap.
    MAX_OUTPUT_TOKENS_BOOK_VISION: int = 16384

    # The cap for ONE page drawn on its own, after its batch came back bad.
    # MEASURED 2026-09-17 on a real scanned workbook ("enterprise 1", PDF page
    # 10): a page of dotted answer lines ran away on 3 of 3 draws, 97-99% of each
    # output being dots, and every draw spent the full batch cap. Such a page is
    # bad on EVERY draw, so each draw of it should cost as little as possible.
    # A real page measured at most ~1,183 output tokens; 4096 leaves ~3.5x
    # headroom for a dense Cyrillic page and makes a runaway draw ~4x cheaper.
    MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE: int = 4096

    # A scanned range still counts as prepared when a page could not be read
    # at all, as long as at least this share of its pages was read. The skipped
    # pages are named to the teacher. Below the share the range fails as
    # before: at that point it is not a usable source for a test.
    MIN_VISION_READ_SHARE: float = 0.5

    # Render DPI for scanned-range transcription. MEASURED 2026-08-29: 18 real
    # trials (3 page ranges x 2 DPIs x 3 trials) against a real scanned book
    # found an IDENTICAL 22% runaway/repetition-garbage rate at 150 and 200
    # DPI, and near-identical clean-transcription length (~8,874 chars mean at
    # 150 vs ~8,719 at 200 for the same ranges) — DPI does not measurably
    # affect what comes back, only the image-token cost of sending the page.
    # 150 halves that cost roughly (measured image byte size: 1.70MB vs
    # 3.10MB for the same page). See MAX_VISION_CHARS_PER_PAGE below for the
    # garbage-detection ceiling this finding also motivated.
    BOOK_VISION_DPI: int = 150

    # JPEG quality for the rendered page sent to Vision. The pages used to go
    # out as PNG, which is what made every multi-page range unsendable: MEASURED
    # on the real scanned book in storage (enterprise 1, 100pp), a 150-DPI page
    # is 1.69MB as PNG and 0.23MB as JPEG q85 at the SAME pixel dimensions —
    # 7.5x smaller for identical pixels. As PNG a 14-page range base64'd to
    # 31.59MB, an 11-page range to 25.47MB and a 9-page range to 21.08MB, ALL
    # of them over the API's 20MB inline-request limit, so they could never
    # succeed however many times they were retried (the observed failure was a
    # write timeout, i.e. the socket dying mid-upload). The same three ranges
    # are 4.24 / 3.33 / 2.73MB as JPEG.
    #
    # This costs NOTHING in tokens: MEASURED from gemini_usage, an image is
    # billed as exactly 258 tokens regardless of its byte size — an 11-page
    # call recorded prompt_tokens=3000 (11*258+162) and a 9-page call 2484
    # (9*258+162). The bytes only ever bought upload time. That also explains
    # the identical 150-vs-200-DPI result recorded above: both are downsampled
    # to the same tile before the model sees them.
    #
    # q85 rather than lower: the compression is already far past the point
    # where the payload matters, so there is no reason to spend image quality
    # buying bytes we do not need.
    BOOK_VISION_JPEG_QUALITY: int = 85

    # How many DRAWS extract_theme_text_vision may take before failing the
    # theme (AUDIT B2). The same 18-trial run showed the truncation/garbage
    # checks fire per DRAW, not per page range: the SAME range came back clean
    # on a different trial, so the 22% rate is a property of the draw. Raising
    # on the first bad one fails a theme a second attempt would likely have
    # transcribed cleanly. TOTAL attempts (1 initial + up to 2 re-draws), which
    # takes an expected ~1.3 calls per scanned theme at the measured rate while
    # cutting the theme-failure rate from ~22% to ~1%.
    #
    # Since 2026-09-17 this budget is spent PER PAGE. A multi-page batch gets
    # one draw; a bad one splits the batch into single pages, and each page gets
    # up to this many draws at MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE. Re-drawing a
    # whole batch was wasted on a page that is bad on every draw (see
    # MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE); a one-off bad draw is recovered just
    # as well by the page draws.
    BOOK_VISION_MAX_RETRIES: int = 3

    # How many pages go into ONE Vision call. extract_theme_text_vision used to
    # send a whole range in a single call and re-draw the WHOLE range on a bad
    # draw; it now sends batches and retries per batch, concatenating the
    # results into ONE text for the range (the range is still ONE theme —
    # batching is internal and nothing downstream can see a seam).
    #
    # 6 is chosen with ~2x headroom on both real ceilings:
    #   * output — 6 x the worst MEASURED 1,183 tokens/page is ~7,100 against
    #     MAX_OUTPUT_TOKENS_BOOK_VISION of 16,384;
    #   * input — 6 x the worst MEASURED 814 KB/page base64 is ~4.9MB against
    #     the API's 20MB inline limit.
    #
    # It costs NO extra tokens: an image bills a flat 258 tokens whatever the
    # batching (MEASURED — an 11-page call recorded prompt_tokens=3000 =
    # 11*258+162), so the only addition is ~162 prompt tokens per extra batch.
    # It makes retries ~4x cheaper: a bad draw re-sends 6 pages, not 30. And
    # because VISION_PROMPT transcribes each page independently (its only
    # ordering rule is within-page column order) and batches are contiguous and
    # in order, reading order is preserved end to end.
    BOOK_VISION_PAGES_PER_CALL: int = 6

    # Length-sanity ceiling, chars/page. AUDIT B-14 hardening: 2 of the 4
    # garbage cases observed in the same 18-trial run had finish_reason=STOP,
    # not MAX_TOKENS — the model can hallucinate a large repeating block and
    # then cleanly stop, which raising on finish_reason==2 alone does not
    # catch. Real garbage measured 53,673 / 123,484 / 129,467 chars against a
    # clean baseline of ~8,200-9,900 chars for the SAME 3-page ranges (~2,700-
    # 3,300 chars/page) — 5.4x-13.6x the clean rate. A per-page ceiling with
    # wide headroom over that catches the severe cases without flagging real
    # dense content (tested against a synthetic ~5,600-char/page dense page).
    #
    # NOT caught by this, and known not to be: a milder case in the same run
    # was only ~3% over its range's clean average (9,942 vs ~9,673) — a short
    # embedded repetition, not a length blowup. Catching that needs pattern/
    # repetition detection, which is a separate, unimplemented follow-up.
    MAX_VISION_CHARS_PER_PAGE: int = 8000
    # Orphaned-original reaping (AUDIT C-8). OFF by default and deliberately
    # so: the job deletes teacher-uploaded files, and its own safety brake was
    # written after a dry run flagged 95% of a dev box's files as orphans
    # (a reset database, not a real backlog). Enable only after reading a dry
    # run against the database that actually owns storage/ — see
    # app/services/original_retention.prune_orphan_originals.
    ORIGINAL_RETENTION_ENABLED: bool = False
    # Grading-only budget (answer-sheet reads). SEPARATE from GEMINI_MAX_RETRIES,
    # which governs question extraction/generation and must stay generous. A
    # teacher waiting on a graded photo needs a tight worst case, not 4.5 min.
    # TOTAL attempts (1 initial + up to 2 retries) — retried on timeout/error AND
    # on an empty/truncated read: 3 x 15s + 2 x 1s backoff ≈ 47s worst case.
    GEMINI_GRADING_MAX_RETRIES: int = 3
    # Per-attempt seconds. MEASURED (thinking disabled, real sheets, n=10):
    # p50 7.4s, p90 11.1s, max 12.3s — so 25s is >2x p90 headroom. The old 15s
    # was too tight: with thinking ON a 25-question sheet took ~26s, so it
    # ALWAYS timed out, retried, and could end up empty — surfacing as a FALSE
    # "Rasm aniq chiqmagan" on a perfectly readable photo. Abandoning a slow but
    # valid call is worse than waiting: the teacher gets a bogus retake request.
    GEMINI_GRADING_TIMEOUT: int = 25
    # Cap simultaneous grading Gemini calls. SIZED AGAINST THE REAL QUOTA
    # (Tier 1: 1000 RPM / 1M TPM / 10k RPD) and the measured p50 of 7.4s per
    # call: 16 concurrent = ~2.2 grades/s = ~130 RPM, i.e. 13% of the RPM quota,
    # leaving deliberate headroom rather than running at the limit. It absorbs a
    # class-sized burst (30 sheets in ~14s) instead of queueing them.
    # NOTE: RPD 10,000/day is the binding ceiling, not RPM — that is ~10,000
    # gradings/day at one call per sheet. Watch the daily count, not the rate.
    MAX_CONCURRENT_GRADING: int = 16
    # asyncio.to_thread runs on the loop's DEFAULT executor, whose default size
    # is min(32, cpu+4) = 12 here. Every in-flight Gemini call holds a thread for
    # its whole duration, so raising the semaphore alone would just move the
    # bottleneck onto the thread pool. Sized to cover grading (16) + extraction
    # (ai_analyzer MAX_CONCURRENT=4) + preprocess with room to spare.
    THREAD_POOL_MAX_WORKERS: int = 32
    # Cost tracking (read-only). Prices are USD per 1M tokens; so'm via UZS rate.
    GEMINI_PRICE_IN_PER_M: float = 0.30    # gemini-2.5-flash input list price
    GEMINI_PRICE_OUT_PER_M: float = 2.50   # gemini-2.5-flash output list price
    UZS_PER_USD: float = 12000.0

    # ── Storage ───────────────────────────────────────────────────────────────
    STORAGE_TYPE: str = "local"  # "local" | "s3"
    LOCAL_STORAGE_PATH: str = "./storage"
    S3_BUCKET: Optional[str] = None
    S3_ACCESS_KEY: Optional[SecretStr] = None   # read via secret_value()
    S3_SECRET_KEY: Optional[SecretStr] = None
    S3_ENDPOINT_URL: Optional[str] = None
    S3_REGION: str = "us-east-1"

    # ── App ───────────────────────────────────────────────────────────────────
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    # Telegram's cloud Bot API caps a bot's getFile DOWNLOAD at 20 MB — a bigger
    # file is accepted as an upload but then fails at get_file() with
    # "Bad Request: file is too big". The old 50 MB value was a lie for this bot
    # (it talks to the public api.telegram.org, no local Bot API server), so
    # anything 20–50 MB passed this pre-check only to fail at download. 20 MB is
    # the real, honest ceiling for a direct Telegram upload.
    MAX_FILE_SIZE_MB: int = 20
    ADMIN_IDS: list[int] = Field(default_factory=list)

    # ── Textbook library ──────────────────────────────────────────────────────
    # A whole textbook, not a test paper. SEPARATE from the upload flow's
    # MAX_PAGES (20), which is a module constant in handlers/upload.py and
    # services/pipeline.py and stays exactly as it is — a 400-page cap there
    # would let one test paper cost a fortune. Only book analysis reads this.
    MAX_BOOK_PAGES: int = 400

    # OPT6 question pool. OFF by default: when False, _run_generation is
    # byte-identical to today (no fetch, no store, no pool tables touched). Flip
    # it on in a deploy to let previously-generated, grounded questions be
    # re-served for the SAME theme + difficulty + option-count, cutting the
    # thinking-token cost of regenerating a theme somebody already generated.
    # A served item never returns to the same teacher (never-repeat guard) and
    # every pooled question is re-validated against CP14 grounding before it is
    # served — the pool never bypasses the grounding bar.
    BOOK_QUESTION_POOL_ENABLED: bool = False

    # Exact requested count per question type. ON by default: a generated test
    # holds exactly the multiple-choice and written counts the teacher asked for
    # (book_generator.trim_to_requested), and the shortfall footer names each
    # type's count. Nothing trimmed before: MEASURED, a 45-question ask came back
    # as 55, and 4 of 18 A/B tests had the wrong type mix under a correct total.
    # False restores the previous behavior exactly: over-delivery kept, the
    # footer comparing totals only. Rollback: BOOK_TRIM_TO_REQUESTED_ENABLED=false
    BOOK_TRIM_TO_REQUESTED_ENABLED: bool = True

    # CP29b — the ambiguity + concept-coverage critique pass. DEFAULT OFF: when
    # off, generate_for_theme is byte-identical to CP29a (no critique call, no
    # extra cost). Flip it on in a deploy ONLY after the 6-theme eval shows the
    # ambiguity rate actually drops — it adds one thinking-ON Gemini call per
    # theme (plus the occasional refill), so it roughly doubles per-theme cost.
    # It is FAIL-OPEN: a critique call that errors or returns garbage keeps every
    # question, so turning it on can never delete good questions on a bad reply.
    BOOK_CRITIQUE_ENABLED: bool = False

    # ── Exam timer ────────────────────────────────────────────────────────────
    # Wall-clock times a teacher types ("14:30") are interpreted in this fixed
    # offset. Uzbekistan is UTC+5 year-round (no DST), so a fixed offset is
    # exact and needs no tzdata/zoneinfo dependency.
    EXAM_TZ_OFFSET_HOURS: int = 5

    # ── AI-text detector (detector/ service, handlers/detector.py) ────────────
    # OFF by default: no menu button, no handler answers, the bot is exactly
    # what it was. The service itself runs only under the compose "detector"
    # profile. The detector makes no paid call; OCR is Tesseract inside it.
    DETECTOR_ENABLED: bool = False
    DETECTOR_URL: str = "http://detector:8010"
    DETECTOR_TIMEOUT: float = 90.0       # one analysis of a long text + queueing
    DETECTOR_API_TOKEN: SecretStr = SecretStr("")   # read via secret_value()
    # Handwriting that Tesseract cannot read goes to Gemini Vision (the VISION
    # role, transcription only). OFF by default: this is the one paid call in
    # the detector flow; every call is logged to gemini_usage (kind
    # "detector_ocr").
    DETECTOR_HANDWRITING_VISION: bool = False

    # ── Online tests (online/ service, handlers/online.py) ────────────────────
    # OFF by default: no "Onlayn / Oflayn" question in any flow, the offline
    # path is exactly what it was, and the online service answers every link
    # with "switched off". The service runs only under the compose "online"
    # profile.
    ONLINE_TEST_ENABLED: bool = False
    # Public address students open; links are <ONLINE_BASE_URL>/t/<code>. Must
    # be HTTPS in production (the attempt cookie is Secure there).
    ONLINE_BASE_URL: str = "http://127.0.0.1:8002"
    # Comma-separated proxy IPs whose X-Forwarded-For is trusted for the
    # code-guessing rate limit. Empty = trust nothing (the TCP peer counts),
    # the same fail-closed rule as ADMIN_TRUSTED_PROXIES.
    ONLINE_TRUSTED_PROXIES: str = ""

    # ── Access control ────────────────────────────────────────────────────────
    TRIAL_DAYS: int = 30          # free trial window applied on first /start
    TRIAL_USES: int = 1           # free full cycles for a fresh user
    TRIAL_CHECK_LIMIT: int = 2    # free answer-sheet checks for a fresh user (0 gap fix)
    ADMIN_USERNAME: str = "admin"  # shown in the blocked message (no leading @)

    # ── Subscription limits ───────────────────────────────────────────────────
    FREE_DAILY_PROJECTS: int = 3
    PRO_DAILY_PROJECTS: int = 50
    CENTER_DAILY_PROJECTS: int = 500

    # ── Admin web panel (HTML dashboard — app/admin/web) ──────────────────────
    # Single-admin session-cookie login. The password is stored ONLY as a bcrypt
    # hash — generate it with `python -m app.admin.web.security <password>` and
    # paste the output here. Empty hash = login disabled (the page says so),
    # never a blank/default password. ADMIN_SESSION_SECRET signs the session
    # cookie (set a long random value in prod; empty = an ephemeral per-boot key,
    # which simply logs everyone out on restart).
    ADMIN_PANEL_USERNAME: str = "admin"
    ADMIN_PASSWORD_HASH: str = ""
    ADMIN_SESSION_SECRET: str = ""
    # AUDIT B-10. Comma-separated proxy IPs whose X-Forwarded-For the panel
    # will trust, passed to uvicorn's forwarded_allow_ips. EMPTY = trust
    # nothing, so request.client.host is the real TCP peer — which is what the
    # login lockout assumes. uvicorn's own default is "127.0.0.1", which under
    # the documented same-host reverse proxy makes a CLIENT-SUPPLIED header
    # authoritative and lets an attacker rotate it for unlimited password
    # guesses. Set this only to the address of a proxy you actually run.
    ADMIN_TRUSTED_PROXIES: str = ""

    # ── Alert thresholds (admin panel monitoring) ─────────────────────────────
    # Cost alerts fire when today's total computed spend, or a single user's
    # 30-day spend, crosses these USD lines. The reconciliation guard flags a
    # stored reconciliation whose Google-vs-ours delta exceeds this percent OR
    # this many dollars (possible uninstrumented spend). A percentage alone is
    # blind to a large gap on a large bill, and undefined when we recorded $0 —
    # that case is always critical (reconciliation.severity). All
    # override-able from .env.
    ALERT_DAILY_USD: float = 5.0
    ALERT_USER_USD_30D: float = 2.0
    RECON_TOLERANCE_PCT: float = 10.0
    RECON_TOLERANCE_USD: float = 1.0

    def __repr_args__(self):
        """repr/str with every secret masked (see _MASKED_STR_FIELDS): the plain
        str secrets by name, the URLs down to their password."""
        for name, value in super().__repr_args__():
            if name in _MASKED_STR_FIELDS and value:
                value = "**********"
            elif name in _URL_FIELDS and isinstance(value, str):
                value = _URL_PASSWORD.sub(r"\1***@", value)
            yield name, value

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def is_webhook_mode(self) -> bool:
        return bool(self.WEBHOOK_URL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()


# ── Gemini per-model pricing (USD per 1M tokens) ─────────────────────────────
# The SINGLE source of truth for cost. Every gemini_usage row is costed by ITS
# OWN model (see usage_log.price_for), so a table that mixes gemini-2.5-flash
# and gemini-3-flash bills each at its own rate instead of one flat price — the
# flat-price bug that goes actively wrong the moment two models share the table.
# `verified` is the date the numbers were last checked against Google's public
# pricing page; the admin panel surfaces it so a stale price is visible, never
# silent. A model ABSENT from this map is still costed (never dropped) at
# GEMINI_PRICING_FALLBACK's price and reported as UNPRICED so it gets a real row.
#
# NOTE: gemini-2.5-flash's numbers intentionally equal the legacy
# GEMINI_PRICE_IN_PER_M / GEMINI_PRICE_OUT_PER_M above — those remain the
# fallback constants and the value existing tests pin against.
GEMINI_PRICING: dict[str, dict[str, object]] = {
    "gemini-2.5-flash": {"in_per_m": 0.30, "out_per_m": 2.50, "verified": "2026-08-22"},
    # Migration target (planned before Oct 2026). PLACEHOLDER prices copied from
    # 2.5-flash — VERIFY against Google's pricing page and set `verified` before
    # the first gemini-3-flash call is made. Until then the panel flags it stale.
    # NOTE 2026-09-21: "gemini-3-flash" is NOT a real model id (models.list has
    # no such model). Kept only because the admin-panel tests seed usage rows
    # under it; nothing routes to it.
    "gemini-3-flash": {"in_per_m": 0.30, "out_per_m": 2.50, "verified": None},
    # Every model in GEMINI_MODEL_THINKING, i.e. everything a role can be set
    # to. Standard paid tier, text/image input, output includes thinking.
    "gemini-3-flash-preview": {"in_per_m": 0.50, "out_per_m": 3.00, "verified": "2026-09-21"},
    "gemini-3.1-flash-lite": {"in_per_m": 0.25, "out_per_m": 1.50, "verified": "2026-09-21"},
    "gemini-3.5-flash-lite": {"in_per_m": 0.30, "out_per_m": 2.50, "verified": "2026-09-21"},
    # 3.6 and 3.8 are priced this way through 2026-12-31 and DOUBLE on
    # 2027-01-01 (1.50 / 7.50). Update these then; the map has no dates.
    "gemini-3.6-flash": {"in_per_m": 0.75, "out_per_m": 3.75, "verified": "2026-09-21"},
    "gemini-3.8-flash": {"in_per_m": 0.75, "out_per_m": 3.75, "verified": "2026-09-21"},
}
GEMINI_PRICING_FALLBACK: str = "gemini-2.5-flash"


# ── Gemini model routing ─────────────────────────────────────────────────────
GEMINI_ROLES: tuple[str, ...] = ("generation", "vision", "extraction", "grading", "toc")

# How each model takes its thinking setting. A model absent from this table is
# REFUSED (at startup by validate_gemini_models, and at call time), never
# guessed: sending the wrong kind is an HTTP 400, and sending neither silently
# runs the model's default thinking.
#
# PROBED live 2026-09-21 (one tiny call per setting per model):
#   * gemini-2.5-flash: "Thinking level is not supported for this model."
#   * gemini-3.5-flash-lite: thinkingBudget 0 -> 400. Levels only.
#   * gemini-3.8-flash: "Thinking level MINIMAL is not supported", and with
#     thinkingBudget 0 it still spent 24 thinking tokens on an image.
#   * every model: thinkingBudget and thinkingLevel together -> 400.
# So every 3.x model is driven by level, even where a budget is accepted.
# minimal/low were probed; medium/high are from Google's thinking docs.
_ALL_LEVELS = ("minimal", "low", "medium", "high")
GEMINI_MODEL_THINKING: dict[str, dict[str, object]] = {
    "gemini-2.5-flash": {"style": "budget"},
    "gemini-3-flash-preview": {"style": "level", "levels": _ALL_LEVELS},
    "gemini-3.1-flash-lite": {"style": "level", "levels": _ALL_LEVELS},
    "gemini-3.5-flash-lite": {"style": "level", "levels": _ALL_LEVELS},
    "gemini-3.6-flash": {"style": "level", "levels": _ALL_LEVELS},
    "gemini-3.8-flash": {"style": "level", "levels": ("low", "medium", "high")},
}


def _check_role(role: str) -> None:
    if role not in GEMINI_ROLES:
        raise ValueError(f"unknown Gemini role {role!r}; one of {GEMINI_ROLES}")


def gemini_models_for(role: str, cfg: "Settings" | None = None) -> list[str]:
    """THE model resolver: the role's model, then its 404 fallbacks, in order.

    The role's own setting, else GEMINI_MODEL. Fallbacks are deduplicated and
    never repeat the primary. Grading has no fallback setting, so its chain is
    always exactly one model.
    """
    cfg = cfg if cfg is not None else settings
    _check_role(role)
    primary = getattr(cfg, f"GEMINI_MODEL_{role.upper()}") or cfg.GEMINI_MODEL
    chain = [primary]
    for name in getattr(cfg, f"GEMINI_FALLBACKS_{role.upper()}", "").split(","):
        name = name.strip()
        if name and name not in chain:
            chain.append(name)
    return chain


def gemini_model_for(role: str, cfg: "Settings" | None = None) -> str:
    return gemini_models_for(role, cfg)[0]


def thinking_config_for(
    role: str, model: str, budget: int, cfg: "Settings" | None = None,
) -> dict[str, object]:
    """The thinkingConfig for ONE request: exactly one of the two fields.

    A budget model gets `budget`, which is what the call site passes today, so
    nothing changes for 2.5. A level model gets the role's
    GEMINI_THINKING_LEVEL_<ROLE> and `budget` is not used.
    """
    cfg = cfg if cfg is not None else settings
    _check_role(role)
    caps = GEMINI_MODEL_THINKING.get(model)
    if caps is None:
        raise ValueError(f"model {model!r} is not in GEMINI_MODEL_THINKING")
    if caps["style"] == "budget":
        return {"thinkingBudget": budget}
    level = getattr(cfg, f"GEMINI_THINKING_LEVEL_{role.upper()}")
    if level not in caps["levels"]:
        raise ValueError(
            f"{model} does not support thinkingLevel {level!r} "
            f"(supports {', '.join(caps['levels'])})"
        )
    return {"thinkingLevel": level}


def validate_gemini_models(cfg: "Settings" | None = None) -> None:
    """Fail fast on a model routing a request could not honour.

    Called once at startup, like validate_security. Every role's model and
    every fallback must be in GEMINI_MODEL_THINKING and support that role's
    thinking level; otherwise the first real call would 400 (or, for an
    unknown model, run with a thinking setting nobody chose).
    """
    cfg = cfg if cfg is not None else settings
    problems = []
    for role in GEMINI_ROLES:
        for position, model in enumerate(gemini_models_for(role, cfg)):
            if position:
                source = f"GEMINI_FALLBACKS_{role.upper()}"
            elif getattr(cfg, f"GEMINI_MODEL_{role.upper()}"):
                source = f"GEMINI_MODEL_{role.upper()}"
            else:
                source = f"GEMINI_MODEL (inherited by {role})"
            try:
                thinking_config_for(role, model, 0, cfg)
            except ValueError as e:
                problems.append(f"{source}: {e}")
    if problems:
        raise RuntimeError("Gemini model configuration refused: " + "; ".join(problems))


def validate_security(cfg: "Settings" | None = None) -> None:
    """Fail fast on a configuration that would expose an unauthenticated door.

    Called once at startup, BEFORE any server binds. Raising here is the point:
    a misconfigured webhook is not a degraded mode to warn about and continue
    from, it is an open endpoint that accepts updates claiming to be from any
    user id — including an ADMIN_IDS one, which reaches /grant and /broadcast.

    AUDIT B-9. aiogram's SimpleRequestHandler.verify_secret is
    `if self.secret_token: return compare_digest(...)` / `return True`, so a
    falsy secret authenticates EVERY request. WEBHOOK_SECRET defaulted to None
    and nothing checked it, while the aiohttp server binds 0.0.0.0:8080.

    This is the only setting in the codebase that defaulted to permissive:
    verify_password returns False on an empty hash, check_credentials returns
    False with no hash configured, and ADMIN_SESSION_SECRET falls back to a
    random per-process key. Those fail CLOSED, which is what made the webhook
    the inconsistency rather than the house style.
    """
    cfg = cfg if cfg is not None else settings
    if cfg.is_webhook_mode and not secret_value(cfg.WEBHOOK_SECRET).strip():
        raise RuntimeError(
            "WEBHOOK_SECRET is not set, but WEBHOOK_URL is — refusing to start. "
            "aiogram accepts ANY request when the secret is empty, so anyone "
            "who reaches the webhook path could inject forged updates with an "
            "arbitrary sender id (including an admin's). Set WEBHOOK_SECRET to "
            "a long random value (e.g. `python -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\"`) and use the SAME value in "
            "setWebhook, or unset WEBHOOK_URL to run in polling mode."
        )
