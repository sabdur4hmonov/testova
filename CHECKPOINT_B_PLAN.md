# Checkpoint B — approved plan (COMPLETE)

**Status: all five items shipped**, each as its own failing-test-first commit
on a clean tree, in the approved order:

| item | commit | what shipped |
|---|---|---|
| B-15 | `4264fcd` | retry inside `gemini_rest.call` (ConnectionError/Timeout/5xx; 429 and 4xx still raise first-attempt) |
| B2 | `4899237` | `extract_theme_text_vision` re-draws a truncated/garbage transcription before failing the theme |
| C-2 | `a0a3185` | regen/backfill/refill — and later chunks of one ask — are told which stems were already accepted |
| C-4 | `adba6b3` | scheme transcription batched one call per PAGE, array reply keyed by question number |
| B-32 | `3b93a5d` | quota fail-open contract kept; a bug now logs at ERROR under its own event name, a DB hiccup keeps today's warning |

Suite grew 1571 → 1607 passing over the checkpoint, green at every commit.
`AUDIT.md` carries the corrected/extended entry for each. What follows is the
plan as approved, kept for the reasoning behind each decision.

**Repo state when this was written:** HEAD `0e14f7e` ("Checkpoint A: prompt
reorder, dead code removal, doc/config mismatches"), tree clean. Checkpoint A
(A1 prompt reorder, A2 dead code, A3 doc/config mismatches) is done and
committed. D-2 and D-3 from the dead-code list were investigated and found
NOT eligible for deletion — see `AUDIT.md`'s corrected entries. B-1 (deskew)
and Batches 2-5 (deskew fix, handler shadowing, grading-correctness bugs)
have **not** been touched in this pass and remain fully open, at their own
pace, per the user's explicit instruction not to compress those.

## Scope

Five items, bundled for reporting only — **each gets its own failing-test-
first commit**, same discipline as every prior checkpoint:

1. **B-15** — add retry to `gemini_rest.call`
2. **B2** — transient-error retry on vision truncation (distinct from the
   length/finish_reason checks already shipped in the C-3 commit)
3. **C-2** — backfill/regen context (tell the model what it already gave)
4. **C-4** — batch scheme transcription (one call per page, not per question)
5. **B-32** — narrow the fail-open in `quota.py`

Order: B-15 first (as told to the user), then the rest in whatever order
makes sense once B-15 lands — not yet fixed.

---

## Investigation: confirmed retry shapes already in the codebase

Read before writing any of B-15/B2, so they mirror a proven pattern instead
of inventing a new one.

### `sheet_reader._read_once` (`app/services/sheet_reader.py:409-438`)

```python
async def _read_once(prompt, png_bytes, *, allow_numeric=False):
    for attempt in range(settings.GEMINI_GRADING_MAX_RETRIES):
        try:
            async with _grading_sem:
                raw, finish_reason = await asyncio.wait_for(
                    asyncio.to_thread(_call_sync, prompt, png_bytes),
                    timeout=settings.GEMINI_GRADING_TIMEOUT,
                )
            result = _build_read(raw, allow_numeric=allow_numeric)
            if _has_content(result):
                return result   # got content -> done
            logger.warning("sheet_read_empty", attempt=attempt + 1, finish_reason=finish_reason)
        except asyncio.TimeoutError:
            logger.warning("sheet_read_timeout", attempt=attempt + 1)
        except Exception as e:
            logger.warning("sheet_read_error", attempt=attempt + 1, error=str(e))
        if attempt < settings.GEMINI_GRADING_MAX_RETRIES - 1:
            await asyncio.sleep(1)  # flat 1s backoff - keep the worst case small
    return _empty_read()
```

Key properties: `GEMINI_GRADING_MAX_RETRIES=3`, `GEMINI_GRADING_TIMEOUT=25`
(both in `app/config.py`). Retries on **exception OR empty/truncated
content**, not just on a raised exception — the photo may be fine and the
model just cut the JSON short on that draw. Flat 1s backoff (deliberately
small — grading has a tight worst-case budget documented in config.py).

### `ai_analyzer._extract_page` (`app/services/ai_analyzer.py:1291` onward)

```python
for attempt in range(settings.GEMINI_MAX_RETRIES):
    try:
        raw, fr = await self._call(VISION_PROMPT, img_bytes)
        ...
        return questions
    except asyncio.TimeoutError:
        logger.warning("page_timeout", page=page_num, attempt=attempt + 1)
    except gemini_rest.GeminiQuotaExceeded:
        # NOT retryable: the quota is already exhausted, so the two
        # remaining attempts would be three guaranteed failures with
        # backoff in between.
        logger.warning("page_quota_exceeded", page=page_num)
        break
    except Exception as e:
        logger.warning("page_error", page=page_num, attempt=attempt + 1, error=str(e))
    if attempt < settings.GEMINI_MAX_RETRIES - 1:
        await asyncio.sleep(2 ** attempt)
return []
```

`GEMINI_MAX_RETRIES=3`. Same shape as grading but exponential `2**attempt`
backoff (extraction can be more patient than grading). **Explicitly excludes
429** (`GeminiQuotaExceeded`) from the retry — proven correct by NEW-1's
`test_a_429_is_not_retried` (asserts exactly 1 call, no retry, on a 429).
This exclusion is a hard requirement for any new retry logic touching
Gemini calls: retrying an exhausted quota is three guaranteed failures with
backoff in between, never a recovery.

### `_recover_missing_options`'s batching pattern (`app/services/ai_analyzer.py:1761-1809`)

This is the pattern C-4 must mirror — the "good" version of the same shape
`ensure_scheme_content` currently does badly:

```python
async def _recover_missing_options(self, questions, images):
    by_page: dict[int, list[dict]] = {}
    for q in questions:
        if q.get("has_image") or q.get("image_description"):
            continue
        if q.get("is_open_ended") and not q.get("options"):
            page = q.get("page_number") or 0
            if 1 <= page <= len(images):
                by_page.setdefault(page, []).append(q)

    if not by_page:
        return questions

    for page, page_targets in sorted(by_page.items()):
        nums = [q.get("question_number") for q in page_targets]
        prompt = RECOVER_OPTIONS_PROMPT.format(nums=", ".join(str(n) for n in nums))
        imgs = [images[page - 1]]
        if page < len(images):
            imgs.append(images[page])
        try:
            raw, _fr = await self._call_multi(prompt, imgs, call_site="extract.recover_page")
            items = self._parse(raw, page_num=page)
        except Exception as e:
            logger.warning("options_recovery_failed", page=page, error=str(e))
            continue   # best-effort: one page's failure doesn't abort the rest
        for item in items:
            clean_question(item)
        repaired = self._apply_recovered_options(page_targets, items)
        logger.info("options_recovery_pass", page=page, asked=nums, repaired=repaired)
    return questions
```

`RECOVER_OPTIONS_PROMPT` (`app/services/ai_analyzer.py:258`) takes a `{nums}`
placeholder (comma-joined question numbers) and returns a JSON **array**,
one item per question number: `[{{"n": 20, "A": "...", ...}}]`. Applied back
via `_apply_recovered_options`, which builds a `by_num` lookup dict and
matches recovered items to their target question, with a hallucination
guard (only accepts a result carrying >=2 non-empty options).

The properties that make this the right model to copy:
- **Group by page FIRST**, not by question — the page image is the
  expensive/shared resource.
- **One call per page**, listing every affected question number on that
  page, not one call per question.
- **Response is a JSON array keyed by question number** (`n`), not a single
  object — lets one call answer for many questions.
- **Best-effort per page** — a failure on one page's batch logs and
  `continue`s to the next page; it does not abort the whole pass.

---

## Confirmed gaps (why each item is needed)

### B-15 — `gemini_rest.call` has zero retry

`app/services/gemini_rest.py`'s `call()` does exactly ONE `requests.post`,
`raise_for_status()`, done (verified by reading the function in full — no
loop, no except-and-retry anywhere). This is the SHARED low-level function
underneath `call_book_analysis` (TOC reads + vision extraction) and
`call_generation` (question generation) — none of which wrap it in their own
retry either:

- `book_generator._call_and_parse` (`app/services/book_generator.py:1060+`):
  a bare `try/except Exception as e: return [], True, str(e)` around the
  `call_generation(...)` call. ONE exception = the theme is marked
  `errored=True`, which per `generate_for_theme`'s own logic **suppresses
  every further regen/backfill round** for that theme ("A call that RAISED
  is an infrastructure failure, not under-delivery... the CP14 regen and the
  CP26 backfill below both refuse to spin more calls once one has raised").
  So a single transient `ConnectionError` (which this session has hit
  repeatedly, live, against this exact API) currently zeroes out a whole
  theme's questions after the teacher's quota unit was already consumed.
- `book_extractor.extract_theme_text_vision` (`app/services/book_extractor.py:269`):
  no try/except around `gemini_rest.call_book_analysis(...)` at all — any
  exception propagates straight up through `extract_theme` ->
  `ensure_theme_text`'s existing claim/fail/retry state machine (which DOES
  eventually recover, but only on a LATER manual teacher action, not
  automatically).
- `book_analyzer.py:905` and `:935` (the two TOC calls): same — no retry
  wrapper found.

### B2 — the length/finish_reason checks in `extract_theme_text_vision` raise on the FIRST bad draw

Shipped in the C-3 commit (`9f461e6`): `extract_theme_text_vision` now raises
on `finish_reason==2` (MAX_TOKENS) or on implausibly-long output (the
repetition-garbage heuristic). Both are checked ONCE, on the first and only
call — no retry. But the 18-trial measurement that justified those checks
(documented in `AUDIT.md`'s corrected C-3 entry) showed the SAME page range
can come back clean on a different draw: garbage/truncation was ~22% per
trial, not deterministic per range. So today, one unlucky draw immediately
raises and marks the theme failed, even though a second attempt at the same
call would likely succeed. This is exactly the gap `_read_once`'s "retry on
bad content, not just exceptions" already closes for grading.

### C-2 — regen/backfill calls don't say what was already accepted

`_ask_in_chunks` (added in the B-65 commit, `ee111bc`) and its callers (the
initial ask, the CP14 regeneration, both CP26 backfill rounds) all build
their prompt via `build_prompt`/`_PROMPT_TEMPLATE`, which has NO parameter
for "here is what you already gave me, don't repeat it." So a regen or
backfill call asks the SAME theme for more questions with no signal about
which ones already exist, the model frequently reproduces near-duplicates,
`_is_near_duplicate` (in `book_generator.py`) discovers this AFTER the call
already happened and paid for it, and the backfill loop then has to spend
ANOTHER round chasing the resulting shortfall.

### C-4 — `ensure_scheme_content` is the "bad" version of `_recover_missing_options`'s pattern

`app/services/ai_analyzer.py` — the loop inside `ensure_scheme_content`
(around line 1457 onward in the current file) iterates `questions` one at a
time; for each one still needing scheme content after the free geometric
re-crop attempt, it makes its OWN Gemini call:

```python
raw, _fr = await self._call_multi(
    TRANSCRIBE_SCHEME_PROMPT.format(n=n), [images[page - 1]], call_site="extract.scheme"
)
```

`TRANSCRIBE_SCHEME_PROMPT` (`app/services/ai_analyzer.py:217`) is written for
exactly ONE question (`"Question {n} on this test page contains a
scheme/diagram..."`) and returns a single JSON OBJECT (`{"chain": "...",
"desc": "..."}`), not an array. So two questions sharing one page's scheme
content today cost two full-page-image calls instead of one — the same
page image sent twice, parsed twice, with no batching at all. This is the
precise inverse of `_recover_missing_options`, which already solves the
identical problem correctly for the "missing options" case.

### B-32 — `quota.py`'s fail-open catches can't distinguish a bug from a hiccup

`app/services/quota.py:47-58`, `_fields(kind)`:

```python
def _fields(kind: str) -> tuple[str, str]:
    if kind == VARIANT: return "monthly_variant_limit", "variant_count_this_period"
    if kind == CHECK: return "monthly_check_limit", "check_count_this_period"
    if kind == BOOK_ANALYSIS: return "monthly_book_analysis_limit", "book_analysis_count_this_period"
    if kind == BOOK_GENERATION: return "monthly_book_generation_limit", "book_generation_count_this_period"
    raise ValueError(f"unknown quota kind: {kind!r}")
```

That `ValueError` is a **programmer bug** (a typo'd kind constant somewhere),
not an infrastructure failure. It propagates up through `try_consume` into
FOUR call sites that all catch bare `except Exception`, log a `warning`, and
return `True` (fail open) with no distinction from a genuine DB outage:

- `check_available` (`quota.py:144-153`) — `except Exception as e: ...
  logger.warning("quota_peek_failopen", ...)`
- `can_add_book` (`quota.py:183-192`) — `except Exception as e: ...
  logger.warning("book_cap_failopen", ...)`
- `check_and_consume` (`quota.py:196-209`) — `except Exception as e: # infra
  failure only ... logger.warning("quota_gate_failopen", ...)`
- `within_token_budget` (`quota.py:212-248`) — same shape,
  `logger.warning("token_budget_failopen", ...)`

The comments on the last two literally say "infra failure only" — but the
code doesn't enforce that; it catches everything.

---

## Concrete proposal for each item

### B-15 — retry inside `gemini_rest.call`

Add a bounded retry loop around the `requests.post` in `call()`
(`app/services/gemini_rest.py`), so all three downstream callers
(`call_book_analysis`, `call_generation`, and any future caller) benefit
without each needing their own loop.

- **Retryable:** `requests.exceptions.ConnectionError`,
  `requests.exceptions.Timeout`, and 5xx server responses (this endpoint's
  own docstring already documents observing 503s).
- **NOT retryable, unchanged:** HTTP 429 still raises `GeminiQuotaExceeded`
  immediately — hard requirement, matches `test_a_429_is_not_retried` and
  every existing retry loop's exclusion of it.
- **Budget:** small — this is the shared, lowest-level layer, called
  synchronously (blocking) from threads in `book_generator`/`book_extractor`/
  `book_analyzer`. Proposed: 3 attempts, short flat backoff (matches
  grading's "keep the worst case small" reasoning, since callers currently
  have NO patience budget of their own layered on top).
- **Test-first:** a test asserting `call()` retries N times on a
  `ConnectionError` then succeeds; a test asserting it does NOT retry on a
  429 (still raises `GeminiQuotaExceeded` on the first attempt); a test
  asserting the retry is exhausted correctly (all attempts fail -> the
  original exception/error surfaces, not swallowed).

### B2 — retry the truncation/garbage checks in `extract_theme_text_vision`

Distinct from B-15 (which handles network-level failures). This handles a
CONTENT-level failure: the call succeeds at the HTTP level but returns bad
content (truncated or garbage-length).

Wrap the existing call + both checks (`finish_reason==2`, oversized output)
in a small retry loop (2-3 attempts) BEFORE raising for real. Mirrors
`_read_once`'s philosophy: retry on bad content, not just on exceptions.
Only raise (triggering the theme-failed path) once every attempt in the
budget has produced bad content.

- **Test-first:** a test where the first draw truncates and the second is
  clean -> the function returns the clean text, not an exception; a test
  where every draw within budget is bad -> still raises after exhausting
  the budget (regression guard against silently accepting garbage forever).

### C-2 — backfill/regen context

Add an "already accepted" parameter to the prompt-building path so
`_ask_in_chunks`'s regen and backfill calls tell the model which stems it
already produced for this theme, so it stops re-deriving them. Needs a new
prompt block (mirroring how `_sample_block`/`_difficulty_block`/etc. are
conditionally appended in `build_prompt`) carrying a compact list of already-
accepted question stems, populated only on regen/backfill calls (empty/absent
on the very first call for a theme, so that path stays byte-identical).

- **Test-first:** a test asserting the prompt sent on a backfill round
  contains the stems of previously-accepted questions for that theme; a test
  asserting the FIRST (non-backfill) call's prompt is unchanged from before
  this fix (regression guard).

### C-4 — batch scheme transcription

Restructure `ensure_scheme_content`'s loop to group by page first (mirroring
`_recover_missing_options` exactly), and rewrite `TRANSCRIBE_SCHEME_PROMPT`
to:

- Accept a `{nums}` list (like `RECOVER_OPTIONS_PROMPT` does) instead of a
  single `{n}`.
- Return a JSON ARRAY, one item per question number:
  `[{{"n": 12, "chain": "...", "desc": "..."}}]` instead of one bare object.

Apply results back by matching `n`, same shape as `_apply_recovered_options`.
The two existing recovery paths inside the same loop (geometric re-crop,
which is free/local; and the final "unrecoverable, drop the description"
step) are untouched — only the Gemini-transcription branch changes shape.

- **Test-first:** a test with 2+ scheme-needing questions on the SAME page
  -> exactly ONE Gemini call is made (not one per question), and both
  questions get their content applied correctly from the single array
  response; a test with questions on DIFFERENT pages -> one call per page,
  still not per question; a regression test that a single question on its
  own page still gets its content applied correctly (the array-of-one case).

### B-32 — narrow the fail-open, without changing the fail-open CONTRACT

Decided against re-raising non-infra exceptions into callers: this
codebase's fail-open philosophy is deliberate and documented at every one of
these four call sites ("a quota hiccup must never block a teacher
mid-exam"), and there is no global `dp.errors` handler (confirmed absent
during item 3's B-8 work) — an exception escaping a quota gate into a live
Telegram handler would likely produce a silent hang, trading "occasionally
too permissive" for "occasionally crashes mid-exam," which is worse, not
better.

What changes instead: all four gates keep returning `True` (fail open)
regardless of exception type — but the LOGGING distinguishes the two cases:

- Genuine infra exceptions (SQLAlchemy `OperationalError`/`DBAPIError`,
  connection/timeout errors) keep today's `logger.warning(...)` under
  today's event names (`quota_peek_failopen`, `book_cap_failopen`,
  `quota_gate_failopen`, `token_budget_failopen`).
- Anything else (a real bug, e.g. `_fields`'s `ValueError` on a bad `kind`)
  logs at `logger.error(...)` under a NEW, distinct event name per site
  (e.g. `quota_gate_unexpected_error`), so it is no longer indistinguishable
  from routine DB-hiccup noise in monitoring/alerting.

- **Test-first:** for each of the four gates, a test that a
  `SQLAlchemyOperationalError`-style failure logs at `warning` under the
  existing event name and returns `True`; a test that a `ValueError` (or
  other non-infra exception) logs at `error` under the new event name and
  ALSO returns `True` (contract unchanged) — proving both the distinction
  and the "still never blocks" guarantee in the same pass.

---

## Order of work

1. B-15 (about to start)
2. B2
3. C-2
4. C-4
5. B-32

(Order among 2-5 wasn't fixed by the user beyond "B-15 first" — pick whatever
makes sense once B-15 lands, e.g. B2 next since it's the closest in shape/
location to B-15.)

Each item: propose exact diff shape if it changed since this plan was
written -> write failing tests first -> confirm they fail for the right
reason -> implement -> full suite green -> commit on a clean tree with the
same evidence rigor as every prior checkpoint (real measurements where
applicable, not assumptions).
