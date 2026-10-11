"""User repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``Customer`` /
``StaffUser`` lookups and inserts, never commits, and never imports ``service``
/ ``routes`` / ``agent``.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user import Customer, StaffUser


class UserRepo:
    @staticmethod
    async def get_customer_by_id(
        session: AsyncSession, customer_id: int
    ) -> Optional[Customer]:
        res = await session.execute(select(Customer).where(Customer.id == customer_id))
        return res.scalar_one_or_none()

    @staticmethod
    async def get_customer_by_email(
        session: AsyncSession, email: str
    ) -> Optional[Customer]:
        res = await session.execute(select(Customer).where(Customer.email == email))
        return res.scalar_one_or_none()

    @staticmethod
    async def create_customer(
        session: AsyncSession,
        *,
        name: str,
        email: str,
        password_hash: str,
        phone: Optional[str] = None,
    ) -> Customer:
        customer = Customer(
            name=name,
            email=email,
            password_hash=password_hash,
            phone=phone,
        )
        session.add(customer)
        await session.flush()
        return customer

    @staticmethod
    async def get_staff_by_id(
        session: AsyncSession, staff_id: int
    ) -> Optional[StaffUser]:
        res = await session.execute(select(StaffUser).where(StaffUser.id == staff_id))
        return res.scalar_one_or_none()

    @staticmethod
    async def get_staff_by_username(
        session: AsyncSession, username: str
    ) -> Optional[StaffUser]:
        res = await session.execute(
            select(StaffUser).where(StaffUser.username == username)
        )
        return res.scalar_one_or_none()


__all__ = ["UserRepo"]
