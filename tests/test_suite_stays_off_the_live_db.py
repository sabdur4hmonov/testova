"""Structural guard: no test may open the live database or act as a real account.

WHY. Until batches 1-4 of the test-DB isolation work, 27 test files built an
engine on `settings.DATABASE_URL` — the LIVE testova_db — and cleaned up in
`finally`. That cleanup could only reach tables with an FK to `users`, so:

  * 6,591 of 8,793 gemini_usage rows (75%) were orphaned test rows, and the
    admin panel's Costs page rendered 140 phantom users out of 143;
  * all 3,146 admin_log rows were test-generated, and every one named the
    REAL admin (8206475760) as the actor, because eight files passed that id
    — so the Audit page showed actions that never happened, and they could not
    be separated from real ones by actor.

Both were purged (dumps under logs/deleted-rows/). This file makes the two
mechanisms fail loudly if they come back, instead of silently accumulating
for another month:

  1. no test source reads `settings.DATABASE_URL` except the two that must:
     conftest.py (derives the per-run database from it) and
     test_usage_logging_stays_out_of_the_app_db.py (opens the live DB
     read-only, because checking that nothing landed there is its job);
  2. no test source contains an integer literal equal to a configured admin
     id — use tests/synthetic_ids.py instead.

Checked on the AST, so docstrings and comments that merely MENTION
DATABASE_URL (several explain this history) do not trip it.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.config import settings

from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT, TEST_ADMIN_ID, TEST_TARGET_ID

TESTS_DIR = Path(__file__).resolve().parent

MAY_READ_DATABASE_URL = {
    "conftest.py",
    "test_usage_logging_stays_out_of_the_app_db.py",
    # Borrows only the server address, then points every connection at port 1
    # under another database name — it never reaches the live database.
    "test_live_db_guard.py",
}


def _sources():
    files = sorted(TESTS_DIR.glob("*.py"))
    assert len(files) > 100, f"found only {len(files)} test sources — wrong directory?"
    for path in files:
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_only_the_allowlisted_files_touch_database_url():
    offenders = []
    for path, tree in _sources():
        if path.name in MAY_READ_DATABASE_URL:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "DATABASE_URL":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these tests read settings.DATABASE_URL, i.e. the LIVE database. Take the "
        "conftest `engine` fixture instead (per-run database, dropped at exit): "
        + ", ".join(offenders)
    )


def test_no_test_uses_a_configured_admin_id_as_a_literal():
    real = {int(i) for i in settings.ADMIN_IDS}
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and type(node.value) is int
                    and node.value in real):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these tests hard-code a real admin's telegram_id; use "
        "tests.synthetic_ids.TEST_ADMIN_ID: " + ", ".join(offenders)
    )


def test_the_synthetic_ids_cannot_be_real_accounts():
    for synthetic in (TEST_ADMIN_ID, TEST_TARGET_ID):
        assert synthetic >= TELEGRAM_USER_ID_LIMIT == 2**52   # beyond any Telegram user id
        assert synthetic < 2**53                               # exact as a JS number (panel JSON)
        assert synthetic not in settings.ADMIN_IDS
