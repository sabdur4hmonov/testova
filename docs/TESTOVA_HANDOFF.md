# TESTOVA — PROJECT HANDOFF (continue from here)

> **Handoff updated: 18 July 2026 — reconciled through v0.17.** Supersedes the previous handoff. Reconciled against the real git log (now pushed to GitHub: `github.com/sabdur4hmonov/testova`). Since v0.12, grading got a lot smarter: **v0.13 short-answer grading** (word/number answers + multiple accepted answers), **v0.14 uncertainty flagging**, **v0.15 flag-caching fix**, **v0.16 To'g'ri/Xato confirm buttons + aggressive name flagging + DESIGN B** (the key architectural call — see its section), and **v0.17** removing the now-dead answer-side flagging. **Generate AND grade is real, timed, auto-identified, and now self-correcting** (the teacher confirms wrong written answers). The next big piece is still **VPS deployment** — see NEXT. Two known bugs are queued (one-line parser + E/C letters) — see KNOWN-OPEN ITEMS.

## CONTEXT

I'm building **Testova** — a Python/Aiogram Telegram bot for teachers (Uzbek market). Pipeline: upload test PDF/DOCX → Gemini extracts questions → generate shuffled variants → export PDF → grade student answer sheets via OCR.

I work with **Claude Code in the terminal**. I'm a beginner — I need **exact copy-paste prompts** for Claude Code and simple step-by-step instructions.

**My workflow rule:** Claude Code explains before editing → I say "Go ahead" → **fully kill the bot process** (Ctrl+C, then `tasklist | findstr python`, `taskkill /F /IM python.exe` if needed — Python does NOT hot-reload) → `python main.py` → test with **FRESH upload** (old DB rows keep old extraction) → git commit.

**Stack:** Windows, venv311, Postgres + SQLAlchemy (autoflush=False) + alembic, aiogram FSM (AuthMiddleware), ReportLab PDFs, DejaVu fonts, pydantic-settings, matplotlib.

**Path trap:** project is nested at `sardorbek\sardorbek\testova` — has caused wrong-file edits before.

**🔴 NETWORK TRAP (Uzbekistan):** My ISP blocks Telegram's API IPs. DNS resolves fine but `ping api.telegram.org` = 100% loss. **I must have Cloudflare WARP running (Private browsing mode, "Traffic and DNS (UDP)") before starting the bot**, or aiogram fails with `ClientConnectorDNSError` / `WinError 121`. This is NOT a code bug. Verify with `curl https://api.telegram.org` before every session. A VPS would kill this permanently.

**🟡 CONTEXT-WINDOW TRAP (new):** When Claude Code hits ~99% context, START A FRESH SESSION (`/clear` or reopen). A near-full window forgets the earliest instructions first — which is exactly where the HARD-WON PRINCIPLES below live. A forgetful Claude Code is how a "do-not-fix" gets fixed. **This document is my real memory between sessions — the context window is not.**

---

## ✅ COMPLETED (all committed & tagged)

**Tags:** `v0.1-math-quality` → `v0.2-compact` → `v0.3-compact-optimized` → `v0.4-cost-tracking` → `v0.5-multisource-format` → `v0.6-grader` → `v0.7-names` → `v0.8-naming` → `v0.9-name-first` → `v0.10-timer` → `v0.11-autodetect` → `v0.12-name-prompt` → `v0.13` → `v0.14` → `v0.15` → `v0.16` → `v0.17`. (v0.13–v0.17 are **lightweight** tags; earlier ones are annotated — cosmetic only.)

**470 tests green.** (3 errors in `test_subscription.py` are the same pre-existing asyncpg `InvalidPasswordError` — environmental, ignore.) Run with `venv311/Scripts/python.exe -m pytest -q`.

### Extraction & cleaning
16-bug audit, token truncation fix (8192 + salvage parser), two-column PDF support, gap recovery, isotope/math verbatim guarantee, OCR confusion dictionary, [Rasm] policy, crop sanity checks, suspicious-question flagging, regression tests.

### Dedup & answer key
Multi-section PDFs politely refused; pipeline order extract → reconcile → full answer key → duplicate detection → teacher decides via buttons; bot NEVER auto-deletes; fingerprints preserve digits/subscripts.

### Multi-source test builder
Working end-to-end; FK-ordering crash fixed; deterministic selection; #GEN-xxxx error codes; generation off event loop.

### Access control / admin panel — ✅ CODE COMPLETE **AND VERIFIED END-TO-END (all 7 tests green)**
- `users` table: access_until, uses_left, is_admin, is_blocked, note. `admin_log` table.
- ONE use = one successful extraction (shared counter across both flows). Session completion guarantee. "Test tekshirish" ignores uses_left.
- Trial: TRIAL_DAYS=30, TRIAL_USES=1
- Commands: /grant, /extend, /setuses, /revoke, /unblock, /info, /users, /stats, /help_admin, /myaccess
- `.env`: `ADMIN_IDS=[8206475760]` (**JSON list REQUIRED**), ADMIN_USERNAME=testova_admin
- **My admin:** @testova_admin, ID **8206475760**
- **Test-teacher (2nd account) ID:** **5037603460** (use @userinfobot for future accounts, OR just read `message.from_user.id`/`.username` from any incoming update — Telegram sends both automatically)

### 🏆 THE TEST GAUNTLET — COMPLETE (was the money path; all verified this session)
The whole point: if `uses_left` doesn't decrement correctly, nobody can ever be a paying user. Now proven.
- ✅ **Test 1** — admin bootstrap (`/help_admin` + `/myaccess` → "Cheklovsiz")
- ✅ **Test 2** — 2nd account trial. NOTE: it hit the pre-migration-003 NULL/NULL trap ("Cheklovsiz"); fixed with `/grant 5037603460 30 1 test-teacher` → "29 kun, 1 marta" (29 not 30 = floor of remaining time, correct).
- ✅ **Test 3** — full cycle: upload → 30 variants + answer key delivered → `/myaccess` = "29 kun va **0** ta". Counter went 1→0 on exactly one extraction. **~200 so'm of Gemini bought 30 variants.**
- ✅ **Test 4** — upload again → ⛔ blocked message naming @testova_admin; `/myaccess` and Yordam STILL respond (no dead-end for a would-be paying user).
- ✅ **Test 5** — admin `/info` + `/grant 5037603460 30 5 test-teacher` → 2nd sees "29 kun va 5 marta".
- ✅ **Test 6** — `/revoke` → blocked; `/unblock` → works, 5 uses survived the round-trip.
- ✅ **Test 7** — `/setuses 5037603460 1` → multi-source builder: file 1 charges → file 2 + finish STILL WORK → next new upload blocked. Session you started completes; only the NEXT one is gated. Charges ONCE per build, not per file.

### v0.4 — Gemini cost tracking (read-only, instrumentation only)
- `gemini_usage` table (migration 004): id, created_at, user_id (nullable), kind, model, prompt/output/thinking/total tokens.
- `app/services/usage_log.py`: `_extract_usage` (defensive read of usage_metadata), `estimate_cost` (pure fn, USD + so'm, thinking billed at output rate), `log_gemini_usage` (sync, fully try/except-wrapped — NEVER crashes the main flow; writes via a fresh NullPool engine under asyncio.run from the Gemini worker thread).
- Instrumented the SINGLE physical `generate_content` call site (`_call_sync_multi`) with one guarded additive block. No signature/logic/return change.
- `/usage` admin command: today + last 30 days — call count, input tokens, output+thinking tokens, cost in USD + so'm.
- Config: `GEMINI_PRICE_IN_PER_M=0.30`, `GEMINI_PRICE_OUT_PER_M=2.50` (gemini-2.5-flash list price), `UZS_PER_USD` default 12000. Documented in `.env.example`.
- Also fixed: `config.py` `GEMINI_MODEL` default was the dead `gemini-2.0-flash-exp` (Google shut down Gemini 2.0 Flash on 1 June 2026). Changed default → `gemini-2.5-flash`. `.env` override still wins; live behavior unchanged. **On the VPS, don't forget `GEMINI_MODEL` in `.env` — the fallback is now valid but you still want the model pinned.**

**REAL COST DATA (measured, not estimated):**
- **One extraction (= 1 use): ~200 so'm (~$0.017)** — roughly half the paper estimate. Page-skip + DPI work is paying off (~2,700 input tokens/call).
- Full cycle incl. ~30 graded sheets (grading not built yet at v0.4): estimated ~1,000 so'm worst case. Grading now ships (v0.6+) and its Gemini Vision cost is auto-captured by the same `usage_log` instrumentation — check `/usage` for live grading numbers.
- VPS ~$5/mo ≈ 60,000 so'm — one paying teacher covers it 20×.
- **Pricing implication: API cost is NOISE. Price on VALUE (hours of hand-grading saved), never cost-plus. `uses_left` is the abuse cap.**

### Live model & billing facts (verified 15 Jul 2026)
- Live model: **gemini-2.5-flash**, $0.30 input / $2.50 output per 1M tokens. Thinks by default; thinking bills at the $2.50 output rate.
- Image tokens: ≤384px = 258 tokens; larger images tiled into 768×768 tiles @ 258 tokens each. A4 page ≈ 6 tiles ≈ ~1,550 tokens (same at 150 & 200 DPI). Phone photo ≈ 4 tiles ≈ ~1,030 tokens.
- Free tier (since 1 Apr 2026): Flash & Flash-Lite only, rate-limited — fine for testing, not production.
- New paid accounts default to Prepay: min $10 credit; when balance hits $0 ALL API keys stop simultaneously. **Set auto-reload before a teacher can hit that mid-exam.**

### Support-handle bug — FIXED
`/help` and the file-error notification pointed to `@testova_support`, which does NOT exist (only the block message was correct). Found **7 hardcoded handles across 3 files** (settings.py, start.py, notification_tasks.py). All now read the single source `settings.ADMIN_USERNAME`. Guard test (`test_contact_handle.py`) fails CI if any `@testova_` literal ever reappears. **Rename the handle in ONE place (ADMIN_USERNAME) and every user-facing mention updates.**

### v0.5 — Oddiy/Ixcham format choice in the multi-source builder
Previously compact format was single-upload only. Now "Ko'p manbadan" also asks "Variantlarni qanday formatda olmoqchisiz?" at **finish** (before the variant-count prompt).
- Reuses the SAME mechanism as single-upload: `format_choice_keyboard()`, FSM key `pdf_format ∈ {"compact","standard"}`, read at build time.
- New state `BuilderStates.waiting_for_builder_format`; new callback `handle_builder_format`. Scoped to that state, so no collision with the single-upload format callback (both share the `fmt:` filter but different states).
- `_do_generate` picks `build_variants_pdf_compact` vs `build_variants_pdf` from the key; absent key → standard (safe fallback, never crashes). Answer key stays single-column in both.
- **Closed the coverage gap the old handoff flagged:** compact is now tested on MULTI-SOURCE pool variants (merged pool, "Ko'p manbadan" title), not just single-upload. It's a genuine drop-in — multi-source variants come through the same `_generate_one_variant` code, so every per-question field the compact builder reads is already present.
- **Multi-source is confirmed to do NO Gemini calls at generate time** — all extraction happens per-file at upload; generation just reads the persisted pool + CPU shuffle. That's why asking format at finish costs zero extra API calls.

### v0.6 — Manual answer-key grading ("Javob orqali tekshirish") + saved-project grader wired
**This is where "generate AND grade" started shipping.** Migration **005** adds two tables:
- `manual_check_sessions` (`app/models/manual_check_session.py`): one row per typed-key sitting — `user_id`, `correct_answers` JSONB `{"1":"A",...}`, `created_at`, `expires_at`.
- `check_results` (`app/models/check_result.py`): ONE row per graded sheet, serves BOTH paths — `manual_session_id` set for the manual flow, `project_id` set for the saved-project flow. Holds `variant_number`, `student_name` (reserved at this version), `score`, `total`, `wrong_answers` JSONB, `unclear` JSONB.

Flow: tap **✅ Test tekshirish** → `CheckingStates.choosing_check_mode` → pick **Saqlangan** (saved project) vs **Javob orqali** (manual). Manual path: teacher types the key → echo shown → confirm / re-enter → send student answer-sheet photos one at a time. Grading is FREE (gated by `can_check`, ignores `uses_left` — by design).

New pure/independent services (heavy reuse, no touching of the protected extraction path):
- `app/services/answer_key_parser.py` — `parse_answer_key(text) -> (key, reason)`. Accepts labelled (`"1) A, 2-B. 3C"` with any separators) OR bare (`"ABCDABCD"` numbered from 1). Folds Cyrillic look-alikes **А В С Д Е → A B C D E**, upper-cases, accepts **only A–D**, returns a human-readable Uzbek reason on failure.
- `app/services/sheet_reader.py` — `read_answer_sheet` via Gemini Vision. Has its OWN `ANSWER_SHEET_PROMPT` and **never imports or touches `VISION_PROMPT`** (which the hard-won extraction rules protect). Reuses the SAME `image_to_pages` / `preprocess_image` decode+deskew helpers. Reads MARKED answers, never guesses; ambiguous marks come back as unclear.
- `app/services/checker.py` — pure grading, no I/O. `grade_for(percent)` is **THE single source of truth for the Uzbek 5/4/3/2 scale** (≥86→5, ≥71→4, ≥56→3, else 2). `compare_with_unclear(student, key, unclear)` — an unclear question counts as WRONG but is reported separately so the teacher grades it by hand; missing answers count as wrong (unanswered).

### v0.7 — Student names + class group-result table with copy-to-Excel (both flows)
- `app/utils/caption_parser.py` `parse_caption(caption) -> (name, variant)`: teacher captions the photo `"13 Saidakbar"` / `"Saidakbar 13"` / `"Saidakbar"` / `"13"`, either order. FIRST all-digit token is the variant; the rest (trimmed, space-collapsed) is the name. `"A12"`/`"12b"` stay part of the name.
- `build_group_result(runs, lang, test_name) -> (pretty_text, tsv_text)` in `checking.py`: pretty text = header + **rank table sorted by score DESC** (stable within ties) + average (score + %) + grade histogram (⭐5/⭐4/⭐3/⭐2 counts). `tsv_text` = `label\tscore\tgrade` per line, same sort — **Excel-ready**.
- Emitted under a **📋 Nusxa olish** button (`group_copy_keyboard`, callback `chk:copy`) so the teacher pastes the whole class straight into a spreadsheet. Works in BOTH grading flows.

### v0.8 — Test naming (all flows) + optional student-name prompt + display_name labelling
- Migration **006** adds `projects.display_name` (String(100), nullable) — the teacher-supplied test name; **falls back to `projects.name` when NULL** so old projects still label correctly. Also added `projects.checking_mode` (reserved for a later phase, added now to avoid re-migrating `projects`).
- Optional per-sheet STUDENT-name prompt: when an answer-sheet photo has **no caption**, the bot asks for the name; `parse_name_input` treats blank / `/skip` as None. New states `waiting_for_saved_name` / `waiting_for_manual_name`.
- `display_name` is used to label the finished group result and results.

### v0.9 — Ask test name FIRST in all flows + test name as PDF title
- The test name is now asked **up-front, before the file/format**, in all three flows: `UploadStates.waiting_for_test_name`, `BuilderStates.waiting_for_test_name`, and the manual checker's `waiting_for_manual_test_name`.
- `validate_test_name(text) -> (name, error)` in `caption_parser.py`: the single-upload / multi-source name is **REQUIRED** (no `/skip`), trimmed, **≤100 chars** (`NAME_EMPTY` / `NAME_TOO_LONG` → caller re-prompts). The manual-session name is still optional (`/skip`).
- The name flows through as `exam_title`. **Where it actually prints:** the **answer-key PDF** header (`{exam_title} — Javob kaliti`) and it becomes the project label. **`build_variants_pdf` / `build_variants_pdf_compact` no longer PRINT the title on the variant sheets** — each variant starts with a handwriting fill-in block and a prominent **"Variant N"** (kept prominent on purpose: grading matches sheets to keys by variant number). `exam_title` is retained in those signatures for compatibility. Fallbacks: single-upload `"<full_name> — Test"`, multi-source `"Ko'p manbali test"`.

### v0.10 — Exam timer (in-process APScheduler, restart-safe, proactive warnings)
Offered right after variants are sent in **Variant yaratish** (single-upload only for now). Teacher sets an exam window and the bot sends a 10-min warning + a time-up notice.
- **APScheduler `AsyncIOScheduler` runs IN-PROCESS inside the bot** (started in `main.py` `on_startup`). **NO Celery, NO broker, NO Redis, NO worker** — the investigation confirmed Celery is dead code (defined tasks, never dispatched). Requires `pip install APScheduler` (added to `requirements.txt`, pinned 3.10.4).
- Uses the **already-reserved `projects.exam_start_time` / `exam_end_time`** columns from migration 006 — **NO new migration.** (Migration 006 also added `expires_at`, still unused; there is NO column named `exam_scheduling`.)
- `app/utils/time_parser.py` — PURE: `parse_clock` ("14:30", "2:30 PM", "14:30:00"), `parse_duration_minutes` (rejects 0/negative/garbage), `combine_today` (anchors wall-clock to today in **fixed UTC+5**, no tzdata dep — Uzbekistan has no DST), `compute_end_time`. Config `EXAM_TZ_OFFSET_HOURS=5`.
- `app/services/exam_timer.py` — PURE `plan_jobs(end, now)` decides which jobs (past-end → none; `<10 min` out → warning skipped, end only; else both). `schedule_exam` / `reload_pending` / `init_scheduler` / `shutdown_scheduler`. Jobs keyed by project id, `replace_existing=True`.
- **Restart safety (REQUIRED):** `reload_pending` re-schedules every project with a future `exam_end_time` on startup, so a mid-exam restart never loses a timer (jobs are in-memory only).
- `app/services/notify.py` — the clean in-process proactive-send helper: `send_text(bot, chat_id, text)`, takes the aiogram `Bot`, swallows+logs failures. **NOT** the dead raw-httpx Celery approach.
- Flow lives in `app/bot/handlers/exam_timer.py` (`ExamTimerStates`), offered from `upload.py` at the flow end. Free — does NOT touch `uses_left`.

### v0.11 — Answer-sheet auto-detect: student name + variant off the photo (both modes)
Removes the manual variant-typing step in **Saqlangan** mode and reads the student name in both modes.
- `sheet_reader.read_answer_sheet` now returns **`{variant, student_name, answers, unclear}`** — the prompt reads the handwritten name RAW (`_clean_name`: trim + 100-char cap, no normalization). Robust as ever (failure → all-empty incl. `student_name=None`, never raises). **`VISION_PROMPT` untouched** (principle #9).
- **Saqlangan mode now reads via `sheet_reader.read_answer_sheet`** instead of the weaker answers-only `AIAnalyzer.analyze_answer_sheet`. **Key-format fix:** `read_answer_sheet` returns `{int: letter}`; converted to `{str: letter}` before `check_answers` (which wants string position keys) — grading path byte-for-byte unchanged (principle #10).
- **Read the sheet ONCE, cache in FSM state** — the variant picker and the name prompt grade from cache, so it's still **one Gemini call per sheet**.
- Variant resolution: caption (fast path) → OCR, cross-checked via new PURE `app/services/variant_match.py` `resolve_variant(candidate, valid)` against the project's real variant numbers (`_project_variants`). Exact match → **auto-grade, no typing**. Null / no-match → **`variant_pick_keyboard`** buttons of the valid numbers only (typing still works as fallback — never hard-break).
- Name resolution (both modes): caption → OCR → the existing optional prompt (type or `/skip`) only when BOTH are empty.
- **Manual mode** refactored to the same read-once-then-grade shape; variant stays display-only (single typed key). **Grading stays FREE** — grep-verified no `decrement_use` in `checking.py`; the only call is `upload.py` (generation).

### v0.12 — Name-prompt tuning + block-letters finding
- **Prompt-wording only** in `sheet_reader.ANSWER_SHEET_PROMPT` (name portion): transcribe the handwritten Uzbek name **letter-by-letter**, keep **Latin/Cyrillic script as written** (no transliteration), do NOT correct into a dictionary word, scope any uncertainty to a single letter (never invent a different name), blank → null. Fixed mangling like "Səyyarəbəf" for "Sanjarbek". Return shape unchanged; variant/answer reading untouched.
- **Block-letters finding (measured):** **cursive** handwritten names read unreliably even after tuning; **BLOCK LETTERS read accurately.** A 💡 tip was added to the answer-sheet photo prompts (`_PHOTO_PROMPTS`, `_SHEET_PROMPT`) telling teachers to have students write names in BLOCK LETTERS. **The photo caption remains the 100% path** — if the teacher captions the photo with the name, OCR is bypassed entirely.

### v0.13 — Short-answer grading in manual "Javob orqali" (words/numbers + multiple accepted)
A test can now MIX A/B/C/D and WRITTEN short answers (e.g. `BANANA`, `TOSHKENT`, `5`). Manual flow only (see KNOWN-OPEN #3 for saved-flow).
- **Answer key stores a LIST per question** in `manual_check_sessions.correct_answers` JSONB — a letter is a one-item list (`["A"]`), so one matching rule covers both kinds. **No migration** (JSONB already held it).
- `answer_key_parser.parse_answer_key` returns `{q: [accepted, ...]}`. Accepts legacy `1A 2B` / bare `ABCD`, plus written `5: TOSHKENT` and multi `22: PHONE / TELEPHONE / SMARTPHONE` (slash-separated = any one is correct). Line-oriented for written entries; a colon marks a written answer.
- **Cyrillic folding applies ONLY to single-letter answers, NEVER to words** — `ТОШКЕНТ` stays Cyrillic (folding a word would mangle it into mixed script). See principle #14.
- **Matching is done in PYTHON, never by Gemini** — `checker.is_correct` / `normalize` = casefold + whitespace-collapse, **no punctuation stripping** (so `-5` ≠ `5`, `x=5` stays intact — respects principle #1). `checker.grade_for` unchanged (principle #10). See principle #13.
- `sheet_reader.read_answer_sheet` gained a `texts` return channel for written answers (letters stay in `answers`); the two ride the SAME single vision call. `_norm_letter` now requires the WHOLE value be one letter, so `"APPLE"` is never read as option `"A"`.
- Grading stays FREE (no `decrement_use`).

### v0.14 — Uncertainty flagging in the sheet reader (self-reported, one call)
- `ANSWER_SHEET_PROMPT` asked Gemini to ALSO flag what it was unsure about, in the same single call: `name_unsure` (bool) and an `unsure` list for written answers → returned as `name_unclear` + `low_confidence`. `_as_bool` guards the `bool("false")` trap.
- **Foundation only — nothing consumed the flags yet.** The name flag survives (see below); the answer-side `low_confidence` was later removed (v0.17).

### v0.15 — Cache the flags through to grading
- The v0.14 flags were being discarded before the grading step. Fixed: `handle_manual_sheet` caches them in FSM, and `_grade_manual_cached` clears the cache AFTER scoring (not before), so a confirm step can read the cached read. Pure plumbing, no behavior change.

### v0.16 — To'g'ri/Xato confirm buttons + aggressive NAME flagging + **DESIGN B**
When the read is uncertain, ask the teacher ONE AT A TIME before showing the score — reusing the same button UI (`confirm_answer_keyboard` → `chk:conf:{q}:ok|no`, `handle_confirm_answer`, head-of-queue guard). Override scoring uses **option (a)**: To'g'ri sets the student answer to an accepted value (forces a match), Xato sets it to `None` (clean miss, shown as "—"); `compare_with_unclear` then runs UNCHANGED (`checker.py` never touched). New state `CheckingStates.waiting_for_confirm`.

**🟢 DESIGN B — the key architectural decision (record this):**
The answer-confirm step triggers on **WRONG WRITTEN ANSWERS after scoring**, NOT on Gemini's self-reported confidence. **Why:** Gemini's answer-confidence proved unreliable — it confidently misread `BANAN`→`BRVAN` and `PEANK`→`PEAVK` **without flagging**, and when the prompt was tuned aggressive it **over-flagged clean answers** like `BANANA`. The deterministic score is reliable: a wrong written answer is EITHER a real mistake OR a misread — **both deserve a teacher check.** Rules:
- Ask about each **WRONG WRITTEN** answer (question order), showing the **CORRECT** answer — never Gemini's read.
- Do **NOT** ask about wrong **A/B/C/D** (marked-option reads are reliable).
- Do **NOT** ask about correct answers.
- Distinguish written vs letter by **"question is in `manual_texts`"** (deterministic).
- **Verified live:** a sheet that scored 6/8 from misreads scored **8/8** after confirmation.

**🟢 NAME flagging is the ONE exception that STAYS aggressive.** `name_unclear` is still used and deliberately biased toward flagging (correctly caught `SAIDAKBAR` misread as `SATDAR BAR`). It drives the name-confirm (blank OR unclear name → ask/confirm, wording branches). See principle #15. Structure: name confirm up front → `_score_and_maybe_confirm` (score → derive wrong-written → confirm or finalize) → `_grade_manual_cached` (unchanged finalizer, applies overrides + re-scores).

### v0.17 — Remove the dead answer-side low_confidence flagging
Since Design B triggers on wrong-written (not `low_confidence`), the answer-side flag became dead. Removed: the WRITTEN-answer confidence prompt bullet, the `low_confidence` return key + `unsure` routing, `_coerce_qnums`, and both `manual_low_confidence` cache lines. **NAME flagging fully preserved** (`name_unclear`, `name_unsure`, `_as_bool`, the aggressive NAME prompt bullet). Pure deletion (−67 lines), no behavior change.

### Earlier work (unchanged, still true)

**THE T-108 MATH QUALITY WAR (won):** tested against a real 2-column, 30-question Uzbek math exam (T-108, code 8000008). `app/services/math_render.py` — tokenizer → recursive-descent parser → AST → LaTeX → cached matplotlib mathtext PNG, inlined as ReportLab `<img>`. LaTeX-quality output: stacked fractions, radicals with vinculum, `log₂`, `2²¹`, `x₁`, `3½`, real number-line image.

**Compact 2-column PDF (v0.2/v0.3):** format choice BEFORE the Gemini call in the single-upload flow (one API call, no regeneration). `build_variants_pdf_compact()` — ReportLab Frame + PageTemplate, 2 columns, each variant on a new PAGE (12→8 pages). Math routes through `math_render.py` identically; `_fit_imgs` scales to column width (never crops, never falls back to ASCII). `build_variants_pdf()` and `build_answer_key_pdf()` UNCHANGED; answer key always single-column.

**Image keyword safety net:** if Gemini returns `has_image=False` but the stem contains rasmda/rasmga/jadvalda/diagrammada/grafikda/shakl/ko'rsatilgan/tasvirlangan/sxemada → force `has_image=True`. Also a trailing rule in VISION_PROMPT.

**Gemini cost optimization:** skip blank pages (>90% white), skip header-only pages (<100 chars), per-session md5 page cache, DPI 150 text-only / 200 for image pages. **All skips guarded by `has_visual`** — a page with any embedded image or vector drawing is NEVER skippable (protects the number-line page).

---

## 🔒 HARD-WON PRINCIPLES — VIOLATING THESE BREAKS SHIPPED, VERIFIED BEHAVIOUR

### 1. A cosmetic/layout/prompt pass must NEVER change mathematical meaning.
Regex string-replacement on math is **banned** (it destroyed `2^21` and `4,(2)`). The renderer **parses** into an AST; if anything fails to parse it falls back to **verbatim text**. Correct-but-ugly beats pretty-but-wrong.

### 2. THE GHOST BUG — NEVER "FIX" THIS
Options extracting as `B) 492`, `A) 4210`, `D) 448`, `B) -1512` — the trailing digit is always the **page number**. It's a **PDF text-extraction artifact only**; the printed PDF is clean. Verified visually many times.
**NEVER add trailing-digit stripping. NEVER add a VISION_PROMPT rule about options "ending in an unexpected digit" or "bleeding from the other column."** It would corrupt real answers like `148`, `12800`, `-12`, `-15`.

### 3. The sqrt rule is BIDIRECTIONAL
A one-directional rule caused a **wrong answer on a real exam**. Gemini nested `4sqrt(3) + 2` as `4sqrt(sqrt(3) + 2)` (7.46 vs 8.93 — different number).
VISION_PROMPT now states BOTH: a term under the radical must not escape it, AND a term outside must not be pulled inside. Worked examples for both directions.

### 4. Radical extraction is NON-DETERMINISTIC
1 in 6 T-108 runs mis-nested a radical. **The renderer is faithful by design and will NOT un-nest a corrupt source** (un-nesting would be regex-on-math and would corrupt genuinely nested expressions like `2·⁴√(x·(7+4√3))`).
**If a wrong radical ever appears in an exported PDF, check the DB source FIRST — it's almost certainly extraction, not rendering.**

### 5. Do NOT re-implement
2-column extraction support and cross-column question ordering are **already shipped**. Any planning doc that lists them as bugs is stale.

### 6. 2 columns is the ceiling on A4
A 3-column mode would drop column width to ~135pt, making math overflow and overlap dramatically worse. Rejected.

### 7. (v0.4) Cost/instrumentation must never crash the main flow
`log_gemini_usage` is entirely inside try/except (warn-and-continue). If `/usage` shows zeros after a successful extraction, the write silently failed BY DESIGN — check the console warning, don't assume nothing was billed.

### 8. (v0.4) One source of truth for the contact handle
Every user-facing @-mention reads `settings.ADMIN_USERNAME`. A guard test blocks any hardcoded `@testova_` literal. Don't reintroduce a second handle.

### 9. (v0.6) The answer-sheet reader must never touch `VISION_PROMPT`
`sheet_reader.py` has its OWN `ANSWER_SHEET_PROMPT`. The extraction prompt is protected by principles 1–4. Reading a filled answer sheet is a DIFFERENT task (read marks, don't extract questions) — keep the two prompts separate forever. Reuse only the image decode/deskew helpers, never the prompt.

### 10. (v0.6) One grading scale, one place
`checker.grade_for()` is the ONLY definition of the Uzbek 5/4/3/2 scale. Nothing else may define its own thresholds. An unclear (unreadable) answer counts as WRONG but is listed apart so the teacher can override by hand — do not silently mark it correct.

### 11. (v0.10) The exam timer is IN-PROCESS and must never take down the bot
APScheduler runs inside the bot process — **do NOT wire up Celery** to run it (Celery is dead code; reviving it is a trap). `init_scheduler`/`reload_pending` are wrapped in try/except in `main.py`: if the scheduler fails to start, **the bot still runs, just without timers.** `shutdown_scheduler` is a safe no-op if it never started and must never hang/throw on exit. Timer times live in the **reserved `projects.exam_*` columns — never add a migration for them.** Jobs are in-memory, so the startup DB reload is what makes a restart safe — keep it.

### 12. (v0.11) Auto-detect never guesses, and grading stays FREE
Read the sheet ONCE and cache it (one Gemini call per sheet). A variant is used ONLY if it exactly matches the project's real variants (`resolve_variant`); otherwise show the picker — **never guess a variant.** The handwritten name is returned RAW (no spelling correction). And **`checking.py` must never call `decrement_use`** — grading is free by design (the `can_check` gate ignores `uses_left`). If you ever add charging, it belongs in generation (`upload.py`), never here.

### 13. (v0.13) Short-answer matching is PYTHON, never Gemini — and never strips meaning
Correctness is decided in `checker.is_correct` (casefold + whitespace-collapse ONLY). **Never** ask Gemini to judge if an answer is right (unreliable — see Design B), and **never** strip punctuation in `normalize`: `-5` must stay ≠ `5` and `x=5` must stay intact (this is principle #1 applied to short answers). A wrong written answer is caught by the score and confirmed by the teacher (Design B), not by fuzzy matching. Multiple accepted answers are the escape hatch — the teacher lists variants (`PHONE / TELEPHONE`), the code never guesses spelling.

### 14. (v0.13) Cyrillic folding is for LETTERS ONLY, never words
`А В С Д Е → A B C D E` folding applies only to a single-letter answer (a multiple-choice option). A WORD is never transliterated: Cyrillic `ТОШКЕНТ` stays Cyrillic. Folding a word would mangle a real answer into mixed script. Same rule as the v0.12 name transcription.

### 15. (v0.16) Answer-confirm triggers on WRONG-WRITTEN (Design B); NAME flagging stays aggressive
Do NOT resurrect Gemini's answer-confidence to decide what to confirm — it's unreliable (confident misreads `BRVAN`/`PEAVK`, and over-flags clean answers when tuned up). The answer-confirm queue is built AFTER scoring from **wrong WRITTEN answers only** (in `manual_texts`), never wrong A/B/C/D, never correct answers. **The NAME flag (`name_unclear`) is the deliberate exception — keep it aggressive** (it catches confident name misreads like `SATDAR BAR`). Override scoring uses option (a) (force match / clean miss); `checker.py` stays untouched.

---

## 🐞 KNOWN-OPEN ITEMS (queued bugs — a future session should pick these up)

### 1. ONE-LINE PARSER BUG (next task — small but fiddly)
In the short-answer key parser, `"1:x 2:y 3:z"` all on ONE line swallows answers 2+ into question 1 (they get treated as part of q1's answer). **Separate lines work fine** (`1: x` / `2: y` on their own lines). Tricky because a written answer can legitimately contain spaces AND digits: `5: SMART PHONE`, `24: 1000 g, 400 g` — so you cannot naively split on whitespace or numbers. The fix needs a smarter line/entry boundary rule that a multi-word, multi-number answer survives. Manual "Javob orqali" only.

### 2. E/C LETTER-PRESERVATION BUG (big — needs migration 007)
The DB has only `option_a..option_d` (`questions` table), so a test with options **A, B, D, E** gets silently **relabelled to A, B, C, D** at extraction/persistence (option E is dropped; the printed "(bor: A, B, C, D)" is the tell). **Accepting a typed `E` WITHOUT fixing storage would grade against the WRONG option — worse than the current bug.** Full fix needs: VISION_PROMPT changes (guarded — principles #1–#4), an `option_e` column, **migration 007**, `letters_by_num` in `upload.py`, and both `_VALID` sets (`answer_key_parser`, `sheet_reader`). Do NOT half-fix by loosening validation alone.

### 3. Short answers are manual "Javob orqali" ONLY
The **Saqlangan** (saved-test) flow can't grade written answers yet — its key rides `questions.correct_answer` which is `String(4)`, so it needs **migration 007** too (this was "Option B" in the v0.13 investigation). Same migration as #2 — do them together.

### 4. Lightweight tags (cosmetic)
`v0.13`–`v0.17` are lightweight tags; `v0.1`–`v0.12` are annotated. Harmless; re-tag with `-a` if you want uniformity.

---

## ⏭️ NEXT — VPS DEPLOYMENT (now the biggest unbuilt piece)

The original "NEXT" (the full generate-and-grade UX) is **substantially SHIPPED**: grading lives behind **✅ Test tekshirish** with two modes — **Saqlangan** (grade against a saved project) and **Javob orqali** (grade against a typed answer key) — plus student names, the class group-result table, copy-to-Excel, test naming, an **exam timer** (v0.10), **auto-detect of variant + student name off the photo** (v0.11–v0.12), **short-answer grading** (v0.13, manual only), and **teacher-confirm of wrong written answers** (v0.16 Design B). Grading is free and ignores `uses_left` by design.

The remaining gate before any real teacher touches this is deployment:

**VPS deployment** — kills the Telegram block permanently, bot online 24/7. ~$5/mo (Hetzner/Contabo/DigitalOcean). On deploy:
- `.env` with `ADMIN_IDS=[8206475760]` (JSON list), `ADMIN_USERNAME=testova_admin`, `GEMINI_MODEL=gemini-2.5-flash`, Gemini price vars if non-default (`GEMINI_PRICE_IN_PER_M`, `GEMINI_PRICE_OUT_PER_M`, `UZS_PER_USD`), Postgres connection string.
- `pip install -r requirements.txt` (incl. matplotlib==3.11.0 **and APScheduler==3.10.4** for the exam timer).
- `alembic upgrade head` — schema is **still at migration 006** (001 initial → 002 builder_sessions → 003 access_control → 004 gemini_usage → 005 manual_checking → 006 project_naming). v0.10–v0.17 added NO migration (timer reused 006's reserved columns; short-answer keys ride the existing `manual_check_sessions.correct_answers` JSONB). **Migration 007 is still owed** for the E/C letters + saved-flow short answers — see KNOWN-OPEN #2/#3.
- Set Gemini Prepay auto-reload so nobody hits a $0 balance mid-exam.

Suggested opening prompt for the deployment session:
> "Walk me through deploying Testova to a VPS step by step — I'm a beginner. We're at migration 006, model gemini-2.5-flash. Cover the .env, Postgres, alembic upgrade head, keeping the bot running 24/7, and Gemini auto-reload."

---

## 🚀 THEN — REMAINING BIG FEATURES (in order)

1. **VPS deployment** (see NEXT) — must happen before any real teacher.
2. **Pricing / monetization** (deferred to the end, per my decision — now backed by real cost data: ~200 so'm/extraction + free grading, so any sane price = huge margin). `uses_left` is the abuse cap.
3. **Optional grade-and-finish polish on the generation side:** the original sketch of appending confirm → send pages as photos → collect sheets → finish → auto-clear directly onto the "Variant yaratish" flow was NOT built as one integrated flow (grading is reached via its own button instead). Revisit only if teachers actually want it inline.
4. Web admin panel later (thin skin over existing admin tables).

---

## 📌 PARKED / BACKLOG (see `docs/BACKLOG.md`)

- Per-user attribution for `gemini_usage`: thread `user_id` through to `AIAnalyzer` (currently logged NULL) so `/usage` can break cost down per teacher. The grading (`sheet_reader`) Gemini calls have the same NULL-attribution gap.
- `/usage` merges output + thinking into one figure. Split into separate columns so the thinking-token share is visible (2.5 Flash thinks by default, bills thinking at $2.50/1M). Low priority — total cost ~200 so'm/extraction.
- Compact PDF: wide figures currently scale to column width. If a real exam needs a true full-page-width figure, add a 2nd PageTemplate with a full-width frame and switch templates mid-document.
- Parser splits `f(x) = x^2 - x + 1` prose/math boundary inconsistently in places. Correct, just cosmetic.
- `check_results.student_name` and `projects.checking_mode` were added ahead of need (to avoid re-migrating). Wire them fully when the saved-project grading UX is fleshed out.
- Main menu is currently Row 1 `[📤 Variant yaratish] [✅ Test tekshirish]`, Row 2 `[📚 Ko'p manbadan test yaratish]`, Row 3 `[projects] [pricing]`, Row 4 `[language] [support]`. File: `app/bot/keyboards/main_menu.py`.
- `ADMIN_IDS` should accept bare `123` or `123,456` via validator, or document JSON format in `.env.example`.
- `admin_log.target` vs `target_user_id` naming; `blocked_text()` has no lang param — deliberately skipped.
- **Git remote is configured** — `origin` → `github.com/sabdur4hmonov/testova` (`autoUpdates` off; push with `git push origin master && git push origin --tags`). History + all tags are backed up there through v0.17.
- Exam timer is **single-upload only** — the "Ko'p manbadan" (multi-source) flow doesn't offer it yet. Auto-detect covers both grading modes; timer does not yet cover both generation flows.

---

## MY FIRST QUESTION IN THE NEW CHAT

[PICK ONE:]
- "Investigation only — fix the ONE-LINE PARSER BUG (KNOWN-OPEN #1): `1:x 2:y 3:z` on one line swallows answers 2+ into q1." (small, queued as next task)
- "Investigation only — the E/C letter bug + saved-flow short answers (KNOWN-OPEN #2/#3): plan migration 007 end-to-end." (big)
- "Walk me through deploying to a VPS." (the last gate before real teachers)
- "Let's design pricing / monetization now that generate-and-grade both ship."
