"""Task19 tests: duplicate requests and failure handling (spec §9).

Spec §9「经营统计与异常处理」requires the system to handle repeated requests,
insufficient stock, booking conflicts and failed operations correctly. The Agent
is the only place a request can arrive twice by accident -- a chat message sent
twice, a retried request, or a small model emitting the same tool call twice in
one turn -- so the tools submit with the duplicate guard on and these tests drive
them at that level.

What "correct" means here:

* a repeat submission of a still-open request returns the order/booking that
  already exists: one row, one stock decrement, one audit entry, and identical
  replies to both submissions;
* a *different* request in the same window, a repeat after the first one was
  cancelled, and a repeat outside the dedupe window all still create a new one;
* a request that fails (stock, table conflict, illegal transition) leaves no
  partial state behind -- no row, no stock movement, no audit entry.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update

import pytest

from backend.app.agent.tools.order_tools import create_order_atomic
from backend.app.agent.tools.table_tools import check_table_availability_and_book
from backend.app.database import transactional_session
from backend.app.exceptions import ConflictError
from backend.app.models.branch import Branch, Table
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.menu import MenuRepo
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService
from backend.app.service.order import OrderService


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
async def _make_branch(*specs, name=None):
    """Create an isolated branch; specs = [(label, area, capacity)]."""
    async with transactional_session() as s:
        branch = Branch(
            name=name or f"__dup{uuid4().hex[:8]}",
            address="x",
            phone="x",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        for label, area, capacity in specs:
            s.add(
                Table(
                    branch_id=branch.id,
                    label=label,
                    seating_area=area,
                    capacity=capacity,
                    is_available=True,
                )
            )
        await s.flush()
        return branch.id


async def _make_customer():
    async with transactional_session() as s:
        customer = Customer(
            name="__dup", email=f"{uuid4().hex}@x.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int, *, stock: int = 10, price: float = 2.0):
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__dup{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name=f"__dup_item{uuid4().hex[:6]}",
            base_price=price,
            is_available=True,
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


async def _stock_of(item_id: int, branch_id: int) -> int:
    async with transactional_session() as s:
        res = await s.execute(
            select(MenuItemStock.stock_quantity).where(
                MenuItemStock.menu_item_id == item_id,
                MenuItemStock.branch_id == branch_id,
            )
        )
        return res.scalar_one()


async def _order_count(customer_id: int) -> int:
    async with transactional_session() as s:
        res = await s.execute(
            select(func.count())
            .select_from(Order)
            .where(Order.customer_id == customer_id)
        )
        return int(res.scalar_one())


async def _reservation_count(branch_id: int) -> int:
    async with transactional_session() as s:
        res = await s.execute(
            select(func.count())
            .select_from(Reservation)
            .where(Reservation.branch_id == branch_id)
        )
        return int(res.scalar_one())


async def _audit_count(action: str, entity_id: int = None, branch_id: int = None) -> int:
    """Count trail rows through the Service, as an unrestricted admin would."""
    async with transactional_session() as s:
        admin = StaffUser(
            username=f"__dup{uuid4().hex[:8]}",
            password_hash="x",
            role="admin",
            branch_id=None,
        )
        s.add(admin)
        await s.flush()
        admin = await UserRepo.get_staff_by_id(s, admin.id)
        rows = await AuditService.list_logs(
            admin, action=action, entity_id=entity_id, branch_id=branch_id, limit=200
        )
        return len(rows)


def _slot(days=400, hour=19):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


async def _place(branch_id, customer_id, item_id, quantity, **kwargs):
    return await create_order_atomic(
        customer_id=customer_id,
        branch_id=branch_id,
        order_type=kwargs.pop("order_type", "dine_in"),
        items=[{"menu_item_id": item_id, "quantity": quantity}],
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Duplicate order submissions
# --------------------------------------------------------------------------- #
async def test_repeat_order_submission_returns_the_first_order():
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)

    first = await _place(branch_id, customer_id, item_id, 2)
    second = await _place(branch_id, customer_id, item_id, 2)

    assert first["success"] is True and second["success"] is True
    assert second["order_id"] == first["order_id"]
    assert second["total_amount"] == first["total_amount"]
    # One order, one stock decrement, one audit entry -- the second submission
    # changed nothing at all.
    assert await _order_count(customer_id) == 1
    assert await _stock_of(item_id, branch_id) == 8
    assert await _audit_count("order.created") >= 1
    assert await _audit_count("order.created", entity_id=first["order_id"]) == 1


async def test_different_order_in_the_same_window_is_a_new_order():
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)

    first = await _place(branch_id, customer_id, item_id, 1)
    second = await _place(branch_id, customer_id, item_id, 2)

    assert second["order_id"] != first["order_id"]
    assert await _order_count(customer_id) == 2
    assert await _stock_of(item_id, branch_id) == 7


async def test_repeat_after_the_first_order_is_cancelled_is_a_new_order():
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)

    first = await _place(branch_id, customer_id, item_id, 2)
    await OrderService.update_status(first["order_id"], "cancelled")

    second = await _place(branch_id, customer_id, item_id, 2)

    assert second["success"] is True
    assert second["order_id"] != first["order_id"]
    assert await _order_count(customer_id) == 2


async def test_repeat_outside_the_dedupe_window_is_a_new_order():
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)

    first = await _place(branch_id, customer_id, item_id, 2)
    async with transactional_session() as s:
        await s.execute(
            update(Order)
            .where(Order.id == first["order_id"])
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )

    second = await _place(branch_id, customer_id, item_id, 2)

    assert second["order_id"] != first["order_id"]
    assert await _order_count(customer_id) == 2
    assert await _stock_of(item_id, branch_id) == 6


async def test_concurrent_identical_submissions_create_one_order():
    """The check and the insert must not interleave (advisory lock)."""
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)

    results = await asyncio.gather(
        _place(branch_id, customer_id, item_id, 2),
        _place(branch_id, customer_id, item_id, 2),
    )

    assert [r["success"] for r in results] == [True, True]
    assert results[0]["order_id"] == results[1]["order_id"]
    assert await _order_count(customer_id) == 1
    assert await _stock_of(item_id, branch_id) == 8


# --------------------------------------------------------------------------- #
# Duplicate booking submissions
# --------------------------------------------------------------------------- #
async def test_repeat_reservation_submission_returns_the_first_booking():
    # Two free tables: without the guard the repeat would hold the second one.
    branch_id = await _make_branch(
        ("T1", "indoor", 4), ("T2", "indoor", 4), name=f"__dup{uuid4().hex[:8]}"
    )
    customer_id = await _make_customer()
    start = _slot()

    first = await check_table_availability_and_book(
        customer_id=customer_id,
        branch_id=branch_id,
        start_time=start,
        party_size=2,
    )
    second = await check_table_availability_and_book(
        customer_id=customer_id,
        branch_id=branch_id,
        start_time=start,
        party_size=2,
    )

    assert first["success"] is True and second["success"] is True
    assert second["reservation_id"] == first["reservation_id"]
    assert second["table_label"] == first["table_label"]
    assert await _reservation_count(branch_id) == 1
    assert await _audit_count("reservation.created", entity_id=first["reservation_id"]) == 1


async def test_a_different_party_size_is_a_new_booking():
    branch_id = await _make_branch(
        ("T1", "indoor", 6), ("T2", "indoor", 6), name=f"__dup{uuid4().hex[:8]}"
    )
    customer_id = await _make_customer()
    start = _slot(hour=18, days=401)

    first = await check_table_availability_and_book(
        customer_id=customer_id, branch_id=branch_id, start_time=start, party_size=2
    )
    second = await check_table_availability_and_book(
        customer_id=customer_id, branch_id=branch_id, start_time=start, party_size=4
    )

    assert second["success"] is True
    assert second["reservation_id"] != first["reservation_id"]
    assert await _reservation_count(branch_id) == 2


async def test_repeat_reservation_after_cancellation_books_again():
    branch_id = await _make_branch(("T1", "indoor", 4), name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    start = _slot(hour=20, days=402)

    first = await check_table_availability_and_book(
        customer_id=customer_id, branch_id=branch_id, start_time=start, party_size=2
    )
    assert first["success"] is True
    async with transactional_session() as s:
        reservation = await s.get(Reservation, first["reservation_id"])
        reservation.status = "cancelled"

    second = await check_table_availability_and_book(
        customer_id=customer_id, branch_id=branch_id, start_time=start, party_size=2
    )

    assert second["success"] is True
    assert second["reservation_id"] != first["reservation_id"]
    assert await _reservation_count(branch_id) == 2


# --------------------------------------------------------------------------- #
# Failures leave nothing behind
# --------------------------------------------------------------------------- #
async def test_insufficient_stock_leaves_no_partial_state():
    """A multi-line order whose second line is short writes nothing at all."""
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    ok_item = await _make_item(branch_id, stock=10)
    short_item = await _make_item(branch_id, stock=1)

    result = await create_order_atomic(
        customer_id=customer_id,
        branch_id=branch_id,
        order_type="dine_in",
        items=[
            {"menu_item_id": ok_item, "quantity": 2},
            {"menu_item_id": short_item, "quantity": 3},
        ],
    )

    assert result["success"] is False
    assert "库存不足" in result["error"]
    # The first line's stock is untouched and no order exists.
    assert await _stock_of(ok_item, branch_id) == 10
    assert await _stock_of(short_item, branch_id) == 1
    assert await _order_count(customer_id) == 0
    assert await _audit_count("order.created", branch_id=branch_id) == 0


async def test_failed_stock_decrement_rolls_back_the_whole_order(monkeypatch):
    """A write that fails halfway must leave no trace of the first line.

    ``try_decrement_stock`` can still lose between the availability check and the
    decrement (another order takes the unit in that window); when it does, the
    stock already taken for the earlier lines has to go back.
    """
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    first_item = await _make_item(branch_id, stock=10)
    second_item = await _make_item(branch_id, stock=10)

    real_decrement = MenuRepo.try_decrement_stock

    async def _decrement(session, item_id, branch_id_, quantity):
        if item_id == second_item:
            return False  # lost the row to a concurrent order
        return await real_decrement(session, item_id, branch_id_, quantity)

    monkeypatch.setattr(MenuRepo, "try_decrement_stock", _decrement)

    with pytest.raises(ConflictError) as excinfo:
        await OrderService.place_order(
            customer_id,
            branch_id,
            "dine_in",
            [
                {"menu_item_id": first_item, "quantity": 2},
                {"menu_item_id": second_item, "quantity": 1},
            ],
        )

    assert "库存已发生变化" in excinfo.value.message
    assert await _stock_of(first_item, branch_id) == 10
    assert await _stock_of(second_item, branch_id) == 10
    assert await _order_count(customer_id) == 0
    assert await _audit_count("order.created", branch_id=branch_id) == 0


async def test_table_conflict_leaves_no_reservation_behind():
    branch_id = await _make_branch(("T1", "indoor", 4), name=f"__dup{uuid4().hex[:8]}")
    first_customer = await _make_customer()
    second_customer = await _make_customer()
    start = _slot(hour=18, days=403)

    won = await check_table_availability_and_book(
        customer_id=first_customer, branch_id=branch_id, start_time=start, party_size=2
    )
    lost = await check_table_availability_and_book(
        customer_id=second_customer, branch_id=branch_id, start_time=start, party_size=2
    )

    assert won["success"] is True
    assert lost["success"] is False
    assert lost["alternatives_available"] is True
    # The loser holds no table and wrote no audit row.
    assert await _reservation_count(branch_id) == 1
    assert await _audit_count("reservation.created", branch_id=branch_id) == 1


async def test_repeated_status_change_is_idempotent(async_client, staff_auth_headers):
    """A retried staff request must not double-apply or double-audit."""
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)
    placed = await _place(branch_id, customer_id, item_id, 2)

    for _ in range(2):
        resp = await async_client.patch(
            f"/api/staff/orders/{placed['order_id']}/status",
            json={"status": "confirmed"},
            headers=staff_auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    assert await _audit_count("order.status_changed", entity_id=placed["order_id"]) == 1
    assert await _stock_of(item_id, branch_id) == 8


async def test_illegal_status_change_leaves_no_audit_row(async_client, staff_auth_headers):
    branch_id = await _make_branch(name=f"__dup{uuid4().hex[:8]}")
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, stock=10)
    placed = await _place(branch_id, customer_id, item_id, 2)

    resp = await async_client.patch(
        f"/api/staff/orders/{placed['order_id']}/status",
        json={"status": "delivered"},  # placed -> delivered is not a legal move
        headers=staff_auth_headers,
    )

    assert resp.status_code == 400
    assert await _audit_count("order.status_changed", entity_id=placed["order_id"]) == 0
    assert await _stock_of(item_id, branch_id) == 8
