"""Database Connection and Session Management."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from backend.app.config import settings

# Async Engine for application CRUD operations
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

# Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for obtaining async DB session in FastAPI routes."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def transactional_session(
    session: Optional[AsyncSession] = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a session with a Service-owned transaction boundary.

    Two usage modes, per the layering rules:

    - ``session is None`` (Agent / background work): a new session is created
      here, committed on success, rolled back on error, and always closed.
    - ``session`` given (API layer injected it): the same session is reused and
      committed/rolled back, but not closed (the caller owns its lifecycle).

    Repos must never call this; they only ever receive the yielded session.
    """
    if session is None:
        async with AsyncSessionLocal() as new_session:
            try:
                yield new_session
                await new_session.commit()
            except Exception:
                await new_session.rollback()
                raise
        return

    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def init_db() -> None:
    """Initialize database tables and required extensions."""
    async with engine.begin() as conn:
        # Enable btree_gist extension for PostgreSQL exclusion constraint on reservations
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist;"))
        # Create all tables defined in models
        await conn.run_sync(Base.metadata.create_all)
