"""Task10 tests: AuthService registration, authentication and lookups.

These exercise the Service directly (transaction boundary + domain exceptions)
and confirm the pure hashing primitive it relies on: stored hashes are bcrypt
and verify only against the right password.
"""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from backend.app.auth.crypto import verify_password
from backend.app.database import transactional_session
from backend.app.exceptions import AuthenticationError, ValidationError
from backend.app.models.user import Customer, StaffUser
from backend.app.service.auth import AuthService


def _email() -> str:
    return f"__as_{uuid4().hex}@x.com"


async def _cleanup(emails):
    async with transactional_session() as s:
        if emails:
            await s.execute(delete(Customer).where(Customer.email.in_(emails)))


async def test_register_customer_creates_hashed_account():
    email = _email()
    try:
        customer = await AuthService.register_customer(
            name="__as", email=email, password="s3cret", phone="555"
        )
        assert customer.id is not None
        assert customer.name == "__as"
        assert customer.email == email
        assert customer.phone == "555"
        assert customer.password_hash != "s3cret"
        assert verify_password("s3cret", customer.password_hash)
        assert not verify_password("wrong", customer.password_hash)
    finally:
        await _cleanup([email])


async def test_register_customer_duplicate_email_raises():
    email = _email()
    try:
        await AuthService.register_customer(name="__as", email=email, password="x")
        with pytest.raises(ValidationError) as exc_info:
            await AuthService.register_customer(name="__as", email=email, password="y")
        assert exc_info.value.message == (
            "An account with this email address already exists."
        )
    finally:
        await _cleanup([email])


async def test_authenticate_customer_success():
    email = _email()
    try:
        created = await AuthService.register_customer(
            name="__as", email=email, password="pw"
        )
        authenticated = await AuthService.authenticate_customer(email, "pw")
        assert authenticated.id == created.id
        assert authenticated.email == email
    finally:
        await _cleanup([email])


async def test_authenticate_customer_wrong_password_raises():
    email = _email()
    try:
        await AuthService.register_customer(name="__as", email=email, password="pw")
        with pytest.raises(AuthenticationError) as exc_info:
            await AuthService.authenticate_customer(email, "nope")
        assert exc_info.value.message == "Invalid email or password."
    finally:
        await _cleanup([email])


async def test_authenticate_customer_unknown_email_raises():
    with pytest.raises(AuthenticationError) as exc_info:
        await AuthService.authenticate_customer(_email(), "pw")
    assert exc_info.value.message == "Invalid email or password."


async def test_get_customer_not_found_raises():
    with pytest.raises(AuthenticationError) as exc_info:
        await AuthService.get_customer(999999999)
    assert exc_info.value.message == "Customer not found"


async def test_get_customer_returns_registered_account():
    email = _email()
    try:
        created = await AuthService.register_customer(
            name="__as", email=email, password="pw"
        )
        fetched = await AuthService.get_customer(created.id)
        assert fetched.id == created.id
    finally:
        await _cleanup([email])


async def test_authenticate_staff_success():
    staff = await AuthService.authenticate_staff("admin", "admin123")
    assert isinstance(staff, StaffUser)
    assert staff.role == "admin"


async def test_authenticate_staff_wrong_password_raises():
    with pytest.raises(AuthenticationError) as exc_info:
        await AuthService.authenticate_staff("admin", "nope")
    assert exc_info.value.message == "Invalid username or password."


async def test_authenticate_staff_unknown_username_raises():
    with pytest.raises(AuthenticationError) as exc_info:
        await AuthService.authenticate_staff("__no_such_staff__", "x")
    assert exc_info.value.message == "Invalid username or password."


async def test_get_staff_not_found_raises():
    with pytest.raises(AuthenticationError) as exc_info:
        await AuthService.get_staff(999999999)
    assert exc_info.value.message == "Staff user not found"


async def test_get_staff_returns_seeded_admin():
    staff = await AuthService.get_staff(1)
    assert staff.username == "admin"
