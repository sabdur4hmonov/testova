"""Reset the READY page ranges prod_recitation_find.py flagged, so the next
teacher to pick one gets a fresh read under the fixed code.

DRY RUN BY DEFAULT. Nothing is written without --apply.

INPUT is the find script's output, verbatim, saved to a file. Every
"SUSPECT theme <uuid>" line is a range to reset (duplicates collapse); "ok:" and
"failed loudly" lines are ignored. A file without the find header is refused,
so a wrong file cannot be fed in by accident. An output with no SUSPECT line
does nothing and exits 0.

Per range, with --apply:
  1. re-read the row and skip it unless it is still READY, still a page range,
     and (with FIX_DEPLOYED_AT set) last saved BEFORE the fix went live;
  2. back it up to logs/data-fixes/theme_<id8>_before_recitation_reextract_<date>.json
     — the convention of the enterprise-1 backup (keys id, status,
     extracted_text, updated_at, all as strings). An existing file is never
     overwritten: a second backup the same day gets a _2, _3 ... suffix;
  3. set status='failed' and extracted_text=NULL, conditional on the row still
     having the updated_at just backed up — a range re-saved in between is
     left alone and reported as a conflict.

What it does NOT touch: the question pool (book_question_pool), the served log
(book_pool_served) and figure crops (book_theme_images) reference the theme
with ON DELETE CASCADE only — this is an UPDATE, the row is never deleted.
Variants and exam editions hold no reference to themes at all (questions are
copied into variants.questions_data). tests/test_recitation_reset.py proves
all of that against a real schema.

Runs on the VPS in the CURRENT prod image, fed on stdin. ./logs is
bind-mounted to /app/logs, so the find output saved there is readable inside
the container and the backups land on the host:

    docker compose exec -T bot python - --input /app/logs/recitation_find.txt < prod_recitation_reset.py
    docker compose exec -T bot python - --input /app/logs/recitation_find.txt --apply < prod_recitation_reset.py

See docs/RECITATION_CLEANUP.md for the full order.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

FIND_HEADER_RE = re.compile(r"^== \d+ Vision call\(s\) that sent pages and got nothing back$",
                            re.MULTILINE)
SUSPECT_RE = re.compile(r"SUSPECT theme ([0-9a-fA-F-]{36})\b")
DEFAULT_BACKUP_DIR = Path("logs") / "data-fixes"


class NotFindOutput(ValueError):
    pass


def parse_find_output(raw: str) -> list[uuid.UUID]:
    """Theme ids from the SUSPECT lines, first-seen order, de-duplicated."""
    if not FIND_HEADER_RE.search(raw or ""):
        raise NotFindOutput("input is not prod_recitation_find.py output (header missing)")
    seen: dict[uuid.UUID, None] = {}
    for match in SUSPECT_RE.finditer(raw):
        seen.setdefault(uuid.UUID(match.group(1)), None)
    return list(seen)


def _backup_path(backup_dir: Path, theme_id: uuid.UUID, today: date) -> Path:
    stem = f"theme_{str(theme_id)[:8]}_before_recitation_reextract_{today.isoformat()}"
    path = backup_dir / f"{stem}.json"
    n = 2
    while path.exists():
        path = backup_dir / f"{stem}_{n}.json"
        n += 1
    return path


async def reset_flagged(session_factory, theme_ids, *, apply: bool,
                        backup_dir=DEFAULT_BACKUP_DIR, fixed_at=None,
                        today: date | None = None) -> list[dict]:
    """One result per id: {id, action, reason?, backup?}. action is one of
    'reset', 'would reset', 'skip', 'conflict'."""
    today = today or date.today()
    backup_dir = Path(backup_dir)
    results = []
    for theme_id in theme_ids:
        async with session_factory() as s:
            row = (await s.execute(text(
                "select id, status, extracted_text, updated_at, "
                "coalesce(is_page_range,false) as is_page_range "
                "from book_themes where id=:t"), {"t": theme_id})).mappings().first()
            reason = None
            if row is None:
                reason = "no such theme"
            elif row["status"] != "ready":
                reason = f"status is {row['status']}, not ready (already reset or re-extracting)"
            elif not row["is_page_range"]:
                reason = "not a page range"
            elif fixed_at and row["updated_at"] >= fixed_at:
                reason = f"last saved {row['updated_at']:%Y-%m-%d %H:%M:%S} by the fixed code"
            if reason:
                results.append({"id": theme_id, "action": "skip", "reason": reason})
                continue
            if not apply:
                results.append({"id": theme_id, "action": "would reset",
                                "chars": len(row["extracted_text"] or "")})
                continue

            backup_dir.mkdir(parents=True, exist_ok=True)
            path = _backup_path(backup_dir, theme_id, today)
            snapshot = {k: str(row[k]) for k in ("id", "status", "extracted_text", "updated_at")}
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            done = await s.execute(text(
                "update book_themes set status='failed', extracted_text=null, updated_at=now() "
                "where id=:t and status='ready' and updated_at=:seen"),
                {"t": theme_id, "seen": row["updated_at"]})
            await s.commit()
            if done.rowcount == 1:
                results.append({"id": theme_id, "action": "reset", "backup": str(path)})
            else:
                results.append({"id": theme_id, "action": "conflict", "backup": str(path),
                                "reason": "row changed after it was read; left as is"})
    return results


def render(results: list[dict], *, apply: bool) -> str:
    if not results:
        return "nothing flagged — no range to reset"
    lines = [f"{'APPLY' if apply else 'DRY RUN (add --apply to write)'}: {len(results)} range(s)"]
    for r in results:
        line = f"  {r['action'].upper():11} {r['id']}"
        if r.get("chars") is not None:
            line += f"  chars={r['chars']}"
        if r.get("backup"):
            line += f"  backup={r['backup']}"
        if r.get("reason"):
            line += f"  ({r['reason']})"
        lines.append(line)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reset ranges flagged by prod_recitation_find.py")
    parser.add_argument("--input", required=True,
                        help="the find script's saved output ('-' = stdin; only with python -m)")
    parser.add_argument("--apply", action="store_true", help="actually write")
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    try:
        ids = parse_find_output(raw)
    except NotFindOutput as e:
        print(f"refused: {e}")
        return 2

    fixed = os.environ.get("FIX_DEPLOYED_AT")
    fixed_at = datetime.fromisoformat(fixed) if fixed else None

    from app.database import async_session_factory

    results = asyncio.run(reset_flagged(async_session_factory, ids, apply=args.apply,
                                        fixed_at=fixed_at))
    print(render(results, apply=args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
