# TESTOVA — PROJECT HANDOFF (continue from here)

> **Handoff updated: 16 July 2026 — reconciled to v0.9.** Supersedes the previous handoff. The old handoff was stale at v0.5 (the commit labelled "docs: update handoff through v0.8" actually contained v0.5 text). This version is reconciled against the real git log through the latest commit `a9b21d0`. Since v0.5 the whole **grading half of the product shipped**: manual answer-key grading, saved-project grading, student names, a class group-result table with copy-to-Excel, test naming across every flow, and test-name-first with the name flowing into the PDF. **Generate AND grade is now real.** The next big piece is **VPS deployment** — see NEXT.

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

**Tags:** `v0.1-math-quality` → `v0.2-compact` → `v0.3-compact-optimized` → `v0.4-cost-tracking` → `v0.5-multisource-format` → `v0.6-grader` → `v0.7-names` → `v0.8-naming` → `v0.9-name-first`

**351 tests green.** (3 errors in `test_subscription.py` are the same pre-existing asyncpg `InvalidPasswordError` — environmental, ignore.) Run with `venv311/Scripts/python.exe -m pytest -q`.

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

---

## ⏭️ NEXT — VPS DEPLOYMENT (now the biggest unbuilt piece)

The original "NEXT" (the full generate-and-grade UX) is **substantially SHIPPED**: grading lives behind **✅ Test tekshirish** with two modes — **Saqlangan** (grade against a saved project) and **Javob orqali** (grade against a typed answer key) — plus student names, the class group-result table, copy-to-Excel, and test naming. Grading is free and ignores `uses_left` by design.

The remaining gate before any real teacher touches this is deployment:

**VPS deployment** — kills the Telegram block permanently, bot online 24/7. ~$5/mo (Hetzner/Contabo/DigitalOcean). On deploy:
- `.env` with `ADMIN_IDS=[8206475760]` (JSON list), `ADMIN_USERNAME=testova_admin`, `GEMINI_MODEL=gemini-2.5-flash`, Gemini price vars if non-default (`GEMINI_PRICE_IN_PER_M`, `GEMINI_PRICE_OUT_PER_M`, `UZS_PER_USD`), Postgres connection string.
- `pip install -r requirements.txt` (incl. matplotlib==3.11.0).
- `alembic upgrade head` — schema is now at migration **006** (001 initial → 002 builder_sessions → 003 access_control → 004 gemini_usage → 005 manual_checking → 006 project_naming).
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
- Nothing pushed to remote yet — `git push && git push --tags` when you want an off-machine backup.

---

## MY FIRST QUESTION IN THE NEW CHAT

[PICK ONE:]
- "Walk me through deploying to a VPS." (recommended — the last gate before real teachers)
- "Let's design pricing / monetization now that generate-and-grade both ship."
- "Investigation only — should the grade-and-finish flow be inline on Variant yaratish, or is the separate Test tekshirish button better?"
