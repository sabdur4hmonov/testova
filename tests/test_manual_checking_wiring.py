"""New states, models and migration are wired correctly."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.bot.states.forms import CheckingStates
from app.database import Base

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "005_manual_checking.py"
)


def test_new_checking_states_exist():
    # Existing states preserved.
    for s in ("waiting_for_project", "waiting_for_answer_sheet",
              "waiting_for_variant_number"):
        assert hasattr(CheckingStates, s)
    # New manual-flow states added to the SAME group.
    for s in ("choosing_check_mode", "waiting_for_key",
              "waiting_for_key_confirm", "waiting_for_manual_sheet"):
        assert hasattr(CheckingStates, s)


def test_models_registered_in_metadata():
    # Importing app.models registers every table on Base.metadata.
    import app.models  # noqa: F401
    tables = Base.metadata.tables
    assert "check_results" in tables
    assert "manual_check_sessions" in tables


def test_check_results_columns():
    import app.models  # noqa: F401
    cols = Base.metadata.tables["check_results"].columns
    for name in ("id", "user_id", "project_id", "manual_session_id",
                 "variant_number", "student_name", "score", "total",
                 "wrong_answers", "unclear", "checked_at"):
        assert name in cols
    # student_name reserved for a later phase but present now (no double migrate)
    assert cols["project_id"].nullable
    assert cols["manual_session_id"].nullable


def test_migration_005_chain():
    assert _MIGRATION.exists()
    spec = importlib.util.spec_from_file_location("mig005", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "005"
    assert mod.down_revision == "004"
    assert callable(mod.upgrade) and callable(mod.downgrade)
