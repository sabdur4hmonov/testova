"""
Repro for the Multi-Source generation crash.

Part 1: synthetic two-source pool with COLLIDING numbers 1..30 (some images),
run the real generation functions for 3 variants x 30.

Part 2: load the REAL failing session from Postgres via the SAME _load_pool
the "Davom etish" handler calls, print one real question dict's key-shape and
where answer keys live, then run the same generation on it.

Run:  ./venv311/Scripts/python.exe tests/repro_multisource.py
"""
from __future__ import annotations

import asyncio
import sys
import traceback

# This Windows console is cp1251; real question text carries √ ∈ etc.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

from app.services.variant_generator import (
    assemble_pool,
    pool_variant_builder,
    select_for_variants,
)


def _run_generation(pool, n, m, label):
    print(f"\n--- generation: {label} ({n} x {m}) ---")
    try:
        selections, stats = select_for_variants(pool, n, m)
        print(f"select_for_variants OK: {len(selections)} variants, "
              f"sizes={[len(s) for s in selections][:5]}..., max_reuse={stats['max_reuse']}")
        total, build_one = pool_variant_builder(selections)
        variants = [build_one(i) for i in range(1, total + 1)]
        print(f"pool_variant_builder OK: built {total} variants")
    except Exception:
        print(f"RESULT: {label} -> EXCEPTION in selection/build")
        print("=== TRACEBACK ===")
        traceback.print_exc()
        return

    # The steps _do_generate runs AFTER building variants — PDF renders.
    from app.services.pdf_generator import build_answer_key_pdf, build_variants_pdf
    try:
        pdf = build_variants_pdf(variants, "Ko'p manbali test")
        print(f"build_variants_pdf OK: {len(pdf)} bytes")
    except Exception:
        print(f"RESULT: {label} -> EXCEPTION in build_variants_pdf")
        print("=== TRACEBACK ===")
        traceback.print_exc()
        return
    try:
        key = build_answer_key_pdf(variants, "Ko'p manbali test")
        print(f"build_answer_key_pdf OK: {len(key)} bytes")
    except Exception:
        print(f"RESULT: {label} -> EXCEPTION in build_answer_key_pdf")
        print("=== TRACEBACK ===")
        traceback.print_exc()
        return
    print(f"RESULT: {label} -> ALL OK (incl. PDFs)")


# ── Part 1: synthetic colliding pool ─────────────────────────────────────────

def make_source(letter, with_images):
    qs = []
    for n in range(1, 31):
        q = {
            "question_id": f"{letter}{n}",
            "question_number": n,                # COLLIDING across sources
            "question_text": f"Source {letter} question {n}",
            "options": {"A": f"{letter}{n}a", "B": f"{letter}{n}b",
                        "C": f"{letter}{n}c", "D": f"{letter}{n}d"},
            "correct_answer": "ABCD"[n % 4],
            "has_image": False, "image_path": None,
            "group_id": None, "group_context": None,
        }
        if with_images and n in (3, 7):
            q["has_image"] = True
            q["image_path"] = f"temp_images/{letter}_q{n}.png"
        qs.append(q)
    return qs


def part1_synthetic():
    print("=" * 60)
    print("PART 1: SYNTHETIC (colliding numbers 1..30 x 2 sources)")
    print("=" * 60)
    srcA = make_source("A", True)
    srcB = make_source("B", True)
    try:
        pool, collapsed, siblings = assemble_pool([srcA, srcB])
        print(f"assemble_pool OK: pool={len(pool)}, collapsed={len(collapsed)}, "
              f"siblings={len(siblings)}")
    except Exception:
        print("assemble_pool EXCEPTION")
        traceback.print_exc()
        return
    _run_generation(pool, 3, 30, "synthetic 3x30")


# ── Part 2: real saved session ───────────────────────────────────────────────

async def part2_real():
    print("\n" + "=" * 60)
    print("PART 2: REAL SAVED SESSION")
    print("=" * 60)
    try:
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models.builder import BuilderSession, BuilderSource
        from app.models.question import Question
        from app.bot.handlers.multi_source import _load_pool
    except Exception:
        print("could not import app modules:")
        traceback.print_exc()
        return

    try:
        async with async_session_factory() as session:
            res = await session.execute(
                select(BuilderSession).order_by(BuilderSession.created_at.desc()).limit(5)
            )
            sessions = list(res.scalars().all())
    except Exception:
        print("DB NOT REACHABLE from this environment — cannot load the real")
        print("session here. Run this same file on the machine hosting the bot")
        print("(its Postgres) to capture the real traceback. Error was:")
        traceback.print_exc()
        return

    if not sessions:
        print("No builder sessions found in the DB.")
        return

    print(f"Found {len(sessions)} recent builder session(s). Using the latest.")
    bs = sessions[0]
    session_id = str(bs.id)
    print(f"session_id={session_id} status={bs.status}")

    # Show sources + how many questions each, and one real question's key-shape
    async with async_session_factory() as session:
        res = await session.execute(
            select(BuilderSource).where(BuilderSource.session_id == bs.id)
        )
        sources = list(res.scalars().all())
        print(f"sources: {len(sources)}")
        for s in sources:
            print(f"  - {s.filename}: {s.question_count} q, "
                  f"key_complete={s.key_complete}, project={s.project_id}")

        if sources:
            qres = await session.execute(
                select(Question).where(Question.project_id == sources[0].project_id).limit(1)
            )
            r = qres.scalar_one_or_none()
            if r:
                print("\nONE REAL Question ROW (answer key is INLINE on the row):")
                print(f"  id={r.id!r} ({type(r.id).__name__})")
                print(f"  question_number={r.question_number!r} ({type(r.question_number).__name__})")
                print(f"  correct_answer={r.correct_answer!r}")
                print(f"  option_a={r.option_a!r}")
                print(f"  has_image={r.has_image!r} image_path={r.image_path!r}")

    # Now the EXACT path the button handler uses
    try:
        pool, collapsed, siblings, srcs = await _load_pool(session_id)
        print(f"\n_load_pool OK: pool={len(pool)}, collapsed={len(collapsed)}, "
              f"siblings={len(siblings)}, sources={len(srcs)}")
        if pool:
            q0 = pool[0]
            print("\nREAL pooled question dict key-shape:")
            for k, v in q0.items():
                print(f"  {k}: {type(v).__name__}")
    except Exception:
        print("_load_pool EXCEPTION")
        traceback.print_exc()
        return

    # Reproduce with the parameters the teacher used (3x30 and 30x30 both fail)
    _run_generation(pool, 3, 30, "REAL 3x30")

    # ── The one remaining untested step: the DB write in _do_generate ────────
    print("\n--- DB write block (Project + Variant inserts), rolled back ---")
    try:
        import uuid as _uuid
        from app.models.project import Project, ProjectStatus
        from app.models.variant import Variant
        from app.services.variant_generator import (
            pool_variant_builder as _pvb, select_for_variants as _sfv,
        )
        sels, _st = _sfv(pool, 3, 30)
        tot, bld = _pvb(sels)
        variants = [bld(i) for i in range(1, tot + 1)]
        async with async_session_factory() as session:
            pool_project_id = _uuid.uuid4()
            session.add(Project(
                id=pool_project_id, user_id=bs.user_id,
                name=f"📚 Bank ({len(pool)} savol)",
                status=ProjectStatus.COMPLETED, question_count=len(pool),
            ))
            for v in variants:
                session.add(Variant(
                    project_id=pool_project_id,
                    variant_number=v["variant_number"],
                    question_order=v["question_order"],
                    option_mapping=v["option_mapping"],
                    answer_key=v["answer_key"],
                ))
            await session.flush()   # forces the INSERTs without committing
            print("DB write OK (flushed; rolling back)")
            await session.rollback()
    except Exception:
        print("RESULT: DB write -> EXCEPTION")
        print("=== TRACEBACK ===")
        traceback.print_exc()


class _FakeMsg:
    """Records Telegram calls without hitting the network."""
    def __init__(self, tag="msg"):
        self.tag = tag
        self.docs = []
    async def answer(self, text, **kw):
        print(f"  [tg] {self.tag}.answer: {ascii(text[:50])}")
        return _FakeMsg("status")
    async def edit_text(self, text, **kw):
        print(f"  [tg] {self.tag}.edit_text: {ascii(text[:50])}")
    async def answer_document(self, doc, caption=None, **kw):
        self.docs.append(doc)
        print(f"  [tg] {self.tag}.answer_document: {getattr(doc,'filename','?')}")


class _FakeState:
    def __init__(self, data):
        self._data = data
    async def get_data(self):
        return dict(self._data)
    async def update_data(self, **kw):
        self._data.update(kw)
    async def set_state(self, s):
        print(f"  [state] -> {s}")


class _FakeUser:
    def __init__(self, uid, lang="uz"):
        self.id = uid
        self.language = type("L", (), {"value": lang})()


async def part4_full_do_generate():
    print("\n" + "=" * 60)
    print("PART 4: REAL _do_generate (stub Telegram, real DB + compute)")
    print("=" * 60)
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.builder import BuilderSession
    from app.bot.handlers.multi_source import _do_generate

    async with async_session_factory() as session:
        res = await session.execute(
            select(BuilderSession).order_by(BuilderSession.created_at.desc()).limit(1)
        )
        bs = res.scalar_one_or_none()
    if not bs:
        print("no session")
        return
    session_id = str(bs.id)
    prev_status, prev_pool = bs.status, bs.pool_project_id
    msg = _FakeMsg()
    status = _FakeMsg("status")
    state = _FakeState({"builder_session_id": session_id, "n_variants": 3, "m_per_variant": 30})
    user = _FakeUser(bs.user_id)
    try:
        await _do_generate(msg, state, user, session_id, 3, 30, status, "uz")
        print("RESULT: _do_generate -> ALL OK")
    except Exception:
        print("RESULT: _do_generate -> EXCEPTION")
        print("=== TRACEBACK ===")
        traceback.print_exc()
        return

    # ── Restore the real session (don't leave it FINISHED / with a stray
    #    pool project) so the teacher's ACTIVE session is untouched. ──────────
    from app.models.builder import BuilderStatus
    from app.models.project import Project as _Proj
    async with async_session_factory() as session:
        r = await session.execute(
            select(BuilderSession).where(BuilderSession.id == bs.id)
        )
        s2 = r.scalar_one()
        created = s2.pool_project_id
        s2.status = prev_status
        s2.pool_project_id = prev_pool
        await session.flush()
        if created and created != prev_pool:
            pr = await session.execute(select(_Proj).where(_Proj.id == created))
            proj = pr.scalar_one_or_none()
            if proj:
                await session.delete(proj)   # cascade-deletes its variants
        await session.commit()
    print("(session restored to ACTIVE; stray pool project cleaned up)")


async def _async_main():
    await part2_real()
    await part4_full_do_generate()


if __name__ == "__main__":
    part1_synthetic()
    asyncio.run(_async_main())
