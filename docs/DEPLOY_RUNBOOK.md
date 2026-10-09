# Deploy runbook — v0.41 (migrations 026 → 035)

<!-- deploy-target: base=026 head=035 -->

> Historical VPS release procedure. The current installation runs on the owner's
> laptop; see RESTORE_DRILL_2026-10-08.md. Production queries, migrations,
> image rebuilds and bot restarts below require explicit owner approval.

Step-by-step for taking **tag `v0.41`** to the VPS. Follow it top to bottom.
`docs/DEPLOY.md` is the general checklist (first-time setup, volumes, admin
panel HTTPS). **This document is specific to this release**, because it carries
two data-rewriting migrations (027, 033) and the first production deploy of the
admin panel.

Everything runs **on the VPS**, from the repo directory, with `.env` present.
`tests/test_deploy_runbook_is_current.py` fails the test suite if this file
stops targeting the newest migration — update it with every new migration.

**The one rule this release depends on:** migrations run from the **new
image**, with **both** `bot` and `admin` stopped. The previous runbook migrated
with `docker compose run --rm bot` *before* rebuilding; the Dockerfile copies
the code into the image (`COPY . .`), so that ran the OLD migrations and stopped
at the old head while the new code then started against an old schema.

---

## 0. What is in this release, and what is risky

| Migration | What it does | Reversible? |
|---|---|---|
| **027** | `UNIQUE (project_id, variant_number)` on `variants` — **deletes duplicate rows first**, re-points their submissions | ⚠️ constraint yes, **deleted rows NO** |
| **028** | Adds `users.check_count_lifetime`, seeds it for trial rows | Yes (loses trial-check history) |
| **029** | Adds `users.price_som_at_purchase` (nullable, no data write) | Yes (loses recorded prices) |
| **030** | Two indexes on `admin_log` (`ix_admin_log_created_at`, `ix_admin_log_action_created_at`) | Yes |
| **031** | Index `ix_gemini_usage_kind_user_id` | Yes |
| **032** | New table `sheet_checks` — one row per answer-sheet photo (CP-4) | Yes (drops per-sheet rows) |
| **033** | `exam_editions` (CP-5). Backfills **one legacy edition per project with variants**, links every variant, makes `variants.edition_id` **NOT NULL**, **drops `uq_variants_project_number`** for `uq_variants_edition_number`, installs trigger **`variants_frozen`** | ⚠️ **only while no project has a second edition** — see §9 |
| **034** | New table `payments` — the manual-payment ledger behind the admin panel's Revenue page. Additive only: no existing column or row is touched, and no access or quota check reads it | Yes (loses recorded payment history) |
| **035** | New tables online_tests, online_attempts, online_attempt_questions, online_events (online tests). Additive only; nothing reads them while ONLINE_TEST_ENABLED is false | No |

**Why the bot AND the admin panel must be stopped:** 033 makes a column NOT
NULL. Code older than v0.41 saves variants without an edition and would fail
against it; and 033's `ALTER TABLE`s lock tables the panel reads. This is not a
rolling migration.

Also changed in this release, no action needed but worth knowing:
- Redis FSM state survives the deploy. A checking run that was in progress
  keeps working: pre-CP-4 sub-states are routed back into the run, and a run
  from before editions binds its project's single legacy edition.
- `RECON_TOLERANCE_USD` is the only new setting (default `1.0`, in `.env.example`).
- The bot runs the `temp_images/` crop cleanup **immediately at start** (then
  daily) — §6 is the gate for that.

---

## 1. Record where production is right now

Nothing here writes anything. Keep the output — it decides §8b and is the
baseline for §9.

```bash
DEPLOY_LOG=~/deploy_v0.41_$(date +%F_%H%M).log
{
  echo "== code";          git rev-parse HEAD; git describe --tags --always
  echo "== schema";        docker compose run --rm --no-deps bot alembic current
  echo "== services";      docker compose ps
  echo "== images";        docker compose images
} 2>&1 | tee "$DEPLOY_LOG"
```

`alembic current` here runs the image that is **currently** built, which is
fine: it only reads `alembic_version`. Expect a revision between **026** and
**029**. Anything newer than 029 means this runbook was already partly applied —
stop and find out what ran before continuing.

---

## 2. Pre-flight (read-only)

### 2a. Rows 027 will delete — skip if §1 printed 027 or later

```bash
docker compose exec -T postgres psql -U testova -d testova_db <<'SQL'
SELECT v.project_id,
       v.variant_number,
       count(DISTINCT v.id)                AS copies,
       count(DISTINCT v.answer_key::text)  AS distinct_keys,
       count(s.id)                         AS submissions_affected
FROM variants v
LEFT JOIN submissions s ON s.variant_id = v.id
GROUP BY v.project_id, v.variant_number
HAVING count(DISTINCT v.id) > 1
ORDER BY distinct_keys DESC, copies DESC;
SQL
```

`count(DISTINCT v.id)`, not `count(*)`: the join to `submissions` repeats a
variant once per submission, so `count(*)` reported one duplicated variant with
three submissions as three copies.

- **No rows** → 027 only adds its constraint.
- **`distinct_keys` > 1** → ⚠️ AUDIT B-17 in the data: same variant number,
  different keys. 027 keeps the newest. **Save the output** — after 027 it is
  the only record that the ambiguity existed:

```bash
docker compose exec -T postgres psql -U testova -d testova_db -c "\copy (SELECT v.project_id, v.variant_number, count(DISTINCT v.id) AS copies, count(DISTINCT v.answer_key::text) AS distinct_keys, count(s.id) AS submissions_affected FROM variants v LEFT JOIN submissions s ON s.variant_id = v.id GROUP BY v.project_id, v.variant_number HAVING count(DISTINCT v.id) > 1) TO STDOUT WITH CSV HEADER" > ~/preflight_duplicates_$(date +%F).csv
```

### 2b. What 033 will backfill

```bash
docker compose exec -T postgres psql -U testova -d testova_db -c \
  "SELECT count(DISTINCT project_id) AS projects_with_variants, count(*) AS variants FROM variants;" \
  | tee -a "$DEPLOY_LOG"
```

After 033 there must be exactly `projects_with_variants` editions and zero
unlinked variants (§5).

---

## 3. Backup, and prove it restores

A backup you have not restored is not a backup.

```bash
mkdir -p ~/backups
BACKUP=~/backups/testova_pre_v0.41_$(date +%F_%H%M).dump
docker compose exec -T postgres pg_dump -U testova -d testova_db -Fc > "$BACKUP"
ls -lh "$BACKUP"        # a zero or few-hundred-byte file means the dump failed

docker compose exec -T postgres createdb -U testova restore_check
docker compose exec -T postgres pg_restore -U testova -d restore_check --no-owner < "$BACKUP"

for db in restore_check testova_db; do
  docker compose exec -T postgres psql -U testova -d "$db" -Atc \
    "SELECT 'users',count(*) FROM users UNION ALL SELECT 'projects',count(*) FROM projects UNION ALL SELECT 'variants',count(*) FROM variants UNION ALL SELECT 'submissions',count(*) FROM submissions UNION ALL SELECT 'check_results',count(*) FROM check_results;"
done
```

`pg_restore` must print **no errors** and the two count blocks must **match**.
If not, stop: there is nothing to roll back to. Then:

```bash
docker compose exec -T postgres dropdb -U testova restore_check
echo "backup: $BACKUP" >> "$DEPLOY_LOG"
```

---

## 4. Build the release image FIRST — the old bot keeps running

```bash
git fetch origin --tags
git checkout --detach v0.41
DEPLOY_SHA=$(git rev-parse 'v0.41^{commit}')
echo "deploying $DEPLOY_SHA" | tee -a "$DEPLOY_LOG"
test "$(git rev-parse HEAD)" = "$DEPLOY_SHA" && echo "checkout OK"

docker compose build bot admin
docker compose images | tee -a "$DEPLOY_LOG"

# Prove the NEW image carries the new migrations. Reads scripts only, no DB.
docker compose run --rm --no-deps bot alembic heads     # expect: 035 (head)
```

If `alembic heads` does not print `035 (head)`, the image is not the release —
**stop here**; nothing has been changed yet and the old bot is still serving.

### Admin panel secrets (first production deploy of the panel)

`.env` must carry these before §7, or the panel starts with login disabled:

```bash
docker compose run --rm --no-deps admin python -m app.admin.web.security 'the-admin-password'
#   → paste the printed ADMIN_PASSWORD_HASH=... into .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
#   → ADMIN_SESSION_SECRET=<that> in .env
grep -cE '^ADMIN_PASSWORD_HASH=.+' .env && grep -cE '^ADMIN_SESSION_SECRET=.+' .env   # both: 1
```

Leave `ADMIN_TRUSTED_PROXIES` empty — the panel stays published on
`127.0.0.1:8001` only and is reached through an SSH tunnel (`docs/DEPLOY.md`).

---

## 5. Stop both writers, migrate with the new image, verify

```bash
docker compose stop bot admin          # "no such service"/not running for admin is fine
docker compose ps

docker compose run --rm bot alembic current      # the revision §1 recorded
docker compose run --rm bot alembic upgrade head # 027 and 033 each run in a transaction
docker compose run --rm bot alembic current      # expect: 035 (head)
```

A failed migration rolls back its own transaction and leaves `alembic current`
at the last revision that succeeded. Do not start the services on a partial
upgrade — go to §9.

Verify the schema actually landed (replaces the old "does
`uq_variants_project_number` exist" check — **033 drops that constraint**):

```bash
docker compose exec -T postgres psql -U testova -d testova_db <<'SQL' | tee -a "$DEPLOY_LOG"
SELECT 'alembic_version = 033'           AS check, (SELECT version_num FROM alembic_version) = '033' AS ok
UNION ALL SELECT '027: no duplicate numbers per edition',
  NOT EXISTS (SELECT 1 FROM variants GROUP BY edition_id, variant_number HAVING count(*) > 1)
UNION ALL SELECT '033: one edition per project with variants',
  (SELECT count(*) FROM exam_editions) = (SELECT count(DISTINCT project_id) FROM variants)
UNION ALL SELECT '033: every variant linked',
  NOT EXISTS (SELECT 1 FROM variants WHERE edition_id IS NULL)
UNION ALL SELECT '033: uq_variants_edition_number present',
  EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_variants_edition_number')
UNION ALL SELECT '033: uq_variants_project_number gone',
  NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_variants_project_number')
UNION ALL SELECT '033: variants_frozen trigger',
  EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'variants_frozen' AND NOT tgisinternal)
UNION ALL SELECT '032: sheet_checks table',
  to_regclass('public.sheet_checks') IS NOT NULL
UNION ALL SELECT '030/031: indexes',
  (SELECT count(*) FROM pg_indexes WHERE indexname IN
     ('ix_admin_log_created_at','ix_admin_log_action_created_at','ix_gemini_usage_kind_user_id')) = 3;
SQL
```

**Every `ok` must be `t`.** The edition count must equal §2b's
`projects_with_variants`.

---

## 6. REQUIRED GATE — crop cleanup dry run

The bot deletes unreferenced crop files in `temp_images/` the moment it starts
(and daily after). Teachers' figures live there permanently. Dry-run it with the
release image **before** starting anything:

```bash
docker compose run --rm bot python -m app.services.image_retention --dry-run | tee -a "$DEPLOY_LOG"
ls temp_images | wc -l
```

It prints `deleted_expired`, `deleted_orphaned`, `kept` and the file lists.
**Do not continue** unless the numbers make sense:
- `deleted_expired` — crops whose every owner (question or book figure) belongs
  to a project inactive for 30+ days. CP-1 made this count **book figures as
  owners**, so it should be no larger than under the old build.
- `deleted_orphaned` — files no row references, older than one hour.
- If either is a large share of `ls temp_images | wc -l`, stop and investigate
  before starting the bot: once it starts, the deletion is real.

---

## 7. Start

```bash
docker compose up -d bot admin          # no --build: the image from §4 is the release
docker compose ps                        # postgres, redis, bot, admin: running/healthy

docker compose logs --since 5m bot | grep -E '"(storage_redis|polling_mode|temp_images_pruned)"'
curl -s http://127.0.0.1:8001/healthz    # {"status":"ok"}
```

`storage_redis` (not `storage_memory_fallback`) and `polling_mode` must appear.
`temp_images_pruned` must match what §6 predicted.

---

## 8. Post-deploy verification

### 8a. This release — do all of these

**A generated test prints its edition code.** Have a teacher (or you) generate
variants from an uploaded test. Each variant header reads **`Variant N · CODE`**
and the answer key columns read **`N-Variant · CODE`**. Then:

```bash
docker compose exec -T postgres psql -U testova -d testova_db -c \
  "SELECT e.created_at, e.code, e.edition_number, e.source, e.variant_count
     FROM exam_editions e WHERE e.source <> 'legacy' ORDER BY e.created_at DESC LIMIT 3;"
```

**One real answer-sheet grade, end to end** (photo → result):

```bash
docker compose exec -T postgres psql -U testova -d testova_db <<'SQL'
SELECT status, flow, variant_number, edition_id IS NOT NULL AS has_edition, updated_at
  FROM sheet_checks ORDER BY created_at DESC LIMIT 3;
SELECT checked_at, score, total, edition_id IS NOT NULL AS has_edition
  FROM check_results WHERE project_id IS NOT NULL ORDER BY checked_at DESC LIMIT 3;
SELECT created_at, call_site, user_id IS NOT NULL AS attributed, total_tokens
  FROM gemini_usage WHERE kind = 'grade' ORDER BY created_at DESC LIMIT 3;
SQL
```

Pass: the sheet is `graded` with an edition; its `check_results` row has an
edition; the newest `grade` usage row is attributed with `total_tokens > 0`.
(`total_tokens = 0` → capture the row and log lines: a `sheet_reader`
instrumentation bug. `attributed = f` → `grep gemini_usage_unattributed`.)

**The admin panel** — from your own machine:

```bash
ssh -L 8001:localhost:8001 you@your-server
# browse http://localhost:8001, log in, open Dashboard, Users, Costs, Audit
```

**Errors since start:**

```bash
docker compose logs --since 15m bot admin | grep -iE '"level": "(error|critical)"'
```

### 8b. Only if §1 recorded **026** — the v0.39 checks never ran on production

If §1 printed 027 or later, v0.39 was already live and these were verified then;
skip to §9.

- **Mojibake repair.** Watch for `book_text_layer_codepage_repaired` with
  `score_after` near 1.0 and `residual` 0.0
  (`docker compose logs -f bot | grep book_text_layer_codepage_repaired`).
  Themes still holding mojibake:
  ```bash
  docker compose exec -T postgres psql -U testova -d testova_db -c "SELECT b.title, t.title, round(length(regexp_replace(t.extracted_text, '[^À-ÿ]', '', 'g'))::numeric / length(t.extracted_text), 3) AS latin1_share FROM books b JOIN book_themes t ON t.book_id = b.id WHERE t.extracted_text IS NOT NULL AND length(t.extracted_text) > 500 AND length(regexp_replace(t.extracted_text, '[^À-ÿ]', '', 'g'))::numeric / length(t.extracted_text) > 0.5 ORDER BY 3 DESC;"
  ```
  The list should shrink to empty as teachers use those books.
- **Book-generation shortfall wording.** A short page-range generation must
  name the real cause — "matnda kam ma'lumot" when the section was thin, the
  grounding-gate wording when questions were rejected. Cross-check
  `grep book_generation_questions_dropped`.
- **Grandfathered prices.** Every paid user still shows the new price until
  recorded: `/setprice <telegram_id> 50000` (only the price — never `/plan`,
  which resets their period). Who is still on the fallback:
  ```bash
  docker compose exec -T postgres psql -U testova -d testova_db -c "SELECT telegram_id, full_name, price_som_at_purchase FROM users WHERE monthly_variant_limit IS NOT NULL ORDER BY price_som_at_purchase NULLS FIRST;"
  ```

---

## 9. Rollback

Decide **which case you are in** first:

```bash
docker compose exec -T postgres psql -U testova -d testova_db -Atc \
  "SELECT count(*) FROM (SELECT project_id FROM exam_editions GROUP BY 1 HAVING count(*) > 1) x;"
```

### Case A — the result is 0 (no project has a second edition yet)

Schema and code can go back without the dump, to the revision §1 recorded
(shown here for 029; use yours):

```bash
docker compose stop bot admin
docker compose run --rm bot alembic downgrade 029   # 033 → 032 → 031 → 030 → 029
git checkout --detach <the SHA §1 recorded>
docker compose build bot && docker compose up -d bot
```

| Downgrade | What is lost |
|---|---|
| **035 → 034** | Every online test, attempt, answer and violation log. Nothing else. |
| **034 → 033** | Every recorded payment. Nothing else — the ledger gates no access, so dropping it changes only what the Revenue page can report. |
| **033 → 032** | Every edition row and its code, `variants.open_positions`, `edition_id` on `check_results`/`sheet_checks`; `uq_variants_project_number` is restored, the trigger dropped. Variants and submissions stay. |
| **032 → 031** | All `sheet_checks` rows (per-photo grading records). `check_results` stay. |
| **031 → 030**, **030 → 029** | Indexes only. |
| **029 → 028**, **028 → 027** | Recorded purchase prices; the lifetime trial-check counter. |
| **027 → 026** | The constraint only — **not** the rows 027 deleted (below). |

The admin panel does not exist before v0.41's code: leave it stopped.

### Case B — the result is > 0, or anything from 027 must come back

033's downgrade **refuses** once a project has two editions (restoring the
per-project constraint would mean deleting a delivered edition), and 027's
downgrade **cannot** bring back the duplicate rows it deleted or the submission
pointers it moved. The only way back is the §3 dump — and it loses **everything
written since the deploy** (new tests, grades, payments recorded):

```bash
docker compose stop bot admin
docker compose exec -T postgres dropdb -U testova testova_db
docker compose exec -T postgres createdb -U testova testova_db
docker compose exec -T postgres pg_restore -U testova -d testova_db --no-owner < "$BACKUP"
git checkout --detach <the SHA §1 recorded>
docker compose build bot && docker compose up -d bot
```

Migrations are one linear chain (`026 → … → 035`); Alembic enforces the order
both ways.

---

## Quick reference

```bash
# 1  record SHA + alembic current + ps + images into $DEPLOY_LOG
# 2  pre-flight: 027 duplicates (count DISTINCT v.id), projects_with_variants
# 3  backup + RESTORE TEST into restore_check
git fetch origin --tags && git checkout --detach v0.41
docker compose build bot admin
docker compose run --rm --no-deps bot alembic heads   # 035 (head)
# admin secrets in .env
docker compose stop bot admin
docker compose run --rm bot alembic upgrade head
docker compose run --rm bot alembic current           # 035 (head)
# 5  schema verification SQL: every ok = t
docker compose run --rm bot python -m app.services.image_retention --dry-run   # GATE
docker compose up -d bot admin
# 8a edition code on PDFs, one real grade, admin panel via tunnel, error grep
# 8b only if §1 recorded 026
```
