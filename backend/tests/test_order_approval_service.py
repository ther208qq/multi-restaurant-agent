"""Task C (AgentFeatures §5): the approval Service -- creating a pending order,
approving with a re-validated gift, and rejecting.

Business-level coverage of the HITL loop, without HTTP: the order and its
approval commit together; approval re-checks the manager's ``gift_id`` against
live inventory rather than trusting an earlier read; a gift that cannot be
honoured fails the whole move; rejection cancels and restocks.
"""

import pytest
from sqlalchemy import delete, func, select

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.models.approval import GIFT_REQUEST_INTENT, OrderApproval
from backend.app.models.branch import Branch
from backend.app.models.gift import Gift
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.menu import MenuRepo
from backend.app.service.approval import ApprovalService
from backend.app.service.order import OrderService

BRANCH2_STAFF = StaffUser(username="s2", role="staff", branch_id=2)


async def _make_customer() -> int:
    from uuid import uuid4

    async with transactional_session() as s:
        customer = Customer(
            name="__apc", email=f"__apc_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_branch(main_stock=10, gift_stock=5, gift_category="甜点"):
    """A branch with one orderable main dish and one giftable dessert."""
    from uuid import uuid4

    async with transactional_session() as s:
        branch = Branch(
            name=f"__apc{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        main_cat = MenuCategory(branch_id=branch.id, name="主食")
        gift_cat = MenuCategory(branch_id=branch.id, name=gift_category)
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


async def _set_stock(item_id, branch_id, qty):
    async with transactional_session() as s:
        await MenuRepo.set_stock(s, menu_item_id=item_id, branch_id=branch_id, stock_quantity=qty)


async def _gift_count(branch_id):
    async with transactional_session() as s:
        return (
            await s.execute(
                select(func.count()).select_from(Gift).where(Gift.branch_id == branch_id)
            )
        ).scalar()


async def _approval_status(approval_id):
    async with transactional_session() as s:
        return (
            await s.execute(
                select(OrderApproval.status).where(OrderApproval.id == approval_id)
            )
        ).scalar_one()


async def _cleanup(branch_id=None, *customer_ids):
    async with transactional_session() as s:
        if branch_id is not None:
            await s.execute(delete(Branch).where(Branch.id == branch_id))
        for cid in customer_ids:
            await s.execute(delete(Customer).where(Customer.id == cid))


async def _pending(branch_id, main_item_id, customer_id, notes="送我一个甜品呗"):
    return await ApprovalService.create_pending_order(
        customer_id, branch_id, "dine_in",
        [{"menu_item_id": main_item_id, "quantity": 2}],
        special_notes=notes, intent=GIFT_REQUEST_INTENT,
    )


# --------------------------------------------------------------------------- #
# create_pending_order
# --------------------------------------------------------------------------- #
async def test_create_pending_order_records_order_and_approval_together():
    branch_id, main_item_id, _gift = await _make_branch(main_stock=10)
    customer = await _make_customer()
    try:
        order, approval = await _pending(branch_id, main_item_id, customer)
        assert order.status == "pending_approval"
        assert await _stock_of(main_item_id, branch_id) == 8  # stock taken
        assert approval.order_id == order.id
        assert approval.status == "pending"
        assert approval.intent == GIFT_REQUEST_INTENT
        assert approval.remark == "送我一个甜品呗"
    finally:
        await _cleanup(branch_id, customer)


async def test_list_approvals_filters_by_status_and_branch():
    branch_id, main_item_id, _gift = await _make_branch()
    other_branch, other_main, _og = await _make_branch()
    customer = await _make_customer()
    try:
        _o1, a1 = await _pending(branch_id, main_item_id, customer)
        _o2, a2 = await _pending(other_branch, other_main, customer)
        pending_b1 = await ApprovalService.list_approvals(
            branch_id=branch_id, status="pending"
        )
        assert [a.id for a in pending_b1] == [a1.id]
    finally:
        await _cleanup(branch_id, customer)
        await _cleanup(other_branch)


async def test_get_approval_missing_raises():
    with pytest.raises(NotFoundError):
        await ApprovalService.get_approval(999999999)


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #
async def test_approve_issues_gift_promotes_order_and_closes_approval():
    branch_id, main_item_id, gift_item_id = await _make_branch(main_stock=10, gift_stock=5)
    customer = await _make_customer()
    try:
        order, approval = await _pending(branch_id, main_item_id, customer)
        approval, updated, gift = await ApprovalService.approve(approval.id, gift_item_id)

        assert approval.status == "approved"
        assert updated.status == "placed"
        assert gift.source_type == "order"
        assert gift.source_id == order.id
        assert gift.item_id == gift_item_id
        assert await _stock_of(gift_item_id, branch_id) == 4  # gift stock taken
        assert await _gift_count(branch_id) == 1

        # The freebie shows on the order as a zero-priced line.
        full = await OrderService.get_order(order.id)
        gift_lines = [i for i in full.items if i.menu_item_id == gift_item_id]
        assert len(gift_lines) == 1
        assert gift_lines[0].quantity == 1
        assert gift_lines[0].unit_price == 0.0
        assert len(full.items) == 2  # ordered dish + gift
    finally:
        await _cleanup(branch_id, customer)


async def test_approve_revalidates_gift_stock_and_fails_whole_move():
    """A gift sold out since it was suggested must fail, changing nothing."""
    branch_id, main_item_id, gift_item_id = await _make_branch(main_stock=10, gift_stock=5)
    customer = await _make_customer()
    try:
        order, approval = await _pending(branch_id, main_item_id, customer)
        await _set_stock(gift_item_id, branch_id, 0)  # gone before the decision

        with pytest.raises(ConflictError):
            await ApprovalService.approve(approval.id, gift_item_id)

        # Rolled back: order still pending, approval still open, no voucher.
        assert await _approval_status(approval.id) == "pending"
        refreshed = await OrderService.get_order(order.id)
        assert refreshed.status == "pending_approval"
        assert await _gift_count(branch_id) == 0
    finally:
        await _cleanup(branch_id, customer)


async def test_approve_rejects_a_gift_from_another_branch():
    branch_id, main_item_id, _gift = await _make_branch()
    other_branch, _om, other_gift = await _make_branch()
    customer = await _make_customer()
    try:
        order, approval = await _pending(branch_id, main_item_id, customer)
        with pytest.raises(ValidationError):
            await ApprovalService.approve(approval.id, other_gift)
        assert await _approval_status(approval.id) == "pending"
    finally:
        await _cleanup(branch_id, customer)
        await _cleanup(other_branch)


async def test_approve_twice_conflicts():
    branch_id, main_item_id, gift_item_id = await _make_branch()
    customer = await _make_customer()
    try:
        _order, approval = await _pending(branch_id, main_item_id, customer)
        await ApprovalService.approve(approval.id, gift_item_id)
        with pytest.raises(ConflictError):
            await ApprovalService.approve(approval.id, gift_item_id)
        assert await _gift_count(branch_id) == 1  # no second voucher
    finally:
        await _cleanup(branch_id, customer)


async def test_approve_foreign_branch_staff_is_forbidden():
    branch_id, main_item_id, gift_item_id = await _make_branch()
    customer = await _make_customer()
    try:
        _order, approval = await _pending(branch_id, main_item_id, customer)
        # ``branch_id`` here is never 2, so a branch-2 staff member may not act.
        with pytest.raises(ForbiddenError):
            await ApprovalService.approve(approval.id, gift_item_id, staff=BRANCH2_STAFF)
        assert await _approval_status(approval.id) == "pending"
    finally:
        await _cleanup(branch_id, customer)


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #
async def test_reject_places_order_without_gift_and_keeps_stock():
    """Rejecting the freebie must not cancel the order or return its stock."""
    branch_id, main_item_id, _gift = await _make_branch(main_stock=10)
    customer = await _make_customer()
    try:
        order, approval = await _pending(branch_id, main_item_id, customer)
        assert await _stock_of(main_item_id, branch_id) == 8
        approval, updated = await ApprovalService.reject(approval.id)
        assert approval.status == "rejected"
        assert updated.status == "placed"  # the order proceeds, gift withheld
        assert await _stock_of(main_item_id, branch_id) == 8  # stock stays consumed
        assert await _gift_count(branch_id) == 0
    finally:
        await _cleanup(branch_id, customer)


async def test_reject_twice_conflicts():
    branch_id, main_item_id, _gift = await _make_branch()
    customer = await _make_customer()
    try:
        _order, approval = await _pending(branch_id, main_item_id, customer)
        await ApprovalService.reject(approval.id)
        with pytest.raises(ConflictError):
            await ApprovalService.reject(approval.id)
    finally:
        await _cleanup(branch_id, customer)


async def test_get_approval_status():
    branch_id, main_item_id, _gift = await _make_branch()
    customer = await _make_customer()
    try:
        order, _approval = await _pending(branch_id, main_item_id, customer)
        statuses = await ApprovalService.get_approval_statuses([order.id])
        assert statuses.get(order.id) == "pending"
        # A normal order has no approval, so no status.
        normal = await OrderService.place_order(
            customer, branch_id, "dine_in", [{"menu_item_id": main_item_id, "quantity": 1}]
        )
        statuses = await ApprovalService.get_approval_statuses([normal.id])
        assert statuses.get(normal.id) is None
    finally:
        await _cleanup(branch_id, customer)


async def test_cancel_order_restores_stock_and_is_idempotent():
    branch_id, main_item_id, _gift = await _make_branch(main_stock=10)
    customer = await _make_customer()
    try:
        order = await OrderService.place_order(
            customer, branch_id, "dine_in", [{"menu_item_id": main_item_id, "quantity": 2}]
        )
        assert await _stock_of(main_item_id, branch_id) == 8
        cancelled = await OrderService.cancel_order(order.id, customer)
        assert cancelled.status == "cancelled"
        assert await _stock_of(main_item_id, branch_id) == 10
        # Cancelling again is a no-op, not an error.
        again = await OrderService.cancel_order(order.id, customer)
        assert again.status == "cancelled"
        assert await _stock_of(main_item_id, branch_id) == 10
    finally:
        await _cleanup(branch_id, customer)


async def test_cancel_order_foreign_customer_forbidden():
    branch_id, main_item_id, _gift = await _make_branch()
    owner = await _make_customer()
    intruder = await _make_customer()
    try:
        order = await OrderService.place_order(
            owner, branch_id, "dine_in", [{"menu_item_id": main_item_id, "quantity": 1}]
        )
        with pytest.raises(ForbiddenError):
            await OrderService.cancel_order(order.id, intruder)
    finally:
        await _cleanup(branch_id, owner, intruder)
