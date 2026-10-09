"""
DB-facing service layer for the Kitobdan flow. Handlers in book.py stay thin
(parse → check → call service → format), the same convention admin_users.py
already follows — this is the layer a future web panel would call too.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import selectinload

from app.models.book import (
    Book, BookAccess, BookStatus, BookTheme, ThemeKind, ThemeStatus,
)
from app.services.book_analyzer import ThemeDraft

# Theme-selection tokens: a bare number ("12") or an inclusive range ("12-28").
_NUMBER = re.compile(r"^\d+$")
_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")

# A garbled/OCR TOC can merge many contents lines into one runaway "title" — a
# real Grammarway 2 upload produced a 1494-char title, which overflowed the old
# String(512) column and crashed the whole insert. The columns are TEXT now, so
# length alone no longer crashes; but a title is a human-facing label, so clamp
# it to a sane maximum as a LAST-RESORT guard. The analyzer's degeneracy check
# should route such a book to the Vision tier long before this ever fires — this
# is defense in depth so no future/unforeseen garbling can crash or produce an
# absurd label, not the primary fix. 500 stays just under the historical 512 so
# a downgrade to the narrower column can never truncate-crash either.
MAX_TITLE_CHARS = 500


def clamp_title(title: str) -> str:
    """Trim a title to MAX_TITLE_CHARS, never raising. Real titles (corpus max
    ~158 chars) are untouched; only a degenerate merged title is shortened."""
    title = (title or "").strip()
    if len(title) <= MAX_TITLE_CHARS:
        return title
    return title[: MAX_TITLE_CHARS - 1].rstrip() + "…"


# ── Duplicate detection ───────────────────────────────────────────────────────

async def find_duplicate_book(
    session, *, checksum: str, title: str | None,
    subject: str | None = None, grade: str | None = None,
) -> Book | None:
    """PRIMARY check: MD5 checksum, among READY books (a book mid-analysis
    cannot be a usable duplicate). FALLBACK: title + subject + grade, among
    PUBLIC books only — a private book is not "in the public library" for
    someone else to be offered.
    """
    res = await session.execute(
        select(Book).where(Book.pdf_checksum == checksum, Book.status == BookStatus.READY)
    )
    book = res.scalars().first()
    if book is not None:
        return book

    if not title:
        return None
    # func.lower(...) == , not ILIKE: a teacher-typed title containing a
    # literal % or _ must match itself exactly, not act as an ILIKE wildcard.
    res = await session.execute(
        select(Book).where(
            Book.is_public.is_(True),
            Book.status == BookStatus.READY,
            func.lower(Book.title) == title.strip().lower(),
            Book.subject.is_(None) if subject is None else Book.subject == subject,
            Book.grade.is_(None) if grade is None else Book.grade == grade,
        )
    )
    return res.scalars().first()


async def increment_use_count(session, book_id, *, commit: bool = True) -> None:
    await session.execute(
        update(Book).where(Book.id == book_id).values(use_count=Book.use_count + 1)
    )
    if commit:
        await session.commit()


async def grant_book_access(session, *, user_id, book_id, commit: bool = True) -> None:
    """Give a teacher persistent access to a book they did NOT upload.

    Idempotent via UNIQUE(user_id, book_id) + ON CONFLICT DO NOTHING — accepting
    the same book twice is a no-op, and two concurrent accepts cannot race.
    A teacher who already OWNS the book needs no grant.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    owner = (await session.execute(
        select(Book.user_id).where(Book.id == book_id)
    )).scalar_one_or_none()
    if owner == user_id:
        return

    await session.execute(
        pg_insert(BookAccess)
        .values(id=uuid.uuid4(), user_id=user_id, book_id=book_id)
        .on_conflict_do_nothing(constraint="uq_book_access_user_book")
    )
    if commit:
        await session.commit()


# ── Creating a book ───────────────────────────────────────────────────────────

async def save_book_with_themes(
    session, *, user, title: str, page_count: int, pdf_path: str,
    has_toc: bool, is_public: bool, checksum: str, language: str | None,
    themes: list[ThemeDraft], subject: str | None = None, grade: str | None = None,
    kind: str = "narrative",
) -> Book:
    """Insert a Book and its BookTheme rows in one transaction.

    Called only once analysis (or manual entry) and the is_public choice are
    both done — status is READY at insert time. PENDING/ANALYZING/FAILED exist
    on the model for a future async/background analysis job; Phase 1's
    analysis runs synchronously during the upload conversation and doesn't
    need them.
    """
    book = Book(
        user_id=user.id, title=clamp_title(title), subject=subject, grade=grade,
        page_count=page_count, original_pdf_path=pdf_path, has_toc=has_toc,
        status=BookStatus.READY, is_public=is_public,
        uploaded_by_user_id=user.telegram_id, use_count=0,
        pdf_checksum=checksum, language=language, kind=kind,
    )
    session.add(book)
    await session.flush()
    for draft in themes:
        session.add(BookTheme(
            book_id=book.id, title=clamp_title(draft.title), order_index=draft.order_index,
            page_start=draft.page_start, page_end=draft.page_end,
            status=ThemeStatus.PENDING,
            kind=getattr(draft, "kind", ThemeKind.SECTION),
        ))
    await session.commit()
    await session.refresh(book)
    return book


# ── Reading a teacher's library ──────────────────────────────────────────────

async def list_user_books(
    session, user_id, *, page: int = 1, per: int = 10,
) -> tuple[list[Book], int]:
    """A teacher's library: books they OWN, plus books granted to them via
    book_access (accepted from the public library). Owning and being granted
    are different relations, so this is a union, not a single FK lookup.

    Paginated, matching admin_users.list_users' (rows, total) shape — a
    teacher's library grows without bound as they accept more public books.
    """
    page = max(1, page)
    granted = select(BookAccess.book_id).where(BookAccess.user_id == user_id)
    where = (
        Book.status == BookStatus.READY,
        or_(Book.user_id == user_id, Book.id.in_(granted)),
    )
    total = (await session.execute(
        select(func.count()).select_from(Book).where(*where)
    )).scalar_one()
    res = await session.execute(
        select(Book).where(*where).order_by(Book.created_at.desc())
        .offset((page - 1) * per).limit(per)
    )
    return list(res.scalars().all()), int(total or 0)


def _readable_by(stmt, user_id):
    """Apply the book-read AUTHORIZATION filter to a Book select.

    Shared by get_book and get_book_with_themes so the two loaders can never
    drift apart — the only difference between them is eager-loading themes,
    never who is allowed to see the row.

    `user_id` is an AUTHORIZATION filter, not an optimization. Book ids arrive
    from callback data, which is user-controlled — without this a crafted
    `book:pick:<uuid>` would open any book in the system, including another
    teacher's PRIVATE one. Callers acting on behalf of a teacher must pass it;
    it is optional only for internal/admin lookups that have already
    established the right to read.
    """
    if user_id is None:
        return stmt
    granted = select(BookAccess.book_id).where(BookAccess.user_id == user_id)
    return stmt.where(
        or_(
            Book.user_id == user_id,
            Book.id.in_(granted),
            Book.is_public.is_(True),
        )
    )


async def get_book(session, book_id, *, user_id=None) -> Book | None:
    """Load a book WITHOUT its themes (AUDIT C-11).

    Identical authorization to get_book_with_themes — same filter, same
    arguments — but it does not eager-load Book.themes.

    Why it exists: BookTheme.extracted_text holds a whole textbook section per
    row and a book can carry 60+ themes, so selectinload(Book.themes) moves
    multiple megabytes. Most callers only need scalar columns off the book
    itself (id, title, original_pdf_path, page_offset, user_id) and were paying
    that transfer on a menu tap.

    Use get_book_with_themes ONLY when book.themes is actually traversed;
    accessing .themes on an instance from here would emit a lazy load, which
    raises under async.
    """
    stmt = _readable_by(select(Book).where(Book.id == book_id), user_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_book_with_themes(session, book_id, *, user_id=None) -> Book | None:
    """Load a book AND eager-load its themes.

    Prefer get_book unless you traverse book.themes — see its docstring for the
    cost. Authorization is identical (both go through _readable_by).
    """
    stmt = _readable_by(
        select(Book).where(Book.id == book_id).options(selectinload(Book.themes)),
        user_id,
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


# ── Public library: browsing and searching ───────────────────────────────────
#
# "Browsable" means the same thing everywhere in this section: is_public,
# READY, and NOT already in the teacher's own library (owned or granted) —
# there is no point offering a book they already have. `_browsable_where`
# is the ONE definition of that filter; list_public_books, search_books_by_title
# and the distinct-value helpers all build on it, so the exclusion rule cannot
# drift between them.

def _browsable_where(user_id):
    granted = select(BookAccess.book_id).where(BookAccess.user_id == user_id)
    return (
        Book.is_public.is_(True),
        Book.status == BookStatus.READY,
        Book.user_id != user_id,
        Book.id.notin_(granted),
    )


async def count_public_library(session) -> int:
    """Every READY public book, INCLUDING ones the caller already has. Used
    only to tell apart the two empty states: "library is empty" vs "you
    already have everything in it"."""
    res = await session.execute(
        select(func.count()).select_from(Book)
        .where(Book.is_public.is_(True), Book.status == BookStatus.READY)
    )
    return int(res.scalar_one() or 0)


async def list_public_books(
    session, *, user_id, subject: str | None = None, grade: str | None = None,
    page: int = 1, per: int = 8,
) -> tuple[list[Book], int]:
    """Browsable public books, optionally filtered, most-used first — so a
    teacher sees the books other teachers actually trust before an unused one.
    """
    page = max(1, page)
    where = list(_browsable_where(user_id))
    if subject is not None:
        where.append(Book.subject == subject)
    if grade is not None:
        where.append(Book.grade == grade)

    total = (await session.execute(
        select(func.count()).select_from(Book).where(*where)
    )).scalar_one()
    res = await session.execute(
        select(Book).where(*where)
        .order_by(Book.use_count.desc(), Book.created_at.desc())
        .offset((page - 1) * per).limit(per)
    )
    return list(res.scalars().all()), int(total or 0)


async def distinct_subjects(session, user_id) -> list[str]:
    """Only values that occur among BROWSABLE books — so a filter button can
    never itself lead to zero results."""
    res = await session.execute(
        select(Book.subject).distinct()
        .where(*_browsable_where(user_id), Book.subject.is_not(None))
        .order_by(Book.subject)
    )
    return [s for s in res.scalars().all() if s]


async def distinct_grades(session, user_id) -> list[str]:
    res = await session.execute(
        select(Book.grade).distinct()
        .where(*_browsable_where(user_id), Book.grade.is_not(None))
        .order_by(Book.grade)
    )
    return [g for g in res.scalars().all() if g]


# Case-insensitive substring, Uzbek-apostrophe-normalised, wildcard-escaped.
# %/_ are LIKE metacharacters — a teacher typing a literal "%" must not match
# every book in the library. Postgres's default ESCAPE is backslash.
_APOSTROPHES = str.maketrans({c: "'" for c in "‘’ʻʼ`´"})
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _normalize_for_search(text: str) -> str:
    return text.translate(_APOSTROPHES).strip().lower()


async def search_books_by_title(
    session, *, user_id, query: str, limit: int = 8,
) -> list[Book]:
    """Books whose title CONTAINS `query`, browsable ones only, most-used
    first — so of several partial matches the one other teachers already
    trust surfaces before an unused upload.

    Substring, not fuzzy: fuzzy matching (pg_trgm) needs a tuned similarity
    threshold, and with a handful of real titles in the library there is no
    corpus to tune it against — a blind threshold would either flood results
    or silently return nothing, and there would be no way to tell which.
    pg_trgm + a dedicated search column is the documented upgrade once real
    titles accumulate; substring is the honest baseline today.
    """
    needle = _normalize_for_search(query)
    if not needle:
        return []
    pattern = f"%{needle.translate(_LIKE_ESCAPE)}%"
    res = await session.execute(
        select(Book)
        .where(*_browsable_where(user_id), func.lower(Book.title).like(pattern))
        .order_by(Book.use_count.desc(), Book.created_at.desc())
        .limit(limit)
    )
    return list(res.scalars().all())


async def link_existing_book(session, *, user_id, book_id) -> None:
    """The ONE reuse path: increment use_count and grant persistent access.

    Shared by every entry point that lets a teacher pick an existing book
    instead of uploading — the duplicate offer, public-library browsing, and
    title search all call this SAME function, so the zero-cost /
    zero-book_limit guarantee cannot drift between them the way three
    hand-written copies could.

    NO visibility check here by design — see link_public_book below for why
    that's the wrong layer for it. Call this directly only from a path with
    its OWN legitimate authorization basis (today: the checksum-duplicate
    offer, where the caller independently uploaded a file with a matching
    checksum — i.e. already possesses the content regardless of the
    original's is_public flag).
    """
    # AUDIT B-35: ONE commit, not two. increment_use_count and
    # grant_book_access each used to commit for themselves, so if the second
    # failed the use_count was permanently inflated with no matching
    # book_access row — and a user retry incremented it AGAIN. use_count is the
    # public-library ranking key, so that drift skews what every teacher sees
    # first. Both writes now land in one transaction: either the teacher gets
    # access and the count moves, or neither happens.
    await increment_use_count(session, book_id, commit=False)
    await grant_book_access(session, user_id=user_id, book_id=book_id, commit=False)
    await session.commit()


async def link_public_book(session, *, user_id, book_id) -> bool:
    """Entry point for the PUBLIC-LIBRARY browsing/search flows (title
    search, public-library browse). Those UI paths only ever SHOW public
    books, but their callback_data is attacker-forgeable via the raw Bot
    API — a crafted `book:searchpick:<uuid>` / `book:publib:pick:<uuid>`
    for a private book's id would otherwise reach link_existing_book with
    no check at all, silently granting the attacker permanent read access
    (and a PDF download) to a book they were never shown.

    Verifies Book.is_public itself, not just where the UI happened to have
    shown the id from. Returns False (grants nothing, mutates nothing) if
    the book doesn't exist or isn't public.
    """
    is_public = (await session.execute(
        select(Book.is_public).where(Book.id == book_id)
    )).scalar_one_or_none()
    if not is_public:  # None (no such book) or False (private) — both refused
        return False
    await link_existing_book(session, user_id=user_id, book_id=book_id)
    return True


async def get_book_pdf(session, book_id, user_id) -> tuple[bytes, str] | None:
    """The original PDF, or None if the teacher has no access or the file is
    missing on disk.

    Authorization is get_book's existing owner/granted/public check — reused,
    not reimplemented. Having access to a book already means having access to
    everything IN it (a grantee can already extract every theme's text from
    this same file), so no separate permission model is introduced here.

    Uses get_book, not get_book_with_themes: this reads original_pdf_path and
    title only, so eager-loading every theme's extracted_text would move
    megabytes to serve a file download (AUDIT C-11).
    """
    from app.services import storage

    book = await get_book(session, book_id, user_id=user_id)
    if book is None:
        return None
    try:
        content = await storage.read_file(book.original_pdf_path)
    except (FileNotFoundError, OSError):
        return None
    filename = f"{book.title}.pdf" if book.title else "book.pdf"
    return content, filename


# ── Editing a SAVED book's themes ────────────────────────────────────────────

def can_edit_book(book, user_id) -> bool:
    """Only the OWNER may edit a saved book's themes.

    A book granted through book_access (accepted from the public library) is
    READ-ONLY for the borrower: the row is shared, so an edit would silently
    rewrite the book for the owner and for every other teacher using it. The
    borrower can still select and generate from it — they just cannot reshape
    someone else's library.
    """
    return book is not None and book.user_id == user_id


def themes_to_rows(themes) -> list[dict]:
    """BookTheme rows → the plain dicts the pure editor works on.

    Carries `id`, `status` and `extracted_text` so edits can be written back
    and so split/merge can invalidate stale text (the pre-save editor's rows
    have none of these).
    """
    return [
        {
            "id": str(th.id), "title": th.title,
            "page_start": th.page_start, "page_end": th.page_end,
            "order_index": th.order_index, "kind": th.kind,
            "status": th.status, "extracted_text": th.extracted_text,
        }
        for th in sorted(themes, key=lambda t: t.order_index)
    ]


async def load_theme_rows(session, book_id) -> list[dict]:
    res = await session.execute(
        select(BookTheme).where(BookTheme.book_id == book_id)
        .order_by(BookTheme.order_index)
    )
    return themes_to_rows(list(res.scalars().all()))


async def any_theme_extracting(session, book_id) -> bool:
    """Is an extraction in flight for this book?

    save_theme_text writes back unconditionally by id, so an edit landing
    mid-extraction would be overwritten by text for the OLD page range and
    marked ready — stale text, silently. The editor refuses to open instead.
    """
    res = await session.execute(
        select(func.count()).select_from(BookTheme).where(
            BookTheme.book_id == book_id,
            BookTheme.status == ThemeStatus.EXTRACTING,
        )
    )
    return int(res.scalar_one() or 0) > 0


async def persist_theme_edits(session, book_id, rows: list[dict]) -> dict:
    """Write an edited row list back to the DB. Returns a counts summary.

    Rows carrying an `id` are UPDATEd, rows without one are INSERTed (a split's
    second half), and any existing row not present in `rows` is DELETEd. All in
    ONE transaction: a half-applied edit would leave page ranges that overlap
    or gap, which is worse than the edit not happening.

    Reconciliation is over TOPIC themes only: synthetic page-range themes (Item
    2) are excluded from the manage UI's rows, so they must also be excluded here
    or the "delete rows not present" step would wipe them (and their cached text)
    on every save.
    """
    res = await session.execute(
        select(BookTheme).where(
            BookTheme.book_id == book_id,
            BookTheme.is_page_range.is_not(True),
        )
    )
    existing = {str(th.id): th for th in res.scalars().all()}
    seen: set[str] = set()
    counts = {"updated": 0, "inserted": 0, "deleted": 0}

    for position, row in enumerate(rows, start=1):
        row_id = row.get("id")
        theme = existing.get(str(row_id)) if row_id else None
        if theme is not None:
            theme.title = clamp_title(row["title"])
            theme.page_start = row["page_start"]
            theme.page_end = row["page_end"]
            theme.kind = row.get("kind", ThemeKind.SECTION)
            theme.order_index = position
            theme.status = row.get("status", ThemeStatus.PENDING)
            theme.extracted_text = row.get("extracted_text")
            seen.add(str(row_id))
            counts["updated"] += 1
        else:
            session.add(BookTheme(
                book_id=book_id, title=clamp_title(row["title"]), order_index=position,
                page_start=row["page_start"], page_end=row["page_end"],
                kind=row.get("kind", ThemeKind.SECTION),
                status=row.get("status") or ThemeStatus.PENDING,
                extracted_text=row.get("extracted_text"),
            ))
            counts["inserted"] += 1

    for row_id, theme in existing.items():
        if row_id not in seen:
            await session.delete(theme)
            counts["deleted"] += 1

    await session.commit()
    return counts


# ── Theme selection parsing ──────────────────────────────────────────────────

# ── Editing a detected theme list ────────────────────────────────────────────
#
# A 59-section textbook cannot be corrected by retyping it, so the teacher fixes
# the DETECTED list in place with one line per change. The syntax follows the
# repo's existing answer-key idiom (upload.py already reads "23: -" as "drop
# question 23"), so a teacher who has used the key step recognises it.
#
#     12 -              delete row 12
#     12 = Yangi nom    rename row 12
#     12 : 10-24        set row 12's pages
#     12 #              flip row 12 between section and heading
#
# Rows are numbered over EVERY row here, headings included, so anything on
# screen can be addressed — including a heading the classifier got wrong.
#     H2 -              delete the 2nd heading
#     H2 = Yangi nom    rename it, and so on
#
# A BARE NUMBER ALWAYS MEANS A SECTION — the same number the teacher just read
# on the confirm screen. Headings are addressed only by their H-reference, so a
# heading can never absorb a number the teacher meant for a section. That
# ambiguity is precisely what silently deleted a real theme (Bug 4).
#     12 / 45 = Nom     split section 12 at page 45 (second half gets the name)
#     12 + 13 = Nom     merge adjacent sections 12 and 13
#
# The "= Nom" is optional on split/merge; without it the caller prompts.
_REF = r"(?:[Hh](\d+)|(\d+))"
_EDIT_DELETE = re.compile(rf"^{_REF}\s*[-–—]\s*$")
_EDIT_PAGES = re.compile(rf"^{_REF}\s*:\s*(\d+)\s*[-–—]\s*(\d+)\s*$")
_EDIT_TOGGLE = re.compile(rf"^{_REF}\s*#\s*$")
# A LEADING "+" is an ADD (no ref before it); a "+" BETWEEN two refs is a
# merge. Checked first so the two can never be confused.
#     + 10-24 = Nom     add a theme covering pages 10-24
# Pages are required inline: a theme with no page range cannot be extracted,
# so there would be nothing useful to store.
_EDIT_ADD = re.compile(r"^\+\s*(\d+)\s*[-–—]\s*(\d+)\s*(?:=\s*(.+))?$")
_EDIT_SPLIT = re.compile(rf"^{_REF}\s*/\s*(\d+)\s*(?:=\s*(.+))?$")
_EDIT_MERGE = re.compile(rf"^{_REF}\s*\+\s*{_REF}\s*(?:=\s*(.+))?$")
# Checked LAST: its "=" cannot appear before the / or + of the two above, but
# ordering it last keeps that independent of regex subtleties.
_EDIT_RENAME = re.compile(rf"^{_REF}\s*=\s*(.+)$")


def _row_indices(rows: list[dict]) -> tuple[list[int], list[int]]:
    """(indices of sections, indices of headings) within `rows`, in order."""
    sections, headers = [], []
    for i, row in enumerate(rows):
        (headers if row.get("kind") == ThemeKind.HEADER else sections).append(i)
    return sections, headers


def parse_theme_edits(
    text: str, rows: list[dict], max_pages: int | None = None,
) -> tuple[list[dict], list[str]]:
    """Parse edit lines into ops carrying a REFERENCE, not a row index.

    A ref is ("section", n) or ("header", n), resolved against `rows` at apply
    time. Keeping display-facing numbers out of the ops is what stops the
    editor's view and the data from drifting apart again.

    Every line is parsed and reported independently: a typo on line 3 must not
    discard the four corrections around it — the same partial-application
    behaviour apply_key_text has for a half-valid answer key.

    Errors are {"line", "reason", "hint"} dicts, not bare strings: a REFUSAL
    ("these themes are not adjacent") is not the same as "I could not read
    this", and telling a teacher the bot did not understand a command it
    understood perfectly well and declined is worse than saying nothing. The
    handler maps `reason` to localized text.
    """
    ops: list[dict] = []
    errors: list[str] = []
    section_idx, header_idx = _row_indices(rows)

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        def _ref(match, g_header: int = 1, g_plain: int = 2) -> tuple[str, int] | None:
            """A ref from one (H-number | bare number) pair of groups."""
            if match.group(g_header) is not None:
                n, kind, limit = int(match.group(g_header)), "header", len(header_idx)
            else:
                n, kind, limit = int(match.group(g_plain)), "section", len(section_idx)
            if 1 <= n <= limit:
                return (kind, n)
            errors.append({"line": line, "reason": "range", "hint": f"1-{limit}"})
            return None

        def _row_index(ref) -> int:
            kind, n = ref
            table = header_idx if kind == "header" else section_idx
            return table[n - 1]

        if (m := _EDIT_ADD.match(line)):
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            if start < 1 or (max_pages is not None and end > max_pages):
                errors.append({
                    "line": line, "reason": "range",
                    "hint": f"1-{max_pages}" if max_pages else "1+",
                })
            else:
                ops.append({
                    "op": "add", "start": start, "end": end,
                    "title": (m.group(3) or "").strip() or None,
                })
        elif (m := _EDIT_DELETE.match(line)):
            if (ref := _ref(m)) is not None:
                ops.append({"op": "delete", "ref": ref})
        elif (m := _EDIT_TOGGLE.match(line)):
            if (ref := _ref(m)) is not None:
                ops.append({"op": "toggle", "ref": ref})
        elif (m := _EDIT_PAGES.match(line)):
            if (ref := _ref(m)) is not None:
                start, end = int(m.group(3)), int(m.group(4))
                if start > end:
                    start, end = end, start
                if start < 1 or (max_pages is not None and end > max_pages):
                    errors.append({"line": line, "reason": "range", "hint": f"1-{max_pages}"})
                else:
                    ops.append({"op": "pages", "ref": ref, "start": start, "end": end})
        elif (m := _EDIT_SPLIT.match(line)):
            ref = _ref(m)
            if ref is not None:
                if ref[0] == "header":
                    # A heading owns no page span, so there is nothing to divide.
                    errors.append({"line": line, "reason": "sections_only"})
                else:
                    row = rows[_row_index(ref)]
                    page = int(m.group(3))
                    # Strictly inside: the split page ENDS the first half, so
                    # page == page_end would leave the second half empty.
                    if not (row["page_start"] <= page < row["page_end"]):
                        errors.append({
                            "line": line, "reason": "range",
                            "hint": f"{row['page_start']}-{row['page_end'] - 1}",
                        })
                    else:
                        ops.append({
                            "op": "split", "ref": ref, "page": page,
                            "title": (m.group(4) or "").strip() or None,
                        })
        elif (m := _EDIT_MERGE.match(line)):
            ref_a = _ref(m, 1, 2)
            ref_b = _ref(m, 3, 4) if ref_a is not None else None
            if ref_a is not None and ref_b is not None:
                if ref_a[0] == "header" or ref_b[0] == "header":
                    errors.append({"line": line, "reason": "sections_only"})
                elif _row_index(ref_b) != _row_index(ref_a) + 1:  # noqa: E501
                    # Adjacent ROWS, not merely consecutive section numbers: a
                    # heading between them would be orphaned above a theme
                    # spanning two chapters, and the range would be nonsense.
                    errors.append({"line": line, "reason": "not_adjacent"})
                else:
                    ops.append({
                        "op": "merge", "ref": ref_a, "ref2": ref_b,
                        "title": (m.group(5) or "").strip() or None,
                    })
        elif (m := _EDIT_RENAME.match(line)):
            if (ref := _ref(m)) is not None:
                title = m.group(3).strip()
                if title:
                    ops.append({"op": "rename", "ref": ref, "title": title})
                else:
                    errors.append({"line": line, "reason": None})
        else:
            errors.append({"line": line, "reason": None})

    return ops, errors


def _invalidate_extraction(row: dict) -> None:
    """Drop saved text whose page range no longer describes it.

    Only touches keys that are already present: the pre-save editor's rows have
    neither, and inventing them would put junk into the draft. Saved-book rows
    have both and MUST be reset — text extracted for one range attached to a
    different range is silently wrong, which is worse than re-extracting.

    Re-cutting the saved text instead is not possible: it carries no page
    markers, so there is nothing to cut on.
    """
    if "status" in row:
        row["status"] = ThemeStatus.PENDING
    if "extracted_text" in row:
        row["extracted_text"] = None


@dataclass
class EditResult:
    """Outcome of one edit batch. The lists let the caller NAME what happened —
    a structural change the teacher did not intend has to be visible at once."""
    rows: list[dict]
    deleted: list[str] = field(default_factory=list)
    split: list[tuple[str, str]] = field(default_factory=list)        # (kept, new)
    merged: list[tuple[str, str, str]] = field(default_factory=list)  # (a, b, into)
    added: list[str] = field(default_factory=list)


def apply_theme_edits(rows: list[dict], ops: list[dict]) -> EditResult:
    """Apply parsed ops. Returns an EditResult.

    Refs are resolved against the ORIGINAL row list once, up front, so every op
    in a batch addresses what the teacher was looking at. Structural changes
    (delete / split / merge) are applied in a final pass by row identity —
    resolving them as we go would renumber mid-batch and make "12 -" followed
    by "13 -" hit the wrong second theme (Bug 4).

    A row targeted by more than one structural op keeps only the first: two
    structural changes to the same theme in one batch cannot both be honoured,
    and guessing between them would be worse than ignoring the second.
    """
    working = [dict(r) for r in rows]
    section_idx, header_idx = _row_indices(rows)
    to_delete: set[int] = set()
    splits: dict[int, dict] = {}
    merges: dict[int, dict] = {}
    structural: set[int] = set()

    def resolve(ref) -> int | None:
        kind, n = ref
        table = header_idx if kind == "header" else section_idx
        return table[n - 1] if 1 <= n <= len(table) else None

    additions: list[dict] = []
    for op in ops:
        if op["op"] == "add":
            # No ref: an add names no existing row. Collected here and merged
            # into the list in page order once the rest has been applied.
            additions.append(op)
            continue
        idx = resolve(op["ref"])
        if idx is None:
            continue
        kind = op["op"]
        if kind in ("delete", "split", "merge"):
            partner = resolve(op["ref2"]) if kind == "merge" else None
            touched = {idx} | ({partner} if partner is not None else set())
            if touched & structural:
                continue                      # already restructured in this batch
            structural |= touched
            if kind == "delete":
                to_delete.add(idx)
            elif kind == "split":
                splits[idx] = op
            elif partner is not None:
                merges[idx] = {**op, "partner": partner}
        elif kind == "rename":
            working[idx]["title"] = op["title"]
        elif kind == "pages":
            working[idx]["page_start"] = op["start"]
            working[idx]["page_end"] = op["end"]
            _invalidate_extraction(working[idx])
        elif kind == "toggle":
            current = working[idx].get("kind", ThemeKind.SECTION)
            working[idx]["kind"] = (
                ThemeKind.HEADER if current == ThemeKind.SECTION else ThemeKind.SECTION
            )

    result = EditResult(rows=[])
    result.deleted = [working[i]["title"] for i in sorted(to_delete)]

    consumed: set[int] = set()
    out: list[dict] = []
    for i, row in enumerate(working):
        if i in to_delete or i in consumed:
            continue

        if i in splits:
            op = splits[i]
            first, second = row, dict(row)
            # The second half is a NEW theme. Without this it would inherit the
            # original's primary key and, on save, overwrite the first half
            # instead of being inserted beside it. Harmless for the pre-save
            # draft (no ids there); destructive for a saved book.
            if "id" in second:
                second["id"] = None
            second["title"] = op["title"]
            second["page_start"] = op["page"] + 1
            second["page_end"] = first["page_end"]
            first["page_end"] = op["page"]
            # Both halves describe a NEW range, so neither can keep the text
            # extracted for the old one.
            _invalidate_extraction(first)
            _invalidate_extraction(second)
            result.split.append((first["title"], second["title"]))
            out.extend([first, second])
            continue

        if i in merges:
            op = merges[i]
            partner = working[op["partner"]]
            consumed.add(op["partner"])
            merged = dict(row)
            merged["title"] = op["title"]
            merged["page_start"] = min(row["page_start"], partner["page_start"])
            merged["page_end"] = max(row["page_end"], partner["page_end"])
            # NOT a concatenation of the two texts: the shared-page split logic
            # can read a merged range differently from two separate ones, so the
            # merged theme is re-extracted from scratch.
            _invalidate_extraction(merged)
            result.merged.append((row["title"], partner["title"], merged["title"]))
            out.append(merged)
            continue

        out.append(row)

    # New themes go in by PAGE ORDER, not appended: the list is read in book
    # order, so a page-8 theme sitting after a page-30 one would make the
    # numbering meaningless. A new row carries only the fields its neighbours
    # already have — inventing an id would make it overwrite an existing row on
    # save, and inventing status/extracted_text would put junk in the draft.
    for op in additions:
        template = next((r for r in out if "status" in r or "id" in r), None)
        new_row: dict = {
            "title": op["title"], "page_start": op["start"],
            "page_end": op["end"], "order_index": 0, "kind": ThemeKind.SECTION,
        }
        if template is not None:
            if "id" in template:
                new_row["id"] = None
            if "status" in template:
                new_row["status"] = ThemeStatus.PENDING
            if "extracted_text" in template:
                new_row["extracted_text"] = None
        # Before the FIRST theme starting at or after the new one's start page.
        # ">=" rather than ">" so a tie puts the new theme first: "add starting
        # at page N" should land at the earliest position page order allows,
        # which is what a teacher inserting an introduction expects.
        position = len(out)
        for i, row in enumerate(out):
            if row["page_start"] >= op["start"]:
                position = i
                break
        out.insert(position, new_row)
        result.added.append(new_row["title"])

    for position, row in enumerate(out, start=1):
        row["order_index"] = position
    result.rows = out
    return result


def parse_theme_selection(text: str, max_n: int) -> tuple[list[int], str | None]:
    """"12" / "12,17,18" / "12-28" (and combinations of these) → sorted unique
    1-based theme numbers. Returns (numbers, error); error is None on success.

    error is one of: "empty", "invalid", "out_of_range" — the caller maps each
    to a localized message.
    """
    text = (text or "").strip()
    if not text:
        return [], "empty"

    numbers: set[int] = set()
    for token in re.split(r"[,\s]+", text):
        if not token:
            continue
        m = _RANGE.match(token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            numbers.update(range(a, b + 1))
            continue
        if _NUMBER.match(token):
            numbers.add(int(token))
            continue
        return [], "invalid"

    if not numbers:
        return [], "invalid"
    if any(n < 1 or n > max_n for n in numbers):
        return [], "out_of_range"
    return sorted(numbers), None


# ── Item 2: page-range input ("1-23, 34-49") ─────────────────────────────────

# One printed page range. Hyphen OR en/em dash, since a teacher on a phone
# keyboard easily types "1–23". A BARE number ("7") is a one-page range.
_PAGE_RANGE = re.compile(r"^(\d+)\s*[-–—]\s*(\d+)$")


def parse_page_ranges(text: str) -> tuple[list[tuple[int, int]], str | None]:
    """"1-23, 34-49" (or a single "7") → merged, sorted, non-overlapping list of
    inclusive (start, end) PRINTED page ranges.

    FORMAT validation only — these are the numbers PRINTED on the page; bounds
    against the book's actual page count are checked by the caller after the
    printed→PDF offset is resolved (the two live in different layers). Returns
    (ranges, error); error is one of "empty" / "invalid", each mapped to a
    localized re-prompt by the handler.

    OVERLAPPING ranges are merged ("1-10, 5-15" → "1-15") so a page is never
    analysed — or billed — twice, and reversed input ("23-1") is normalized
    rather than rejected.

    ADJACENT ranges are deliberately NOT merged (LIVE BUG 1). "4-18, 19-32"
    stays two ranges, because those share no page: merging bought no
    double-billing protection and instead collapsed the teacher's input into a
    single 29-page span that `validate_page_range_span` then rejected against
    the 15-page cap. It also broke that validator's own documented escape hatch
    — "submit it as several ranges (1-15, 16-30, 31-40), each safely under the
    cap" — since 16 is adjacent to 15 and the pieces were re-collapsed into one.

    The teacher's commas are meaningful: each piece becomes its OWN theme and
    its own extraction, which is what they asked for by typing it that way.

    A trailing sentence stop ("4-18, 19-32.") is tolerated rather than rejected
    as unparseable — it is a typing habit, not malformed input.
    """
    text = (text or "").strip()
    # A trailing full stop / stray comma is a typing habit, not malformed input.
    # Stripping it here keeps the generic "invalid" re-prompt meaning something.
    text = text.rstrip(".,;: ").strip()
    if not text:
        return [], "empty"

    parsed: list[tuple[int, int]] = []
    for token in re.split(r"\s*,\s*", text):
        token = token.strip()
        if not token:
            continue
        m = _PAGE_RANGE.match(token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
        elif _NUMBER.match(token):
            a = b = int(token)
        else:
            return [], "invalid"
        if a < 1 or b < 1:
            return [], "invalid"
        if a > b:
            a, b = b, a
        parsed.append((a, b))

    if not parsed:
        return [], "invalid"

    # Merge OVERLAPPING ranges only — never merely adjacent ones.
    #
    # LIVE BUG 1: this used to read `a <= last_b + 1`, and that "+ 1" is what
    # made 19 adjacent to 18 and collapsed "4-18, 19-32" into "4-32" — a
    # 29-page span the 15-page cap then rejected, blocking the feature for
    # exactly the input its own docs recommend. Overlapping ranges must still
    # merge (a shared page would otherwise be extracted and billed twice);
    # adjacent ranges share no page, so there was nothing to protect against
    # and the merge only destroyed the teacher's intent.
    parsed.sort()
    merged: list[tuple[int, int]] = [parsed[0]]
    for a, b in parsed[1:]:
        last_a, last_b = merged[-1]
        if a <= last_b:
            merged[-1] = (last_a, max(last_b, b))
        else:
            merged.append((a, b))
    return merged, None


def validate_page_range_span(
    ranges: list[tuple[int, int]], max_span: int,
) -> tuple[bool, list[tuple[int, int]]]:
    """Reject any INDIVIDUAL range wider than `max_span` pages (AUDIT C-3).

    Checked per range, not summed: create_page_range_themes turns each
    (start, end) pair into its OWN synthetic theme, and a scanned theme is
    extracted in ONE Gemini call — so the failure mode this guards against
    (vision output truncation / cost blow-up) is per range, not per
    submission. A teacher who legitimately needs a wider section can still
    submit it as several ranges ("1-15, 16-30, 31-40"), each safely under the
    cap and billed/extracted independently, same as today.

    Returns (ok, offending_ranges) — offending_ranges is empty when ok is True,
    so a caller can report exactly which of several ranges were too wide.
    """
    offending = [(a, b) for a, b in ranges if (b - a + 1) > max_span]
    return not offending, offending


async def create_page_range_themes(
    session, *, book_id, ranges: list[tuple[int, int]], offset: int,
    label_fmt: str = "{start}-{end}-betlar",
) -> list[BookTheme]:
    """Turn resolved printed ranges into SYNTHETIC page-range themes on `book_id`.

    Each printed range (start, end) maps to the PDF range (start+offset,
    end+offset) and becomes ONE `is_page_range=True` BookTheme, which then flows
    through the shared extract → cache → generate pipeline exactly like a topic
    theme. The title shows the PRINTED numbers the teacher typed (offset is a
    file-index detail they never see).

    CACHING (cost): a range already materialised for this book — same
    (page_start, page_end), is_page_range — is REUSED, so re-selecting the same
    pages costs no new extraction and no new Gemini call (the row keeps its
    `extracted_text`). Only genuinely new ranges insert a row.

    Returns the themes in the order the ranges were given. order_index is set
    ABOVE any existing theme so a synthetic row never collides with a topic
    theme's number, but it is irrelevant to selection: these are handed straight
    to generation, never rendered in the numbered topic list.
    """
    existing = (await session.execute(
        select(BookTheme).where(
            BookTheme.book_id == book_id,
            BookTheme.is_page_range.is_(True),
        )
    )).scalars().all()
    by_range = {(t.page_start, t.page_end): t for t in existing}

    max_order = max((t.order_index for t in existing), default=0)
    # Keep new synthetic rows well clear of topic order_indexes too.
    base_order = max(max_order, 10_000)

    out: list[BookTheme] = []
    for printed_start, printed_end in ranges:
        pdf_start = printed_start + offset
        pdf_end = printed_end + offset
        cached = by_range.get((pdf_start, pdf_end))
        if cached is not None:
            out.append(cached)
            continue
        base_order += 1
        theme = BookTheme(
            book_id=book_id,
            title=clamp_title(label_fmt.format(start=printed_start, end=printed_end)),
            order_index=base_order,
            page_start=pdf_start,
            page_end=pdf_end,
            status=ThemeStatus.PENDING,
            kind=ThemeKind.SECTION,
            is_page_range=True,
        )
        session.add(theme)
        by_range[(pdf_start, pdf_end)] = theme
        out.append(theme)

    await session.commit()
    for theme in out:
        await session.refresh(theme)
    return out
