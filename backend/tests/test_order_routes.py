"""Task7 route tests: staff order endpoints served through OrderService.

These verify the migrated API layer keeps its previous HTTP behavior (status
codes, branch scoping, stock reversal) while all order business rules now live
in the Service; domain exceptions are mapped to HTTP by the global handlers.
"""

from sqlalchemy import delete

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import StaffUser
from backend.app.repo.menu import MenuRepo
from backend.app.service.order import OrderService


def _staff_headers(staff_id: int, role: str = "staff") -> dict:
    token = create_access_token(
        {"sub": str(staff_id), "type": "staff", "username": f"__rt{staff_id}", "role": role}
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_item(branch_id: int = 1, stock: int = 10, base_price: float = 2.0):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=branch_id, name="__rt_cat")
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name="__rt_item", base_price=base_price, is_available=True
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock))
        await s.flush()
        return item.id, cat.id


async def _place(branch_id: int, item_id: int, qty: int = 1, order_type: str = "dine_in"):
    return await OrderService.place_order(
        1, branch_id, order_type, [{"menu_item_id": item_id, "quantity": qty}]
    )


async def _make_staff(branch_id=None) -> int:
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__rt_s_{branch_id}", password_hash="x", role="staff", branch_id=branch_id
        )
        s.add(staff)
        await s.flush()
        return staff.id


async def _cleanup(cat_id, order_id=None, staff_id=None):
    async with transactional_session() as s:
        if order_id is not None:
            await s.execute(delete(Order).where(Order.id == order_id))
        if staff_id is not None:
            await s.execute(delete(StaffUser).where(StaffUser.id == staff_id))
        await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))


async def test_list_orders_requires_auth(async_client):
    resp = await async_client.get("/api/staff/orders")
    assert resp.status_code == 401


async def test_list_orders_admin_sees_all_and_filters(async_client, staff_auth_headers):
    item_id, cat_id = await _make_item(branch_id=1, stock=5)
    order_id = None
    try:
        order = await _place(1, item_id)
        order_id = order.id

        all_resp = await async_client.get(
            "/api/staff/orders?branch_id=1", headers=staff_auth_headers
        )
        assert all_resp.status_code == 200
        assert any(o["id"] == order_id for o in all_resp.json())

        placed = await async_client.get(
            "/api/staff/orders?branch_id=1&status=placed", headers=staff_auth_headers
        )
        assert any(o["id"] == order_id for o in placed.json())

        delivered = await async_client.get(
            "/api/staff/orders?branch_id=1&status=delivered", headers=staff_auth_headers
        )
        assert all(o["id"] != order_id for o in delivered.json())
    finally:
        await _cleanup(cat_id, order_id)


async def test_list_orders_is_bounded_and_validated(
    async_client, staff_auth_headers
):
    """A real branch can hold tens of thousands of orders; the list endpoint
    must return a bounded newest-first page instead of the whole history."""
    item_id, cat_id = await _make_item(branch_id=1, stock=5)
    order_id = None
    try:
        order = await _place(1, item_id)
        order_id = order.id

        newest = await async_client.get(
            "/api/staff/orders?branch_id=1&limit=1", headers=staff_auth_headers
        )
        assert newest.status_code == 200
        assert [o["id"] for o in newest.json()] == [order_id]

        for invalid in (0, 501):
            resp = await async_client.get(
                f"/api/staff/orders?branch_id=1&limit={invalid}",
                headers=staff_auth_headers,
            )
            assert resp.status_code == 422
    finally:
        await _cleanup(cat_id, order_id)


async def test_list_orders_branch_scoped_staff_sees_only_own_branch(async_client):
    item1, cat1 = await _make_item(branch_id=1, stock=5)
    item2, cat2 = await _make_item(branch_id=2, stock=5)
    staff_id = await _make_staff(branch_id=1)
    order1 = order2 = None
    try:
        order1 = await _place(1, item1)
        order2 = await _place(2, item2)
        resp = await async_client.get("/api/staff/orders", headers=_staff_headers(staff_id))
        assert resp.status_code == 200
        ids = [o["id"] for o in resp.json()]
        assert order1.id in ids
        assert order2.id not in ids
    finally:
        await _cleanup(cat1, order1.id if order1 else None, staff_id)
        await _cleanup(cat2, order2.id if order2 else None)


async def test_patch_status_success_reverses_stock(async_client, staff_auth_headers):
    item_id, cat_id = await _make_item(branch_id=1, stock=5)
    order_id = None
    try:
        order = await _place(1, item_id, qty=2)
        order_id = order.id
        resp = await async_client.patch(
            f"/api/staff/orders/{order_id}/status",
            json={"status": "cancelled"},
            headers=staff_auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        async with transactional_session() as s:
            stock = await MenuRepo.get_stock(s, item_id, 1)
            assert stock.stock_quantity == 5
    finally:
        await _cleanup(cat_id, order_id)


async def test_patch_status_not_found(async_client, staff_auth_headers):
    resp = await async_client.patch(
        "/api/staff/orders/999999999/status",
        json={"status": "ready"},
        headers=staff_auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]


async def test_patch_status_invalid_value(async_client, staff_auth_headers):
    item_id, cat_id = await _make_item(branch_id=1, stock=3)
    order_id = None
    try:
        order = await _place(1, item_id)
        order_id = order.id
        resp = await async_client.patch(
            f"/api/staff/orders/{order_id}/status",
            json={"status": "flying"},
            headers=staff_auth_headers,
        )
        assert resp.status_code == 400
    finally:
        await _cleanup(cat_id, order_id)


async def test_patch_status_forbidden_for_other_branch(async_client):
    item_id, cat_id = await _make_item(branch_id=1, stock=3)
    staff_id = await _make_staff(branch_id=2)
    order_id = None
    try:
        order = await _place(1, item_id)
        order_id = order.id
        resp = await async_client.patch(
            f"/api/staff/orders/{order_id}/status",
            json={"status": "ready"},
            headers=_staff_headers(staff_id),
        )
        assert resp.status_code == 403
    finally:
        await _cleanup(cat_id, order_id, staff_id)
