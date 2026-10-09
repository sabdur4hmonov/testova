"""
Migration 033 (exam_editions) — the up/down cycle, run explicitly, on data.

033 is the first migration of this pass that is NOT purely additive (approved):
it drops `uq_variants_project_number`, makes `variants.edition_id` NOT NULL
after a backfill, and installs the `variants_frozen` trigger. So this test runs
the real Alembic chain on a database of its own that holds legacy rows, and
checks:

  1. upgrade 032 -> 033 creates exactly the models' columns (exam_editions,
     variants, sheet_checks, check_results) with the models' nullability;
  2. the backfill gives every project with variants ONE legacy edition, links
     every variant, and links saved check_results and sheet_checks — without
     touching submissions;
  3. the per-edition unique constraint replaces the per-project one, and the
     trigger exists and refuses a key rewrite;
  4. downgrade to 032 restores the old schema with every row intact;
  5. once a project really has two editions, downgrade REFUSES rather than
     silently deleting one.

Deploy note (CP-6): NOT NULL on variants.edition_id means the bot is stopped
before this migration runs — old code would insert variants without it.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.config import settings
from app.database import Base
from tests.conftest import _admin_db_url, db_url_for, new_test_db_name
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TABLES = ("exam_editions", "variants", "sheet_checks", "check_results")
TG = TELEGRAM_USER_ID_LIMIT + 533


def _admin(sql: str) -> None:
    async def _run():
        eng = create_async_engine(_admin_db_url(), poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")
        try:
            async with eng.connect() as conn:
                await conn.execute(text(sql))
        finally:
            await eng.dispose()
    asyncio.run(_run())


def _db(url: str, fn):
    async def _run():
        eng = create_async_engine(url, poolclass=NullPool)
        try:
            async with eng.begin() as conn:
                return await fn(conn)
        finally:
            await eng.dispose()
    return asyncio.run(_run())


def _schema(url: str) -> dict:
    async def fn(conn):
        def _read(sync_conn):
            insp = inspect(sync_conn)
            out = {}
            for t in TABLES:
                if not insp.has_table(t):
                    continue
                out[t] = {
                    "columns": {c["name"]: c["nullable"] for c in insp.get_columns(t)},
                    "uniques": {u["name"] for u in insp.get_unique_constraints(t)},
                }
            return out
        return await conn.run_sync(_read)
    return _db(url, fn)


def _model_columns(table: str) -> dict:
    return {c.name: c.nullable for c in Base.metadata.tables[table].columns}


IDS = {k: uuid.uuid4() for k in (
    "user", "p1", "p2", "p1v1", "p1v2", "p2v1", "sub", "cr_saved", "cr_manual",
    "sc_linked", "sc_unlinked",
)}
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


async def _seed_legacy(conn) -> None:
    q = lambda sql, **p: conn.execute(text(sql), p)  # noqa: E731
    await q("INSERT INTO users (id, telegram_id, full_name, language, subscription_plan, "
            "daily_projects_used, monthly_projects_used, total_projects, is_banned, "
            "is_admin, is_blocked) VALUES (:id, :tg, 'Mig', 'uz', 'free', 0, 0, 0, "
            "false, false, false)", id=IDS["user"], tg=TG)
    for key, name in (("p1", "P1"), ("p2", "P2")):
        await q("INSERT INTO projects (id, user_id, name, status, question_count) "
                "VALUES (:id, :u, :n, 'completed', 2)", id=IDS[key], u=IDS["user"], n=name)
    for key, project, number, answer, minutes in (
        ("p1v1", "p1", 1, '{"1": ["A"]}', 0),
        ("p1v2", "p1", 2, '{"1": ["B"]}', 5),
        ("p2v1", "p2", 1, '{"1": ["C"]}', 9),
    ):
        await q("INSERT INTO variants (id, project_id, variant_number, question_order, "
                "option_mapping, answer_key, created_at) VALUES (:id, :p, :n, '[]'::jsonb, "
                "'{}'::jsonb, CAST(:k AS jsonb), :ts)",
                id=IDS[key], p=IDS[project], n=number, k=answer,
                ts=T0 + timedelta(minutes=minutes))
    await q("INSERT INTO submissions (id, variant_id, student_answers, results) "
            "VALUES (:id, :v, '{}'::jsonb, '{}'::jsonb)", id=IDS["sub"], v=IDS["p1v1"])
    await q("INSERT INTO check_results (id, user_id, project_id, variant_number, score, total, "
            "wrong_answers, unclear) VALUES (:id, :tg, :p, 1, 1, 2, '[]'::jsonb, '[]'::jsonb)",
            id=IDS["cr_saved"], tg=TG, p=IDS["p1"])
    await q("INSERT INTO check_results (id, user_id, score, total, wrong_answers, unclear) "
            "VALUES (:id, :tg, 1, 2, '[]'::jsonb, '[]'::jsonb)", id=IDS["cr_manual"], tg=TG)
    await q("INSERT INTO sheet_checks (id, user_id, run_id, flow, status, variant_id) "
            "VALUES (:id, :tg, :r, 'saved', 'graded', :v)",
            id=IDS["sc_linked"], tg=TG, r=uuid.uuid4(), v=IDS["p2v1"])
    await q("INSERT INTO sheet_checks (id, user_id, run_id, flow, status) "
            "VALUES (:id, :tg, :r, 'manual', 'graded')",
            id=IDS["sc_unlinked"], tg=TG, r=uuid.uuid4())


async def _snapshot(conn) -> dict:
    one = lambda sql, **p: conn.execute(text(sql), p)  # noqa: E731
    editions = (await one(
        "SELECT project_id, id, edition_number, code, source, variant_count, created_at "
        "FROM exam_editions")).all()
    variants = dict((await one("SELECT id, edition_id FROM variants")).all())
    checks = dict((await one("SELECT id, edition_id FROM check_results")).all())
    sheets = dict((await one("SELECT id, edition_id FROM sheet_checks")).all())
    subs = (await one("SELECT count(*) FROM submissions WHERE variant_id = :v",
                      v=IDS["p1v1"])).scalar()
    triggers = (await one("SELECT count(*) FROM pg_trigger WHERE tgname = 'variants_frozen' "
                          "AND NOT tgisinternal")).scalar()
    return {"editions": {e.project_id: e for e in editions}, "variants": variants,
            "checks": checks, "sheets": sheets, "subs": subs, "triggers": triggers}


def test_migration_033_up_down_on_legacy_data(monkeypatch):
    db_name = new_test_db_name()
    url = db_url_for(db_name)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))

    _admin(f'CREATE DATABASE "{db_name}"')
    try:
        command.upgrade(cfg, "032")
        before = _schema(url)
        assert "exam_editions" not in before
        assert "uq_variants_project_number" in before["variants"]["uniques"]
        _db(url, _seed_legacy)

        # ── up ──────────────────────────────────────────────────────────────
        command.upgrade(cfg, "033")
        up = _schema(url)
        for table in TABLES:
            assert up[table]["columns"] == _model_columns(table), (
                f"migration 033 and the model disagree on {table}")
        assert "uq_variants_edition_number" in up["variants"]["uniques"]
        assert "uq_variants_project_number" not in up["variants"]["uniques"]
        assert {"uq_exam_editions_project_number", "uq_exam_editions_code"} <= up["exam_editions"]["uniques"]

        snap = _db(url, _snapshot)
        e1, e2 = snap["editions"][IDS["p1"]], snap["editions"][IDS["p2"]]
        assert len(snap["editions"]) == 2, "one legacy edition per project with variants"
        assert (e1.edition_number, e1.source, e1.variant_count) == (1, "legacy", 2)
        assert (e2.edition_number, e2.source, e2.variant_count) == (1, "legacy", 1)
        assert e1.created_at == T0, "a legacy edition dates from its earliest variant"
        assert e1.code != e2.code and len(e1.code) == 4
        assert snap["variants"] == {IDS["p1v1"]: e1.id, IDS["p1v2"]: e1.id, IDS["p2v1"]: e2.id}
        assert snap["checks"] == {IDS["cr_saved"]: e1.id, IDS["cr_manual"]: None}
        assert snap["sheets"] == {IDS["sc_linked"]: e2.id, IDS["sc_unlinked"]: None}
        assert snap["subs"] == 1, "a submission lost its variant"
        assert snap["triggers"] == 1

        async def _rewrite(conn):
            await conn.execute(text(
                "UPDATE variants SET answer_key = '{\"1\": [\"Z\"]}'::jsonb WHERE id = :v"),
                {"v": IDS["p1v1"]})
        with pytest.raises(Exception, match="immutable"):
            _db(url, _rewrite)

        # ── down (one edition per project: allowed, lossless) ────────────────
        command.downgrade(cfg, "032")
        down = _schema(url)
        assert "exam_editions" not in down
        assert down["variants"]["columns"] == before["variants"]["columns"]
        assert down["sheet_checks"]["columns"] == before["sheet_checks"]["columns"]
        assert down["check_results"]["columns"] == before["check_results"]["columns"]
        assert "uq_variants_project_number" in down["variants"]["uniques"]

        async def _counts(conn):
            out = []
            for sql in ("SELECT count(*) FROM variants", "SELECT count(*) FROM submissions",
                        "SELECT count(*) FROM check_results", "SELECT count(*) FROM sheet_checks",
                        "SELECT count(*) FROM pg_trigger WHERE tgname = 'variants_frozen'"):
                out.append((await conn.execute(text(sql))).scalar())
            return tuple(out)
        assert _db(url, _counts) == (3, 1, 2, 2, 0)

        # ── up again, then a real second edition: downgrade must refuse ──────
        # Pinned to 033, not "head": this test is about 033's up/down behaviour
        # and asserts the version lands back on 033 below. "head" only meant
        # 033 while 033 happened to be the newest migration, so every later
        # migration (034 added the payments ledger) broke it.
        command.upgrade(cfg, "033")
        again = _schema(url)
        for table in TABLES:
            assert again[table]["columns"] == _model_columns(table)

        async def _second_edition(conn):
            edition = uuid.uuid4()
            await conn.execute(text(
                "INSERT INTO exam_editions (id, project_id, edition_number, code, source, "
                "variant_count) VALUES (:id, :p, 2, 'ZZZZ', 'upload', 1)"),
                {"id": edition, "p": IDS["p1"]})
            await conn.execute(text(
                "INSERT INTO variants (id, project_id, edition_id, variant_number, "
                "question_order, option_mapping, answer_key) VALUES (:id, :p, :e, 1, "
                "'[]'::jsonb, '{}'::jsonb, '{\"1\": [\"D\"]}'::jsonb)"),
                {"id": uuid.uuid4(), "p": IDS["p1"], "e": edition})
        _db(url, _second_edition)

        with pytest.raises(Exception, match="more than one edition"):
            command.downgrade(cfg, "032")

        async def _version(conn):
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert _db(url, _version) == "033"
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def test_033_chains_from_032():
    import importlib.util

    path = MIGRATIONS / "versions" / "033_exam_editions.py"
    spec = importlib.util.spec_from_file_location("mig033", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("033", "032")
