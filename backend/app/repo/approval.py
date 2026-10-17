"""Order-approval repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``OrderApproval`` queries
and inserts, never commits, and never imports ``service`` / ``routes`` / ``agent``.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.approval import OrderApproval
from backend.app.models.order import Order, OrderItem


def _order_load_options():
    """Eager-load a gated order fully enough to render it in a response.

    The approval board shows each gated order's lines (with item names) and its
    branch, so loading the order alone is not enough -- touching an unloaded
    relationship in async context raises instead of fetching it. ``customer`` is
    left out: the board does not need it, and it stays an optional field.
    """
    return [
        selectinload(OrderApproval.order)
        .selectinload(Order.items)
        .selectinload(OrderItem.menu_item),
        selectinload(OrderApproval.order).selectinload(Order.branch),
    ]


class ApprovalRepo:
    @staticmethod
    async def create_approval(
        session: AsyncSession, values: Dict[str, Any]
    ) -> OrderApproval:
        approval = OrderApproval(**values)
        session.add(approval)
        await session.flush()
        return approval

    @staticmethod
    async def get_approval(
        session: AsyncSession, approval_id: int, *, with_order: bool = False
    ) -> Optional[OrderApproval]:
        stmt = select(OrderApproval).where(OrderApproval.id == approval_id)
        if with_order:
            stmt = stmt.options(*_order_load_options())
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_by_orders(
        session: AsyncSession, order_ids: List[int]
    ) -> List[OrderApproval]:
        """The approvals gating a batch of orders, for a one-query lookup."""
        if not order_ids:
            return []
        stmt = select(OrderApproval).where(OrderApproval.order_id.in_(order_ids))
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_approvals(
        session: AsyncSession,
        *,
        branch_id: Optional[int] = None,
        status: Optional[str] = None,
        with_order: bool = False,
        limit: Optional[int] = None,
    ) -> List[OrderApproval]:
        """Approvals, oldest first (the queue order a manager works through).

        ``branch_id`` filters by the *order's* branch -- the approval row carries
        no branch of its own. The join is on the ``order_id`` FK (one approval per
        order), so it never multiplies rows.
        """
        stmt = select(OrderApproval)
        if with_order:
            stmt = stmt.options(*_order_load_options())
        if branch_id is not None:
            stmt = stmt.join(Order, Order.id == OrderApproval.order_id).where(
                Order.branch_id == branch_id
            )
        if status is not None:
            stmt = stmt.where(OrderApproval.status == status)
        stmt = stmt.order_by(OrderApproval.created_at.asc(), OrderApproval.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def update_status(
        session: AsyncSession, approval: OrderApproval, new_status: str
    ) -> OrderApproval:
        approval.status = new_status
        await session.flush()
        return approval


__all__ = ["ApprovalRepo"]
