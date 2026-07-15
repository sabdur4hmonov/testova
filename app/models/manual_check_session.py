"""A manual "Javob orqali tekshirish" session — holds one typed answer key."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ManualCheckSession(Base):
    """
    Created once the teacher confirms a typed answer key; every sheet graded
    in that sitting references it via CheckResult.manual_session_id.
    """

    __tablename__ = "manual_check_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)  # telegram_id
    # {"1": "A", "2": "B", ...} — the confirmed correct answers.
    correct_answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<ManualCheckSession {self.id} q={len(self.correct_answers or {})}>"
