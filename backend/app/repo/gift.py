"""Gift repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``Gift`` queries and
inserts, never commits, and never imports ``service`` / ``routes`` / ``agent``.
"""

from typing import Any, Dict, Optional

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

__all__ = ["GiftRepo"]
