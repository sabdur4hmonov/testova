"""docs/DEPLOY_RUNBOOK.md must target the migration head the code actually has.

WHY. The runbook written for v0.39 said "expect 029 (head)" while the code had
already moved to 033, and its verification step checked for a constraint
(`uq_variants_project_number`) that migration 033 drops. Following it would have
reported a successful deploy against the wrong schema — the same shape as the
dev bot that ran 033 code against a 031 database. Nothing noticed, because a
runbook is prose.

These tests make the two facts a runbook can drift on machine-checkable:

  * every "(head)" the runbook expects names the newest migration file;
  * the machine-readable target marker names it too, and the runbook's
    migration table lists every revision from the marker's base up to it.

When a migration is added, this fails until the runbook is updated.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "DEPLOY_RUNBOOK.md"
VERSIONS = ROOT / "migrations" / "versions"


def _newest_revision() -> str:
    revisions = sorted(p.name[:3] for p in VERSIONS.glob("[0-9][0-9][0-9]_*.py"))
    assert revisions, "no migration files found"
    return revisions[-1]


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_every_expected_head_in_the_runbook_is_the_newest_migration():
    newest = _newest_revision()
    expected = re.findall(r"\b(\d{3}) \(head\)", _runbook())
    assert expected, "the runbook never states which head to expect"
    stale = sorted(set(expected) - {newest})
    assert not stale, (
        f"docs/DEPLOY_RUNBOOK.md expects head {stale}, but the newest migration is "
        f"{newest} — update the runbook for the new migration(s)")


def test_the_target_marker_and_migration_table_cover_base_to_head():
    newest = _newest_revision()
    text = _runbook()
    marker = re.search(r"<!--\s*deploy-target:\s*base=(\d{3})\s+head=(\d{3})\s*-->", text)
    assert marker, "the runbook has no <!-- deploy-target: base=NNN head=NNN --> marker"
    base, head = marker.groups()
    assert head == newest, f"runbook targets head {head}, newest migration is {newest}"

    listed = set(re.findall(r"^\|\s*\*\*(\d{3})\*\*", text, flags=re.MULTILINE))
    wanted = {f"{n:03d}" for n in range(int(base) + 1, int(head) + 1)}
    missing = sorted(wanted - listed)
    assert not missing, f"the runbook's migration table does not describe {missing}"
