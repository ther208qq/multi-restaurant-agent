"""Pytest Configuration and Fixtures."""

import asyncio
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Test-database isolation.
#
# The application engine is built from DATABASE_URL at import time
# (backend/app/database.py), so the override below MUST happen before any
# backend.app module is imported. Tests must never run against the dev
# database: several of them delete real rows -- e.g.
# test_reservation_exclusion.py DELETEs every branch-1 reservation around
# "tomorrow 19:00 +/-2h" -- and others leak orders/customers into whatever
# database they touch.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_DB_NAME = os.environ.get("TEST_DATABASE_NAME", "restaurant_ai_test")
_DEFAULT_DB_URL = "postgresql+asyncpg://postgres:postgrespassword@localhost:5432/restaurant_ai"


def _configured_database_url() -> str:
    """Resolve DATABASE_URL (env, then .env) without importing app config."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return _DEFAULT_DB_URL


def _swap_db_name(url: str, new_name: str) -> str:
    """Return ``url`` with its database name replaced by ``new_name``."""
    prefix, sep, tail = url.rpartition("/")
    if not sep or not prefix:
        raise ValueError(f"Cannot parse a database name out of URL: {url!r}")
    tail = tail.split("?", 1)[0]  # drop any query string
    return f"{prefix}/{new_name}"


_BASE_DB_URL = _configured_database_url()
if _BASE_DB_URL.rstrip("/").endswith(f"/{_TEST_DB_NAME}"):
    raise RuntimeError(
        f"DATABASE_URL already points at {_TEST_DB_NAME!r}; refusing to run tests "
        "against it as its own base (would wipe the isolated test database)."
    )
# The test DB is dropped and recreated every session, so only ever touch a
# name that is unambiguously a test database.
if not _TEST_DB_NAME.endswith("_test"):
    raise RuntimeError(
        f"TEST_DATABASE_NAME={_TEST_DB_NAME!r} must end in '_test' -- it gets "
        "dropped and recreated on every test session."
    )

TEST_DATABASE_URL = _swap_db_name(_BASE_DB_URL, _TEST_DB_NAME)
_ADMIN_DATABASE_URL = _swap_db_name(_BASE_DB_URL, "postgres")

# Point every subsequent import at the test database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from backend.app.auth.security import create_access_token, get_password_hash  # noqa: E402
from backend.app.database import AsyncSessionLocal, engine, init_db  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models.branch import Branch, Table  # noqa: E402,F401
from backend.app.models.menu import (  # noqa: E402,F401
    MenuCategory,
    MenuItem,
    MenuItemStock,
    MenuItemVariant,
)
from backend.app.models.user import Customer, StaffUser  # noqa: E402,F401


async def _reset_test_database() -> None:
    """Drop and recreate the test database so every session starts clean."""
    import asyncpg

    conn = await asyncpg.connect(_ADMIN_DATABASE_URL.replace("+asyncpg", ""))
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _TEST_DB_NAME,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}"')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create the isolated test DB, build its schema, and seed it once."""
    from backend.app.seed import seed_data

    async def _setup() -> None:
        await _reset_test_database()
        await init_db()
        await seed_data()
        await engine.dispose()

    asyncio.run(_setup())
    yield


@pytest.fixture(scope="function", autouse=True)
async def prepare_database():
    """Ensure database tables and btree_gist extension are initialized and pool disposed."""
    await init_db()
    yield
    await engine.dispose()


@pytest.fixture
async def async_client():
    """Provide AsyncClient for FastAPI test requests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def customer_auth_headers():
    """Generate headers for a valid test customer."""
    token = create_access_token({"sub": "1", "type": "customer", "email": "customer@example.com"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def staff_auth_headers():
    """Generate headers for a valid staff admin."""
    token = create_access_token({"sub": "1", "type": "staff", "username": "admin", "role": "admin"})
    return {"Authorization": f"Bearer {token}"}
