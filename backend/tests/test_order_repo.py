"""Task5 tests: OrderRepo and the atomic stock operations on MenuRepo."""

from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.repo.menu import MenuRepo
from backend.app.repo.order import OrderRepo


async def _make_item_with_stock(stock: int = 5):
    """Create an isolated category + item + stock row; returns (item_id, cat_id)."""
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=1, name="__repo_test_cat")
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name="__repo_test_item", base_price=1.0, is_available=True
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=1, stock_quantity=stock))
        await s.flush()
        return item.id, cat.id


async def _read_stock(item_id: int) -> int:
    async with transactional_session() as s:
        stock = await MenuRepo.get_stock(s, item_id, 1)
        return stock.stock_quantity


async def _cleanup_category(cat_id: int):
    async with transactional_session() as s:
        await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))


# --------------------------------------------------------------------------- #
# Atomic stock operations
# --------------------------------------------------------------------------- #


async def test_try_decrement_success_reduces_stock():
    item_id, cat_id = await _make_item_with_stock(stock=5)
    try:
        async with transactional_session() as s:
            ok = await MenuRepo.try_decrement_stock(s, item_id, 1, 3)
        assert ok is True
        assert await _read_stock(item_id) == 2
    finally:
        await _cleanup_category(cat_id)


async def test_try_decrement_insufficient_returns_false_without_change():
    item_id, cat_id = await _make_item_with_stock(stock=2)
    try:
        async with transactional_session() as s:
            ok = await MenuRepo.try_decrement_stock(s, item_id, 1, 3)
        assert ok is False
        assert await _read_stock(item_id) == 2
    finally:
        await _cleanup_category(cat_id)


async def test_try_decrement_allows_exact_remaining():
    item_id, cat_id = await _make_item_with_stock(stock=4)
    try:
        async with transactional_session() as s:
            assert await MenuRepo.try_decrement_stock(s, item_id, 1, 4) is True
        assert await _read_stock(item_id) == 0
    finally:
        await _cleanup_category(cat_id)


async def test_try_decrement_missing_row_returns_false():
    async with transactional_session() as s:
        assert await MenuRepo.try_decrement_stock(s, 999999, 1, 1) is False


async def test_restore_stock_increments():
    item_id, cat_id = await _make_item_with_stock(stock=1)
    try:
        async with transactional_session() as s:
            rowcount = await MenuRepo.restore_stock(s, item_id, 1, 4)
        assert rowcount == 1
        assert await _read_stock(item_id) == 5
    finally:
        await _cleanup_category(cat_id)


# --------------------------------------------------------------------------- #
# OrderRepo
# --------------------------------------------------------------------------- #


async def test_create_order_and_item_then_fetch_with_items():
    item_id, cat_id = await _make_item_with_stock(stock=5)
    order_id = None
    try:
        async with transactional_session() as s:
            order = await OrderRepo.create_order(
                s,
                {
                    "customer_id": 1,
                    "branch_id": 1,
                    "order_type": "dine_in",
                    "status": "placed",
                    "total_amount": 2.5,
                },
            )
            order_id = order.id
            await OrderRepo.add_order_item(
                s,
                {
                    "order_id": order.id,
                    "menu_item_id": item_id,
                    "quantity": 2,
                    "unit_price": 1.25,
                },
            )

        async with transactional_session() as s:
            fetched = await OrderRepo.get_order(s, order_id, with_items=True)
            assert fetched is not None
            assert fetched.total_amount == 2.5
            assert len(fetched.items) == 1
            assert fetched.items[0].quantity == 2
    finally:
        if order_id is not None:
            async with transactional_session() as s:
                await s.execute(delete(Order).where(Order.id == order_id))
        await _cleanup_category(cat_id)


async def test_get_order_missing_returns_none():
    async with transactional_session() as s:
        assert await OrderRepo.get_order(s, 999999999, with_items=True) is None


async def test_list_orders_filters_by_customer_and_status():
    async with transactional_session() as s:
        order = await OrderRepo.create_order(
            s,
            {
                "customer_id": 1,
                "branch_id": 1,
                "order_type": "takeaway",
                "status": "preparing",
                "total_amount": 9.0,
            },
        )
        order_id = order.id
    try:
        async with transactional_session() as s:
            mine = await OrderRepo.list_orders(s, customer_id=1, order_status="preparing")
            assert any(o.id == order_id for o in mine)

            not_mine = await OrderRepo.list_orders(s, order_status="delivered")
            assert all(o.id != order_id for o in not_mine)

            wrong_status = await OrderRepo.list_orders(
                s, customer_id=1, order_status="delivered"
            )
            assert all(o.id != order_id for o in wrong_status)
    finally:
        async with transactional_session() as s:
            await s.execute(delete(Order).where(Order.id == order_id))


async def test_update_order_status():
    async with transactional_session() as s:
        order = await OrderRepo.create_order(
            s,
            {
                "customer_id": 1,
                "branch_id": 1,
                "order_type": "dine_in",
                "status": "placed",
                "total_amount": 1.0,
            },
        )
        order_id = order.id
    try:
        async with transactional_session() as s:
            fetched = await OrderRepo.get_order(s, order_id)
            await OrderRepo.update_order_status(s, fetched, "preparing")
        async with transactional_session() as s:
            reloaded = await OrderRepo.get_order(s, order_id)
            assert reloaded.status == "preparing"
    finally:
        async with transactional_session() as s:
            await s.execute(delete(Order).where(Order.id == order_id))
