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
    async def order_date_range(
        session: AsyncSession,
        *,
        branch_id: int,
    ) -> tuple:
        """First and last order timestamp in the branch, or ``(None, None)``.

        Used to describe what history the branch actually has, so a request for
        "the last seven days" does not have to hit an empty window when the only
        available data is historical. Every order status is included: even a
        cancelled order proves the branch was recording data that day.
        """
        stmt = select(
            func.min(Order.created_at).label("first_at"),
            func.max(Order.created_at).label("last_at"),
        ).where(Order.branch_id == branch_id)
        row = (await session.execute(stmt)).one()
        return row.first_at, row.last_at

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
    async def order_type_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
        cancelled_statuses: tuple,
    ) -> List[Dict[str, Any]]:
        """Sold/cancelled counts and revenue per order type in the window.

        One row per type that has any order at all. The ``order_count`` and
        ``revenue`` columns count sold orders only, matching every other
        aggregate; ``cancelled_count`` is reported beside them rather than
        folded in, so a type's structure never hides its cancellations.
        """
        stmt = (
            select(
                Order.order_type.label("order_type"),
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
            .group_by(Order.order_type)
            .order_by(Order.order_type)
        )
        res = await session.execute(stmt)
        return [
            {
                "order_type": row.order_type,
                "order_count": int(row.order_count or 0),
                "revenue": float(row.revenue or 0),
                "cancelled_count": int(row.cancelled_count or 0),
            }
            for row in res.all()
        ]

    @staticmethod
    async def weekday_hour_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
        timezone_name: str,
    ) -> List[Dict[str, Any]]:
        """Sold counts and revenue per weekday and hour *in a business timezone*.

        The window is still the same half-open UTC range every other aggregate
        uses, but each row's weekday (ISO 1=Monday...7=Sunday) and hour come
        from the timestamp converted to ``timezone_name``. That is what makes a
        12:00 lunch order show up at hour 12 instead of 04:00. One row per
        (weekday, hour) cell that sold something; the Service pads the rest.
        """
        local_time = func.timezone(timezone_name, Order.created_at)
        weekday = func.extract("isodow", local_time)
        hour = func.extract("hour", local_time)
        stmt = (
            select(
                weekday.label("weekday"),
                hour.label("hour"),
                func.count().label("order_count"),
                func.coalesce(func.sum(Order.total_amount), 0).label("revenue"),
            )
            .where(
                Order.branch_id == branch_id,
                Order.created_at >= start,
                Order.created_at < end,
                Order.status.in_(sold_statuses),
            )
            .group_by(weekday, hour)
            .order_by(weekday, hour)
        )
        res = await session.execute(stmt)
        return [
            {
                "weekday": int(row.weekday),
                "hour": int(row.hour),
                "order_count": int(row.order_count or 0),
                "revenue": float(row.revenue or 0),
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


    @staticmethod
    async def dish_daily_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        sold_statuses: tuple,
        menu_item_ids: List[int],
    ) -> List[Dict[str, Any]]:
        """Units per requested dish per UTC day, ordered by dish then day.

        Only days on which a requested dish actually sold produce a row. A
        missing day is *not* a zero: the Service pads the calendar so that a day
        with no order lines stays distinguishable from a real zero-sales day.

        This is the only aggregate with both a dish and a day dimension. It
        deliberately applies no ranking or limit -- the caller names the dishes.
        """
        if not menu_item_ids:
            return []
        quantity = func.sum(OrderItem.quantity)
        stmt = (
            select(
                OrderItem.menu_item_id.label("menu_item_id"),
                _UTC_DAY.label("day"),
                quantity.label("quantity"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.branch_id == branch_id,
                Order.created_at >= start,
                Order.created_at < end,
                Order.status.in_(sold_statuses),
                OrderItem.menu_item_id.in_(menu_item_ids),
            )
            .group_by(OrderItem.menu_item_id, _UTC_DAY)
            .order_by(OrderItem.menu_item_id, _UTC_DAY)
        )
        res = await session.execute(stmt)
        return [
            {
                "menu_item_id": row.menu_item_id,
                "day": row.day,
                "quantity": int(row.quantity or 0),
            }
            for row in res.all()
        ]


__all__ = ["StatsRepo"]
