"""Business-profile diagnosis dimensions (Task61).

Four questions the order/diagnosis aggregates cannot answer on their own:

* **Reservation health** -- how many bookings were made, cancelled, seated,
  completed, and how many slots came and went while the booking was still
  ``pending`` / ``confirmed`` (a *suspected* no-show; there is no explicit
  no-show status in the data).
* **Weekday and time-of-day distribution** -- when the branch actually sells.
* **Order-type structure** -- dine-in / takeaway / delivery mix.
* **Last-week-same-weekday and last-year-same-period baselines** -- two
  familiar comparison anchors beside the adjacent equal-length baseline the
  deterministic diagnosis already uses.

Everything is read-only and aggregated here, in the Service layer; the Agent
never touches SQL and never names a branch. Branch scope is enforced by
:func:`resolve_branch_scope`, so an out-of-scope staff member gets
``ForbiddenError`` before any number is read.

Two definitions are chosen deliberately and stated in the payload rather than
silently assumed:

* a **sold** order is every status except ``cancelled`` (the statistics
  service's existing rule), so cancelled orders appear only as their own
  counts;
* a reservation belongs to the window by its **start time**, and "no-show"
  means the slot has passed while the status is still pending/confirmed. It is
  labelled ``suspected`` and accompanied by the denominator it was computed
  over, because the data cannot prove a guest never arrived.

Weekday/hour buckets are rendered in ``BUSINESS_TIMEZONE`` while the window
itself remains the same UTC half-open day range every other aggregate uses. The
window edges can therefore cut a local day in half; the note in the payload
says so.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.database import transactional_session
from backend.app.exceptions import NotFoundError, ValidationError
from backend.app.labels import ORDER_TYPE_LABELS, RESERVATION_STATUS_LABELS
from backend.app.models.user import StaffUser
from backend.app.repo.branch import BranchRepo
from backend.app.repo.reservation import ReservationRepo
from backend.app.repo.stats import StatsRepo
from backend.app.service.diagnostics import relative_change_pct
from backend.app.service.reservation import current_wall_time
from backend.app.service.stats import (
    CANCELLED_ORDER_STATUSES,
    DEFAULT_RANGE_DAYS,
    MAX_RANGE_DAYS,
    SOLD_ORDER_STATUSES,
    resolve_branch_scope,
)

DateLike = Union[date, datetime, str]

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# The canonical lifecycle; unknown statuses that show up in the data are kept
# in ``by_status`` too instead of being dropped.
RESERVATION_STATUSES = ("pending", "confirmed", "seated", "completed", "cancelled")
ARRIVED_RESERVATION_STATUSES = ("seated", "completed")
NO_SHOW_RESERVATION_STATUSES = ("pending", "confirmed")

BASELINE_LABELS = {
    "last_week_same_weekday": "上周同星期（整体前移 7 天）",
    "last_year_same_period": "去年同期（整体前移 1 年）",
}


def _to_date(value: Optional[DateLike], field: str) -> Optional[date]:
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


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _utc_bounds(start: date, end: date) -> Tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )


def _resolve_window(
    start_date: Optional[DateLike], end_date: Optional[DateLike]
) -> Tuple[date, date]:
    end = _to_date(end_date, "结束日期") or _today()
    start = _to_date(start_date, "开始日期") or (
        end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    )
    if start > end:
        raise ValidationError("开始日期不能晚于结束日期。")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValidationError(f"统计区间最长 {MAX_RANGE_DAYS} 天。")
    return start, end


def _shift_year(day: date, years: int = -1) -> date:
    """The same calendar date ``years`` away; Feb 29 falls back to Feb 28."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def _weekday_days(start: date, end: date, iso_weekday: int) -> int:
    """How many times ``iso_weekday`` (1=Mon..7=Sun) occurs in the window."""
    span = (end - start).days + 1
    return sum(
        1
        for offset in range(span)
        if (start + timedelta(days=offset)).isoweekday() == iso_weekday
    )


def _period(totals: Dict[str, Any], start: date, end: date) -> Dict[str, Any]:
    order_count = totals["order_count"]
    revenue = round(totals["revenue"], 2)
    return {
        "start_date": start,
        "end_date": end,
        "order_count": order_count,
        "cancelled_count": totals["cancelled_count"],
        "revenue": revenue,
        "average_order_value": (
            round(revenue / order_count, 2) if order_count else 0.0
        ),
    }


def _baseline(
    label: str,
    current: Dict[str, Any],
    baseline: Dict[str, Any],
    *,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """One comparison anchor, stated as facts plus a relative change.

    A baseline with no sold orders cannot yield a percentage; the status says
    so instead of presenting an undefined change as 0% or 100%.
    """
    status = "ok" if baseline["order_count"] else "zero_baseline"
    messages = list(notes or [])
    if status == "zero_baseline":
        messages.append("该基线窗口没有成交订单，无法计算变化比例。")
    return {
        "label": label,
        **baseline,
        "status": status,
        "order_count_change_pct": (
            round(relative_change_pct(current["order_count"], baseline["order_count"]), 2)
            if baseline["order_count"]
            else None
        ),
        "revenue_change_pct": (
            round(relative_change_pct(current["revenue"], baseline["revenue"]), 2)
            if baseline["revenue"] > 0
            else None
        ),
        "notes": messages,
    }


class BusinessProfileService:
    @staticmethod
    async def get_business_profile(
        branch_id: Optional[int] = None,
        start_date: Optional[DateLike] = None,
        end_date: Optional[DateLike] = None,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
        timezone_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reservation health plus weekday/hour/order-type structure and baselines.

        ``start_date`` / ``end_date`` are inclusive UTC days and default to the
        last :data:`DEFAULT_RANGE_DAYS` days ending today. ``timezone_name``
        defaults to :attr:`settings.BUSINESS_TIMEZONE` and affects only the
        weekday/hour buckets, never the window itself.
        """
        timezone_name = timezone_name or settings.BUSINESS_TIMEZONE
        try:
            ZoneInfo(timezone_name)
        except Exception:
            raise ValidationError(f"无效的业务时区：{timezone_name}。")

        start, end = _resolve_window(start_date, end_date)
        resolved_branch_id = resolve_branch_scope(branch_id, staff)
        start_dt, end_dt = _utc_bounds(start, end)

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
            order_types = await StatsRepo.order_type_totals(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                sold_statuses=SOLD_ORDER_STATUSES,
                cancelled_statuses=CANCELLED_ORDER_STATUSES,
            )
            weekday_hours = await StatsRepo.weekday_hour_totals(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                sold_statuses=SOLD_ORDER_STATUSES,
                timezone_name=timezone_name,
            )
            reservations = await ReservationRepo.reservation_status_totals(
                s,
                branch_id=resolved_branch_id,
                start=start_dt,
                end=end_dt,
                now=current_wall_time(),
            )
            last_week_start = start - timedelta(days=7)
            last_week_end = end - timedelta(days=7)
            last_week_totals = await StatsRepo.order_totals(
                s,
                branch_id=resolved_branch_id,
                start=datetime.combine(
                    last_week_start, time.min, tzinfo=timezone.utc
                ),
                end=datetime.combine(
                    last_week_end + timedelta(days=1),
                    time.min,
                    tzinfo=timezone.utc,
                ),
                sold_statuses=SOLD_ORDER_STATUSES,
                cancelled_statuses=CANCELLED_ORDER_STATUSES,
            )
            last_year_start = _shift_year(start)
            last_year_end = _shift_year(end)
            last_year_totals = await StatsRepo.order_totals(
                s,
                branch_id=resolved_branch_id,
                start=datetime.combine(
                    last_year_start, time.min, tzinfo=timezone.utc
                ),
                end=datetime.combine(
                    last_year_end + timedelta(days=1),
                    time.min,
                    tzinfo=timezone.utc,
                ),
                sold_statuses=SOLD_ORDER_STATUSES,
                cancelled_statuses=CANCELLED_ORDER_STATUSES,
            )

        current = _period(totals, start, end)
        total_orders = current["order_count"]

        type_rows = []
        for row in order_types:
            order_type = row["order_type"] or "unknown"
            count = row["order_count"]
            type_rows.append(
                {
                    "order_type": order_type,
                    "label": ORDER_TYPE_LABELS.get(order_type, order_type),
                    "order_count": count,
                    "revenue": round(row["revenue"], 2),
                    "cancelled_count": row["cancelled_count"],
                    "share_pct": (
                        round(count / total_orders * 100.0, 2) if total_orders else 0.0
                    ),
                }
            )

        cells = {
            (row["weekday"], row["hour"]): row for row in weekday_hours
        }
        weekday_totals = {weekday: 0 for weekday in range(1, 8)}
        weekday_revenue = {weekday: 0.0 for weekday in range(1, 8)}
        for (weekday, _hour), row in cells.items():
            weekday_totals[weekday] += row["order_count"]
            weekday_revenue[weekday] += row["revenue"]
        weekday_distribution = []
        for weekday in range(1, 8):
            count = weekday_totals[weekday]
            calendar_days = _weekday_days(start, end, weekday)
            weekday_distribution.append(
                {
                    "weekday": weekday,
                    "label": WEEKDAY_LABELS[weekday - 1],
                    "order_count": count,
                    "revenue": round(weekday_revenue[weekday], 2),
                    "share_pct": (
                        round(count / total_orders * 100.0, 2) if total_orders else 0.0
                    ),
                    "calendar_days": calendar_days,
                    "avg_orders_per_calendar_day": (
                        round(count / calendar_days, 2) if calendar_days else 0.0
                    ),
                }
            )

        hour_totals = {hour: 0 for hour in range(24)}
        hour_revenue = {hour: 0.0 for hour in range(24)}
        for (_weekday, hour), row in cells.items():
            hour_totals[hour] += row["order_count"]
            hour_revenue[hour] += row["revenue"]
        hour_distribution = [
            {
                "hour": hour,
                "order_count": hour_totals[hour],
                "revenue": round(hour_revenue[hour], 2),
                "share_pct": (
                    round(hour_totals[hour] / total_orders * 100.0, 2)
                    if total_orders
                    else 0.0
                ),
            }
            for hour in range(24)
        ]

        reservation_health = _reservation_health(reservations, start, end)

        notes = [
            f"时段与星期按 {timezone_name} 划分；统计窗口仍为 UTC 自然日 "
            f"{start} 至 {end}，窗口首尾可能各包含半个本地日。",
            "现有数据没有独立的“未到店”状态：未到店 = 预订时间已过但状态仍为待确认/已确认，"
            "属于疑似值，且不包含之后可能补录的到店记录。",
        ]
        if current["order_count"] == 0:
            notes.append("窗口内没有成交订单，订单类型、时段与星期分布没有可比较的结构。")
        if end >= _today():
            notes.append("窗口包含今天或未来日期，分布与基线反映的是尚未完成的区间。")
        if not reservation_health["booked_count"]:
            notes.append("窗口内没有预订记录，预订健康度不可判断。")

        last_week = _baseline(
            BASELINE_LABELS["last_week_same_weekday"],
            current,
            _period(last_week_totals, last_week_start, last_week_end),
        )
        year_notes = []
        original_span = (end - start).days + 1
        shifted_span = (last_year_end - last_year_start).days + 1
        if shifted_span != original_span:
            year_notes.append(
                "去年同期按日期前移一年；窗口跨闰日，长度相差一天。"
            )
        last_year = _baseline(
            BASELINE_LABELS["last_year_same_period"],
            current,
            _period(last_year_totals, last_year_start, last_year_end),
            notes=year_notes,
        )

        return {
            "branch_id": resolved_branch_id,
            "branch_name": branch.name,
            "start_date": start,
            "end_date": end,
            "timezone": timezone_name,
            "summary": current,
            "order_types": type_rows,
            "weekday_distribution": weekday_distribution,
            "hour_distribution": hour_distribution,
            "reservations": reservation_health,
            "baselines": {
                "last_week_same_weekday": last_week,
                "last_year_same_period": last_year,
            },
            "notes": notes,
        }


def _reservation_health(
    rows: List[Dict[str, Any]], start: date, end: date
) -> Dict[str, Any]:
    """Fold the status/elapsed rows into booked, cancelled, arrived and no-show."""
    by_status: Dict[str, int] = {status: 0 for status in RESERVATION_STATUSES}
    party_size_by_status: Dict[str, int] = {
        status: 0 for status in RESERVATION_STATUSES
    }
    passed_by_status: Dict[str, int] = {
        status: 0 for status in RESERVATION_STATUSES
    }
    no_show_party_size = 0

    for row in rows:
        status = row["status"] or "unknown"
        by_status[status] = by_status.get(status, 0) + row["reservation_count"]
        party_size_by_status[status] = (
            party_size_by_status.get(status, 0) + row["party_size"]
        )
        if row["start_passed"]:
            passed_by_status[status] = (
                passed_by_status.get(status, 0) + row["reservation_count"]
            )
            if status in NO_SHOW_RESERVATION_STATUSES:
                no_show_party_size += row["party_size"]

    booked = sum(by_status.values())
    cancelled = by_status.get("cancelled", 0)
    no_show = sum(passed_by_status.get(status, 0) for status in NO_SHOW_RESERVATION_STATUSES)
    arrived = sum(by_status.get(status, 0) for status in ARRIVED_RESERVATION_STATUSES)
    # Cancelled bookings were not expected to arrive, so they are not part of
    # the no-show denominator; every other booking whose slot has passed is.
    due = sum(
        passed_by_status.get(status, 0)
        for status in RESERVATION_STATUSES
        if status != "cancelled"
    )

    return {
        "booked_count": booked,
        "by_status": {
            status: {
                "label": RESERVATION_STATUS_LABELS.get(status, status),
                "count": count,
                "party_size": party_size_by_status.get(status, 0),
            }
            for status, count in by_status.items()
        },
        "cancelled_count": cancelled,
        "cancellation_rate_pct": (
            round(cancelled / booked * 100.0, 2) if booked else 0.0
        ),
        "arrived_count": arrived,
        "pending_count": by_status.get("pending", 0),
        "confirmed_count": by_status.get("confirmed", 0),
        "due_count": due,
        "no_show_count": no_show,
        "no_show_party_size": no_show_party_size,
        "no_show_rate_pct": round(no_show / due * 100.0, 2) if due else 0.0,
        "no_show_basis": "suspected",
        "window": {"start_date": start, "end_date": end},
    }


__all__ = [
    "ARRIVED_RESERVATION_STATUSES",
    "BASELINE_LABELS",
    "BusinessProfileService",
    "NO_SHOW_RESERVATION_STATUSES",
    "RESERVATION_STATUSES",
    "WEEKDAY_LABELS",
]
