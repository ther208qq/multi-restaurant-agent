"""Task19 tests: business statistics per branch and date range (spec §9).

Spec §9「经营统计与异常处理」asks for order volume, revenue and dish sales by
branch and date. These tests pin down what the numbers mean:

* a cancelled order is **not** a sale -- it is excluded from the order count,
  revenue and dish ranking, and reported separately;
* the range is a half-open UTC window with ``end_date`` inclusive, so an order
  just outside the range never leaks in;
* a branch never sees another branch's numbers, and a branch-bound staff member
  cannot aggregate a branch that is not theirs.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.user import UserRepo
from backend.app.service.order import OrderService
from backend.app.service.stats import StatsService


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
async def _make_branch(name=None):
    async with transactional_session() as s:
        branch = Branch(
            name=name or f"__st{uuid4().hex[:8]}",
            address="x",
            phone="x",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        return branch.id


async def _make_customer():
    async with transactional_session() as s:
        customer = Customer(
            name="__st", email=f"{uuid4().hex}@x.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int, *, name: str, price: float, stock: int = 100):
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__st{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id, name=name, base_price=price, is_available=True
        )
        s.add(item)
        await s.flush()
        s.add(
            MenuItemStock(
                menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock
            )
        )
        await s.flush()
        return item.id


async def _place(
    branch_id: int,
    customer_id: int,
    lines,
    *,
    created_at: datetime = None,
    order_type: str = "dine_in",
):
    """Place an order and (optionally) backdate it for range assertions."""
    order = await OrderService.place_order(
        customer_id,
        branch_id,
        order_type,
        [{"menu_item_id": item_id, "quantity": qty} for item_id, qty in lines],
    )
    if created_at is not None:
        async with transactional_session() as s:
            await s.execute(
                update(Order).where(Order.id == order.id).values(created_at=created_at)
            )
    return order
    return order


async def _make_staff(branch_id=None, role="staff"):
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__st{uuid4().hex[:8]}",
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


def _headers(staff_id: int, role: str = "staff") -> dict:
    token = create_access_token(
        {
            "sub": str(staff_id),
            "type": "staff",
            "username": f"__st{staff_id}",
            "role": role,
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _today():
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
async def test_summary_counts_orders_revenue_and_dish_sales():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    alpha = await _make_item(branch_id, name="__st_alpha", price=10.0)
    beta = await _make_item(branch_id, name="__st_beta", price=4.5)

    await _place(branch_id, customer_id, [(alpha, 2)])  # 20.00
    await _place(branch_id, customer_id, [(alpha, 1), (beta, 2)])  # 19.00
    cancelled = await _place(branch_id, customer_id, [(beta, 1)])  # 4.50, cancelled
    await OrderService.update_status(cancelled.id, "cancelled")

    summary = await StatsService.get_branch_summary(branch_id)

    assert summary["branch_id"] == branch_id
    assert summary["start_date"] == summary["end_date"] - timedelta(days=6)
    assert summary["end_date"] == _today()
    # The cancelled order is reported, but is not a sale.
    assert summary["order_count"] == 2
    assert summary["cancelled_count"] == 1
    assert summary["revenue"] == 39.0
    assert summary["average_order_value"] == 19.5
    # 2 + 1 + 2 units; the cancelled order's unit is not counted.
    assert summary["units_sold"] == 5

    top = summary["top_items"]
    assert [(row["name"], row["quantity"]) for row in top] == [
        ("__st_alpha", 3),
        ("__st_beta", 2),
    ]
    assert top[0]["revenue"] == 30.0
    assert top[0]["order_count"] == 2
    assert top[1]["revenue"] == 9.0
    assert top[1]["order_count"] == 1

    assert len(summary["daily"]) == 1
    day = summary["daily"][0]
    assert day["date"] == _today()
    assert day["order_count"] == 2
    assert day["revenue"] == 39.0
    assert day["cancelled_count"] == 1


async def test_restored_order_counts_as_a_sale_again():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__st_restore", price=3.0)

    order = await _place(branch_id, customer_id, [(item_id, 1)])
    await OrderService.update_status(order.id, "cancelled")
    assert (await StatsService.get_branch_summary(branch_id))["order_count"] == 0

    await OrderService.update_status(order.id, "placed")
    summary = await StatsService.get_branch_summary(branch_id)
    assert summary["order_count"] == 1
    assert summary["cancelled_count"] == 0
    assert summary["revenue"] == 3.0


async def test_summary_is_scoped_to_branch_and_date_range():
    branch_a = await _make_branch()
    branch_b = await _make_branch()
    customer_id = await _make_customer()
    item_a = await _make_item(branch_a, name="__st_a", price=5.0)
    item_b = await _make_item(branch_b, name="__st_b", price=7.0)

    today = _today()
    await _place(branch_a, customer_id, [(item_a, 2)])  # today, in range
    await _place(
        branch_a,
        customer_id,
        [(item_a, 3)],
        created_at=datetime.combine(
            today - timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc
        ),
    )  # too old
    await _place(branch_b, customer_id, [(item_b, 1)])  # another branch

    summary = await StatsService.get_branch_summary(branch_a, today, today)
    assert summary["order_count"] == 1
    assert summary["revenue"] == 10.0
    assert summary["units_sold"] == 2
    assert [row["name"] for row in summary["top_items"]] == ["__st_a"]


async def test_daily_series_splits_by_utc_day():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__st_daily", price=2.0)

    today = _today()
    day_one = today - timedelta(days=2)
    day_two = today - timedelta(days=1)
    # 23:30 on day one and 00:30 on day two are 60 minutes apart but belong to
    # different UTC days -- the boundary the series has to respect.
    await _place(
        branch_id,
        customer_id,
        [(item_id, 1)],
        created_at=datetime.combine(day_one, datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(hours=23, minutes=30),
    )
    await _place(
        branch_id,
        customer_id,
        [(item_id, 2)],
        created_at=datetime.combine(day_two, datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(minutes=30),
    )

    summary = await StatsService.get_branch_summary(branch_id, day_one, today)

    assert [point["date"] for point in summary["daily"]] == [day_one, day_two]
    assert [point["order_count"] for point in summary["daily"]] == [1, 1]
    assert [point["revenue"] for point in summary["daily"]] == [2.0, 4.0]
    assert summary["order_count"] == 2
    assert summary["revenue"] == 6.0


async def test_requested_range_is_inclusive_on_both_ends():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__st_edges", price=1.0)

    today = _today()
    boundary = today - timedelta(days=3)
    await _place(
        branch_id,
        customer_id,
        [(item_id, 1)],
        created_at=datetime.combine(
            boundary, datetime.min.time(), tzinfo=timezone.utc
        ),
    )
    await _place(
        branch_id,
        customer_id,
        [(item_id, 1)],
        created_at=datetime.combine(
            today, datetime.min.time(), tzinfo=timezone.utc
        )
        + timedelta(hours=23, minutes=59),
    )

    inside = await StatsService.get_branch_summary(branch_id, boundary, today)
    assert inside["order_count"] == 2

    # The day before the boundary and the day after the end are both outside.
    outside = await StatsService.get_branch_summary(
        branch_id, boundary + timedelta(days=1), today - timedelta(days=1)
    )
    assert outside["order_count"] == 0


async def test_top_limit_caps_the_dish_ranking():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    items = [
        await _make_item(branch_id, name=f"__st_{index}", price=1.0)
        for index in range(4)
    ]
    for item_id in items:
        await _place(branch_id, customer_id, [(item_id, 1)])

    summary = await StatsService.get_branch_summary(branch_id, top_limit=2)
    assert len(summary["top_items"]) == 2
    # The cap does not change the totals.
    assert summary["order_count"] == 4
    assert summary["units_sold"] == 4


async def test_empty_range_reports_zeroes_not_errors():
    branch_id = await _make_branch()
    summary = await StatsService.get_branch_summary(branch_id)
    assert summary["order_count"] == 0
    assert summary["cancelled_count"] == 0
    assert summary["revenue"] == 0.0
    # No division by zero on an empty range.
    assert summary["average_order_value"] == 0.0
    assert summary["units_sold"] == 0
    assert summary["top_items"] == []
    assert summary["daily"] == []


# --------------------------------------------------------------------------- #
# Validation and permissions
# --------------------------------------------------------------------------- #
async def test_invalid_range_and_limit_are_rejected():
    branch_id = await _make_branch()
    today = _today()

    with pytest.raises(ValidationError):
        await StatsService.get_branch_summary(
            branch_id, today, today - timedelta(days=1)
        )
    with pytest.raises(ValidationError):
        await StatsService.get_branch_summary(
            branch_id, today - timedelta(days=400), today
        )
    with pytest.raises(ValidationError):
        await StatsService.get_branch_summary(branch_id, top_limit=0)
    with pytest.raises(ValidationError):
        await StatsService.get_branch_summary(branch_id, "not-a-date", today)
    with pytest.raises(ValidationError):
        await StatsService.get_branch_summary(None)


async def test_unknown_branch_is_not_found():
    with pytest.raises(NotFoundError):
        await StatsService.get_branch_summary(987654321)


async def test_branch_bound_staff_only_sees_their_own_branch():
    branch_id = await _make_branch()
    other_branch = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__st_mine", price=3.0)
    await _place(branch_id, customer_id, [(item_id, 1)])

    staff = await _load_staff(await _make_staff(branch_id=branch_id))

    # Their own branch, asked explicitly or by default.
    explicit = await StatsService.get_branch_summary(branch_id, staff=staff)
    default = await StatsService.get_branch_summary(staff=staff)
    assert explicit["branch_id"] == branch_id
    assert default["branch_id"] == branch_id
    assert default["order_count"] == 1

    with pytest.raises(ForbiddenError):
        await StatsService.get_branch_summary(other_branch, staff=staff)


async def test_admin_without_a_branch_must_name_one():
    admin = await _load_staff(await _make_staff(role="admin"))
    with pytest.raises(ValidationError):
        await StatsService.get_branch_summary(staff=admin)

    branch_id = await _make_branch()
    summary = await StatsService.get_branch_summary(branch_id, staff=admin)
    assert summary["branch_id"] == branch_id


# --------------------------------------------------------------------------- #
# HTTP route
# --------------------------------------------------------------------------- #
async def test_stats_route_returns_the_summary(async_client, staff_auth_headers):
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__st_route", price=6.0)
    await _place(branch_id, customer_id, [(item_id, 3)])

    resp = await async_client.get(
        f"/api/staff/stats?branch_id={branch_id}", headers=staff_auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch_id"] == branch_id
    assert body["order_count"] == 1
    assert body["revenue"] == 18.0
    assert body["units_sold"] == 3
    assert body["top_items"][0]["name"] == "__st_route"
    assert body["start_date"] <= body["end_date"]


async def test_stats_route_accepts_an_explicit_range(async_client, staff_auth_headers):
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__st_route_old", price=6.0)
    old_day = _today() - timedelta(days=30)
    await _place(
        branch_id,
        customer_id,
        [(item_id, 1)],
        created_at=datetime.combine(old_day, datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(hours=12),
    )

    resp = await async_client.get(
        f"/api/staff/stats?branch_id={branch_id}"
        f"&start_date={old_day.isoformat()}&end_date={old_day.isoformat()}",
        headers=staff_auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["order_count"] == 1

    # The default window does not reach back that far.
    default_resp = await async_client.get(
        f"/api/staff/stats?branch_id={branch_id}", headers=staff_auth_headers
    )
    assert default_resp.json()["order_count"] == 0


async def test_stats_route_requires_a_token(async_client):
    resp = await async_client.get("/api/staff/stats?branch_id=1")
    assert resp.status_code == 401


async def test_stats_route_rejects_a_foreign_branch(async_client):
    branch_id = await _make_branch()
    other_branch = await _make_branch()
    staff_id = await _make_staff(branch_id=branch_id)

    resp = await async_client.get(
        f"/api/staff/stats?branch_id={other_branch}",
        headers=_headers(staff_id),
    )
    assert resp.status_code == 403
    assert "权限" in resp.json()["detail"]

    ok = await async_client.get(
        f"/api/staff/stats?branch_id={branch_id}", headers=_headers(staff_id)
    )
    assert ok.status_code == 200


async def test_stats_route_rejects_a_malformed_date(async_client, staff_auth_headers):
    resp = await async_client.get(
        "/api/staff/stats?branch_id=1&start_date=yesterday",
        headers=staff_auth_headers,
    )
    assert resp.status_code == 422
