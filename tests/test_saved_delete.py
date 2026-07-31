"""
Part D.1a — project deletion moved into the Saqlangan (Test tekshirish) list.
The picker now has a 🗑 per project (reusing the project_delete confirm flow),
and deletion works end to end (real Postgres).
"""
import uuid
from types import SimpleNamespace

import pytest

from app.bot.keyboards.inline import check_project_keyboard


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_saved_picker_has_grade_and_delete_per_project():
    projects = [
        SimpleNamespace(id="p1", name="a.pdf", display_name=None),
        SimpleNamespace(id="p2", name="b.pdf", display_name="8B"),
    ]
    cbs = _cbs(check_project_keyboard(projects, "uz"))
    assert "check_project:p1" in cbs and "project_delete:p1" in cbs
    assert "check_project:p2" in cbs and "project_delete:p2" in cbs
    assert "cancel" in cbs


async def _engine():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1 FROM projects LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


class _CBMsg:
    def __init__(self):
        self.edits = []
    async def edit_text(self, text, **k):
        self.edits.append(text)


class _CB:
    def __init__(self, data):
        self.data = data
        self.message = _CBMsg()
    async def answer(self, *a, **k):
        pass


async def test_delete_confirm_removes_project(monkeypatch):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.project import Project, ProjectStatus
    from app.models.user import User
    from app.bot.handlers import projects

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(projects, "async_session_factory", sm)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            u = User(telegram_id=tg, full_name="Del T")
            s.add(u)
            await s.flush()
            p = Project(user_id=u.id, name="to-delete", status=ProjectStatus.COMPLETED)
            s.add(p)
            await s.commit()
            uid, pid = u.id, p.id
        db_user = SimpleNamespace(id=uid, language=SimpleNamespace(value="uz"))
        # the 🗑 button fires project_delete → confirm:delete_project (real handler)
        cb = _CB(f"confirm:delete_project:{pid}")
        await projects.handle_confirm_delete(cb, db_user)
        async with sm() as s:
            gone = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
            assert gone is None                      # actually deleted
        assert cb.message.edits and "chir" in cb.message.edits[0].lower()  # "o'chirildi"
    finally:
        async with sm() as s:
            await s.execute(delete(Project).where(Project.user_id == uid))
            await s.execute(delete(User).where(User.id == uid))
            await s.commit()
        await engine.dispose()
