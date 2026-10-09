# Testova — Deploy Checklist

Fresh Ubuntu (or Oracle Cloud ARM64 free tier) → running bot.

## Architecture (what actually runs)

Four containers: **postgres + redis + bot + admin** (`docker-compose.yml`).
Celery and the old FastAPI admin (`app.admin.main`, built on the dead
subscription model) have been removed — Celery is dead code, and
`app.admin.main` no longer exists in this repo. `app.admin.web` (the session-
cookie HTML dashboard covered below) replaced it.

The **bot** runs in **polling** mode, so it needs no reverse proxy — it makes
outbound connections only, nothing listens for inbound traffic on it. The
**admin panel is different**: it serves HTTP for a browser to log into, and
its login **will not work at all without HTTPS** (see the next section) — that
is new since this file was last accurate, and is the thing to get right before
assuming the panel is just "up."

## ⚠️ Migrations run BEFORE the bot — always

Alembic is the **single source of truth** for the schema. The bot no longer
calls `create_all()` at startup, so the tables must already exist when it
starts. Run migrations first, every deploy:

```bash
docker compose run --rm bot alembic upgrade head
```

> ### ⚠️ PENDING ON PRODUCTION: migrations **027 → 033** — follow `docs/DEPLOY_RUNBOOK.md`
>
> Production is behind by up to eight migrations, two of which rewrite data:
> **027** deletes duplicate `variants` rows, and **033** backfills exam editions,
> makes `variants.edition_id` NOT NULL and drops `uq_variants_project_number`.
> Deploy them with the v0.41 runbook, not with the generic steps below: it
> builds the release image **before** migrating (a `docker compose run --rm bot`
> against an old image migrates to the OLD head), stops **both** `bot` and
> `admin`, verifies the schema, gates on a crop-cleanup dry run, and has the
> rollback matrix.
>
> Delete this box once v0.41 is live on production.

Then, and only then, start the bot:

```bash
docker compose up -d bot
```

If you start the bot against an empty DB without migrating first, it will fail
loudly (missing tables) — that is intentional. Do NOT work around it by
re-adding `create_all()`; run the migration.

## Step by step

1. Install Docker + the Compose plugin. With `ufw`, allow only SSH (22). The
   admin panel's port is published to loopback only and reached via that same
   SSH connection (tunnel) or a reverse proxy you add yourself — see "Admin
   panel" below before opening anything else. The bot itself is outbound-only
   in polling mode; do **not** expose 5432/6379 to the world.
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

## Admin panel (`app/admin/web`, port 8001)

A session-cookie HTML dashboard — separate process, separate container
(`admin` in `docker-compose.yml`), same image as the bot, different
entrypoint (`admin_main.py`). It shares the bot's Postgres; it does not use
Redis and does not touch `storage/`/`temp_images/`, so it gets none of the
bot's volumes.

### It needs HTTPS to function AT ALL — not "recommended," mandatory

The session cookie is set `https_only=True`. Over plain HTTP the browser
**silently refuses to store it** — there is no error page, no warning banner,
just a login that appears to succeed (redirects to `/`) and then bounces you
straight back to `/login` forever. If the panel "isn't working" and every
symptom is "I log in and it just asks me to log in again," this is almost
certainly why. `admin_main.py` prints a startup warning to the same effect,
but only to its own stdout/logs — it does not appear anywhere in the browser.

You have exactly two ways to get HTTPS in front of it; **pick one**, this repo
does not pick for you:

1. **SSH tunnel (simplest, no certs, fine for a single admin).**
   ```bash
   ssh -L 8001:localhost:8001 you@your-server
   ```
   Then browse `http://localhost:8001` **on your own machine** — that hop is
   loopback-to-loopback over the tunnel, so the browser's same-origin view is
   `localhost`, which most browsers treat as a secure context even over
   plain HTTP, satisfying `https_only`. Nothing else to configure. The
   `admin` service already publishes only to `127.0.0.1:8001` on the server
   for exactly this path — it is not reachable from outside the server without
   the tunnel.

2. **A real reverse proxy with real TLS certs** (nginx, Caddy, or similar), if
   you want to reach the panel from a phone/laptop without an SSH client, or
   want it reachable by more than one admin. `docker/nginx.conf` in this repo
   is a **stale leftover from before the panel existed** — wrong upstream
   port, a `/webhook` location for a bot that has no listening port in polling
   mode — do not uncomment it as-is. Whatever proxy you stand up:
   - terminate TLS at the proxy (Let's Encrypt via certbot/Caddy's automatic
     HTTPS both work fine — this repo ships neither, since that choice depends
     on your domain and DNS, which a git commit can't supply);
   - proxy to `127.0.0.1:8001` (or expose the `admin` service on the compose
     network instead of the host, and proxy to `admin:8001`, if the proxy
     itself runs as a container on the same network);
   - set `ADMIN_TRUSTED_PROXIES` in `.env` to the proxy's address (`127.0.0.1`
     for a same-host proxy) — **do this even though it feels optional.**
     Leaving it empty behind a proxy collapses every client's login-lockout
     counter onto the proxy's own IP (one mistyped password from any admin
     locks out everyone for 15 minutes); setting it while NOT actually running
     a proxy lets anyone spoof `X-Forwarded-For` and bypass the lockout
     entirely (AUDIT B-10 — this is why the default is to trust nothing).

Either way, generate the password hash and set the two required secrets
before starting the container — an empty `ADMIN_PASSWORD_HASH` disables login
outright (fails closed, not a security hole, but the panel is then useless):

```bash
python -m app.admin.web.security 'your-password'   # prints ADMIN_PASSWORD_HASH=...
```

paste that plus a long random `ADMIN_SESSION_SECRET` into `.env` (an empty
secret still works, but mints a fresh key every restart — every admin gets
force-logged-out on every redeploy).

### Start it

```bash
docker compose up -d admin
```

It needs the same migrated schema the bot needs — if you're deploying both
for the first time, run migrations (see above) before starting either.

### Runbook

**Is it up?**
```bash
curl -s http://127.0.0.1:8001/healthz
```
`{"status":"ok"}` means the process is alive AND its database query round-
tripped. `{"status":"degraded"}` (HTTP 503) means the process is alive but
can't reach Postgres — check the postgres container before the admin one.
Nothing at all (connection refused) means the container itself is down.
This endpoint takes no login on purpose — it exists for exactly this check —
and by design tells you nothing else (no version, no internals): don't
extend it to return more without re-reading `app/admin/web/routers/health.py`'s
own reasoning first.

**Where do its logs go?**
Its own process logs go to stdout, same as any other container —
`docker compose logs -f admin`. Its **error-monitoring page** (`/alerts`)
reads a different thing: the bot's own structured log at `./logs/testova.log`
(bind-mounted read-only into the admin container), so a spike shown there is
the bot's errors, not the panel's own.

**Restart it:**
```bash
docker compose restart admin
```
Every logged-in admin session ends on restart unless `ADMIN_SESSION_SECRET`
is set in `.env` (see above) — with it set, sessions (up to 12h old) survive.

**Rebuild after a code change** (same image as the bot, so a bot rebuild
already covers it, but if you change only `app/admin/web/`):
```bash
docker compose build admin && docker compose up -d admin
```

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
