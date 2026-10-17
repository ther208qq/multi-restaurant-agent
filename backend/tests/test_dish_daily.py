"""Task23 tests: per-dish daily series and direction-aware shape.

The value of this capability is not "a list of seven numbers". It is that three
states which look identical in a period total stay distinct:

* the branch traded and the dish sold nothing -- a genuine zero;
* the branch has no order record that day -- unknown, not zero;
* whether the dish was sellable that day is unknowable from current stock rows.

The shape verdict must also use exactly the same arithmetic as the store-level
findings, so the two can be read side by side.
"""

from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, ValidationError
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.user import UserRepo
from backend.app.service.diagnostics import (
    MAX_DISH_DAILY_ITEMS,
    TREND_GRADUAL,
    TREND_SINGLE_DAY,
    TREND_SINGLE_DAY_UP,
    DishDailyService,
)
from backend.app.service.order import OrderService


_TODAY = datetime.now(timezone.utc).date()
_END = _TODAY - timedelta(days=1)
_START = _END - timedelta(days=3)  # four-day current window
_BASE_END = _START - timedelta(days=1)
_BASE_START = _BASE_END - timedelta(days=3)  # four-day baseline


# --------------------------------------------------------------------------- #
# Scenario builders
# --------------------------------------------------------------------------- #
async def _make_branch() -> int:
    async with transactional_session() as s:
        branch = Branch(
            name=f"__dd{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        return branch.id


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__dd", email=f"__dd_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int, *, name: str, price: float = 10.0) -> int:
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__dd{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id, name=name, base_price=price, is_available=True
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


async def _make_staff(branch_id=None) -> int:
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__dd{uuid4().hex[:8]}",
            password_hash="x",
            role="staff",
            branch_id=branch_id,
        )
        s.add(staff)
        await s.flush()
        return staff.id


async def _load_staff(staff_id: int):
    async with transactional_session() as s:
        return await UserRepo.get_staff_by_id(s, staff_id)


async def _place(
    branch_id: int,
    customer_id: int,
    item_id: int,
    quantity: int = 1,
    *,
    day,
) -> Order:
    order = await OrderService.place_order(
        customer_id,
        branch_id,
        "dine_in",
        [{"menu_item_id": item_id, "quantity": quantity}],
    )
    moment = datetime.combine(day, time(12, 0), tzinfo=timezone.utc)
    async with transactional_session() as s:
        await s.execute(
            update(Order).where(Order.id == order.id).values(created_at=moment)
        )
    return order


async def _seed_window(
    branch_id, customer_id, item_id, start, end, *, per_day, quantity=1
):
    day = start
    while day <= end:
        for _ in range(per_day):
            await _place(
                branch_id, customer_id, item_id, quantity, day=day
            )
        day += timedelta(days=1)


def _days(start, end):
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# --------------------------------------------------------------------------- #
# Series and shape
# --------------------------------------------------------------------------- #
async def test_series_distinguishes_a_business_day_from_no_record():
    """3 -> 3 -> (closed) -> 3: the closed day is null, not a zero."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_target")
    anchor = await _make_item(branch_id, name="__dd_anchor")

    await _seed_window(
        branch_id, customer_id, target, _BASE_START, _BASE_END, per_day=3
    )
    days = _days(_START, _END)
    await _place(branch_id, customer_id, target, 3, day=days[0])
    # Business day without a target sale: a genuine zero.
    await _place(branch_id, customer_id, anchor, 1, day=days[2])
    # days[1] and days[3] have no orders at all.

    result = await DishDailyService.get_dish_daily(
        branch_id, [target], _START, _END
    )
    dish = result["dishes"][0]

    assert dish["status"] == "ok"
    assert dish["direction"] == "down"
    assert dish["change_pct"] == pytest.approx(-75.0, abs=0.01)
    assert [(point["date"], point["quantity"], point["day_type"]) for point in dish["daily"]] == [
        (days[0], 3, "business"),
        (days[1], None, "no_record"),
        (days[2], 0, "business"),
        (days[3], None, "no_record"),
    ]
    assert dish["availability"] == "unknown"
    assert dish["shape"]["value"] == TREND_GRADUAL
    assert any("2 天没有任何订单记录" in note for note in result["notes"])
    assert any("availability" in note for note in result["notes"])


async def test_single_bad_day_is_classified_as_a_single_day_fall():
    """12 -> 9 with the whole shortfall on day one: 单日塌方, not 整期缓降."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_single_day")
    anchor = await _make_item(branch_id, name="__dd_anchor2")

    await _seed_window(
        branch_id, customer_id, target, _BASE_START, _BASE_END, per_day=3
    )
    days = _days(_START, _END)
    # A single order keeps day one a business day while the dish sells nothing.
    await _place(branch_id, customer_id, anchor, 1, day=days[0])
    for day in days[1:]:
        await _place(branch_id, customer_id, target, 3, day=day)

    result = await DishDailyService.get_dish_daily(
        branch_id, [target], _START, _END
    )
    dish = result["dishes"][0]

    assert dish["change_pct"] == pytest.approx(-25.0, abs=0.01)
    assert dish["shape"]["value"] == TREND_SINGLE_DAY
    assert [point["quantity"] for point in dish["daily"]] == [0, 3, 3, 3]
    assert dish["current_period"]["active_days"] == 4


async def test_a_rise_uses_the_up_facing_shape_label():
    """12 -> 13 with the gain on the last day: 单日冲高."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_rise")

    await _seed_window(
        branch_id, customer_id, target, _BASE_START, _BASE_END, per_day=3
    )
    days = _days(_START, _END)
    for day in days[:-1]:
        await _place(branch_id, customer_id, target, 3, day=day)
    await _place(branch_id, customer_id, target, 4, day=days[-1])

    result = await DishDailyService.get_dish_daily(
        branch_id, [target], _START, _END
    )
    dish = result["dishes"][0]

    assert dish["direction"] == "up"
    assert dish["change_pct"] == pytest.approx(8.33, abs=0.01)
    assert dish["shape"]["value"] == TREND_SINGLE_DAY_UP


# --------------------------------------------------------------------------- #
# Statuses
# --------------------------------------------------------------------------- #
async def test_no_current_orders_is_no_data():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_nodata")

    await _seed_window(
        branch_id, customer_id, target, _BASE_START, _BASE_END, per_day=3
    )

    result = await DishDailyService.get_dish_daily(
        branch_id, [target], _START, _END
    )
    dish = result["dishes"][0]

    assert dish["status"] == "no_data"
    assert dish["change_pct"] is None
    assert dish["shape"]["value"] is None


async def test_a_thin_baseline_is_insufficient_not_a_percentage():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_thin")

    # Only two active baseline days, below MIN_BASELINE_DAYS.
    await _seed_window(
        branch_id, customer_id, target, _BASE_START,
        _BASE_START + timedelta(days=1), per_day=3,
    )
    await _seed_window(branch_id, customer_id, target, _START, _END, per_day=1)

    result = await DishDailyService.get_dish_daily(
        branch_id, [target], _START, _END
    )
    dish = result["dishes"][0]

    assert dish["status"] == "insufficient_baseline"
    assert dish["change_pct"] is None
    assert dish["shape"]["value"] is None
    assert "2 天" in dish["shape"]["basis"]


async def test_a_dish_never_sold_in_the_baseline_is_zero_baseline():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_new")
    anchor = await _make_item(branch_id, name="__dd_anchor3")

    # The branch traded in the baseline, so the window is judgeable; the dish
    # itself did not sell, so its own baseline is zero.
    await _seed_window(
        branch_id, customer_id, anchor, _BASE_START, _BASE_END, per_day=1
    )
    await _seed_window(branch_id, customer_id, target, _START, _END, per_day=2)

    result = await DishDailyService.get_dish_daily(
        branch_id, [target], _START, _END
    )
    dish = result["dishes"][0]

    assert dish["status"] == "zero_baseline"
    assert dish["change_pct"] is None
    assert dish["shape"]["value"] is None
    assert "销量为 0" in dish["shape"]["basis"]


# --------------------------------------------------------------------------- #
# Bounds and scope
# --------------------------------------------------------------------------- #
async def test_dish_count_is_capped_and_empty_requests_are_rejected():
    branch_id = await _make_branch()

    with pytest.raises(ValidationError):
        await DishDailyService.get_dish_daily(branch_id, [], _START, _END)

    with pytest.raises(ValidationError):
        await DishDailyService.get_dish_daily(
            branch_id, list(range(1, MAX_DISH_DAILY_ITEMS + 2)), _START, _END
        )


async def test_duplicate_dish_ids_collapse_to_one_series():
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    target = await _make_item(branch_id, name="__dd_dedupe")
    await _seed_window(
        branch_id, customer_id, target, _BASE_START, _BASE_END, per_day=1
    )
    await _seed_window(branch_id, customer_id, target, _START, _END, per_day=1)

    result = await DishDailyService.get_dish_daily(
        branch_id, [target, target], _START, _END
    )

    assert [dish["menu_item_id"] for dish in result["dishes"]] == [target]


async def test_a_branch_bound_staffer_cannot_read_another_branch():
    own_branch = await _make_branch()
    other_branch = await _make_branch()
    staff = await _load_staff(await _make_staff(branch_id=own_branch))

    with pytest.raises(ForbiddenError):
        await DishDailyService.get_dish_daily(
            other_branch, [1], _START, _END, staff=staff
        )
