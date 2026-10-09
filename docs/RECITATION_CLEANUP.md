# Silent-refusal cleanup — branch `fix/publib-detour-vision-page-skip`

Before this branch, a Vision batch Gemini refused (RECITATION/SAFETY) came back
empty and was accepted as blank pages, so a scanned page range could be saved
READY with pages missing. Possible only on v0.39+ (chunked Vision). This finds
those ranges on prod and resets them so the next pick re-reads them with the
fixed code. It also carries the stale book-kind check.

All three scripts run in the **current** prod image, fed on stdin. `ENV` below
means: on the VPS, in the repo directory, `.env` present.

| Script | Writes? |
|---|---|
| `app/tools/prod_kind_dryrun.py` | never (READ ONLY transaction) |
| `app/tools/prod_recitation_find.py` | never (READ ONLY transaction) |
| `app/tools/prod_recitation_reset.py` | only with `--apply`; backs up first |

---

## 1. Copy the scripts (from the dev machine — the branch is not pushed)

```bash
scp app/tools/prod_kind_dryrun.py app/tools/prod_recitation_find.py app/tools/prod_recitation_reset.py you@your-server:~/recitation/
```

## 2. Book-kind dry run (ENV) — paste the output to Claude

```bash
docker compose exec -T bot python - < ~/recitation/prod_kind_dryrun.py
```

`STALE` lines are what `reclassify_book_kind --apply` would change (step 7).
Do not apply anything yet.

## 3. Find the silent gaps (ENV) — paste the output to Claude

```bash
docker compose exec -T bot python - < ~/recitation/prod_recitation_find.py | tee logs/recitation_find.txt
```

No `SUSPECT theme` line → skip to step 5.

## 4. Reset the flagged ranges (ENV) — only if step 3 printed `SUSPECT`

Dry run, check the list matches step 3:

```bash
docker compose exec -T bot python - --input /app/logs/recitation_find.txt < ~/recitation/prod_recitation_reset.py
```

Then write:

```bash
docker compose exec -T bot python - --input /app/logs/recitation_find.txt --apply < ~/recitation/prod_recitation_reset.py
ls logs/data-fixes/
```

Each `RESET` line names its backup, `logs/data-fixes/theme_<id8>_before_recitation_reextract_<date>.json`
(old text, status, updated_at). Question pools, served logs, figure crops,
variants and editions are not touched. Re-running is harmless: an already-reset
range is reported `SKIP`.

## 5. Redeploy with this branch

The branch is `master` (v0.41 + CP-6 runbook) plus this fix. Push it, then
follow `docs/DEPLOY_RUNBOOK.md` with the branch's commit in place of tag
`v0.41`. **Write down the time the new bot started**:

```bash
FIX_DEPLOYED_AT=$(docker inspect -f '{{.State.StartedAt}}' $(docker compose ps -q bot)); echo "$FIX_DEPLOYED_AT"
```

## 6. Confirm nothing slipped through the deploy window (ENV)

A range reset in step 4 and picked before step 5 was re-read by the old code.
Re-run the find with the cutoff; ranges saved by the fixed code print `ok:`.

```bash
docker compose exec -T -e FIX_DEPLOYED_AT="$FIX_DEPLOYED_AT" bot python -m app.tools.prod_recitation_find | tee logs/recitation_find_after.txt
```

Any `SUSPECT` line → repeat step 4 with this file:

```bash
docker compose exec -T -e FIX_DEPLOYED_AT="$FIX_DEPLOYED_AT" bot python -m app.tools.prod_recitation_reset --input /app/logs/recitation_find_after.txt --apply
```

## 7. Fix stale book kinds (ENV) — only the ids step 2 marked `STALE`

```bash
docker compose exec -T bot python -m app.tools.reclassify_book_kind
docker compose exec -T bot python -m app.tools.reclassify_book_kind --apply <book_id> [<book_id> ...]
```

Backup: `logs/data-fixes/reclassify_book_kind_<UTC>.json`. A re-run reports
`every stored kind matches the detector`.
