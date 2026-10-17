"""Business statistics service (spec §9 「经营统计与异常处理」).

Answers "how did this branch do?" for a date range: order volume, revenue,
average order value, cancelled orders, total dish units, a per-day series and a
best-selling-dish ranking.

Two rules decide what the numbers mean, and both live here rather than in the
query layer:

* **A cancelled order is not a sale.** It is excluded from the order count, the
  revenue, the units and the dish ranking, and reported separately as
  ``cancelled_count``. An order that was cancelled and later restored
  (``cancelled -> placed``) counts as a sale again, because it is not cancelled
  any more -- the numbers always describe the current state of the orders in the
  window, so staff and customers read the same story.
* **A day is a UTC day.** Timestamps are stored as ``timestamptz``; the range is
  applied as a half-open UTC window ``[start 00:00, end+1 00:00)``, with
  ``end_date`` inclusive so the range reads the way it is written.

Branch scope is enforced here too (spec §9 权限与审计): a branch-bound staff
member can only aggregate their own branch, and an out-of-scope ``branch_id`` is
a ``ForbiddenError`` rather than a silent empty result.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import NotFoundError, ValidationError
from backend.app.models.user import StaffUser
from backend.app.repo.branch import BranchRepo
from backend.app.repo.stats import StatsRepo
from backend.app.service.order import VALID_ORDER_STATUSES
from backend.app.service.scope import resolve_staff_branch

# Everything that is not cancelled is a sale -- including ``delivered`` (the
# order was served) and orders still moving through the lifecycle.
SOLD_ORDER_STATUSES = tuple(s for s in VALID_ORDER_STATUSES if s != "cancelled")
CANCELLED_ORDER_STATUSES = ("cancelled",)

DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 366
DEFAULT_TOP_ITEMS = 10
MAX_TOP_ITEMS = 50

DateLike = Union[date, datetime, str]


def _to_date(value: Optional[DateLike], field: str) -> Optional[date]:
    """Coerce a route/query value into a ``date``, or reject it clearly."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValidationError(f"{field} 的日期格式无效，请使用 YYYY-MM-DD。")


def _day_start(day: date) -> datetime:
    """Midnight UTC of ``day``."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def resolve_branch_scope(
    branch_id: Optional[int], staff: Optional[StaffUser]
) -> int:
    """The branch a request may read, applying the staff scope rule.

    A branch-bound staff member may only name their own branch and defaults to
    it; an unscoped staff member must name one. Shared by every service that
    aggregates branch data so the permission rule exists once.
    """
    return resolve_staff_branch(
        staff, branch_id, required_message="请指定要统计的门店。"
    )


class StatsService:
    @staticmethod
    async def get_branch_summary(
        branch_id: Optional[int] = None,
        start_date: Optional[DateLike] = None,
        end_date: Optional[DateLike] = None,
        *,
        top_limit: int = DEFAULT_TOP_ITEMS,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Order volume, revenue, dish sales and a daily series for one branch.

        ``start_date`` / ``end_date`` default to the last ``DEFAULT_RANGE_DAYS``
        days ending today (UTC) and are inclusive on both ends.
        """
        if top_limit < 1:
            raise ValidationError("排行条数至少为 1。")
        top_limit = min(top_limit, MAX_TOP_ITEMS)

        resolved_branch_id = resolve_branch_scope(branch_id, staff)

        end = _to_date(end_date, "结束日期") or datetime.now(timezone.utc).date()
        start = _to_date(start_date, "开始日期") or (
            end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        )
        if start > end:
            raise ValidationError("开始日期不能晚于结束日期。")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValidationError(f"统计区间最长 {MAX_RANGE_DAYS} 天。")

        start_dt = _day_start(start)
        end_dt = _day_start(end + timedelta(days=1))

        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, resolved_branch_id)
            if branch is None:
                raise NotFoundError(f"门店 #{resolved_branch_id} 不存在。")

            totals = await StatsRepo.order_totals(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                sold_statuses=SOLD_ORDER_STATUSES,
                cancelled_statuses=CANCELLED_ORDER_STATUSES,
            )
            units = await StatsRepo.units_sold(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                sold_statuses=SOLD_ORDER_STATUSES,
            )
            daily = await StatsRepo.daily_totals(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                sold_statuses=SOLD_ORDER_STATUSES,
                cancelled_statuses=CANCELLED_ORDER_STATUSES,
            )
            dishes = await StatsRepo.dish_totals(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                sold_statuses=SOLD_ORDER_STATUSES,
                limit=top_limit,
            )

        order_count = totals["order_count"]
        revenue = round(totals["revenue"], 2)
        return {
            "branch_id": resolved_branch_id,
            "branch_name": branch.name,
            "start_date": start,
            "end_date": end,
            "order_count": order_count,
            "cancelled_count": totals["cancelled_count"],
            "revenue": revenue,
            "average_order_value": (
                round(revenue / order_count, 2) if order_count else 0.0
            ),
            "units_sold": units,
            "top_items": [
                {
                    "menu_item_id": dish["menu_item_id"],
                    "name": dish["name"],
                    "quantity": dish["quantity"],
                    "revenue": round(dish["revenue"], 2),
                    "order_count": dish["order_count"],
                }
                for dish in dishes
            ],
            "daily": [
                {
                    "date": point["day"].date(),
                    "order_count": point["order_count"],
                    "revenue": round(point["revenue"], 2),
                    "cancelled_count": point["cancelled_count"],
                }
                for point in daily
            ],
        }


__all__ = [
    "StatsService",
    "CANCELLED_ORDER_STATUSES",
    "SOLD_ORDER_STATUSES",
    "DEFAULT_RANGE_DAYS",
    "MAX_RANGE_DAYS",
    "DEFAULT_TOP_ITEMS",
    "MAX_TOP_ITEMS",
    "resolve_branch_scope",
]
