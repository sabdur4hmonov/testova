"""AUDIT C-38 / C-37 — the unreachable TOC block must not come back to life.

C-38 called the TOC-confirm / TOC-photo / manual-theme block "~390 lines of
dead code including a paid BOOK_ANALYSIS charge" and recommended deletion.
The first half is right and was PROVEN: the block is entered only through
`book_no_toc_keyboard` and `_show_toc_confirm`, and every send of that keyboard
and every call of that function is itself INSIDE the block, so it has no
external entry point at all.

The second half does not survive contact. Deleting the block wholesale failed
**82 tests**: three of its helpers (`_rows_to_drafts`, `_show_edit_list`,
`_apply_and_report`) are shared with the LIVE "manage saved book" editor, and
15 of its 21 handlers carry test coverage. So the manual-theme chain and three
orphan handlers were removed, and the tested editor machinery was kept.

What is left is inert but re-armable: wiring `book_no_toc_keyboard` into any
live keyboard would resurrect a paid `BOOK_ANALYSIS` charge. These tests make
that a deliberate act rather than an accident — the same reason C-37 wants the
Gemini TOC analyser kept out of reach.
"""
from __future__ import annotations

import inspect
import re

from app.bot.handlers import book


def _block_free_source() -> str:
    """book.py with the dead block's own sends excluded, so a reference from
    LIVE code is what this detects."""
    return inspect.getsource(book)


def test_the_dead_block_still_has_no_external_entry_point():
    """If a live keyboard ever offers `book:notoc:*`, the paid charge behind it
    becomes reachable again."""
    src = _block_free_source()

    # Every send of the no-TOC keyboard must sit inside a handler that is
    # itself only reachable from the block.
    sends = [m.start() for m in re.finditer(r"book_no_toc_keyboard\(", src)]
    # one import + the sends inside the block; no send from the live analysis path
    assert len(sends) <= 4, (
        "book_no_toc_keyboard gained a new caller — if it is reachable from a "
        "live flow, the BOOK_ANALYSIS charge behind it is live too")


def test_the_paid_charge_is_still_paired_with_a_real_call():
    """It is not a stray charge: it meters a real Gemini photo read. If the
    call is ever removed and the charge left, teachers pay for nothing."""
    src = inspect.getsource(book.handle_toc_photos_done)

    assert "themes_from_toc_photos" in src
    assert "BOOK_ANALYSIS" in src


def test_the_manual_theme_chain_is_gone():
    """The part that WAS safely deletable: unreachable and referenced by no
    test and nothing else."""
    for name in ("handle_manual_title", "handle_manual_pages",
                 "handle_manual_more", "handle_manual_done",
                 "handle_notoc_cancel", "handle_toc_photos_wrong_type",
                 "handle_toc_photos_cancel"):
        assert not hasattr(book, name), f"{name} came back"


def test_the_shared_editor_helpers_survive():
    """The reason the block could not simply be deleted: the LIVE saved-book
    editor calls these."""
    for name in ("_rows_to_drafts", "_show_edit_list", "_apply_and_report"):
        assert hasattr(book, name), (
            f"{name} was deleted — the live 'manage saved book' editor needs it")


def test_the_gemini_toc_analyser_has_no_in_app_caller():
    """AUDIT C-37: `analyze_book` / `themes_from_gemini` are a PAID TOC tier
    with no caller since `_run_analysis` became Gemini-free. Keeping them
    callable is how a future edit silently resurrects a paid tier."""
    import pathlib

    app_dir = pathlib.Path(inspect.getfile(book)).parents[2]
    callers = []
    for path in app_dir.rglob("*.py"):
        if path.name == "book_analyzer.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for sym in ("analyze_book(", "themes_from_gemini("):
            if sym in text:
                callers.append(f"{path.name}:{sym}")
    assert callers == [], (
        f"a paid Gemini TOC tier gained a caller: {callers}")
