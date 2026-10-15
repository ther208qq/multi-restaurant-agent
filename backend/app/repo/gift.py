"""Gift repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``Gift`` queries and
inserts, never commits, and never imports ``service`` / ``routes`` / ``agent``.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.gift import Gift


class GiftRepo:
    @staticmethod
    async def create_gift(session: AsyncSession, values: Dict[str, Any]) -> Gift:
        gift = Gift(**values)
        session.add(gift)
        await session.flush()
        return gift

    @staticmethod
    async def get_gift(
        session: AsyncSession, gift_id: int, *, with_item: bool = False
    ) -> Optional[Gift]:
        stmt = select(Gift).where(Gift.id == gift_id)
        if with_item:
            stmt = stmt.options(selectinload(Gift.item))
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_source(
        session: AsyncSession,
        *,
        source_type: str,
        source_id: int,
        with_item: bool = False,
    ) -> Optional[Gift]:
        """The voucher issued for one booking / order, if any.

        Lets a repeat "accept the offer" call hand back the voucher it already
        issued rather than take a second unit of stock for the same booking.
        """
        stmt = (
            select(Gift)
            .where(Gift.source_type == source_type, Gift.source_id == source_id)
            .order_by(Gift.id.asc())
        )
        if with_item:
            stmt = stmt.options(selectinload(Gift.item))
        res = await session.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def list_gifts(
        session: AsyncSession,
        *,
        branch_id: Optional[int] = None,
        status: Optional[str] = None,
        source_type: Optional[str] = None,
        with_item: bool = False,
        limit: Optional[int] = None,
    ) -> List[Gift]:
        stmt = select(Gift)
        if with_item:
            stmt = stmt.options(selectinload(Gift.item))
        if branch_id is not None:
            stmt = stmt.where(Gift.branch_id == branch_id)
        if status is not None:
            stmt = stmt.where(Gift.status == status)
        if source_type is not None:
            stmt = stmt.where(Gift.source_type == source_type)
        stmt = stmt.order_by(Gift.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def update_status(
        session: AsyncSession, gift: Gift, new_status: str
    ) -> Gift:
        gift.status = new_status
        await session.flush()
        return gift


__all__ = ["GiftRepo"]
