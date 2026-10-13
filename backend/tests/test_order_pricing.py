"""Task16 tests: order pricing -- authoritative variant markup and price snapshot.

Spec §9: support variant price markup and amount calculation, and persist a
price snapshot so later menu price changes do not alter historical orders. The
line unit price is the item's base price plus the variant deltas read from the
database -- never from the request payload.
"""

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import ValidationError
from backend.app.models.menu import (
    MenuCategory,
    MenuItem,
    MenuItemStock,
    MenuItemVariant,
)
from backend.app.models.order import Order
from backend.app.repo.menu import MenuRepo
from backend.app.service.order import OrderService

# (variant_type, variant_name, price_delta)
VARIANTS = [("size", "大份", 4.0), ("size", "小份", -2.0), ("topping", "加奶酪", 1.5)]


async def _make_item(branch_id=1, stock=10, base_price=10.0, variants=None):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=branch_id, name="__pr_cat")
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name="__pr_item", base_price=base_price, is_available=True
        )
        s.add(item)
        await s.flush()
        for vtype, vname, delta in variants or []:
            s.add(
                MenuItemVariant(
                    menu_item_id=item.id,
                    variant_type=vtype,
                    variant_name=vname,
                    price_delta=delta,
                )
            )
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


async def test_preview_applies_db_variant_markup():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    try:
        res = await OrderService.preview_order(
            1, [{"menu_item_id": item_id, "quantity": 1, "selected_variants": [{"name": "大份"}]}]
        )
        assert res["items"][0]["unit_price"] == 14.0
        assert res["total_amount"] == 14.0
    finally:
        await _cleanup(cat_id)


async def test_multiple_variants_sum_into_unit_price():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    try:
        res = await OrderService.preview_order(
            1,
            [{"menu_item_id": item_id, "quantity": 2, "selected_variants": [{"name": "大份"}, {"name": "加奶酪"}]}],
        )
        # 10 + 4 + 1.5 = 15.5 ; x2 = 31.0
        assert res["items"][0]["unit_price"] == 15.5
        assert res["total_amount"] == 31.0
    finally:
        await _cleanup(cat_id)


async def test_negative_variant_delta_lowers_price():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    try:
        res = await OrderService.preview_order(
            1, [{"menu_item_id": item_id, "quantity": 1, "selected_variants": [{"name": "小份"}]}]
        )
        assert res["items"][0]["unit_price"] == 8.0
    finally:
        await _cleanup(cat_id)


async def test_client_supplied_delta_is_ignored():
    """A tampered ``delta`` in the request must not move the price."""
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    order_id = None
    try:
        order = await OrderService.place_order(
            1,
            1,
            "dine_in",
            [{"menu_item_id": item_id, "quantity": 1, "selected_variants": [{"name": "大份", "delta": -999.0}]}],
        )
        order_id = order.id
        fetched = await OrderService.get_order(order_id)
        assert fetched.items[0].unit_price == 14.0
        assert fetched.total_amount == 14.0
    finally:
        await _cleanup(cat_id, order_id)


async def test_order_item_snapshots_resolved_variants():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    order_id = None
    try:
        order = await OrderService.place_order(
            1, 1, "dine_in",
            [{"menu_item_id": item_id, "quantity": 1, "selected_variants": [{"name": "大份"}]}],
        )
        order_id = order.id
        fetched = await OrderService.get_order(order_id)
        assert fetched.items[0].selected_variants == [
            {"variant_type": "size", "variant_name": "大份", "price_delta": 4.0}
        ]
    finally:
        await _cleanup(cat_id, order_id)


async def test_unknown_variant_rejected_in_preview():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    try:
        with pytest.raises(ValidationError):
            await OrderService.preview_order(
                1, [{"menu_item_id": item_id, "quantity": 1, "selected_variants": [{"name": "巨型"}]}]
            )
    finally:
        await _cleanup(cat_id)


async def test_nameless_variant_rejected():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    try:
        with pytest.raises(ValidationError):
            await OrderService.preview_order(
                1, [{"menu_item_id": item_id, "quantity": 1, "selected_variants": [{"delta": 1.0}]}]
            )
    finally:
        await _cleanup(cat_id)


async def test_unknown_variant_rejected_without_side_effects():
    item_id, cat_id = await _make_item(base_price=10.0, stock=5, variants=VARIANTS)
    try:
        with pytest.raises(ValidationError):
            await OrderService.place_order(
                1, 1, "dine_in",
                [{"menu_item_id": item_id, "quantity": 2, "selected_variants": [{"name": "巨型"}]}],
            )
        assert await _read_stock(item_id) == 5, "rejected order must not have touched stock"
    finally:
        await _cleanup(cat_id)


async def test_price_snapshot_survives_menu_price_change():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    order_id = None
    try:
        order = await OrderService.place_order(
            1, 1, "dine_in",
            [{"menu_item_id": item_id, "quantity": 2, "selected_variants": [{"name": "大份"}]}],
        )
        order_id = order.id  # 14.0 * 2 = 28.0
        # Menu changes after the fact: base price and the variant delta.
        async with transactional_session() as s:
            item = await MenuRepo.get_item(s, item_id, with_variants=True)
            await MenuRepo.update_item(s, item, {"base_price": 99.0})
            for v in item.variants:
                if v.variant_name == "大份":
                    await MenuRepo.update_item(s, v, {"price_delta": 50.0})
        fetched = await OrderService.get_order(order_id)
        assert fetched.total_amount == 28.0
        assert fetched.items[0].unit_price == 14.0
        assert fetched.items[0].selected_variants[0]["price_delta"] == 4.0
    finally:
        await _cleanup(cat_id, order_id)


async def test_total_equals_sum_of_line_subtotals():
    item1, cat1 = await _make_item(base_price=10.0, variants=VARIANTS)
    item2, cat2 = await _make_item(base_price=7.5, variants=[])
    try:
        res = await OrderService.preview_order(
            1,
            [
                {"menu_item_id": item1, "quantity": 3, "selected_variants": [{"name": "加奶酪"}]},
                {"menu_item_id": item2, "quantity": 2},
            ],
        )
        subtotals = [l["subtotal"] for l in res["items"]]
        assert res["total_amount"] == round(sum(subtotals), 2)
        assert res["total_amount"] == 49.5  # 11.5*3 + 7.5*2
    finally:
        await _cleanup(cat1)
        await _cleanup(cat2)


async def test_place_total_matches_preview_total():
    item_id, cat_id = await _make_item(base_price=10.0, variants=VARIANTS)
    order_id = None
    try:
        req = [{"menu_item_id": item_id, "quantity": 3, "selected_variants": [{"name": "大份"}, {"name": "加奶酪"}]}]
        prev = await OrderService.preview_order(1, req)
        order = await OrderService.place_order(1, 1, "dine_in", req)
        order_id = order.id
        fetched = await OrderService.get_order(order_id)
        assert fetched.total_amount == prev["total_amount"]
        assert fetched.items[0].unit_price == prev["items"][0]["unit_price"]
    finally:
        await _cleanup(cat_id, order_id)
