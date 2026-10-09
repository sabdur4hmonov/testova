"""
Every table that owns a temp_images/ file must keep it alive, and a cached book
figure whose file is gone must be recaptured — never silently dropped.

WHY. temp_images/ is the permanent figure store (see image_retention's module
docstring). Two tables point into it:

  * questions.image_path              — upload crops, and book figures copied
                                         onto each generated question;
  * book_theme_images.image_path /
    book_theme_images.gen_image_path   — CP24 figures cached per book theme.

`prune_expired_project_crops` only ever inventoried `questions`, so a live
book figure and its ≤384px generation copy were "orphans" after the 1h grace
and were deleted. Its EXPIRED branch was also checked before any other owner,
so a crop shared with a live project (book generation copies the same path
onto every project built from that theme) was deleted because ONE of its owners
had gone quiet.

And once a figure file is gone, `_get_or_capture_theme_images` still trusted
the book_theme_images rows, `_load_gen_images` skipped the unreadable file, and
generation carried on with fewer figures and no signal anywhere.

The `session` fixture is a SAVEPOINT session; the code under test opens its
own sessions, so a tiny factory hands it that same session.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 — registers every table
from app.bot.handlers import book
from app.models.book import Book, BookTheme, BookThemeImage
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.services import image_retention
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

OWNER_TG = TELEGRAM_USER_ID_LIMIT + 31  # never a real account (see synthetic_ids)
TWO_HOURS = 2 * 3600


class _Factory:
    """async_session_factory stand-in that yields the test's own session."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _aged_file(path: Path, age_seconds: float) -> str:
    path.write_bytes(b"png")
    t = time.time() - age_seconds
    os.utime(path, (t, t))
    return str(path)


@pytest.fixture
def crop_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "temp_images"
    d.mkdir()
    monkeypatch.setattr(image_retention, "IMAGE_SAVE_DIR", d)
    return d


async def _owner(session) -> User:
    u = User(telegram_id=OWNER_TG, full_name="Crop Owner")
    session.add(u)
    await session.flush()
    return u


async def _theme(session, owner: User) -> BookTheme:
    b = Book(user_id=owner.id, title="Figures Book", original_pdf_path="books/fig.pdf",
             pdf_checksum="f1a0e5", uploaded_by_user_id=OWNER_TG)
    session.add(b)
    await session.flush()
    th = BookTheme(book_id=b.id, title="1-5-betlar", order_index=0, page_start=1, page_end=5)
    session.add(th)
    await session.flush()
    return th


async def _project(session, owner: User, last_activity: datetime) -> Project:
    p = Project(user_id=owner.id, name="p", status=ProjectStatus.COMPLETED,
                created_at=last_activity, updated_at=last_activity)
    session.add(p)
    await session.flush()
    return p


def _question(project: Project, image_path: str) -> Question:
    return Question(project_id=project.id, question_number=1, question_text="q",
                    has_image=True, image_path=image_path)


# ── retention: every owner counts ────────────────────────────────────────────

async def test_a_live_book_figure_and_its_generation_copy_survive_the_sweep(
    session, crop_dir
):
    owner = await _owner(session)
    theme = await _theme(session, owner)
    hi = _aged_file(crop_dir / "q1_p3_book.png", TWO_HOURS)
    gen = _aged_file(crop_dir / "q1_p3_book_gen.png", TWO_HOURS)
    stray = _aged_file(crop_dir / "q9_p9_stray.png", TWO_HOURS)   # control: a real orphan
    session.add(BookThemeImage(theme_id=theme.id, image_path=hi, gen_image_path=gen,
                               page_number=3, order_index=0))
    await session.flush()

    res = await image_retention.prune_expired_project_crops(_Factory(session), dry_run=False)

    assert Path(hi).exists() and Path(gen).exists(), res
    assert res["orphaned_files"] == ["q9_p9_stray.png"], res
    assert not Path(stray).exists()


async def test_a_crop_shared_with_a_live_project_is_not_deleted_as_expired(
    session, crop_dir
):
    now = datetime.now(timezone.utc)
    owner = await _owner(session)
    shared = _aged_file(crop_dir / "q1_p12_shared.png", 40 * 86400)
    expired = await _project(session, owner, now - timedelta(days=60))
    live = await _project(session, owner, now - timedelta(days=1))
    session.add_all([_question(expired, shared), _question(live, shared)])
    await session.flush()

    res = await image_retention.prune_expired_project_crops(_Factory(session), dry_run=True)

    assert res["expired_files"] == [], res
    assert res["kept"] == 1, res


async def test_a_crop_shared_with_a_book_figure_is_not_deleted_as_expired(
    session, crop_dir
):
    now = datetime.now(timezone.utc)
    owner = await _owner(session)
    theme = await _theme(session, owner)
    shared = _aged_file(crop_dir / "q2_p4_fig.png", 40 * 86400)
    session.add(BookThemeImage(theme_id=theme.id, image_path=shared, page_number=4,
                               order_index=0))
    expired = await _project(session, owner, now - timedelta(days=60))
    session.add(_question(expired, shared))
    await session.flush()

    res = await image_retention.prune_expired_project_crops(_Factory(session), dry_run=True)

    assert res["expired_files"] == [], res


async def test_a_crop_owned_only_by_expired_projects_is_still_reaped(session, crop_dir):
    """Control: the fix must not turn retention into keep-everything."""
    now = datetime.now(timezone.utc)
    owner = await _owner(session)
    only = _aged_file(crop_dir / "q3_p1_old.png", 40 * 86400)
    session.add(_question(await _project(session, owner, now - timedelta(days=60)), only))
    await session.flush()

    res = await image_retention.prune_expired_project_crops(_Factory(session), dry_run=True)

    assert res["expired_files"] == ["q3_p1_old.png"], res


# ── recapture: a cached row is only as good as its files ─────────────────────

@pytest.fixture
def capture(session, crop_dir, monkeypatch):
    """Wire _get_or_capture_theme_images to the test session and a fake cropper
    that records each call and writes real files, as the real one does."""
    calls: list[tuple[int, int]] = []

    async def _read_file(_key):
        return b"%PDF-1.4 fake"

    def _capture(_pdf, page_start, page_end):
        calls.append((page_start, page_end))
        hi = _aged_file(crop_dir / f"q1_p{page_start}_recap.png", 0)
        gen = _aged_file(crop_dir / f"q1_p{page_start}_recap_gen.png", 0)
        return [{"image_path": hi, "gen_image_path": gen,
                 "page_number": page_start, "order_index": 0}]

    monkeypatch.setattr(book, "async_session_factory", _Factory(session))
    monkeypatch.setattr(book.storage, "read_file", _read_file)
    monkeypatch.setattr(book.book_images, "theme_has_figures", lambda *_a: True)
    monkeypatch.setattr(book.book_images, "capture_theme_images", _capture)
    return calls


async def _rows(session, theme):
    return (await session.execute(
        select(BookThemeImage).where(BookThemeImage.theme_id == theme.id)
        .order_by(BookThemeImage.order_index)
    )).scalars().all()


async def test_cached_figures_whose_files_exist_are_reused(session, crop_dir, capture):
    """Control: recapture must not fire when nothing is missing."""
    owner = await _owner(session)
    theme = await _theme(session, owner)
    hi = _aged_file(crop_dir / "q1_p2_cached.png", 0)
    gen = _aged_file(crop_dir / "q1_p2_cached_gen.png", 0)
    session.add(BookThemeImage(theme_id=theme.id, image_path=hi, gen_image_path=gen,
                               page_number=2, order_index=0))
    await session.flush()

    out = await book._get_or_capture_theme_images([str(theme.id)])

    assert capture == []
    assert out[str(theme.id)] == [{"image_path": hi, "gen_image_path": gen}]


async def test_a_missing_crop_invalidates_the_theme_and_recaptures(
    session, crop_dir, capture
):
    owner = await _owner(session)
    theme = await _theme(session, owner)
    present = _aged_file(crop_dir / "q1_p1_ok.png", 0)
    gone = str(crop_dir / "q2_p1_gone.png")           # row survives, file does not
    session.add_all([
        BookThemeImage(theme_id=theme.id, image_path=present, page_number=1, order_index=0),
        BookThemeImage(theme_id=theme.id, image_path=gone, page_number=1, order_index=1),
    ])
    await session.flush()

    out = await book._get_or_capture_theme_images([str(theme.id)])

    assert capture == [(1, 5)], "a missing figure was not recaptured"
    recaptured = str(crop_dir / "q1_p1_recap.png")
    assert out[str(theme.id)] == [
        {"image_path": recaptured, "gen_image_path": str(crop_dir / "q1_p1_recap_gen.png")}
    ]
    rows = await _rows(session, theme)
    assert [r.image_path for r in rows] == [recaptured], "stale rows were not replaced"


async def test_a_missing_generation_copy_also_recaptures(session, crop_dir, capture):
    owner = await _owner(session)
    theme = await _theme(session, owner)
    hi = _aged_file(crop_dir / "q1_p1_hi.png", 0)
    session.add(BookThemeImage(theme_id=theme.id, image_path=hi,
                               gen_image_path=str(crop_dir / "q1_p1_hi_gen.png"),
                               page_number=1, order_index=0))
    await session.flush()

    await book._get_or_capture_theme_images([str(theme.id)])

    assert capture == [(1, 5)], "a missing generation copy was not recaptured"
