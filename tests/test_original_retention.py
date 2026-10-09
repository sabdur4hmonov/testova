"""AUDIT C-8: orphaned-original reaping.

This code DELETES teacher-uploaded files, so the cases that must never delete
are tested as carefully as the case that must.

No database is needed: the reaper's only query is "give me every referenced
path", which a tiny stub answers exactly.
"""
from __future__ import annotations

import time

import pytest

from app.services import original_retention
from app.services.original_retention import prune_orphan_originals


class _Result:
    def __init__(self, keys):
        self._keys = keys

    def all(self):
        return [(k,) for k in self._keys]


class _Session:
    """Answers the two SELECTs in order: books, then projects."""

    def __init__(self, book_keys, project_keys):
        self._queued = [book_keys, project_keys]

    async def execute(self, _stmt):
        return _Result(self._queued.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(book_keys=(), project_keys=()):
    return lambda: _Session(list(book_keys), list(project_keys))


def _write(root, key, *, age_seconds=0):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 stub")
    if age_seconds:
        old = time.time() - age_seconds
        import os
        os.utime(path, (old, old))
    return path


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "STORAGE_TYPE", "local")
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    return tmp_path


OLD = original_retention.ORPHAN_GRACE_SECONDS + 3600


async def test_referenced_file_is_never_deleted(storage_root):
    """The whole safety contract: a row points at it, so it stays — however old."""
    key = "books/b1/original/keep.pdf"
    path = _write(storage_root, key, age_seconds=OLD * 10)

    report = await prune_orphan_originals(_factory(book_keys=[key]), dry_run=False)

    assert path.exists(), "a referenced original must never be deleted"
    assert report["deleted"] == 0
    assert report["orphans"] == []
    assert report["referenced"] == 1


async def test_orphan_past_grace_is_deleted(storage_root):
    orphan = _write(storage_root, "books/b1/original/gone.pdf", age_seconds=OLD)
    kept = _write(storage_root, "books/b2/original/keep.pdf", age_seconds=OLD)

    report = await prune_orphan_originals(
        _factory(book_keys=["books/b2/original/keep.pdf"]), dry_run=False
    )

    assert not orphan.exists()
    assert kept.exists()
    assert report["deleted"] == 1


async def test_orphan_inside_grace_is_kept(storage_root):
    """A file written seconds ago may belong to a flow that has not committed
    its row yet — deleting it would race the upload that created it."""
    fresh = _write(storage_root, "projects/p1/original/fresh.pdf")

    report = await prune_orphan_originals(_factory(), dry_run=False)

    assert fresh.exists()
    assert report["in_grace"] == 1
    assert report["deleted"] == 0


async def test_dry_run_deletes_nothing_but_reports(storage_root):
    orphan = _write(storage_root, "projects/p1/original/gone.pdf", age_seconds=OLD)

    report = await prune_orphan_originals(_factory(), dry_run=True)

    assert orphan.exists(), "dry_run must not touch the filesystem"
    assert report["deleted"] == 0
    assert [o["key"] for o in report["orphans"]] == ["projects/p1/original/gone.pdf"]


async def test_brake_refuses_when_most_files_look_orphaned(storage_root):
    """Guards the real failure mode: a reaper pointed at a database that does
    not own this storage directory sees nearly everything as an orphan.

    Dry-running against the dev box did exactly this — 146 of 153 files — after
    its database had been dropped and recreated.
    """
    paths = [
        _write(storage_root, f"projects/p{i}/original/f{i}.pdf", age_seconds=OLD)
        for i in range(10)
    ]

    report = await prune_orphan_originals(
        _factory(project_keys=["projects/p0/original/f0.pdf"]), dry_run=False
    )

    assert all(p.exists() for p in paths), "the brake must prevent every deletion"
    assert report["deleted"] == 0
    assert report["refused"] is not None
    assert report["orphan_fraction"] == 0.9


async def test_force_overrides_the_brake(storage_root):
    paths = [
        _write(storage_root, f"projects/p{i}/original/f{i}.pdf", age_seconds=OLD)
        for i in range(10)
    ]

    report = await prune_orphan_originals(_factory(), dry_run=False, force=True)

    assert not any(p.exists() for p in paths)
    assert report["deleted"] == 10
    assert report["refused"] is None


async def test_s3_mode_is_a_no_op(storage_root, monkeypatch):
    """No bucket listing is attempted — it reports and returns."""
    from app.config import settings
    monkeypatch.setattr(settings, "STORAGE_TYPE", "s3")
    kept = _write(storage_root, "books/b1/original/x.pdf", age_seconds=OLD)

    report = await prune_orphan_originals(_factory(), dry_run=False)

    assert kept.exists()
    assert report["skipped"] == "non-local storage"


async def test_unrelated_subtrees_are_out_of_scope(storage_root):
    """Only books/*/original and projects/*/original are ever considered."""
    temp = _write(storage_root, "temp/answer_sheets/photo.jpg", age_seconds=OLD)
    export = _write(storage_root, "exports/report.pdf", age_seconds=OLD)

    report = await prune_orphan_originals(_factory(), dry_run=False)

    assert temp.exists() and export.exists()
    assert report["scanned"] == 0
