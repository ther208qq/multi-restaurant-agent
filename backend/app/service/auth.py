"""Authentication service.

Owns the transaction boundary for user lookups and credential verification
(spec §3.5 / D3: the *pure* JWT/hash primitives stay in
:mod:`backend.app.auth.crypto`, while querying users and checking credentials
sinks here so the "only the Repo touches the DB" invariant holds). Reused by
the customer/staff auth routes and the auth dependencies.

Raises domain exceptions (never HTTP types); the API maps them to status codes.
"""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.crypto import get_password_hash, verify_password
from backend.app.database import transactional_session
from backend.app.exceptions import AuthenticationError, ValidationError
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.user import UserRepo

DUPLICATE_EMAIL_MESSAGE = "An account with this email address already exists."
INVALID_CREDENTIALS_CUSTOMER_MESSAGE = "Invalid email or password."
INVALID_CREDENTIALS_STAFF_MESSAGE = "Invalid username or password."


class AuthService:
    @staticmethod
    async def register_customer(
        *,
        name: str,
        email: str,
        password: str,
        phone: Optional[str] = None,
        session: Optional[AsyncSession] = None,
    ) -> Customer:
        """Create a customer account, rejecting a duplicate email address."""
        async with transactional_session(session) as s:
            existing = await UserRepo.get_customer_by_email(s, email)
            if existing is not None:
                raise ValidationError(DUPLICATE_EMAIL_MESSAGE)
            return await UserRepo.create_customer(
                s,
                name=name,
                email=email,
                password_hash=get_password_hash(password),
                phone=phone,
            )

    @staticmethod
    async def authenticate_customer(
        email: str,
        password: str,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Customer:
        """Return the customer matching ``email``/``password`` or raise."""
        async with transactional_session(session) as s:
            customer = await UserRepo.get_customer_by_email(s, email)
            if customer is None or not verify_password(password, customer.password_hash):
                raise AuthenticationError(INVALID_CREDENTIALS_CUSTOMER_MESSAGE)
            return customer

    @staticmethod
    async def authenticate_staff(
        username: str,
        password: str,
        *,
        session: Optional[AsyncSession] = None,
    ) -> StaffUser:
        """Return the staff member matching ``username``/``password`` or raise."""
        async with transactional_session(session) as s:
            staff = await UserRepo.get_staff_by_username(s, username)
            if staff is None or not verify_password(password, staff.password_hash):
                raise AuthenticationError(INVALID_CREDENTIALS_STAFF_MESSAGE)
            return staff

    @staticmethod
    async def get_customer(
        customer_id: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Customer:
        """Load an authenticated customer by id, or raise ``AuthenticationError``."""
        async with transactional_session(session) as s:
            customer = await UserRepo.get_customer_by_id(s, customer_id)
            if customer is None:
                raise AuthenticationError("Customer not found")
            return customer

    @staticmethod
    async def get_staff(
        staff_id: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> StaffUser:
        """Load an authenticated staff member by id, or raise ``AuthenticationError``."""
        async with transactional_session(session) as s:
            staff = await UserRepo.get_staff_by_id(s, staff_id)
            if staff is None:
                raise AuthenticationError("Staff user not found")
            return staff


__all__ = ["AuthService"]
