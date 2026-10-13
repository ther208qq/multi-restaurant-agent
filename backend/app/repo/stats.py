"""Business-statistics repository: aggregate queries only.

Receives a session from the Service layer and returns plain rows (counts, sums,
grouped series) — no business interpretation of *which* statuses count or which
timezone a day starts in, which belongs to :mod:`backend.app.service.stats`.

Every aggregate is scoped to one branch and one half-open UTC window; the caller
passes the status filter so the definition of a "sold" order lives in one place.
"""

from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.menu import MenuItem
from backend.app.models.order import Order, OrderItem

# ``date_trunc`` on a ``timestamptz`` would follow the session's TimeZone
# setting, so the day boundary would silently move with the server config.
# Converting to UTC first pins it: a "day" is a UTC day for every caller.
_UTC_DAY = func.date_trunc("day", func.timezone("UTC", Order.created_at))


class StatsRepo:
    @staticmethod
    async def order_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
        cancelled_statuses: tuple,
    ) -> Dict[str, Any]:
        """One row of counts and revenue for the branch and window."""
        stmt = select(
            func.count()
            .filter(Order.status.in_(sold_statuses))
            .label("order_count"),
            func.coalesce(
                func.sum(Order.total_amount).filter(Order.status.in_(sold_statuses)),
                0,
            ).label("revenue"),
            func.count()
            .filter(Order.status.in_(cancelled_statuses))
            .label("cancelled_count"),
        ).where(
            Order.branch_id == branch_id,
            Order.created_at >= start,
            Order.created_at < end,
        )
        res = await session.execute(stmt)
        row = res.one()
        return {
            "order_count": int(row.order_count or 0),
            "revenue": float(row.revenue or 0),
            "cancelled_count": int(row.cancelled_count or 0),
        }

    @staticmethod
    async def daily_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
        cancelled_statuses: tuple,
    ) -> List[Dict[str, Any]]:
        """Per-UTC-day counts and revenue, oldest day first."""
        stmt = (
            select(
                _UTC_DAY.label("day"),
                func.count()
                .filter(Order.status.in_(sold_statuses))
                .label("order_count"),
                func.coalesce(
                    func.sum(Order.total_amount).filter(
                        Order.status.in_(sold_statuses)
                    ),
                    0,
                ).label("revenue"),
                func.count()
                .filter(Order.status.in_(cancelled_statuses))
                .label("cancelled_count"),
            )
            .where(
                Order.branch_id == branch_id,
                Order.created_at >= start,
                Order.created_at < end,
            )
            .group_by(_UTC_DAY)
            .order_by(_UTC_DAY)
        )
        res = await session.execute(stmt)
        return [
            {
                "day": row.day,
                "order_count": int(row.order_count or 0),
                "revenue": float(row.revenue or 0),
                "cancelled_count": int(row.cancelled_count or 0),
            }
            for row in res.all()
        ]

    @staticmethod
    async def units_sold(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
    ) -> int:
        """Total dish units sold in the branch and window (all dishes, not just
        the top-N ranking)."""
        stmt = (
            select(func.coalesce(func.sum(OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.branch_id == branch_id,
                Order.created_at >= start,
                Order.created_at < end,
                Order.status.in_(sold_statuses),
            )
        )
        res = await session.execute(stmt)
        return int(res.scalar_one() or 0)

    @staticmethod
    async def dish_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Units and revenue per dish, best selling first.

        Revenue uses the line's *snapshot* ``unit_price`` (base price plus the
        resolved variant deltas, frozen at placement), so a later menu price
        change cannot rewrite the history it reports on.
        """
        units = func.sum(OrderItem.quantity)
        stmt = (
            select(
                MenuItem.id.label("menu_item_id"),
                MenuItem.name.label("name"),
                units.label("quantity"),
                func.coalesce(
                    func.sum(OrderItem.quantity * OrderItem.unit_price), 0
                ).label("revenue"),
                func.count(func.distinct(OrderItem.order_id)).label("order_count"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
            .where(
                Order.branch_id == branch_id,
                Order.created_at >= start,
                Order.created_at < end,
                Order.status.in_(sold_statuses),
            )
            .group_by(MenuItem.id, MenuItem.name)
            .order_by(units.desc(), MenuItem.name.asc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return [
            {
                "menu_item_id": row.menu_item_id,
                "name": row.name,
                "quantity": int(row.quantity or 0),
                "revenue": float(row.revenue or 0),
                "order_count": int(row.order_count or 0),
            }
            for row in res.all()
        ]


__all__ = ["StatsRepo"]
