"""Tests for the Task1 infrastructure: domain exceptions, the session /
transaction boundary, and the domain-exception -> HTTP mapping."""

import ast
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from backend.app.database import AsyncSessionLocal, transactional_session
from backend.app.error_handlers import register_exception_handlers, status_for_exception
from backend.app.exceptions import (
    AuthenticationError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from backend.app.models.branch import Branch


# --------------------------------------------------------------------------- #
# Domain exceptions
# --------------------------------------------------------------------------- #

DOMAIN_EXCEPTIONS = [
    NotFoundError,
    ValidationError,
    ConflictError,
    AuthenticationError,
    ForbiddenError,
    ServiceError,
]


@pytest.mark.parametrize("exc_cls", DOMAIN_EXCEPTIONS)
def test_domain_exception_carries_message(exc_cls):
    err = exc_cls("boom")
    assert isinstance(err, DomainError)
    assert err.message == "boom"
    assert str(err) == "boom"


@pytest.mark.parametrize(
    "exc_cls,expected_status",
    [
        (NotFoundError, 404),
        (ValidationError, 400),
        (ConflictError, 409),
        (AuthenticationError, 401),
        (ForbiddenError, 403),
        (ServiceError, 500),
    ],
)
def test_status_mapping(exc_cls, expected_status):
    assert status_for_exception(exc_cls("x")) == expected_status


def test_unmapped_domain_error_defaults_to_500():
    assert status_for_exception(DomainError("x")) == 500


def test_exceptions_module_is_web_framework_independent():
    """Spec §5: domain exceptions must not depend on FastAPI/SQLAlchemy."""
    import backend.app.exceptions as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert "fastapi" not in imported
    assert "sqlalchemy" not in imported


# --------------------------------------------------------------------------- #
# Session / transaction boundary
# --------------------------------------------------------------------------- #

async def test_transactional_session_commits():
    name = f"__infra_commit_{uuid.uuid4().hex}"
    async with transactional_session() as session:
        session.add(Branch(name=name, address="addr", phone="000"))

    async with transactional_session() as session:
        res = await session.execute(select(Branch).where(Branch.name == name))
        assert res.scalar_one_or_none() is not None
        await session.execute(delete(Branch).where(Branch.name == name))


async def test_transactional_session_rolls_back_on_error():
    name = f"__infra_rollback_{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError):
        async with transactional_session() as session:
            session.add(Branch(name=name, address="addr", phone="000"))
            raise RuntimeError("boom")

    async with transactional_session() as session:
        res = await session.execute(select(Branch).where(Branch.name == name))
        assert res.scalar_one_or_none() is None


async def test_transactional_session_reuses_injected_session():
    async with AsyncSessionLocal() as injected:
        async with transactional_session(injected) as yielded:
            assert yielded is injected


# --------------------------------------------------------------------------- #
# Domain exception -> HTTP mapping
# --------------------------------------------------------------------------- #

def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    async def _not_found():
        raise NotFoundError("missing")

    @app.get("/validation")
    async def _validation():
        raise ValidationError("bad request")

    @app.get("/conflict")
    async def _conflict():
        raise ConflictError("conflict")

    @app.get("/auth")
    async def _auth():
        raise AuthenticationError("nope")

    @app.get("/forbidden")
    async def _forbidden():
        raise ForbiddenError("denied")

    @app.get("/service")
    async def _service():
        raise ServiceError("kaboom")

    return app


@pytest.mark.parametrize(
    "path,expected_status",
    [
        ("/not-found", 404),
        ("/validation", 400),
        ("/conflict", 409),
        ("/auth", 401),
        ("/forbidden", 403),
        ("/service", 500),
    ],
)
async def test_domain_error_maps_to_http(path, expected_status):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code == expected_status
    assert resp.json()["detail"]
