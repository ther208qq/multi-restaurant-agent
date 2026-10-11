"""Task11 route tests: auth endpoints served through AuthService.

These verify the migrated HTTP layer keeps its previous behavior (status codes,
response shape, exact error messages) now that all user lookups and credential
checks live in AuthService.
"""

from uuid import uuid4
from pathlib import Path

from sqlalchemy import delete

from backend.app.auth import security as security_module
from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.user import Customer
from backend.app.routes import auth as auth_routes
from backend.app.routes import staff_auth as staff_auth_routes


def _email() -> str:
    return f"__ar_{uuid4().hex}@example.com"


def test_auth_modules_do_not_query_the_database_directly():
    """Task11: auth routes and the auth deps delegate DB access to AuthService."""
    for module in (auth_routes, staff_auth_routes, security_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "select(" not in source, module.__name__
        assert ".execute(" not in source, module.__name__
        assert ".commit(" not in source, module.__name__
    assert "AuthService" in Path(auth_routes.__file__).read_text(encoding="utf-8")
    assert "AuthService" in Path(staff_auth_routes.__file__).read_text(encoding="utf-8")
    assert "AuthService" in Path(security_module.__file__).read_text(encoding="utf-8")


async def _cleanup(emails):
    async with transactional_session() as s:
        if emails:
            await s.execute(delete(Customer).where(Customer.email.in_(emails)))


async def test_register_login_and_me_roundtrip(async_client):
    email = _email()
    try:
        reg = await async_client.post(
            "/api/auth/register",
            json={"name": "Bob Tester", "email": email, "password": "pw123", "phone": "555"},
        )
        assert reg.status_code == 200
        reg_body = reg.json()
        assert reg_body["token_type"] == "bearer"
        assert reg_body["user"]["email"] == email
        assert reg_body["user"]["type"] == "customer"

        login = await async_client.post(
            "/api/auth/login", json={"email": email, "password": "pw123"}
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        me = await async_client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == email
    finally:
        await _cleanup([email])


async def test_register_duplicate_email_returns_400(async_client):
    email = _email()
    payload = {"name": "Bob Tester", "email": email, "password": "pw123"}
    try:
        first = await async_client.post("/api/auth/register", json=payload)
        assert first.status_code == 200

        dup = await async_client.post("/api/auth/register", json=payload)
        assert dup.status_code == 400
        assert dup.json()["detail"] == "An account with this email address already exists."
    finally:
        await _cleanup([email])


async def test_login_wrong_password_returns_401(async_client):
    email = _email()
    try:
        await async_client.post(
            "/api/auth/register", json={"name": "Bob", "email": email, "password": "pw123"}
        )
        resp = await async_client.post(
            "/api/auth/login", json={"email": email, "password": "wrong"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password."
    finally:
        await _cleanup([email])


async def test_staff_login_and_me(async_client):
    login = await async_client.post(
        "/api/staff/login", json={"username": "admin", "password": "admin123"}
    )
    assert login.status_code == 200
    body = login.json()
    assert body["user"]["role"] == "admin"

    me = await async_client.get(
        "/api/staff/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


async def test_staff_login_wrong_password_returns_401(async_client):
    resp = await async_client.post(
        "/api/staff/login", json={"username": "admin", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid username or password."


async def test_me_without_token_returns_401(async_client):
    assert (await async_client.get("/api/auth/me")).status_code == 401
    assert (await async_client.get("/api/staff/me")).status_code == 401


async def test_customer_me_unknown_id_returns_401(async_client):
    token = create_access_token(
        {"sub": "999999999", "type": "customer", "email": "ghost@example.com"}
    )
    resp = await async_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Customer not found"


async def test_staff_me_unknown_id_returns_401(async_client):
    token = create_access_token(
        {"sub": "999999999", "type": "staff", "username": "ghost", "role": "staff"}
    )
    resp = await async_client.get(
        "/api/staff/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Staff user not found"
