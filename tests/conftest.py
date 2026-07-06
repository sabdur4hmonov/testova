"""Pytest configuration and fixtures."""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.user import User, Language, SubscriptionPlan

TEST_DB_URL = "postgresql+asyncpg://testova:password@localhost:5432/testova_test"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


@pytest_asyncio.fixture
async def sample_user(session: AsyncSession) -> User:
    user = User(
        telegram_id=123456789,
        username="testuser",
        full_name="Test User",
        language=Language.EN,
        subscription_plan=SubscriptionPlan.FREE,
    )
    session.add(user)
    await session.flush()
    return user
