"""Task D (AgentFeatures §5): the HTTP surface of the approval flow.

The customer's order endpoint gates a freebie request behind approval (202 +
``approval_id``), a plain or delivery note is placed normally (200), the customer
can poll one order by id, and staff list / approve / reject the queue -- with the
usual 401 / 403 / 404 surface. LLM intent recognition is forced offline so the
deterministic rules decide.
"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.branch import Branch
from backend.app.models.gift import Gift
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.menu import MenuRepo


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    """No LLM endpoint in tests -- the rules carry intent recognition."""

    async def _boom(*_args, **_kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _boom)


def _customer_headers(customer_id: int) -> dict:
    token = create_access_token(
        {"sub": str(customer_id), "type": "customer", "email": "x@example.com"}
    )
    return {"Authorization": f"Bearer {token}"}


def _staff_headers(staff_id: int) -> dict:
    token = create_access_token(
        {"sub": str(staff_id), "type": "staff", "username": "x", "role": "staff"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__apr", email=f"__apr_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_staff(branch_id) -> int:
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__apr{uuid4().hex[:8]}",
            password_hash="x",
            role="staff",
            branch_id=branch_id,
        )
        s.add(staff)
        await s.flush()
        return staff.id


async def _make_branch(main_stock=10, gift_stock=5):
    async with transactional_session() as s:
        branch = Branch(
            name=f"__apr{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        main_cat = MenuCategory(branch_id=branch.id, name="主食")
        gift_cat = MenuCategory(branch_id=branch.id, name="甜点")
        s.add_all([main_cat, gift_cat])
        await s.flush()
        main_item = MenuItem(
            category_id=main_cat.id, name=f"__饭{uuid4().hex[:6]}",
            base_price=10.0, is_available=True,
        )
        gift_item = MenuItem(
            category_id=gift_cat.id, name=f"__甜品{uuid4().hex[:6]}",
            base_price=6.0, is_available=True,
        )
        s.add_all([main_item, gift_item])
        await s.flush()
        s.add_all([
            MenuItemStock(menu_item_id=main_item.id, branch_id=branch.id, stock_quantity=main_stock),
            MenuItemStock(menu_item_id=gift_item.id, branch_id=branch.id, stock_quantity=gift_stock),
        ])
        return branch.id, main_item.id, gift_item.id


async def _stock_of(item_id, branch_id):
    async with transactional_session() as s:
        stock = await MenuRepo.get_stock(s, item_id, branch_id)
        return stock.stock_quantity if stock else None


async def _cleanup(branch_id=None, *customer_ids, staff_ids=()):
    async with transactional_session() as s:
        if branch_id is not None:
            await s.execute(delete(Branch).where(Branch.id == branch_id))
        for cid in customer_ids:
            await s.execute(delete(Customer).where(Customer.id == cid))
        for sid in staff_ids:
            await s.execute(delete(StaffUser).where(StaffUser.id == sid))


async def _place(client, customer_id, branch_id, item_id, notes):
    return await client.post(
        "/api/orders",
        headers=_customer_headers(customer_id),
        json={
            "branch_id": branch_id,
            "order_type": "dine_in",
            "special_notes": notes,
            "items": [{"menu_item_id": item_id, "quantity": 2}],
        },
    )


# --------------------------------------------------------------------------- #
# The customer's order endpoint: the intent gate
# --------------------------------------------------------------------------- #
async def test_gift_note_gates_the_order_behind_approval(async_client):
    branch_id, main_item_id, _gift = await _make_branch(main_stock=10)
    customer = await _make_customer()
    try:
        resp = await _place(async_client, customer, branch_id, main_item_id, "送我一个甜品呗")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending_approval"
        assert body["approval_id"] is not None
        assert await _stock_of(main_item_id, branch_id) == 8  # stock taken up front
    finally:
        await _cleanup(branch_id, customer)


async def test_plain_note_is_placed_normally(async_client):
    branch_id, main_item_id, _gift = await _make_branch()
    customer = await _make_customer()
    try:
        resp = await _place(async_client, customer, branch_id, main_item_id, "少辣，不要香菜")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "placed"
        assert body["approval_id"] is None
    finally:
        await _cleanup(branch_id, customer)


async def test_delivery_note_is_not_gated(async_client):
    """A "送" about delivery must not trip the approval gate."""
    branch_id, main_item_id, _gift = await _make_branch()
    customer = await _make_customer()
    try:
        resp = await _place(async_client, customer, branch_id, main_item_id, "送餐快一点")
        assert resp.status_code == 200
        assert resp.json()["status"] == "placed"
    finally:
        await _cleanup(branch_id, customer)


async def test_order_without_notes_is_placed(async_client):
    branch_id, main_item_id, _gift = await _make_branch()
    customer = await _make_customer()
    try:
        resp = await _place(async_client, customer, branch_id, main_item_id, None)
        assert resp.status_code == 200
        assert resp.json()["status"] == "placed"
    finally:
        await _cleanup(branch_id, customer)


async def test_get_my_order_enforces_ownership(async_client):
    branch_id, main_item_id, _gift = await _make_branch()
    owner = await _make_customer()
    stranger = await _make_customer()
    try:
        order_id = (await _place(async_client, owner, branch_id, main_item_id, None)).json()["id"]
        assert (
            await async_client.get(
                f"/api/orders/{order_id}", headers=_customer_headers(owner)
            )
        ).status_code == 200
        assert (
            await async_client.get(
                f"/api/orders/{order_id}", headers=_customer_headers(stranger)
            )
        ).status_code == 403
        assert (
            await async_client.get(
                "/api/orders/999999999", headers=_customer_headers(owner)
            )
        ).status_code == 404
    finally:
        await _cleanup(branch_id, owner, stranger)


# --------------------------------------------------------------------------- #
# The staff queue
# --------------------------------------------------------------------------- #
async def test_staff_lists_pending_approval_with_suggestion(async_client, staff_auth_headers):
    branch_id, main_item_id, gift_item_id = await _make_branch()
    customer = await _make_customer()
    try:
        await _place(async_client, customer, branch_id, main_item_id, "送我一个甜品呗")
        resp = await async_client.get(
            f"/api/staff/approvals?branch_id={branch_id}", headers=staff_auth_headers
        )
        assert resp.status_code == 200
        rows = [r for r in resp.json() if r["order"]["branch_id"] == branch_id]
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"
        assert row["intent"] == "gift_request"
        assert row["remark"] == "送我一个甜品呗"
        assert row["order"]["status"] == "pending_approval"
        assert row["suggestion"]["recommended_item"]["item_id"] == gift_item_id
        assert row["suggestion"]["candidates"]
    finally:
        await _cleanup(branch_id, customer)


async def test_staff_approve_issues_gift_and_places_order(async_client, staff_auth_headers):
    branch_id, main_item_id, gift_item_id = await _make_branch(gift_stock=5)
    customer = await _make_customer()
    try:
        placed = (await _place(async_client, customer, branch_id, main_item_id, "送我一个甜品呗")).json()
        approval_id = placed["approval_id"]

        resp = await async_client.post(
            f"/api/staff/approvals/{approval_id}/approve",
            headers=staff_auth_headers,
            json={"gift_id": gift_item_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["order"]["status"] == "placed"
        assert await _stock_of(gift_item_id, branch_id) == 4  # gift stock taken

        # The customer's poll now sees the promoted order.
        polled = await async_client.get(
            f"/api/orders/{placed['id']}", headers=_customer_headers(customer)
        )
        assert polled.json()["status"] == "placed"
    finally:
        await _cleanup(branch_id, customer)


async def test_staff_approve_with_unavailable_gift_is_409(async_client, staff_auth_headers):
    branch_id, main_item_id, gift_item_id = await _make_branch(gift_stock=5)
    customer = await _make_customer()
    try:
        placed = (await _place(async_client, customer, branch_id, main_item_id, "送我一个甜品呗")).json()
        # The gift disappears before the manager decides.
        async with transactional_session() as s:
            await MenuRepo.set_stock(
                s, menu_item_id=gift_item_id, branch_id=branch_id, stock_quantity=0
            )
        resp = await async_client.post(
            f"/api/staff/approvals/{placed['approval_id']}/approve",
            headers=staff_auth_headers,
            json={"gift_id": gift_item_id},
        )
        assert resp.status_code == 409
    finally:
        await _cleanup(branch_id, customer)


async def test_staff_reject_cancels_order(async_client, staff_auth_headers):
    branch_id, main_item_id, _gift = await _make_branch(main_stock=10)
    customer = await _make_customer()
    try:
        placed = (await _place(async_client, customer, branch_id, main_item_id, "送我一个甜品呗")).json()
        resp = await async_client.post(
            f"/api/staff/approvals/{placed['approval_id']}/reject",
            headers=staff_auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        assert resp.json()["order"]["status"] == "cancelled"
        assert await _stock_of(main_item_id, branch_id) == 10  # released
    finally:
        await _cleanup(branch_id, customer)


async def test_approval_endpoints_require_auth(async_client):
    assert (await async_client.get("/api/staff/approvals")).status_code == 401
    assert (
        await async_client.post("/api/staff/approvals/1/approve", json={"gift_id": 1})
    ).status_code == 401
    assert (await async_client.post("/api/staff/approvals/1/reject")).status_code == 401


async def test_branch_scoped_staff_cannot_act_on_another_branch(async_client):
    branch_id, main_item_id, gift_item_id = await _make_branch()
    other_branch, _om, _og = await _make_branch()
    customer = await _make_customer()
    staff_id = await _make_staff(other_branch)
    try:
        placed = (await _place(async_client, customer, branch_id, main_item_id, "送我一个甜品呗")).json()
        resp = await async_client.post(
            f"/api/staff/approvals/{placed['approval_id']}/approve",
            headers=_staff_headers(staff_id),
            json={"gift_id": gift_item_id},
        )
        assert resp.status_code == 403
        # Nothing happened.
        async with transactional_session() as s:
            status = (
                await s.execute(select(Order.status).where(Order.id == placed["id"]))
            ).scalar_one()
            assert status == "pending_approval"
    finally:
        await _cleanup(branch_id, customer, staff_ids=(staff_id,))
        await _cleanup(other_branch)
