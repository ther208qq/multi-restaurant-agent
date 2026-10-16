"""Staff-side simulated KDS (feature 1 completeness, plan A).

The kitchen's half of the simulated KDS: staff advance ``queued -> cooking ->
done`` and can send a held task back to the queue. Covers the Service transition
rules, branch scoping and the ``PATCH /staff/orders/{id}/kds`` endpoint.
"""

import pytest
from sqlalchemy import delete, select

from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, ValidationError
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.service.order import OrderService

BRANCH2_STAFF = StaffUser(username="s2", role="staff", branch_id=2)


async def _make_branch():
    from uuid import uuid4

    async with transactional_session() as s:
        branch = Branch(
            name=f"__kds{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        cat = MenuCategory(branch_id=branch.id, name="主食")
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name=f"__面{uuid4().hex[:6]}",
            base_price=10.0, is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=branch.id, stock_quantity=10))
        return branch.id, item.id


async def _make_customer():
    from uuid import uuid4

    async with transactional_session() as s:
        customer = Customer(
            name="__kds", email=f"__kds_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _place(customer_id, branch_id, item_id):
    return await OrderService.place_order(
        customer_id, branch_id, "dine_in", [{"menu_item_id": item_id, "quantity": 1}]
    )


async def _kds_of(order_id):
    async with transactional_session() as s:
        return (
            await s.execute(select(Order.kds_status).where(Order.id == order_id))
        ).scalar_one()


async def _cleanup(branch_id=None, *customer_ids):
    async with transactional_session() as s:
        if branch_id is not None:
            await s.execute(delete(Branch).where(Branch.id == branch_id))
        for cid in customer_ids:
            await s.execute(delete(Customer).where(Customer.id == cid))


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
async def test_kds_advances_queued_cooking_done():
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order = await _place(customer, branch_id, item_id)
        assert await _kds_of(order.id) == "queued"
        await OrderService.update_kds_status(order.id, "cooking")
        assert await _kds_of(order.id) == "cooking"
        await OrderService.update_kds_status(order.id, "done")
        assert await _kds_of(order.id) == "done"
        # ``done`` is terminal.
        with pytest.raises(ValidationError):
            await OrderService.update_kds_status(order.id, "cooking")
    finally:
        await _cleanup(branch_id, customer)


async def test_kds_rejects_illegal_queued_to_done():
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order = await _place(customer, branch_id, item_id)
        with pytest.raises(ValidationError):
            await OrderService.update_kds_status(order.id, "done")
    finally:
        await _cleanup(branch_id, customer)


async def test_kds_staff_resumes_held():
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order = await _place(customer, branch_id, item_id)
        await OrderService.hold_kds_task(order.id, customer, reason="等人")
        assert await _kds_of(order.id) == "held"
        await OrderService.update_kds_status(order.id, "queued")
        assert await _kds_of(order.id) == "queued"
    finally:
        await _cleanup(branch_id, customer)


async def test_kds_branch_scoping_forbidden():
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order = await _place(customer, branch_id, item_id)
        with pytest.raises(ForbiddenError):
            await OrderService.update_kds_status(
                order.id, "cooking", staff=BRANCH2_STAFF
            )
    finally:
        await _cleanup(branch_id, customer)


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #
async def test_staff_kds_endpoint(async_client, staff_auth_headers):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order = await _place(customer, branch_id, item_id)
        resp = await async_client.patch(
            f"/api/staff/orders/{order.id}/kds",
            headers=staff_auth_headers,
            json={"status": "cooking"},
        )
        assert resp.status_code == 200
        assert resp.json()["kds_status"] == "cooking"
    finally:
        await _cleanup(branch_id, customer)
