"""Customer self-service ordering over HTTP (OpenItems section 10).

The customer-facing ``POST /api/orders`` and ``POST /api/orders/preview`` routes
let a customer order from the sidebar cart without going through the chat Agent.
They are a thin HTTP skin over ``OrderService``; this suite proves the wiring and
the guarantees that matter:

* pricing comes from the database (base price + authoritative variant deltas),
  never from the request body;
* placing an order decrements stock and writes the ``order.created`` audit row
  attributed to the authenticated customer;
* the customer id always comes from the JWT -- a forged ``customer_id`` in the
  body is ignored;
* a rejected order (insufficient stock) leaves no trace: no order row, no stock
  movement, no audit entry;
* ``preview`` prices the basket without writing anything or reserving stock.
"""

from uuid import uuid4

from sqlalchemy import delete, select

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.menu import (
    MenuCategory,
    MenuItem,
    MenuItemStock,
    MenuItemVariant,
)
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService

BRANCH = 1  # seeded branch, used for the menu/order tests


def _headers(customer_id: int) -> dict:
    token = create_access_token(
        {"sub": str(customer_id), "type": "customer", "email": "x@example.com"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__co", email=f"__co_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(
    branch_id: int = BRANCH,
    stock: int = 10,
    price: float = 2.0,
    variants=(),
):
    """Create an isolated category + item (+ optional variants) with stock."""
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__co{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name="__co_item",
            base_price=price,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        for variant_type, variant_name, delta in variants:
            s.add(
                MenuItemVariant(
                    menu_item_id=item.id,
                    variant_type=variant_type,
                    variant_name=variant_name,
                    price_delta=delta,
                )
            )
        s.add(
            MenuItemStock(
                menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock
            )
        )
        await s.flush()
        return item.id, category.id


async def _stock_of(item_id: int, branch_id: int = BRANCH):
    async with transactional_session() as s:
        row = (
            await s.execute(
                select(MenuItemStock).where(
                    MenuItemStock.menu_item_id == item_id,
                    MenuItemStock.branch_id == branch_id,
                )
            )
        ).scalar_one_or_none()
        return row.stock_quantity if row else None


async def _orders_for(customer_id: int):
    async with transactional_session() as s:
        return list(
            (
                await s.execute(select(Order).where(Order.customer_id == customer_id))
            ).scalars()
        )


async def _logs(action=None, entity_id=None):
    """Read the audit trail through the Service, as an unrestricted admin."""
    async with transactional_session() as s:
        admin = StaffUser(
            username=f"__co{uuid4().hex[:8]}", password_hash="x", role="admin"
        )
        s.add(admin)
        await s.flush()
        admin_id = admin.id
    async with transactional_session() as s:
        admin = await UserRepo.get_staff_by_id(s, admin_id)
    return await AuditService.list_logs(
        admin, action=action, entity_id=entity_id, limit=200
    )


async def _cleanup(customer_id=None, cat_id=None):
    async with transactional_session() as s:
        if customer_id is not None:
            await s.execute(delete(Order).where(Order.customer_id == customer_id))
        if cat_id is not None:
            await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))
        if customer_id is not None:
            await s.execute(delete(Customer).where(Customer.id == customer_id))


# --------------------------------------------------------------------------- #
# Preview (read-only costing)
# --------------------------------------------------------------------------- #
async def test_preview_prices_lines_without_touching_stock(async_client):
    item_id, cat_id = await _make_item(stock=5, price=2.0)
    customer_id = await _make_customer()
    try:
        resp = await async_client.post(
            "/api/orders/preview",
            headers=_headers(customer_id),
            json={
                "branch_id": BRANCH,
                "order_type": "dine_in",
                "items": [{"menu_item_id": item_id, "quantity": 2}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_amount"] == 4.0
        assert body["has_shortfall"] is False
        assert body["items"][0]["qty"] == 2
        assert body["items"][0]["subtotal"] == 4.0
        assert await _stock_of(item_id) == 5  # preview never reserves stock
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


async def test_preview_reports_shortfall(async_client):
    item_id, cat_id = await _make_item(stock=1, price=2.0)
    customer_id = await _make_customer()
    try:
        resp = await async_client.post(
            "/api/orders/preview",
            headers=_headers(customer_id),
            json={
                "branch_id": BRANCH,
                "items": [{"menu_item_id": item_id, "quantity": 3}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_shortfall"] is True
        assert body["items"][0]["shortfall"] == 2
        assert body["total_amount"] == 0.0
        assert await _stock_of(item_id) == 1
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


# --------------------------------------------------------------------------- #
# Placing an order
# --------------------------------------------------------------------------- #
async def test_create_order_happy_path(async_client):
    item_id, cat_id = await _make_item(stock=5, price=2.0)
    customer_id = await _make_customer()
    try:
        resp = await async_client.post(
            "/api/orders",
            headers=_headers(customer_id),
            json={
                "branch_id": BRANCH,
                "order_type": "dine_in",
                "items": [{"menu_item_id": item_id, "quantity": 2}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "placed"
        assert body["customer_id"] == customer_id
        assert body["branch_name"]  # branch relationship loaded on re-read
        assert len(body["items"]) == 1
        assert body["total_amount"] == 4.0
        assert body["items"][0]["menu_item_name"] == "__co_item"
        assert await _stock_of(item_id) == 3  # stock decremented

        rows = await _logs(action="order.created", entity_id=body["id"])
        assert len(rows) == 1
        assert rows[0].actor_type == "customer"
        assert rows[0].actor_id == customer_id
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


async def test_create_order_prices_from_db_not_request(async_client):
    item_id, cat_id = await _make_item(
        stock=5, price=2.0, variants=[("size", "Large", 1.5)]
    )
    customer_id = await _make_customer()
    payload = {
        "branch_id": BRANCH,
        "items": [
            {
                "menu_item_id": item_id,
                "quantity": 1,
                "selected_variants": [{"name": "Large", "price_delta": 999}],
            }
        ],
    }
    try:
        resp = await async_client.post(
            "/api/orders", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 200
        body = resp.json()
        # 2.0 base + 1.5 DB delta -- the forged 999 in the body is ignored.
        assert body["items"][0]["unit_price"] == 3.5
        assert body["total_amount"] == 3.5
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


async def test_delivery_without_address_is_rejected(async_client):
    item_id, cat_id = await _make_item()
    customer_id = await _make_customer()
    payload = {
        "branch_id": BRANCH,
        "order_type": "delivery",
        "items": [{"menu_item_id": item_id, "quantity": 1}],
    }
    try:
        resp = await async_client.post(
            "/api/orders", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 400
        assert "配送地址" in resp.json()["detail"]
        assert await _orders_for(customer_id) == []
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


async def test_insufficient_stock_leaves_no_trace(async_client):
    item_id, cat_id = await _make_item(stock=1)
    customer_id = await _make_customer()
    before = len(await _logs(action="order.created"))
    payload = {
        "branch_id": BRANCH,
        "items": [{"menu_item_id": item_id, "quantity": 5}],
    }
    try:
        resp = await async_client.post(
            "/api/orders", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 409
        assert await _orders_for(customer_id) == []  # no order row
        assert await _stock_of(item_id) == 1  # no stock movement
        assert len(await _logs(action="order.created")) == before  # no audit row
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


async def test_create_order_ignores_forged_customer_id(async_client):
    item_id, cat_id = await _make_item()
    customer_id = await _make_customer()
    other_id = await _make_customer()
    payload = {
        "branch_id": BRANCH,
        "customer_id": other_id,  # should be ignored: the JWT decides owner
        "items": [{"menu_item_id": item_id, "quantity": 1}],
    }
    try:
        resp = await async_client.post(
            "/api/orders", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["customer_id"] == customer_id
        assert body["customer_id"] != other_id
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)
        await _cleanup(customer_id=other_id)


async def test_create_order_requires_auth(async_client):
    payload = {"branch_id": BRANCH, "items": []}
    resp = await async_client.post("/api/orders", json=payload)
    assert resp.status_code == 401
    preview = await async_client.post("/api/orders/preview", json=payload)
    assert preview.status_code == 401
