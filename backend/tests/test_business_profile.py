"""Task61: business profile dimensions and their honest boundaries.

The profile adds four views on data the branch already records:

* order-type structure (dine-in / takeaway / delivery);
* weekday and time-of-day distribution, rendered in the business timezone;
* reservation health, including the *suspected* no-show proxy;
* "last week same weekday" and "last year same period" baselines.

These tests pin down the definitions the Agent is allowed to talk about:
cancelled orders are never sales, a future booking cannot be a no-show, a
zero-order baseline cannot yield a percentage, and a branch-bound staff member
can only read their own branch.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.reservation import ReservationRepo
from backend.app.repo.user import UserRepo
from backend.app.service.business_profile import BusinessProfileService
from backend.app.service.order import OrderService
from backend.app.service.reservation import current_wall_time
from sqlalchemy import update


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
async def _make_branch(name=None) -> int:
    async with transactional_session() as s:
        branch = Branch(
            name=name or f"__bp{uuid4().hex[:8]}",
            address="x",
            phone="x",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        return branch.id


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__bp", email=f"{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int, *, price: float = 10.0) -> int:
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__bp{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name=f"__bp_item_{uuid4().hex[:6]}",
            base_price=price,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(
            MenuItemStock(
                menu_item_id=item.id, branch_id=branch_id, stock_quantity=10000
            )
        )
        await s.flush()
        return item.id


async def _place(
    branch_id: int,
    customer_id: int,
    item_id: int,
    *,
    created_at: datetime,
    order_type: str = "dine_in",
    quantity: int = 1,
) -> int:
    order = await OrderService.place_order(
        customer_id,
        branch_id,
        order_type,
        [{"menu_item_id": item_id, "quantity": quantity}],
        delivery_address="x" if order_type == "delivery" else None,
    )
    async with transactional_session() as s:
        await s.execute(
            update(Order).where(Order.id == order.id).values(created_at=created_at)
        )
    return order.id


async def _cancel(order_id: int, customer_id: int) -> None:
    await OrderService.cancel_order(order_id, customer_id)


async def _reserve(
    branch_id: int,
    customer_id: int,
    *,
    start_time: datetime,
    status: str,
    party_size: int = 2,
) -> int:
    """Insert a booking directly: the service rules reject past slots."""
    async with transactional_session() as s:
        reservation = await ReservationRepo.create_reservation(
            s,
            customer_id=customer_id,
            branch_id=branch_id,
            table_id=None,
            party_size=party_size,
            start_time=start_time,
            end_time=start_time + timedelta(minutes=90),
            duration_minutes=90,
            seating_area_preference="indoor",
            status=status,
            notes=None,
        )
        return reservation.id


async def _make_staff(branch_id=None, role="staff") -> int:
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__bp{uuid4().hex[:8]}",
            password_hash="x",
            role=role,
            branch_id=branch_id,
        )
        s.add(staff)
        await s.flush()
        return staff.id


async def _load_staff(staff_id: int):
    async with transactional_session() as s:
        return await UserRepo.get_staff_by_id(s, staff_id)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        hours=hour, minutes=minute
    )


# 2026-06-01 is a Monday; the week runs to Sunday 2026-06-07.
_WEEK_START = date(2026, 6, 1)
_WEEK_END = date(2026, 6, 7)


# --------------------------------------------------------------------------- #
# Order structure
# --------------------------------------------------------------------------- #
async def test_order_type_weekday_and_hour_structure():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, price=10.0)

    # 04:00 UTC is 12:00 in Asia/Shanghai -- the lunch hour the report should show.
    monday = _WEEK_START
    tuesday = _WEEK_START + timedelta(days=1)
    wednesday = _WEEK_START + timedelta(days=2)
    await _place(
        branch_id, customer_id, item_id,
        created_at=_at(monday, 4, 0), order_type="dine_in",
    )
    await _place(
        branch_id, customer_id, item_id,
        created_at=_at(monday, 4, 30), order_type="dine_in",
    )
    await _place(
        branch_id, customer_id, item_id,
        created_at=_at(tuesday, 5, 0), order_type="takeaway",
    )
    await _place(
        branch_id, customer_id, item_id,
        created_at=_at(wednesday, 10, 0), order_type="delivery",
    )
    cancelled_id = await _place(
        branch_id, customer_id, item_id,
        created_at=_at(monday, 11, 0), order_type="dine_in",
    )
    await _cancel(cancelled_id, customer_id)

    profile = await BusinessProfileService.get_business_profile(
        branch_id, _WEEK_START, _WEEK_END
    )

    assert profile["branch_id"] == branch_id
    assert profile["timezone"] == "Asia/Shanghai"
    assert profile["summary"]["order_count"] == 4
    assert profile["summary"]["cancelled_count"] == 1
    assert profile["summary"]["revenue"] == 40.0
    assert profile["summary"]["average_order_value"] == 10.0

    types = {row["order_type"]: row for row in profile["order_types"]}
    assert types["dine_in"]["order_count"] == 2
    assert types["dine_in"]["cancelled_count"] == 1
    assert types["dine_in"]["label"] == "堂食"
    assert types["takeaway"]["order_count"] == 1
    assert types["delivery"]["order_count"] == 1
    assert types["dine_in"]["share_pct"] == 50.0
    assert types["takeaway"]["share_pct"] == 25.0
    assert sum(row["share_pct"] for row in profile["order_types"]) == 100.0

    weekdays = {row["weekday"]: row for row in profile["weekday_distribution"]}
    assert len(weekdays) == 7
    assert weekdays[1]["label"] == "周一"
    assert weekdays[1]["order_count"] == 2
    assert weekdays[2]["order_count"] == 1
    assert weekdays[3]["order_count"] == 1
    assert weekdays[1]["calendar_days"] == 1
    assert weekdays[1]["avg_orders_per_calendar_day"] == 2.0
    assert weekdays[1]["share_pct"] == 50.0

    hours = {row["hour"]: row for row in profile["hour_distribution"]}
    assert len(hours) == 24
    assert hours[12]["order_count"] == 2  # 04:00 UTC -> 12:00 Shanghai
    assert hours[12]["share_pct"] == 50.0
    assert hours[13]["order_count"] == 1  # 05:00 UTC -> 13:00
    assert hours[18]["order_count"] == 1  # 10:00 UTC -> 18:00
    assert hours[4]["order_count"] == 0
    assert sum(row["share_pct"] for row in profile["hour_distribution"]) == 100.0
    assert any("Asia/Shanghai" in note for note in profile["notes"])


async def test_cancelled_only_window_has_empty_structure_not_fake_shares():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id)
    order_id = await _place(
        branch_id, customer_id, item_id, created_at=_at(_WEEK_START, 4)
    )
    await _cancel(order_id, customer_id)

    profile = await BusinessProfileService.get_business_profile(
        branch_id, _WEEK_START, _WEEK_END
    )

    assert profile["summary"]["order_count"] == 0
    assert profile["summary"]["cancelled_count"] == 1
    assert profile["order_types"][0]["order_count"] == 0
    assert profile["order_types"][0]["cancelled_count"] == 1
    assert profile["order_types"][0]["share_pct"] == 0.0
    assert all(row["share_pct"] == 0.0 for row in profile["weekday_distribution"])
    assert any("没有成交订单" in note for note in profile["notes"])


# --------------------------------------------------------------------------- #
# Reservation health
# --------------------------------------------------------------------------- #
async def test_reservation_health_separates_cancelled_arrived_and_suspected_no_show():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    now = current_wall_time()

    await _reserve(
        branch_id, customer_id,
        start_time=now - timedelta(days=2), status="pending", party_size=2,
    )
    await _reserve(
        branch_id, customer_id,
        start_time=now - timedelta(days=1), status="confirmed", party_size=4,
    )
    await _reserve(
        branch_id, customer_id,
        start_time=now - timedelta(days=1, hours=2), status="seated", party_size=2,
    )
    await _reserve(
        branch_id, customer_id,
        start_time=now - timedelta(days=2, hours=1), status="completed", party_size=3,
    )
    await _reserve(
        branch_id, customer_id,
        start_time=now - timedelta(days=1, hours=3), status="cancelled", party_size=2,
    )
    # A future booking is not due yet, so it must not count as a no-show.
    await _reserve(
        branch_id, customer_id,
        start_time=now + timedelta(days=1), status="confirmed", party_size=2,
    )

    today = now.date()
    profile = await BusinessProfileService.get_business_profile(
        branch_id, today - timedelta(days=3), today + timedelta(days=2)
    )
    health = profile["reservations"]

    assert health["booked_count"] == 6
    assert health["cancelled_count"] == 1
    assert health["cancellation_rate_pct"] == pytest.approx(16.67, abs=0.01)
    assert health["arrived_count"] == 2  # seated + completed
    assert health["pending_count"] == 1
    assert health["confirmed_count"] == 2
    assert health["due_count"] == 4  # passed slots, excluding the cancelled one
    assert health["no_show_count"] == 2
    assert health["no_show_party_size"] == 6
    assert health["no_show_rate_pct"] == 50.0
    assert health["no_show_basis"] == "suspected"
    assert health["by_status"]["seated"]["count"] == 1
    assert health["by_status"]["completed"]["party_size"] == 3
    assert any("疑似值" in note for note in profile["notes"])


async def test_future_only_window_has_no_no_shows():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    now = current_wall_time()
    await _reserve(
        branch_id, customer_id,
        start_time=now + timedelta(days=2), status="confirmed",
    )

    today = now.date()
    profile = await BusinessProfileService.get_business_profile(
        branch_id, today + timedelta(days=1), today + timedelta(days=3)
    )

    assert profile["reservations"]["booked_count"] == 1
    assert profile["reservations"]["due_count"] == 0
    assert profile["reservations"]["no_show_count"] == 0
    assert profile["reservations"]["no_show_rate_pct"] == 0.0
    assert any("没有独立的“未到店”状态" in note for note in profile["notes"])


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #
async def test_same_weekday_and_same_year_baselines_use_exact_calendar_shifts():
    branch_id = await _make_branch()
    other_branch = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id)
    other_item = await _make_item(other_branch)

    start = date(2026, 6, 2)  # Tuesday
    end = date(2026, 6, 4)  # Thursday
    last_week_start = date(2026, 5, 26)
    last_year_start = date(2025, 6, 2)

    for offset in range(3):
        day = start + timedelta(days=offset)
        for _ in range(2):
            await _place(
                branch_id, customer_id, item_id, created_at=_at(day, 4)
            )
    for offset in range(3):
        day = last_week_start + timedelta(days=offset)
        for _ in range(3):
            await _place(
                branch_id, customer_id, item_id, created_at=_at(day, 4)
            )
    # Another branch at the same dates must not leak into the baselines.
    for offset in range(3):
        await _place(
            other_branch,
            customer_id,
            other_item,
            created_at=_at(last_week_start + timedelta(days=offset), 4),
        )

    profile = await BusinessProfileService.get_business_profile(
        branch_id, start, end
    )
    week = profile["baselines"]["last_week_same_weekday"]
    year = profile["baselines"]["last_year_same_period"]

    assert week["start_date"] == last_week_start
    assert week["end_date"] == last_week_start + timedelta(days=2)
    assert week["order_count"] == 9
    assert week["revenue"] == 90.0
    assert week["status"] == "ok"
    assert week["order_count_change_pct"] == pytest.approx(-33.33, abs=0.01)
    assert week["revenue_change_pct"] == pytest.approx(-33.33, abs=0.01)

    assert year["start_date"] == last_year_start
    assert year["end_date"] == last_year_start + timedelta(days=2)
    assert year["order_count"] == 0
    assert year["status"] == "zero_baseline"
    assert year["order_count_change_pct"] is None
    assert any("没有成交订单" in note for note in year["notes"])


async def test_leap_day_last_year_baseline_falls_back_to_feb_28():
    branch_id = await _make_branch()

    profile = await BusinessProfileService.get_business_profile(
        branch_id, date(2024, 2, 29), date(2024, 2, 29)
    )
    year = profile["baselines"]["last_year_same_period"]

    assert year["start_date"] == date(2023, 2, 28)
    assert year["end_date"] == date(2023, 2, 28)
    assert year["status"] == "zero_baseline"


# --------------------------------------------------------------------------- #
# Validation and permissions
# --------------------------------------------------------------------------- #
async def test_profile_is_branch_scoped():
    branch_id = await _make_branch()
    other_branch = await _make_branch()
    staff = await _load_staff(await _make_staff(branch_id=branch_id))

    own = await BusinessProfileService.get_business_profile(
        staff=staff, start_date=_WEEK_START, end_date=_WEEK_END
    )
    assert own["branch_id"] == branch_id

    with pytest.raises(ForbiddenError):
        await BusinessProfileService.get_business_profile(
            other_branch, staff=staff, start_date=_WEEK_START, end_date=_WEEK_END
        )

    admin = await _load_staff(await _make_staff(role="admin"))
    with pytest.raises(ValidationError):
        await BusinessProfileService.get_business_profile(staff=admin)
    with pytest.raises(NotFoundError):
        await BusinessProfileService.get_business_profile(987654321)


async def test_invalid_range_and_timezone_are_rejected():
    branch_id = await _make_branch()

    with pytest.raises(ValidationError):
        await BusinessProfileService.get_business_profile(
            branch_id, date(2026, 6, 2), date(2026, 6, 1)
        )
    with pytest.raises(ValidationError):
        await BusinessProfileService.get_business_profile(
            branch_id, date(2025, 1, 1), date(2026, 6, 1)
        )
    with pytest.raises(ValidationError):
        await BusinessProfileService.get_business_profile(
            branch_id, "not-a-date"
        )
    with pytest.raises(ValidationError):
        await BusinessProfileService.get_business_profile(
            branch_id, timezone_name="Mars/Olympus"
        )
