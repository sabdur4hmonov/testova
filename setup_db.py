"""Database setup: create tables + add any new columns safely (idempotent)."""
import asyncio

from sqlalchemy import text

from app.models.user import User
from app.models.project import Project
from app.models.question import Question
from app.models.variant import Variant
from app.models.submission import Submission
from app.models.subscription import Subscription
from app.database import engine, Base


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Safe column additions for Question (idempotent on PostgreSQL)
        for stmt in [
            "ALTER TABLE questions ADD COLUMN IF NOT EXISTS group_id VARCHAR(64)",
            "ALTER TABLE questions ADD COLUMN IF NOT EXISTS group_context TEXT",
            "ALTER TABLE questions ADD COLUMN IF NOT EXISTS image_description TEXT",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                print(f"  (skip: {e})")

    await engine.dispose()
    print("OK: Barcha jadvallar va ustunlar tayyor!")


asyncio.run(main())
