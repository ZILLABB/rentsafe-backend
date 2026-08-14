"""Async SQLAlchemy engine/session wiring."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

# echo is opt-in (SQL_ECHO=true): it logs bound parameters, which include review
# text and other user content.
engine = create_async_engine(_settings.database_url, echo=_settings.sql_echo, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with SessionLocal() as session:
        yield session
