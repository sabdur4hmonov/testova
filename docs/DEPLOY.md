# Testova — Deploy Checklist

Fresh Ubuntu (or Oracle Cloud ARM64 free tier) → running bot.

## Architecture (what actually runs)

Three containers only: **postgres + redis + bot** (`docker-compose.yml`).
Everything else (Celery worker/beat/flower, the FastAPI admin API, nginx) has
been removed — Celery is dead code, admin is done via in-bot commands, and the
bot runs in **polling** mode so no reverse proxy is needed.

## ⚠️ Migrations run BEFORE the bot — always

Alembic is the **single source of truth** for the schema. The bot no longer
calls `create_all()` at startup, so the tables must already exist when it
starts. Run migrations first, every deploy:

```bash
docker compose run --rm bot alembic upgrade head
```

Then, and only then, start the bot:

```bash
docker compose up -d bot
```

If you start the bot against an empty DB without migrating first, it will fail
loudly (missing tables) — that is intentional. Do NOT work around it by
re-adding `create_all()`; run the migration.

## Step by step

1. Install Docker + the Compose plugin. With `ufw`, allow only SSH (22). The bot
   is outbound-only in polling mode; do **not** expose 5432/6379 to the world.
2. `git clone` the repo, then `cp .env.example .env` and fill in:
   - `BOT_TOKEN`
   - `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-2.5-flash`
   - **`ADMIN_IDS=[<your_telegram_id>]`** — JSON brackets are required; a bare or
     comma-separated value crashes the bot at startup.
   - `ADMIN_USERNAME` (shown in the "limit reached" message, no leading `@`)
   - `POSTGRES_PASSWORD` (strong; used by the postgres service)
   - `DEBUG=false`
3. Start the data services and wait for health:
   ```bash
   docker compose up -d postgres redis
   ```
4. **Run migrations** (see the warning above):
   ```bash
   docker compose run --rm bot alembic upgrade head
   ```
5. Start the bot:
   ```bash
   docker compose up -d bot
   ```
   Confirm the logs show `storage_redis` (not `storage_memory_fallback`) and
   `polling_mode`.
6. Set Gemini **Prepay auto-reload** so no teacher hits a $0 balance mid-exam.
7. Add a nightly DB backup, e.g.:
   ```bash
   0 3 * * * docker compose exec -T postgres pg_dump -U testova testova_db | gzip > /backups/testova_$(date +\%F).sql.gz
   ```
8. Smoke test: fresh upload → variants + answer key, then grade one sheet.
   Redeploy once and re-check that a regenerated variant **still shows its
   figures** — this proves the `temp_images` volume is mounted.

## Persistent volumes (do not lose these)

- `postgres_data` — users, quotas, projects, results.
- `redis_data` — FSM state.
- `./temp_images` — figure crops read back at variant-build time. **Bind-mounted**
  into the bot; without it, every redeploy silently blanks teachers' figures.
- `./logs` — rotating log files (see `app/utils/logging.py`).

## ARM64 (Oracle free tier, month 1)

The whole stack runs on ARM64. **Build the image on the ARM host itself** (or
`docker buildx build --platform linux/arm64`) — do not build on an x86 machine
and push. Moving to a paid x86 VPS later needs no code changes; just rebuild.
