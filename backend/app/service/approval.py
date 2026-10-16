"""Order-approval business service (``sdd/AgentFeatures.md`` §5).

Owns the HITL approval flow's transaction boundaries and rules, and is the only
place that turns a customer's freebie request into a manager decision and back
into business effects:

* :meth:`ApprovalService.create_pending_order` records the order in
  ``pending_approval`` and its approval row in one transaction -- the order
  already holds its stock, but waits for the decision;
* :meth:`ApprovalService.approve` re-validates the manager's chosen ``gift_id``
  against live inventory (never trusting an earlier read), issues the voucher,
  adds the gift line and promotes the order to ``placed`` -- all or nothing;
* :meth:`ApprovalService.reject` cancels the order and releases its stock.

The module never talks to the Agent: recognising the request and proposing a gift
is the agent sub-flow's job (``agent/approvals.py``); this layer executes what the
manager decides. Raises domain exceptions (mapped to HTTP by the API layer).
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.models.approval import GIFT_REQUEST_INTENT, OrderApproval
from backend.app.models.gift import Gift
from backend.app.models.order import Order
from backend.app.models.user import StaffUser
from backend.app.repo.approval import ApprovalRepo
from backend.app.repo.order import OrderRepo
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService
from backend.app.service.gift import issue_gift
from backend.app.service.order import ORDER_TRANSITIONS, OrderService


class ApprovalService:
    # ------------------------------------------------------------------ #
    # Creation: the order that waits for a decision.
    # ------------------------------------------------------------------ #
    @staticmethod
    async def create_pending_order(
        customer_id: int,
        branch_id: int,
        order_type: str,
        items: List[Dict[str, Any]],
        delivery_address: Optional[str] = None,
        special_notes: Optional[str] = None,
        *,
        intent: str = GIFT_REQUEST_INTENT,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[Order, OrderApproval]:
        """Record an order in ``pending_approval`` together with its approval row.

        One transaction: the order and the approval that gates it commit together,
        so there is never a pending order without a manager-visible decision, nor
        an approval pointing at an order that was not created. The order's stock is
        taken here exactly as a normal order's would be -- only the freebie awaits
        approval.

        The ``intent`` the agent recognised is stored on the approval so the staff
        board can show why the order was gated.
        """
        async with transactional_session(session) as s:
            order = await OrderService._place_order_in_session(
                s,
                customer_id=customer_id,
                branch_id=branch_id,
                order_type=order_type,
                items=items,
                delivery_address=delivery_address,
                special_notes=special_notes,
                deduplicate=False,
                status="pending_approval",
            )
            approval = await ApprovalRepo.create_approval(
                s,
                {
                    "order_id": order.id,
                    "remark": special_notes,
                    "intent": intent,
                    "status": "pending",
                },
            )
            await AuditService.record(
                s,
                "approval.created",
                entity_type="approval",
                entity_id=approval.id,
                branch_id=branch_id,
                actor=await UserRepo.get_customer_by_id(s, customer_id),
                details={"order_id": order.id, "intent": intent},
            )
            return order, approval

    # ------------------------------------------------------------------ #
    # Reading the queue.
    # ------------------------------------------------------------------ #
    @staticmethod
    async def list_approvals(
        *,
        branch_id: Optional[int] = None,
        status: Optional[str] = None,
        session: Optional[AsyncSession] = None,
    ) -> List[OrderApproval]:
        """Approvals for the staff board, oldest first, with their orders loaded."""
        async with transactional_session(session) as s:
            return await ApprovalRepo.list_approvals(
                s, branch_id=branch_id, status=status, with_order=True
            )

    @staticmethod
    async def get_approval(
        approval_id: int, *, session: Optional[AsyncSession] = None
    ) -> OrderApproval:
        async with transactional_session(session) as s:
            approval = await ApprovalRepo.get_approval(s, approval_id, with_order=True)
            if approval is None:
                raise NotFoundError(f"审批 #{approval_id} 不存在。")
            return approval

    @staticmethod
    async def get_approval_status(
        order_id: int, *, session: Optional[AsyncSession] = None
    ) -> Optional[str]:
        """The outcome of the approval gating an order, or ``None`` when there is
        no approval (a normal order)."""
        async with transactional_session(session) as s:
            approval = await ApprovalRepo.get_by_order(s, order_id)
            return approval.status if approval else None

    @staticmethod
    async def get_approval_statuses(
        order_ids: list, *, session: Optional[AsyncSession] = None
    ) -> Dict[int, str]:
        """``{order_id: approval_status}`` for a batch of orders (one query)."""
        async with transactional_session(session) as s:
            approvals = await ApprovalRepo.list_by_orders(s, list(order_ids))
            return {a.order_id: a.status for a in approvals}

    # ------------------------------------------------------------------ #
    # Deciding.
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _load_pending_decision(
        s: AsyncSession, approval_id: int, staff: Optional[StaffUser]
    ) -> Tuple[OrderApproval, Order]:
        """Fetch an approval + its order and enforce the shared preconditions.

        Every decision needs the same three guards -- the approval exists, it is
        still ``pending``, it belongs to a branch this staff may act on, and the
        order is really in ``pending_approval``. Bothered once here so approve and
        reject can never diverge on them.
        """
        approval = await ApprovalRepo.get_approval(s, approval_id)
        if approval is None:
            raise NotFoundError(f"审批 #{approval_id} 不存在。")
        order = await OrderRepo.get_order(
            s, approval.order_id, with_items_menu_item=True, with_branch=True
        )
        if order is None:
            raise NotFoundError(f"订单 #{approval.order_id} 不存在。")
        if staff is not None and staff.branch_id and order.branch_id != staff.branch_id:
            raise ForbiddenError("您没有权限处理该门店的订单。")
        if approval.status != "pending":
            raise ConflictError(
                f"该审批已处理（当前状态：{approval.status}），无法再次操作。"
            )
        if order.status != "pending_approval":
            raise ConflictError(
                f"订单当前状态为「{order.status}」，无法通过或拒绝审批。"
            )
        return approval, order

    @staticmethod
    async def approve(
        approval_id: int,
        gift_id: int,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[OrderApproval, Order, Gift]:
        """Approve the request and give ``gift_id``, in one transaction.

        The manager's ``gift_id`` is *re-validated* against live inventory by
        :func:`issue_gift` -- an earlier suggestion is never trusted, so a gift
        that has since been disabled or sold out raises :class:`ConflictError`
        (mapped to 409) instead of being given away. On success the voucher is
        issued, a zero-priced gift line is appended to the order, the order moves
        to ``placed`` and the approval to ``approved``.
        """
        async with transactional_session(session) as s:
            approval, order = await ApprovalService._load_pending_decision(
                s, approval_id, staff
            )
            # Re-validate the choice and reserve its stock (raises on unknown item
            # / wrong branch / unavailable / out of stock -- the "clear business
            # error" the flow promises when the gift cannot be honoured).
            gift = await issue_gift(
                s,
                branch_id=order.branch_id,
                item_id=gift_id,
                source_type="order",
                source_id=order.id,
                actor=staff,
            )
            # The freebie shows on the customer's order as a zero-priced line; its
            # one unit of stock was already taken by ``issue_gift`` above.
            await OrderRepo.add_order_item(
                s,
                {
                    "order_id": order.id,
                    "menu_item_id": gift_id,
                    "quantity": 1,
                    "unit_price": 0.0,
                    "item_notes": "赠品（审批通过）",
                },
            )
            await OrderRepo.update_order_status(s, order, "placed")
            await ApprovalRepo.update_status(s, approval, "approved")
            await AuditService.record(
                s,
                "approval.approved",
                entity_type="approval",
                entity_id=approval.id,
                branch_id=order.branch_id,
                actor=staff,
                details={"order_id": order.id, "gift_id": gift_id, "gift_name": gift.item.name if gift.item else None},
            )
            await s.refresh(order)
            return approval, order, gift

    @staticmethod
    async def reject(
        approval_id: int,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[OrderApproval, Order]:
        """Reject the freebie request -- the order itself still goes through.

        Declining a gift is not the same as cancelling the order: the customer
        ordered real dishes and their stock was already taken. Rejection only
        withholds the freebie -- the order proceeds to ``placed`` (with no gift
        line) and the approval closes as ``rejected``. The customer is told the
        request was declined and may cancel the order themselves if they no longer
        want it.
        """
        async with transactional_session(session) as s:
            approval, order = await ApprovalService._load_pending_decision(
                s, approval_id, staff
            )
            if "placed" not in ORDER_TRANSITIONS.get(order.status, frozenset()):
                raise ValidationError("该订单当前状态不允许通过或拒绝审批。")
            # No gift is issued, so there is no stock to reverse -- the order's
            # own lines keep their stock (the dishes will be prepared).
            await OrderRepo.update_order_status(s, order, "placed")
            await ApprovalRepo.update_status(s, approval, "rejected")
            await AuditService.record(
                s,
                "approval.rejected",
                entity_type="approval",
                entity_id=approval.id,
                branch_id=order.branch_id,
                actor=staff,
                details={"order_id": order.id, "order_status": "placed"},
            )
            await s.refresh(order)
            return approval, order


__all__ = ["ApprovalService"]
