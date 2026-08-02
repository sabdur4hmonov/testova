# TESTOVA — Pre-Deployment Audit Report

> Read-only audit performed against the code at migration `012` (well past the
> handoff's "006"). Every claim is verified against a file/line or a command
> that was actually run. **The audit changed nothing** — fixes were applied
> afterwards in separate commits (see the status table at the end).

**One-paragraph verdict:** The *application* is in good shape — grading thinking
is off, extraction thinking is on, charging is atomic, the admin boundary holds
(36 security tests pass), no secrets in git, no SQL injection, heavy work is off
the event loop. **The gaps were almost entirely in the deployment layer.** Two
were silent-data-loss / instant-outage class.

---

## The 4 MUST-FIX items (all now fixed)

1. **`temp_images/` was not mounted → every redeploy silently destroyed teachers' figures.** (Part 5.1)
2. **`.env.example`'s `ADMIN_IDS` format crashed the bot at startup** — reproduced. (Part 4.1)
3. **`docker-compose.yml` was stale** — launched dead Celery workers (crash-loop) and an insecure, PII-exposing admin API on port 8000. (Parts 2/4/5)
4. **`create_all_tables()` at startup collided with `alembic upgrade head`** on a fresh DB. (Part 4.2)

---

## PART 1 — COST / Gemini call sites — SOLID

- ✅ **Grading has thinking OFF and has not regressed.** `sheet_reader._call_sync`
  calls REST with `"thinkingConfig": {"thinkingBudget": 0}` — `sheet_reader.py:216`.
- ✅ **Extraction keeps thinking ON (correct).** `ai_analyzer._call_sync_multi` uses
  the SDK with no `thinkingConfig` — `ai_analyzer.py:1193`.
- ✅ **No third "pure extraction with thinking on" path to trim.** Only two call sites exist.
- ✅ **No redundant/duplicate calls.** Grading = one call per sheet, bounded retries,
  semaphore-gated (`sheet_reader.py:385-406`), read once and cached in FSM.
  Extraction has a per-page md5 cache (`ai_analyzer.py:1257`).
- 🟠 **SHOULD-FIX (fixed):** grading imports `requests` (`sheet_reader.py:199`) but it
  was unpinned in requirements.txt (transitive only).

## PART 2 — SECURITY

- **2.1 Secrets — ✅ CLEAN.** `.env` untracked, never in history, no live token/key
  patterns. Only `.env.example` (placeholders) tracked.
- **2.2 Admin boundary — ✅ INTACT.** Every command + callback calls `_is_admin`
  (`admin.py:35`). Re-ran the security suite: **36 passed**.
- **2.3 SQL injection — ✅ CLEAN.** All queries ORM or parameterized `text()` with
  bound params (`access.py:104`, `quota.py`). No string-interpolated SQL.
- **2.4 Atomicity — ✅ SOLID.** `uses_left` and builder-session charges use guarded
  `UPDATE ... RETURNING` (`access.py:103`, `:127`); the monthly quota uses
  `SELECT ... FOR UPDATE` (`quota.py:64-85`). No quota-bypass race.
  - 🟢 NICE-TO-HAVE: the generation *peek* (`has_quota`, read-only, fail-open,
    `quota.py:120`) is racy — a double-submit can bill 2 extractions but deliver
    only 1 variant. Self-inflicted cost leak (~200 so'm), not a bypass.
- **2.5 File uploads — mixed.** ✅ size capped (`upload.py:719`). ⚠️ SHOULD-FIX
  (fixed): `pdf_to_images` rendered *every* page before the `[:20]` slice
  (`file_processor.py:140`) → OOM on a large/malicious PDF. Type check is
  extension-only; no explicit PIL decompression-bomb guard.
- 🟠 **SHOULD-FIX (fixed via compose trim):** the FastAPI admin service
  (`app/admin/main.py`) exposed user PII + ban/plan endpoints, guarded only by
  `ADMIN_API_SECRET` defaulting to `"change_me_in_production"` (`admin/main.py:29`),
  with Swagger at `/admin/docs`, built on the dead subscription model.

## PART 3 — CAPACITY / RELIABILITY

- **3.1 Webhook vs polling — ✅ polling is fine** for this scale (`main.py:43`).
- **3.2 DB pool — ✅ correct for one process** (25+50=75 < 100; `database.py:15`).
  Caveat: a second process (the removed admin service) would have doubled it past 100.
- **3.3 Blocking calls — ✅ CLEAN.** Gemini, `pdf_to_images`, `docx_to_images`,
  `docx_to_pdf` (LibreOffice `subprocess.run` with timeout, `file_processor.py:1594`),
  preprocessing all run via `asyncio.to_thread`; file I/O uses `aiofiles`.
- **3.4 Graceful degradation — ✅ SOLID.** Gemini failure → `_empty_read()` + retake
  message (`sheet_reader.py:406`); extraction has `#GEN-xxxx` codes; cost logging is
  try/except-wrapped; the exam scheduler is non-fatal at startup (`main.py:29-33`).

## PART 4 — DEPLOYMENT READINESS

- **4.1 Env vars.** 🔴 MUST-FIX (fixed): `.env.example` showed
  `ADMIN_IDS=123456789,987654321` but the field is `list[int]` parsed as JSON —
  the comma form raised `SettingsError` and crashed the bot at startup (reproduced;
  `[..]` form works). 🟠 SHOULD-FIX: `GEMINI_API_KEY` defaults to `""` → silent
  per-call failure if omitted (`config.py:39`).
- **4.2 Alembic.** ✅ Chain is linear `001→012`; offline `alembic upgrade head --sql`
  runs cleanly and produces the current schema; migration `012` matches the model.
  🔴 MUST-FIX (fixed): `on_startup` called `create_all_tables()` (`main.py:24`),
  which cannot ALTER existing tables (masks a missing migration) and, on a fresh DB,
  builds an un-stamped schema that then collides with `alembic upgrade head`.
- **4.3 Hardcoded paths — ✅ mostly clean.** `DATABASE_URL`/`REDIS_URL` default to
  localhost but are overridden by compose/`.env`; `SOFFICE_BIN` env-overridable;
  `temp_images` is a relative path (see 5.1).

## PART 5 — DATA PERSISTENCE & OPERATIONS

- 🔴 **5.1 MUST-FIX (fixed):** `IMAGE_SAVE_DIR = Path("temp_images")`
  (`file_processor.py:39`) holds figure crops read back at every variant build
  (`pdf_generator.py:203`), but compose mounted only `./storage` and `./logs` —
  so crops lived in the container layer and were destroyed on every redeploy.
  Postgres (`postgres_data`) and Redis (`redis_data`) *were* persistent; only
  `temp_images` was missing.
- **5.2 Redis / FSM — ✅ connects in compose.** `_make_storage()` pings Redis and
  uses `RedisStorage`, falling back to memory only if unreachable (`bot/main.py:28-37`).
  Notes: the fallback is silent; Redis runs with `allkeys-lru` and no persistence
  (fine for transient FSM state).
- 🟠 **5.3 Backups — none existed. SHOULD-FIX.** Recommend a nightly `pg_dump` cron
  (documented in `docs/DEPLOY.md`).
- 🟠 **5.4 Logs — SHOULD-FIX (fixed):** logging went only to stdout
  (`logging.py:42`), so the `./logs` mount captured nothing and Docker's json-file
  driver had no rotation.
- **5.5 Restart policy — ✅ present** (`restart: always` on the bot).
- 🔴 **5.x MUST-FIX (fixed):** the rest of `docker-compose.yml` was stale — it ran
  dead Celery worker/beat (ImportError crash-loop), flower/admin on :5555/:8000,
  and nginx needing missing certs. None belong in the current single-process design.

## PART 6 — ARM64 / Oracle Free Tier — COMPATIBLE

- ✅ `python:3.11-slim`, `libreoffice-writer`+`libreoffice-math`, and all pinned
  native wheels (PyMuPDF, opencv-headless, numpy, Pillow, reportlab, matplotlib)
  have arm64 builds; Postgres/Redis alpine images are multi-arch.
- Operational note: **build the image on the ARM host** (or `buildx --platform
  linux/arm64`). Moving to x86 later needs no code changes.

---

## VPS spec & deploy checklist

See `docs/DEPLOY.md` for the full step-by-step. Summary:

- **Oracle ARM free tier (month 1):** Ampere A1 up to 4 OCPU / 24 GB is comfortable;
  ~50 GB block storage.
- **Paid x86 later:** ~4 vCPU / 8 GB / 80 GB SSD.
- **Always `alembic upgrade head` before starting the bot** (create_all no longer runs).

---

## Severity summary & fix status

| # | Finding | Part | Severity | Status |
|---|---|---|---|---|
| 1 | `temp_images/` not mounted → silent figure loss | 5.1 | 🔴 MUST-FIX | ✅ fixed (`5d399f1`) |
| 2 | `.env.example` `ADMIN_IDS` comma crashes bot | 4.1 | 🔴 MUST-FIX | ✅ fixed (`773c6cf`) |
| 3 | Stale compose: dead Celery + insecure admin API + nginx | 2/4/5 | 🔴 MUST-FIX | ✅ fixed (`5d399f1`) |
| 4 | `create_all_tables()` collides with alembic | 4.2 | 🔴 MUST-FIX | ✅ fixed (`cfac60d`) |
| 5 | `requests` unpinned (grading dep) | 1/4 | 🟠 SHOULD-FIX | ✅ fixed (`60ff9d1`) |
| 6 | `pdf_to_images` renders all pages → OOM | 2.5/3 | 🟠 SHOULD-FIX | ✅ fixed (`6faf7e0`) |
| 7 | Logs stdout-only; mount unused; no rotation | 5.4 | 🟠 SHOULD-FIX | ✅ fixed (`95d9f96`) |
| 8 | No DB backup strategy | 5.3 | 🟠 SHOULD-FIX | ⏳ documented in DEPLOY.md (cron) |
| 9 | `GEMINI_API_KEY` defaults to `""` (silent) | 4.1 | 🟠 SHOULD-FIX | ⏳ open |
| 10 | Generation peek racy/fail-open (cost leak) | 2.4 | 🟢 NICE | ⏳ open |
| 11 | Redis fallback silent; no persistence | 5.2 | 🟢 NICE | ⏳ open |
| 12 | Build image on ARM host (operational note) | 6 | 🟢 NICE | ⏳ note |

**Confirmed solid (no action):** grading thinking-off & extraction thinking-on with
no duplicate calls; no secrets in git; admin boundary (36 tests); no SQL injection;
atomic charging & quota; event loop never blocked; graceful Gemini degradation;
auto-restart; full ARM64 compatibility.
