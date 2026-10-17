"""Task A (AgentFeatures §5): the ``order_approvals`` model + repo, and the new
``pending_approval`` order state with its transition matrix.

The approval flow's data foundation: the table that records a manager's pending
decision, and the extra order status an order enters when its notes read as a
freebie request. Behaviour that *uses* these (the intent gate, approve / reject)
is covered by the later tasks' suites.
"""

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import ValidationError
from backend.app.models.approval import GIFT_REQUEST_INTENT, OrderApproval
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order, OrderItem
from backend.app.repo.approval import ApprovalRepo
from backend.app.repo.menu import MenuRepo
from backend.app.service.order import (
    KDS_ACTIONABLE_ORDER_STATUSES,
    ORDER_TRANSITIONS,
    VALID_ORDER_STATUSES,
    OrderService,
)


async def _make_item(branch_id=1, stock=10, is_available=True, base_price=2.0):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=branch_id, name='__ap_cat')
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name='__ap_item', base_price=base_price, is_available=is_available
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


async def _make_order(branch_id=1, status='pending_approval'):
    async with transactional_session() as s:
        order = Order(
            customer_id=1, branch_id=branch_id, order_type='dine_in',
            status=status, total_amount=0.0,
        )
        s.add(order)
        await s.flush()
        return order.id


async def _make_pending_order(item_id, qty=1, branch_id=1):
    """A pending-approval order with one line, its stock already taken."""
    async with transactional_session() as s:
        assert await MenuRepo.try_decrement_stock(s, item_id, branch_id, qty)
        order = Order(
            customer_id=1, branch_id=branch_id, order_type='dine_in',
            status='pending_approval', total_amount=0.0,
        )
        s.add(order)
        await s.flush()
        s.add(OrderItem(order_id=order.id, menu_item_id=item_id, quantity=qty, unit_price=2.0))
        await s.flush()
        return order.id


async def _make_approval(order_id, intent=GIFT_REQUEST_INTENT, status='pending', remark='送个甜点'):
    async with transactional_session() as s:
        approval = await ApprovalRepo.create_approval(
            s,
            {
                'order_id': order_id,
                'remark': remark,
                'intent': intent,
                'status': status,
            },
        )
        return approval.id


async def _cleanup_order(*order_ids):
    async with transactional_session() as s:
        for oid in order_ids:
            await s.execute(delete(Order).where(Order.id == oid))


async def _cleanup_item(cat_id, *order_ids):
    await _cleanup_order(*order_ids)
    async with transactional_session() as s:
        await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))


# --------------------------------------------------------------------------- #
# The ``pending_approval`` state and its transitions
# --------------------------------------------------------------------------- #
def test_pending_approval_is_a_valid_status():
    assert 'pending_approval' in VALID_ORDER_STATUSES


def test_pending_approval_leaves_only_to_placed_or_cancelled():
    assert ORDER_TRANSITIONS['pending_approval'] == frozenset({'placed', 'cancelled'})


def test_nothing_transitions_into_pending_approval():
    """It is an entry status -- only ever set at creation, never moved into."""
    for destinations in ORDER_TRANSITIONS.values():
        assert 'pending_approval' not in destinations


def test_pending_approval_is_not_kds_actionable():
    """A kitchen task exists only once an order is placed; not while it waits."""
    assert 'pending_approval' not in KDS_ACTIONABLE_ORDER_STATUSES


async def test_placed_cannot_move_to_pending_approval():
    item_id, cat_id = await _make_item(stock=5)
    order = await OrderService.place_order(
        1, 1, 'dine_in', [{'menu_item_id': item_id, 'quantity': 1}]
    )
    try:
        with pytest.raises(ValidationError):
            await OrderService.update_status(order.id, 'pending_approval')
    finally:
        await _cleanup_item(cat_id, order.id)


async def test_cancel_from_pending_approval_restores_stock():
    item_id, cat_id = await _make_item(stock=5)
    order_id = await _make_pending_order(item_id, qty=2)
    try:
        assert await _read_stock(item_id) == 3  # stock taken at placement
        await OrderService.update_status(order_id, 'cancelled')
        assert await _read_stock(item_id) == 5  # released on rejection
    finally:
        await _cleanup_item(cat_id, order_id)


# --------------------------------------------------------------------------- #
# The repository
# --------------------------------------------------------------------------- #
async def test_approval_repo_roundtrip():
    order_id = await _make_order()
    approval_id = await _make_approval(order_id)
    try:
        async with transactional_session() as s:
            got = await ApprovalRepo.get_approval(s, approval_id, with_order=True)
            assert got is not None
            assert got.order_id == order_id
            assert got.intent == GIFT_REQUEST_INTENT
            assert got.status == 'pending'
            assert got.remark == '送个甜点'
            assert got.order.id == order_id  # relationship eager-loaded

            by_order = await ApprovalRepo.list_by_orders(s, [order_id])
            assert [a.id for a in by_order] == [approval_id]

            updated = await ApprovalRepo.update_status(s, got, 'approved')
            assert updated.status == 'approved'
    finally:
        await _cleanup_order(order_id)


async def test_get_approval_missing_returns_none():
    async with transactional_session() as s:
        assert await ApprovalRepo.get_approval(s, 999999999) is None


async def test_list_approvals_filters_by_status_and_branch():
    o1 = await _make_order(branch_id=1)
    o2 = await _make_order(branch_id=1)
    o3 = await _make_order(branch_id=2)
    try:
        a1 = await _make_approval(o1, status='pending')
        a2 = await _make_approval(o2, status='approved')
        a3 = await _make_approval(o3, status='pending')
        async with transactional_session() as s:
            pending_b1 = await ApprovalRepo.list_approvals(
                s, branch_id=1, status='pending'
            )
            assert [x.id for x in pending_b1] == [a1]

            all_b1 = await ApprovalRepo.list_approvals(s, branch_id=1)
            assert {x.id for x in all_b1} == {a1, a2}

            pending_all = await ApprovalRepo.list_approvals(s, status='pending')
            assert {a1, a3}.issubset({x.id for x in pending_all})
    finally:
        await _cleanup_order(o1, o2, o3)


async def test_deleting_order_cascades_to_approval():
    order_id = await _make_order()
    approval_id = await _make_approval(order_id)
    await _cleanup_order(order_id)
    async with transactional_session() as s:
        assert await ApprovalRepo.get_approval(s, approval_id) is None
