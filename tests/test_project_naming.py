"""Phase 3: project display_name, migration 006, label fallback, states, header."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from app.bot.handlers.checking import build_group_result
from app.bot.keyboards.inline import check_project_keyboard
from app.bot.states.forms import BuilderStates, CheckingStates, UploadStates
from app.database import Base

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "006_project_naming.py"
)


# ── migration 006 ─────────────────────────────────────────────────────────────

def test_migration_006_chain():
    assert _MIGRATION.exists()
    spec = importlib.util.spec_from_file_location("mig006", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "006"
    assert mod.down_revision == "005"
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_projects_new_columns_registered():
    import app.models  # noqa: F401  (registers tables)
    cols = Base.metadata.tables["projects"].columns
    for name in ("display_name", "checking_mode", "exam_start_time",
                 "exam_end_time", "expires_at"):
        assert name in cols
    assert cols["display_name"].nullable


# ── display_name label with fallback to name ─────────────────────────────────

def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_label_uses_display_name_when_set():
    projects = [SimpleNamespace(id="p1", name="test.pdf", display_name="8B")]
    texts = _btn_texts(check_project_keyboard(projects, "uz"))
    assert any("8B" in t for t in texts)
    assert not any("test.pdf" in t for t in texts)


def test_label_falls_back_to_name_when_display_name_none():
    projects = [SimpleNamespace(id="p1", name="test.pdf", display_name=None)]
    texts = _btn_texts(check_project_keyboard(projects, "uz"))
    assert any("test.pdf" in t for t in texts)


def test_label_falls_back_when_no_display_name_attr():
    # A bare object with no display_name attribute must still label by name.
    projects = [SimpleNamespace(id="p1", name="legacy.pdf")]
    texts = _btn_texts(check_project_keyboard(projects, "uz"))
    assert any("legacy.pdf" in t for t in texts)


# ── new states exist (existing ones preserved) ───────────────────────────────

def test_naming_first_states_exist():
    # Up-front naming states (asked at the START of each flow).
    assert hasattr(UploadStates, "waiting_for_test_name")
    assert hasattr(BuilderStates, "waiting_for_test_name")
    for s in ("waiting_for_manual_test_name", "waiting_for_saved_name",
              "waiting_for_manual_name"):
        assert hasattr(CheckingStates, s)


def test_phase3_post_gen_naming_states_removed():
    # The post-generation naming states/keyboard were replaced by naming-first.
    assert not hasattr(UploadStates, "waiting_for_project_name_choice")
    assert not hasattr(UploadStates, "waiting_for_project_name")
    assert not hasattr(BuilderStates, "waiting_for_builder_name")


# ── manual test name flows into the group header ─────────────────────────────

def test_group_header_includes_test_name():
    runs = [{"name": "Ali", "variant": 1, "score": 10, "total": 20, "grade": 3}]
    text, _ = build_group_result(runs, "uz", test_name="8B")
    first_lines = text.splitlines()[:2]
    assert any("8B —" in ln for ln in first_lines)  # "8B — <date>"


def test_group_header_date_only_without_name():
    runs = [{"name": "Ali", "variant": 1, "score": 10, "total": 20, "grade": 3}]
    text, _ = build_group_result(runs, "uz", test_name=None)
    # No "— " test-name separator on the header's second line.
    assert " — " not in text.splitlines()[1]
