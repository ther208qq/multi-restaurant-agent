"""Order repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``Order`` / ``OrderItem``
queries and inserts, never commits, and never imports ``service`` / ``routes`` /
``agent``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.order import Order, OrderItem


class OrderRepo:
    @staticmethod
    async def create_order(session: AsyncSession, values: Dict[str, Any]) -> Order:
        order = Order(**values)
        session.add(order)
        await session.flush()
        return order

    @staticmethod
    async def add_order_item(
        session: AsyncSession, values: Dict[str, Any]
    ) -> OrderItem:
        order_item = OrderItem(**values)
        session.add(order_item)
        await session.flush()
        return order_item

    @staticmethod
    async def get_order(
        session: AsyncSession,
        order_id: int,
        *,
        with_items: bool = False,
        with_items_menu_item: bool = False,
        with_customer: bool = False,
        with_branch: bool = False,
    ) -> Optional[Order]:
        stmt = select(Order).where(Order.id == order_id)
        options = []
        if with_items_menu_item:
            options.append(
                selectinload(Order.items).selectinload(OrderItem.menu_item)
            )
        elif with_items:
            options.append(selectinload(Order.items))
        if with_customer:
            options.append(selectinload(Order.customer))
        if with_branch:
            options.append(selectinload(Order.branch))
        if options:
            stmt = stmt.options(*options)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_orders(
        session: AsyncSession,
        *,
        customer_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        order_status: Optional[str] = None,
        with_items: bool = True,
        with_items_menu_item: bool = False,
        with_customer: bool = False,
        with_branch: bool = False,
        limit: Optional[int] = None,
    ) -> List[Order]:
        stmt = select(Order)
        options = []
        if with_items_menu_item:
            options.append(
                selectinload(Order.items).selectinload(OrderItem.menu_item)
            )
        elif with_items:
            options.append(selectinload(Order.items))
        if with_customer:
            options.append(selectinload(Order.customer))
        if with_branch:
            options.append(selectinload(Order.branch))
        if options:
            stmt = stmt.options(*options)

        if customer_id is not None:
            stmt = stmt.where(Order.customer_id == customer_id)
        if branch_id is not None:
            stmt = stmt.where(Order.branch_id == branch_id)
        if order_status is not None:
            stmt = stmt.where(Order.status == order_status)

        stmt = stmt.order_by(Order.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_recent_orders_for_customer(
        session: AsyncSession,
        *,
        customer_id: int,
        branch_id: int,
        statuses: Sequence[str],
        since: datetime,
        limit: int = 20,
    ) -> List[Order]:
        """This customer's still-open orders in a branch, newest first.

        Feeds the duplicate-request check: the caller compares item lines itself
        (the repo does not know what "the same order" means), so this returns
        candidates with their lines loaded and leaves the judgement to the
        Service.
        """
        stmt = (
            select(Order)
            .options(selectinload(Order.items))
            .where(
                Order.customer_id == customer_id,
                Order.branch_id == branch_id,
                Order.status.in_(tuple(statuses)),
                Order.created_at >= since,
            )
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def update_order_status(
        session: AsyncSession, order: Order, new_status: str
    ) -> Order:
        order.status = new_status
        await session.flush()
        return order

    @staticmethod
    async def update_kds_status(
        session: AsyncSession,
        order: Order,
        new_status: str,
        held_reason: Optional[str] = None,
    ) -> Order:
        """Move the simulated KDS state and record why a task is held.

        The Service has already validated the transition; this only writes the
        two fields, clearing ``kds_held_reason`` whenever the task is not held so
        a stale reason never lingers on a live order.
        """
        order.kds_status = new_status
        order.kds_held_reason = held_reason if new_status == "held" else None
        await session.flush()
        return order


__all__ = ["OrderRepo"]
