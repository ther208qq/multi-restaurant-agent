"""Task6 tests: OrderService placement, preview, status transitions, queries."""

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import StaffUser
from backend.app.repo.menu import MenuRepo
from backend.app.service.order import OrderService

ADMIN = StaffUser(username='admin', role='admin', branch_id=None)
BRANCH1_STAFF = StaffUser(username='s1', role='staff', branch_id=1)
BRANCH2_STAFF = StaffUser(username='s2', role='staff', branch_id=2)


async def _make_item(branch_id=1, stock=10, is_available=True, base_price=2.0):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=branch_id, name='__os_cat')
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name='__os_item', base_price=base_price, is_available=is_available
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock))
        await s.flush()
        return item.id, cat.id


async def _read_stock(item_id, branch_id=1):
    async with transactional_session() as s:
        stock = await MenuRepo.get_stock(s, item_id, branch_id)
        return stock.stock_quantity if stock else None


async def _cleanup(cat_id, order_id=None):
    async with transactional_session() as s:
        if order_id is not None:
            await s.execute(delete(Order).where(Order.id == order_id))
        await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))


async def test_preview_order_totals_and_shortfall():
    item_id, cat_id = await _make_item(stock=3, base_price=2.0)
    try:
        result = await OrderService.preview_order(1, [{'menu_item_id': item_id, 'quantity': 2}])
        assert result['has_shortfall'] is False
        assert result['total_amount'] == 4.0
        assert result['items'][0]['available_quantity'] == 3
    finally:
        await _cleanup(cat_id)


async def test_preview_order_flags_shortfall():
    item_id, cat_id = await _make_item(stock=1)
    try:
        result = await OrderService.preview_order(1, [{'menu_item_id': item_id, 'quantity': 5}])
        assert result['has_shortfall'] is True
        assert result['items'][0]['shortfall'] == 4
        assert result['total_amount'] == 0.0
    finally:
        await _cleanup(cat_id)


async def test_preview_order_empty_raises():
    with pytest.raises(ValidationError):
        await OrderService.preview_order(1, [])


async def test_preview_order_missing_item_raises():
    with pytest.raises(NotFoundError):
        await OrderService.preview_order(1, [{'menu_item_id': 999999, 'quantity': 1}])


async def test_place_order_success_decrements_stock():
    item_id, cat_id = await _make_item(stock=5, base_price=3.0)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 2}])
        order_id = order.id
        assert order.total_amount == 6.0
        assert order.status == 'placed'
        assert await _read_stock(item_id) == 3
    finally:
        await _cleanup(cat_id, order_id)


async def test_place_order_empty_raises():
    with pytest.raises(ValidationError):
        await OrderService.place_order(1, 1, 'dine_in', [])


async def test_place_order_delivery_requires_address():
    with pytest.raises(ValidationError):
        await OrderService.place_order(1, 1, 'delivery', [{'menu_item_id': 1, 'quantity': 1}])


async def test_place_order_unknown_item_not_found():
    with pytest.raises(NotFoundError):
        await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': 999999, 'quantity': 1}])


async def test_place_order_unavailable_conflict():
    item_id, cat_id = await _make_item(stock=5, is_available=False)
    try:
        with pytest.raises(ConflictError):
            await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 1}])
    finally:
        await _cleanup(cat_id)


async def test_place_order_insufficient_stock_unchanged():
    item_id, cat_id = await _make_item(stock=2)
    try:
        with pytest.raises(ConflictError):
            await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 5}])
        assert await _read_stock(item_id) == 2
    finally:
        await _cleanup(cat_id)


async def test_place_order_invalid_quantity():
    item_id, cat_id = await _make_item(stock=5)
    try:
        with pytest.raises(ValidationError):
            await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 0}])
    finally:
        await _cleanup(cat_id)


async def test_get_order_missing_raises():
    with pytest.raises(NotFoundError):
        await OrderService.get_order(999999999)


async def test_get_order_returns_with_relations():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'takeaway', [{'menu_item_id': item_id, 'quantity': 1}])
        order_id = order.id
        fetched = await OrderService.get_order(order_id)
        assert fetched.id == order_id
        assert len(fetched.items) == 1
    finally:
        await _cleanup(cat_id, order_id)


async def test_list_orders_filters_by_status():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 1}])
        order_id = order.id
        placed = await OrderService.list_orders(customer_id=1, order_status='placed')
        assert any(o.id == order_id for o in placed)
        delivered = await OrderService.list_orders(customer_id=1, order_status='delivered')
        assert all(o.id != order_id for o in delivered)
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_confirm_then_preparing_no_stock_change():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 2}])
        order_id = order.id
        updated = await OrderService.update_status(order_id, 'confirmed')
        assert updated.status == 'confirmed'
        assert await _read_stock(item_id) == 3
        updated = await OrderService.update_status(order_id, 'preparing')
        assert updated.status == 'preparing'
        assert await _read_stock(item_id) == 3
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_cancel_restores_stock():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 2}])
        order_id = order.id
        await OrderService.update_status(order_id, 'cancelled')
        assert await _read_stock(item_id) == 5
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_uncancel_redecrements_stock():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 2}])
        order_id = order.id
        await OrderService.update_status(order_id, 'cancelled')
        await OrderService.update_status(order_id, 'placed')
        assert await _read_stock(item_id) == 3
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_uncancel_insufficient_conflict():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 3}])
        order_id = order.id
        await OrderService.update_status(order_id, 'cancelled')
        async with transactional_session() as s:
            await MenuRepo.set_stock(s, menu_item_id=item_id, branch_id=1, stock_quantity=1)
        with pytest.raises(ConflictError):
            await OrderService.update_status(order_id, 'placed')
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_cancelled_to_other_raises():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 1}])
        order_id = order.id
        await OrderService.update_status(order_id, 'cancelled')
        with pytest.raises(ValidationError):
            await OrderService.update_status(order_id, 'ready')
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_invalid_status_raises():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 1}])
        order_id = order.id
        with pytest.raises(ValidationError):
            await OrderService.update_status(order_id, 'flying')
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_missing_order_raises():
    with pytest.raises(NotFoundError):
        await OrderService.update_status(999999999, 'ready')


async def test_update_status_branch_mismatch_forbidden():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 1}])
        order_id = order.id
        with pytest.raises(ForbiddenError):
            await OrderService.update_status(order_id, 'ready', staff=BRANCH2_STAFF)
    finally:
        await _cleanup(cat_id, order_id)


async def test_update_status_same_status_is_noop():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order = await OrderService.place_order(1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 2}])
        order_id = order.id
        result = await OrderService.update_status(order_id, 'placed')
        assert result.status == 'placed'
        assert await _read_stock(item_id) == 3
    finally:
        await _cleanup(cat_id, order_id)