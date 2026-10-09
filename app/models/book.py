"""
AI Textbook Library models.

A teacher uploads a textbook ONCE. The book's table of contents becomes
`book_themes` rows; each theme's text is extracted LAZILY (the first time that
theme is actually used) and then reused forever. Nothing here re-analyses a
book that has already been processed.

WHY `status` IS A PLAIN String(20), NOT A PG ENUM: every other status column in
this repo is a native `*_enum` type, and getting them right cost five separate
fix commits (labels created as NAMES instead of VALUES). A native enum also
makes a migration's downgrade fiddly — the type must be dropped separately and
in the right order. These two tables carry no such baggage: the allowed values
live in BookStatus / ThemeStatus below, and `downgrade()` is a plain
drop_table. Do NOT "upgrade" these to enums.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BookStatus:
    """Lifecycle of a whole book."""
    PENDING = "pending"      # row created, TOC not confirmed yet
    ANALYZING = "analyzing"  # TOC detection in flight
    READY = "ready"          # themes saved; usable for generation
    FAILED = "failed"        # analysis failed; the teacher may retry
    ALL = frozenset({PENDING, ANALYZING, READY, FAILED})


class BookKind:
    """What KIND of book this is, which decides the natural question shape.

    NARRATIVE (default) is the ordinary prose textbook every book was before
    CP31 — questions are written ABOUT the content. WORKBOOK is a grammar /
    exercise book (Grammarway-style) whose natural questions are fill-in-the-
    blank and choose-the-correct-form. VOCAB is a word/translation list (CP31b).

    Plain String(16) with a 'narrative' server default, NOT a PG enum (same
    reasoning as BookStatus/ThemeKind above) — every pre-CP31 book reads as
    'narrative' and behaves exactly as before.
    """
    NARRATIVE = "narrative"
    WORKBOOK = "workbook"
    VOCAB = "vocab"
    ALL = frozenset({NARRATIVE, WORKBOOK, VOCAB})


class ThemeKind:
    """Is this row real content, or just a heading that groups other rows?

    A table of contents interleaves both:
        Глава II. Клетка – основа жизни      <- HEADER  (groups the rows below)
          § 3. Строение клетки               <- SECTION (real content)
          § 4. Ткани растений                <- SECTION

    HEADER rows are kept so the teacher sees the book's structure, but they are
    never selectable, never numbered, never extracted, and take no part in
    page-range computation. Treating them as content is what turned a
    59-section book into 65 themes and shifted every selection number.
    """
    SECTION = "section"
    HEADER = "header"
    ALL = frozenset({SECTION, HEADER})


class ThemeStatus:
    """Lifecycle of ONE theme's extracted text.

    The pending → extracting transition MUST be done as a conditional UPDATE
    (`WHERE status IN ('pending','failed')`), never check-then-set: two
    teachers can select the same theme of the same public book at the same
    moment, and a check-then-set would extract — and bill — it twice.
    """
    PENDING = "pending"        # never extracted
    EXTRACTING = "extracting"  # a worker owns it right now
    READY = "ready"            # extracted_text is populated and reusable
    FAILED = "failed"          # extraction failed; retried on next selection
    ALL = frozenset({PENDING, EXTRACTING, READY, FAILED})
    # A theme in one of these may be claimed for extraction; READY is done and
    # EXTRACTING belongs to somebody else.
    CLAIMABLE = frozenset({PENDING, FAILED})


class Book(Base):
    __tablename__ = "books"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # TEXT, not String(512): a garbled/OCR TOC can merge many contents lines
    # into one runaway title (a real Grammarway 2 upload produced 1494 chars),
    # which overflowed the old VARCHAR(512) and crashed the insert. Migration
    # 022 widens it; book_library.clamp_title still trims to a sane length.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    page_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    original_pdf_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    has_toc: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default=BookStatus.PENDING,
        server_default=BookStatus.PENDING, nullable=False,
    )
    # Public-library sharing. Phase 1 stores it and uses it for the duplicate
    # offer; BROWSING the public library is Phase 2.
    is_public: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # telegram_id of whoever uploaded it. Kept alongside user_id (the owning
    # UUID FK) because a public book outlives its uploader's row being reworked,
    # and admin cost views key off telegram_id everywhere else in this codebase.
    uploaded_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    use_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # MD5 of the raw PDF bytes — the PRIMARY duplicate check (title+subject+grade
    # is only the fallback). Indexed: looked up on every upload.
    pdf_checksum: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Auto-detected from the book's own content; generated tests must come out in
    # THIS language, whatever it is. NULL until detected — no default, because a
    # hardcoded language assumption is exactly what this field exists to avoid.
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # CP31: narrative textbook / grammar-exercise workbook / vocabulary list —
    # decides the natural question shape. 'narrative' server default so every
    # pre-CP31 book behaves exactly as before. See BookKind.
    kind: Mapped[str] = mapped_column(
        String(16), default=BookKind.NARRATIVE,
        server_default=BookKind.NARRATIVE, nullable=False,
    )
    # Item 2 (page-range tests): the shift between a page's PRINTED number
    # (footer/header) and its PDF file index, measured ONCE from the margins and
    # reused. NULL = not detected yet; 0 is a real detected value (printed == PDF
    # index), so this is nullable rather than defaulting to 0. See
    # book_analyzer.detect_page_offset_from_margins.
    page_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    themes: Mapped[list["BookTheme"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", lazy="select",
        order_by="BookTheme.order_index",
    )

    def __repr__(self) -> str:
        return f"<Book {self.title!r} pages={self.page_count} {self.status}>"


class BookTheme(Base):
    __tablename__ = "book_themes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)  # TEXT — see Book.title
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1-based and INCLUSIVE on both ends. Two consecutive themes may share a
    # page (page_end == next.page_start) — that page is split at the exact line
    # where the next theme's title begins.
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=ThemeStatus.PENDING,
        server_default=ThemeStatus.PENDING, nullable=False,
    )
    # 'section' (real content, selectable) or 'header' (grouping label only).
    # Defaults to section so pre-016 rows keep behaving exactly as before.
    kind: Mapped[str] = mapped_column(
        String(16), default=ThemeKind.SECTION,
        server_default=ThemeKind.SECTION, nullable=False,
    )
    # Item 2 (page-range tests): True marks a SYNTHETIC theme created from a
    # teacher's page range (not from the TOC). Such rows flow through the same
    # extract → cache → generate → pool pipeline, but are excluded from the
    # topic-selection / manage UI so the "By topic" flow stays unchanged. NULL /
    # False on every TOC/manual/outline theme, so those behave exactly as before.
    is_page_range: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    book: Mapped["Book"] = relationship(back_populates="themes")

    # Themes are always read as "this book's themes, in order" — one composite
    # index serves both the filter and the sort.
    __table_args__ = (
        Index("ix_book_themes_book_id_order", "book_id", "order_index"),
    )

    def __repr__(self) -> str:
        return f"<BookTheme {self.order_index}. {self.title!r} {self.status}>"


class BookThemeImage(Base):
    """CP24 — a figure cropped from a theme's page range, kept for image-based
    question generation.

    Two paths per figure on purpose (a cost control, not redundancy):
      * image_path      — the HIGH-DPI (CROP_DPI) crop, embedded on the final
                          PDF at print quality.
      * gen_image_path  — a ≤384px downscaled copy sent to Gemini for
                          generation, so the multimodal call hits the flat
                          258-token rate instead of tiled pricing (~4x cheaper).

    Rows exist ONLY for themes a teacher opted into image questions for AND
    that actually carry figures — an image-free or opt-out theme stores nothing.
    """
    __tablename__ = "book_theme_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    theme_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("book_themes.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    image_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    gen_image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_book_theme_images_theme_order", "theme_id", "order_index"),
    )

    def __repr__(self) -> str:
        return f"<BookThemeImage theme={self.theme_id} #{self.order_index} p{self.page_number}>"


class BookQuestionPoolItem(Base):
    """OPT6 — one previously-generated, grounded question kept for reuse.

    The pool is PER THEME (grounding is per theme), so cross-teacher reuse
    happens exactly when two teachers share the same theme_id — i.e. the same
    public book. It is guarded by settings.BOOK_QUESTION_POOL_ENABLED; nothing
    reads or writes this table when the guard is off.

    STORED CANONICALLY, LABEL-FREE, ON PURPOSE. A multiple-choice question is
    kept as its ORDERED option *values* plus the index of the correct one —
    never the A/B/C/D (or 1/2/3/4, or А/Б/В/Г) labels it was first generated
    under. That is what lets one stored question serve a teacher asking for ANY
    label style, as long as the option COUNT matches: on serve, book_pool relabels
    it to the requested labels and the answer index maps straight across. Because
    relabeling only ever touches keys, never values, a relabeled question is
    exactly as grounded as when it was stored — re-validation (which scores
    values) is label-independent.

    Image-based questions (CP24) are NOT pooled — they are tied to specific
    figures and their grounding is visual, not text-scoreable — so has_image /
    img_index questions are never stored here and never served from here.

    question_hash is md5 of the grounding-normalized stem; the UNIQUE constraint
    on (theme_id, difficulty, is_open_ended, option_count, question_hash) makes
    store idempotent (insert-ignore) so the pool cannot balloon with duplicates
    as many teachers regenerate the same theme.
    """
    __tablename__ = "book_question_pool"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    theme_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("book_themes.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Param key — a pooled question only serves a request whose settings match.
    difficulty: Mapped[str] = mapped_column(String(10), nullable=False)
    is_open_ended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Number of options (0 for a written question). The MC match key: relabeling
    # can change the label STYLE freely but never the count.
    option_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Canonical payload.
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Ordered option VALUES (label-free); empty list for a written question.
    option_values: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Index into option_values of the correct option (NULL for written).
    correct_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Expected written answer (NULL for multiple choice — derived from the index).
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_hash: Mapped[str] = mapped_column(String(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "theme_id", "difficulty", "is_open_ended", "option_count",
            "question_hash", name="uq_book_question_pool_dedup",
        ),
        # The serve query filters theme + params; this index covers it.
        Index(
            "ix_book_question_pool_lookup",
            "theme_id", "difficulty", "is_open_ended", "option_count",
        ),
    )

    def __repr__(self) -> str:
        kind = "written" if self.is_open_ended else f"mc{self.option_count}"
        return f"<BookQuestionPoolItem {kind} {self.difficulty} theme={self.theme_id}>"


class BookPoolServed(Base):
    """OPT6 never-repeat guard — a ledger of which pooled items a teacher has
    already been served for a theme, so the same question is never handed to the
    same teacher for the same theme twice.

    Keyed on telegram_id (BigInteger, no FK) rather than the user UUID: this is a
    variety ledger, not referential data, and telegram_id is what the generation
    handler already has in hand (matching the cost-tracking convention used
    everywhere else in this repo). theme_id is denormalized so the exclusion
    lookup is a plain index scan with no join. UNIQUE(telegram_id, pool_item_id)
    makes marking-served idempotent under a race.
    """
    __tablename__ = "book_pool_served"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    theme_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("book_themes.id", ondelete="CASCADE"),
        nullable=False,
    )
    pool_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("book_question_pool.id", ondelete="CASCADE"),
        nullable=False,
    )
    served_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "telegram_id", "pool_item_id", name="uq_book_pool_served_once",
        ),
        Index("ix_book_pool_served_lookup", "telegram_id", "theme_id"),
    )

    def __repr__(self) -> str:
        return f"<BookPoolServed tg={self.telegram_id} item={self.pool_item_id}>"


class BookAccess(Base):
    """A grant letting a teacher use a book they did NOT upload.

    books.user_id remains the OWNER (counts against their book_limit, their
    delete cascades the book away). This is the weaker "may use" relation,
    created when a teacher accepts the duplicate offer on someone else's public
    book. A teacher's library = books they own UNION books granted here.

    UNIQUE(user_id, book_id) makes accepting twice idempotent, so the handler
    can insert-and-ignore instead of racing a check-then-insert.
    """
    __tablename__ = "book_access"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_book_access_user_book"),
    )

    def __repr__(self) -> str:
        return f"<BookAccess user={self.user_id} book={self.book_id}>"
