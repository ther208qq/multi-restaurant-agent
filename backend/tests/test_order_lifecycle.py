"""Task15 tests: order lifecycle -- legal transitions and cancellation stock rules.

Spec §9: the order lifecycle is ``placed -> confirmed -> preparing -> ready ->
delivered``; ``cancelled`` is reachable from any non-terminal status and moves
the order's stock back, ``delivered`` is terminal, and ``cancelled -> placed``
(staff undoing a mistake) re-takes the stock. Illegal transitions are rejected
and leave the order untouched.
"""

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import ValidationError
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.repo.menu import MenuRepo
from backend.app.service.order import (
    ORDER_TRANSITIONS,
    VALID_ORDER_STATUSES,
    OrderService,
)

CHAIN = ("placed", "confirmed", "preparing", "ready", "delivered")

# Statuses that are not part of the forward chain: ``cancelled`` (reachable from
# any non-terminal status) and ``pending_approval`` -- the entry state for an
# order awaiting a manager's freebie decision (AgentFeatures §5), which lives on
# the matrix but is never moved *into*.
OFF_CHAIN = ("cancelled", "pending_approval")


async def _make_item(branch_id: int = 1, stock: int = 10, base_price: float = 2.0):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=branch_id, name="__lc_cat")
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name="__lc_item", base_price=base_price, is_available=True
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock))
        await s.flush()
        return item.id, cat.id


async def _read_stock(item_id, branch_id: int = 1):
    async with transactional_session() as s:
        stock = await MenuRepo.get_stock(s, item_id, branch_id)
        return stock.stock_quantity if stock else None


async def _place(item_id, qty: int = 2) -> int:
    order = await OrderService.place_order(
        1, 1, "dine_in", [{"menu_item_id": item_id, "quantity": qty}]
    )
    return order.id


async def _reach(order_id: int, target: str) -> None:
    """Drive a freshly placed order to ``target`` along the legal chain."""
    for status in CHAIN[1 : CHAIN.index(target) + 1]:
        await OrderService.update_status(order_id, status)


async def _cleanup(cat_id, order_id=None):
    async with transactional_session() as s:
        if order_id is not None:
            await s.execute(delete(Order).where(Order.id == order_id))
        await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))


def test_lifecycle_vocabulary_and_matrix_are_complete():
    assert set(VALID_ORDER_STATUSES) == set(CHAIN) | set(OFF_CHAIN)
    assert set(ORDER_TRANSITIONS) == set(VALID_ORDER_STATUSES)
    # ``delivered`` is terminal and only a cancelled order can be restored.
    assert ORDER_TRANSITIONS["delivered"] == frozenset()
    assert ORDER_TRANSITIONS["cancelled"] == frozenset({"placed"})
    # The approval entry state leaves the chain only via a manager's decision.
    assert ORDER_TRANSITIONS["pending_approval"] == frozenset({"placed", "cancelled"})


async def test_happy_path_forward_transitions_leave_stock_untouched():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order_id = await _place(item_id, qty=2)
        assert await _read_stock(item_id) == 3
        for status in ("confirmed", "preparing", "ready", "delivered"):
            updated = await OrderService.update_status(order_id, status)
            assert updated.status == status
            assert await _read_stock(item_id) == 3, "non-cancel moves must not touch stock"
    finally:
        await _cleanup(cat_id, order_id)


ILLEGAL_TRANSITIONS = [
    ("placed", "preparing"),  # must be confirmed first
    ("placed", "ready"),
    ("placed", "delivered"),
    ("confirmed", "ready"),
    ("confirmed", "delivered"),
    ("preparing", "confirmed"),  # no going backwards
    ("preparing", "delivered"),
    ("ready", "preparing"),
    ("ready", "confirmed"),
    ("delivered", "cancelled"),  # terminal
    ("delivered", "ready"),
    ("delivered", "placed"),
    ("cancelled", "confirmed"),  # only -> placed allowed
    ("cancelled", "ready"),
    ("cancelled", "delivered"),
]


@pytest.mark.parametrize("start,target", ILLEGAL_TRANSITIONS)
async def test_illegal_transition_is_rejected_and_status_unchanged(start, target):
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order_id = await _place(item_id, qty=1)
        if start == "cancelled":
            await OrderService.update_status(order_id, "cancelled")
        elif start != "placed":
            await _reach(order_id, start)
        with pytest.raises(ValidationError):
            await OrderService.update_status(order_id, target)
        current = await OrderService.get_order(order_id)
        assert current.status == start
    finally:
        await _cleanup(cat_id, order_id)


@pytest.mark.parametrize("start", ["placed", "confirmed", "preparing", "ready"])
async def test_cancel_restores_stock_from_any_non_terminal_state(start):
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order_id = await _place(item_id, qty=2)  # stock -> 3
        if start != "placed":
            await _reach(order_id, start)
        assert await _read_stock(item_id) == 3
        updated = await OrderService.update_status(order_id, "cancelled")
        assert updated.status == "cancelled"
        assert await _read_stock(item_id) == 5
    finally:
        await _cleanup(cat_id, order_id)


async def test_uncancel_restores_to_placed_and_redecrements_stock():
    item_id, cat_id = await _make_item(stock=5)
    order_id = None
    try:
        order_id = await _place(item_id, qty=2)  # stock -> 3
        await OrderService.update_status(order_id, "cancelled")  # stock -> 5
        assert await _read_stock(item_id) == 5
        restored = await OrderService.update_status(order_id, "placed")
        assert restored.status == "placed"
        assert await _read_stock(item_id) == 3
    finally:
        await _cleanup(cat_id, order_id)
