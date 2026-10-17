"""Order business service.

Owns the transaction boundary and holds the single implementation of the order
business rules shared by the staff/customer order APIs and the Agent ordering
flow: price estimation, stock validation, atomic placement, the order lifecycle
(legal status transitions) and cancellation / un-cancellation stock reversal.

Pricing is authoritative and snapshot at placement (spec §9): the line unit
price is the item's base price plus the variant deltas read from the database
(never from the request), persisted on the order line together with the resolved
variants and the frozen order total -- so later menu price changes cannot alter
historical orders.

Lifecycle (spec §9): ``placed -> confirmed -> preparing -> ready -> delivered``.
``cancelled`` is reachable from any non-terminal status and restores the stock
the order held; ``delivered`` is terminal. A cancelled order may be restored to
``placed`` only (staff undoing a mistake), which atomically re-takes its stock.

Raises domain exceptions (never HTTP types); the API maps them to status codes
and the Agent turns them into user-facing replies.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.labels import KDS_STATUS_LABELS, ORDER_STATUS_LABELS
from backend.app.models.order import Order
from backend.app.models.user import StaffUser
from backend.app.repo.locks import lock_key, lock_request
from backend.app.repo.menu import MenuRepo
from backend.app.repo.order import OrderRepo
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService
from backend.app.service.scope import ensure_branch_scope

VALID_ORDER_STATUSES = (
    # ``pending_approval`` is the entry state for an order whose notes read as a
    # freebie request (AgentFeatures §5): the order is recorded -- and its stock
    # taken -- but waits for a manager's decision before it becomes ``placed``.
    "pending_approval",
    "placed",
    "confirmed",
    "preparing",
    "ready",
    "delivered",
    "cancelled",
)

# Statuses that still have something left to happen to them. A repeat submission
# is a duplicate of an order in one of these -- once an order is cancelled or
# delivered, ordering the same thing again is a genuinely new request.
OPEN_ORDER_STATUSES = tuple(
    status for status in VALID_ORDER_STATUSES if status not in ("cancelled", "delivered")
)

# How long a still-open order keeps absorbing repeat submissions of itself
# (spec §9 重复请求). Long enough to cover a double-sent chat message, a retried
# request or a model that emits the same tool call twice in one turn; short
# enough that "the same order, again" a few minutes later is a new order.
DEDUPLICATE_WINDOW_MINUTES = 5

# Legal status moves. Forward-only along the lifecycle, plus cancellation from
# any non-terminal status and the single ``cancelled -> placed`` restore. A
# status absent from the map (or an empty set) has no outgoing transitions.
#
# ``pending_approval`` is an *entry* status (AgentFeatures §5): it is set when the
# order is created, so nothing transitions *into* it here, and it leaves only to
# ``placed`` (a manager approves) or ``cancelled`` (a manager rejects).
ORDER_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "pending_approval": frozenset({"placed", "cancelled"}),
    "placed": frozenset({"confirmed", "cancelled"}),
    "confirmed": frozenset({"preparing", "cancelled"}),
    "preparing": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"delivered", "cancelled"}),
    "delivered": frozenset(),
    "cancelled": frozenset({"placed"}),
}

# Legal moves of the *simulated* Kitchen Display System state (AgentFeatures §3).
# The kitchen advances a task ``queued -> cooking -> done``; a customer "hold"
# parks it in ``held`` from either the queue or mid-cooking, and "resume" sends
# it back to the queue. ``done`` is terminal. This is a simulation -- the tools
# are the only writers -- so the matrix is deliberately small.
KDS_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "queued": frozenset({"cooking", "held"}),
    "cooking": frozenset({"held", "done"}),
    "held": frozenset({"queued"}),
    "done": frozenset(),
}

# Order statuses whose kitchen task can still be touched. Once an order is
# cancelled or delivered there is nothing left in the kitchen to hold or resume;
# an order still awaiting approval has not been sent to the kitchen at all.
KDS_ACTIONABLE_ORDER_STATUSES = tuple(
    status
    for status in VALID_ORDER_STATUSES
    if status not in ("cancelled", "delivered", "pending_approval")
)

# ``kds_held_reason`` is a 255-char column; the customer's message is trimmed to
# fit rather than rejected, since a long reason is not a user error.
KDS_HELD_REASON_MAX_LENGTH = 255


def _status_label(status: str) -> str:
    """Chinese label for a status, falling back to the raw code."""
    return ORDER_STATUS_LABELS.get(status, status)


def _kds_status_label(status: str) -> str:
    """Chinese label for a KDS state, falling back to the raw code."""
    return KDS_STATUS_LABELS.get(status, status)


def _resolve_variants(
    menu_item, requested: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Resolve requested variants against the item's authoritative variant rows.

    The caller (Agent / API) sends ``[{"name": ...}]`` references. The
    ``price_delta`` always comes from the database, never from the request, so a
    tampered ``delta`` in the payload cannot change the price. Unknown or
    nameless variants are rejected rather than silently priced at the base.
    """
    variants = {v.variant_name: v for v in (menu_item.variants or [])}
    resolved: List[Dict[str, Any]] = []
    for req in requested or []:
        name = str(req.get("name") or req.get("variant_name") or "").strip()
        if not name:
            raise ValidationError(f"「{menu_item.name}」的规格信息不完整。")
        match = variants.get(name)
        if match is None:
            available = "、".join(variants) or "无"
            raise ValidationError(
                f"「{menu_item.name}」没有名为「{name}」的规格（可选：{available}）。"
            )
        resolved.append(
            {
                "variant_type": match.variant_type,
                "variant_name": match.variant_name,
                "price_delta": float(match.price_delta),
            }
        )
    return resolved


def _unit_price(base_price: float, resolved_variants: List[Dict[str, Any]]) -> float:
    """Authoritative unit price: base price plus the resolved variant deltas."""
    return float(base_price) + sum(
        float(v["price_delta"]) for v in resolved_variants
    )


def _variant_names(variants: Optional[List[Dict[str, Any]]]) -> tuple:
    """The variant names on a line, order-insensitively, for request comparison."""
    return tuple(
        sorted(
            str(v.get("variant_name") or v.get("name") or "").strip()
            for v in (variants or [])
        )
    )


def _order_signature(
    order_type: Optional[str],
    delivery_address: Optional[str],
    lines: List[Dict[str, Any]],
) -> tuple:
    """What makes two order requests *the same request*, independent of ids.

    Compared against :meth:`OrderService._find_duplicate_order`, which builds the
    same tuple from stored order lines -- so the two sides have to keep agreeing
    on the shape (item, quantity, variant names, line note).
    """
    signature = [
        order_type or "",
        (delivery_address or "").strip(),
    ]
    for line in sorted(lines, key=lambda x: (x["menu_item_id"], x["quantity"])):
        signature.append(
            "{}x{}[{}]{}".format(
                line["menu_item_id"],
                line["quantity"],
                ",".join(_variant_names(line.get("selected_variants"))),
                (line.get("item_notes") or "").strip(),
            )
        )
    return tuple(signature)


class OrderService:
    @staticmethod
    async def preview_order(
        branch_id: int,
        items: List[Dict[str, Any]],
        *,
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Resolve items, prices and stock availability without writing."""
        if not items:
            raise ValidationError(
                "订单不能为空，请指定想要点的菜品。"
            )
        async with transactional_session(session) as s:
            lines: List[Dict[str, Any]] = []
            total_amount = 0.0
            has_shortfall = False
            for req in items:
                item_id = req.get("menu_item_id")
                qty = req.get("quantity", 1)
                menu_item = await MenuRepo.get_item(s, item_id, with_variants=True)
                if menu_item is None:
                    raise NotFoundError(f"菜品 #{item_id} 不存在。")
                stock = await MenuRepo.get_stock(s, item_id, branch_id)
                available = stock.stock_quantity if stock else 0
                variants = _resolve_variants(menu_item, req.get("selected_variants"))
                unit_price = _unit_price(menu_item.base_price, variants)
                shortfall = max(0, qty - available)
                if shortfall:
                    has_shortfall = True
                subtotal = round(unit_price * qty, 2)
                if not shortfall:
                    total_amount += subtotal
                lines.append(
                    {"menu_item_id": item_id, "name": menu_item.name, "qty": qty, "unit_price": unit_price, "subtotal": subtotal, "selected_variants": variants, "available_quantity": available, "shortfall": shortfall})
            return {
                "items": lines,
                "total_amount": round(total_amount, 2),
                "has_shortfall": has_shortfall,
            }

    @staticmethod
    async def place_order(
        customer_id: int,
        branch_id: int,
        order_type: str,
        items: List[Dict[str, Any]],
        delivery_address: Optional[str] = None,
        special_notes: Optional[str] = None,
        *,
        deduplicate: bool = False,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        async with transactional_session(session) as s:
            return await OrderService._place_order_in_session(
                s,
                customer_id=customer_id,
                branch_id=branch_id,
                order_type=order_type,
                items=items,
                delivery_address=delivery_address,
                special_notes=special_notes,
                deduplicate=deduplicate,
                status="placed",
            )

    @staticmethod
    async def _place_order_in_session(
        s: AsyncSession,
        *,
        customer_id: int,
        branch_id: int,
        order_type: str,
        items: List[Dict[str, Any]],
        delivery_address: Optional[str],
        special_notes: Optional[str],
        deduplicate: bool,
        status: str = "placed",
    ) -> Order:
        """Create one order inside the caller's open transaction.

        The transaction-free core of :meth:`place_order`, exposed so a caller whose
        order is one half of a larger unit of work can place it in its own
        transaction -- the approval flow (AgentFeatures §5) records the order and
        its approval row together. ``status`` lets that caller place the order
        straight into ``pending_approval`` instead of ``placed``; the validation,
        stock decrement and audit trail are otherwise identical.

        Validate stock, atomically decrement, and persist the order.

        Two passes on purpose. The first resolves and validates every line
        without writing anything; the second takes the stock and inserts. That
        keeps the duplicate check (which must not consume stock) between the
        two, and means a bad line aborts before the first decrement instead of
        halfway through.
        """
        if not items:
            raise ValidationError(
                "订单不能为空，请指定想要点的菜品。"
            )
        if order_type == "delivery" and not delivery_address:
            raise ValidationError(
                "外卖订单需要填写配送地址，请提供您的收货地址。"
            )
        planned: List[Dict[str, Any]] = []
        total_amount = 0.0
        for req in items:
            item_id = req.get('menu_item_id')
            qty = req.get('quantity', 1)
            if qty <= 0:
                raise ValidationError(f'菜品 #{item_id} 的数量至少为 1。')
            menu_item = await MenuRepo.get_item(s, item_id, with_variants=True)
            if menu_item is None:
                raise NotFoundError(f'菜品 #{item_id} 不存在。')
            if not menu_item.is_available:
                raise ConflictError(f"「{menu_item.name}」当前不可供应。")
            stock = await MenuRepo.get_stock_for_update(s, item_id, branch_id)
            available = stock.stock_quantity if stock else 0
            if available < qty:
                raise ConflictError(f'「{menu_item.name}」库存不足，仅剩 {available} 份（需要 {qty} 份，缺 {qty - available} 份）。')
            # Resolve variants against the DB *before* touching stock, so a
            # bad variant reference aborts without a half-applied order.
            variants = _resolve_variants(menu_item, req.get('selected_variants'))
            unit_price = _unit_price(menu_item.base_price, variants)
            total_amount += round(unit_price * qty, 2)
            planned.append({'menu_item_id': item_id, 'name': menu_item.name, 'quantity': qty, 'unit_price': unit_price, 'selected_variants': variants, 'item_notes': req.get('item_notes')})

        if deduplicate:
            existing = await OrderService._find_duplicate_order(
                s,
                customer_id=customer_id,
                branch_id=branch_id,
                order_type=order_type,
                delivery_address=delivery_address,
                planned=planned,
            )
            if existing is not None:
                # The same request, already placed: hand back the original
                # order instead of selling the customer a second one. No
                # stock is taken twice, no second order row, no second audit
                # entry -- the caller's reply is identical either way.
                return existing

        for line in planned:
            if not await MenuRepo.try_decrement_stock(s, line['menu_item_id'], branch_id, line['quantity']):
                raise ConflictError(f"「{line['name']}」库存已发生变化，请重试。")
        order = await OrderRepo.create_order(
            s,
            {
                'customer_id': customer_id,
                'branch_id': branch_id,
                'order_type': order_type,
                'status': status,
                'delivery_address': delivery_address if order_type == 'delivery' else None,
                'special_notes': special_notes,
                'total_amount': round(total_amount, 2),
            },
        )
        for p in planned:
            payload = dict(p)
            # ``name`` is carried only for error messages, not stored.
            payload.pop('name', None)
            await OrderRepo.add_order_item(s, {'order_id': order.id, **payload})
        # Audit trail (spec §9): who placed the order, in which branch.
        await AuditService.record(
            s,
            'order.created',
            entity_type='order',
            entity_id=order.id,
            branch_id=branch_id,
            actor=await UserRepo.get_customer_by_id(s, customer_id),
            details={
                'order_type': order_type,
                'status': order.status,
                'total_amount': round(total_amount, 2),
                'line_count': len(planned),
            },
        )
        return order

    @staticmethod
    async def _find_duplicate_order(
        s: AsyncSession,
        *,
        customer_id: int,
        branch_id: int,
        order_type: str,
        delivery_address: Optional[str],
        planned: List[Dict[str, Any]],
    ) -> Optional[Order]:
        """The still-open order this submission repeats, if there is one.

        A repeat submission of the same request (spec §9 重复请求) must return the
        order that already exists rather than place a second one. What counts as
        "the same request" is the item lines, the order type and the delivery
        address -- deliberately *not* the ids: two submissions minutes apart carry
        different drafts but describe the same order.

        An advisory lock serialises identical submissions, because the check and
        the insert are otherwise two transactions that can interleave. The lock is
        keyed on the request, not the customer, so unrelated orders still place
        concurrently.
        """
        signature = _order_signature(order_type, delivery_address, planned)
        await lock_request(
            s, lock_key("order-request", customer_id, branch_id, *signature)
        )
        since = datetime.now(timezone.utc) - timedelta(
            minutes=DEDUPLICATE_WINDOW_MINUTES
        )
        candidates = await OrderRepo.list_recent_orders_for_customer(
            s,
            customer_id=customer_id,
            branch_id=branch_id,
            statuses=OPEN_ORDER_STATUSES,
            since=since,
        )
        for candidate in candidates:
            stored = _order_signature(
                candidate.order_type,
                candidate.delivery_address,
                [
                    {
                        'menu_item_id': item.menu_item_id,
                        'quantity': item.quantity,
                        'selected_variants': item.selected_variants,
                        'item_notes': item.item_notes,
                    }
                    for item in candidate.items
                ],
            )
            if stored == signature:
                return candidate
        return None

    @staticmethod
    async def get_order(
        order_id: int, *, session: Optional[AsyncSession] = None
    ) -> Order:
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(
                s,
                order_id,
                with_items_menu_item=True,
                with_customer=True,
                with_branch=True,
            )
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            return order

    @staticmethod
    async def list_orders(
        *,
        customer_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        order_status: Optional[str] = None,
        limit: Optional[int] = None,
        session: Optional[AsyncSession] = None,
    ) -> List[Order]:
        async with transactional_session(session) as s:
            return await OrderRepo.list_orders(
                s,
                customer_id=customer_id,
                branch_id=branch_id,
                order_status=order_status,
                with_items_menu_item=True,
                with_customer=True,
                with_branch=True,
                limit=limit,
            )

    @staticmethod
    async def _apply_status_stock_effects(s, order: Order, new_status: str) -> None:
        """Move a cancelled order's stock, or take it back when restoring.

        Called only for a legal move (``update_status`` has already checked the
        transition matrix). Cancelling any non-terminal order returns its stock;
        restoring ``cancelled -> placed`` re-takes it atomically, raising
        :class:`ConflictError` if the stock has since been consumed elsewhere
        (the order then stays cancelled -- the write is rolled back).
        """
        if new_status == 'cancelled':
            for item in order.items:
                await MenuRepo.restore_stock(s, item.menu_item_id, order.branch_id, item.quantity)
        elif order.status == 'cancelled' and new_status == 'placed':
            failed = []
            for item in order.items:
                ok = await MenuRepo.try_decrement_stock(s, item.menu_item_id, order.branch_id, item.quantity)
                if not ok:
                    name = item.menu_item.name if item.menu_item else f'Item #{item.menu_item_id}'
                    failed.append(name)
            if failed:
                unique = list(dict.fromkeys(failed))
                raise ConflictError(f'无法恢复已取消的订单：以下菜品库存不足：{", ".join(unique)}。')

    @staticmethod
    async def update_status(
        order_id: int,
        new_status: str,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(
                s,
                order_id,
                with_items_menu_item=True,
                with_customer=True,
                with_branch=True,
            )
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            ensure_branch_scope(staff, order.branch_id, message='您没有权限管理该门店的订单。')
            if new_status not in VALID_ORDER_STATUSES:
                raise ValidationError(f'无效的状态 {new_status}，必须是以下之一：{list(VALID_ORDER_STATUSES)}。')
            if new_status == order.status:
                return order
            if new_status not in ORDER_TRANSITIONS.get(order.status, frozenset()):
                raise ValidationError(
                    f'不允许将订单从「{_status_label(order.status)}」变更为'
                    f'「{_status_label(new_status)}」。'
                )
            previous_status = order.status
            await OrderService._apply_status_stock_effects(s, order, new_status)
            await OrderRepo.update_order_status(s, order, new_status)
            # Audit trail (spec §9): status moves -- including cancellation and
            # the cancelled -> placed restore -- record actor and from/to.
            await AuditService.record(
                s,
                'order.status_changed',
                entity_type='order',
                entity_id=order.id,
                branch_id=order.branch_id,
                actor=staff,
                details={'from': previous_status, 'to': new_status},
            )
            # ``updated_at`` is server-generated (``onupdate``), so SQLAlchemy
            # expires it after the UPDATE; refresh here, inside the async
            # boundary, to keep the returned object fully populated.
            await s.refresh(order)
            return order

    # ------------------------------------------------------------------ #
    # Simulated KDS: the customer's "contact the kitchen" flow (§3).
    # ------------------------------------------------------------------ #
    @staticmethod
    async def get_customer_order(
        order_id: int,
        customer_id: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        """Fetch an order, proving the caller owns it.

        Raises :class:`NotFoundError` when the order does not exist and
        :class:`ForbiddenError` when it belongs to another customer. A caller
        never learns whether someone else's order id exists, but the two cases
        stay distinct so the API can answer 404 / 403 accordingly.
        """
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(s, order_id)
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            if order.customer_id != customer_id:
                raise ForbiddenError('您只能操作自己的订单。')
            return order

    @staticmethod
    async def cancel_order(
        order_id: int,
        customer_id: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        """Cancel one of the customer's own orders, releasing its stock.

        Only the customer who placed the order may cancel it, and only while it is
        still open (not delivered, not already cancelled). Idempotent: an
        already-cancelled order is returned unchanged. The stock is released
        exactly as a staff cancellation would.
        """
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(s, order_id, with_items_menu_item=True)
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            if order.customer_id != customer_id:
                raise ForbiddenError('您只能操作自己的订单。')
            if order.status == 'cancelled':
                return order
            if 'cancelled' not in ORDER_TRANSITIONS.get(order.status, frozenset()):
                raise ValidationError(
                    f'订单当前状态为「{_status_label(order.status)}」，无法取消。'
                )
            previous_status = order.status
            await OrderService._apply_status_stock_effects(s, order, 'cancelled')
            await OrderRepo.update_order_status(s, order, 'cancelled')
            await AuditService.record(
                s,
                'order.status_changed',
                entity_type='order',
                entity_id=order.id,
                branch_id=order.branch_id,
                actor=await UserRepo.get_customer_by_id(s, customer_id),
                details={'from': previous_status, 'to': 'cancelled', 'by': 'customer'},
            )
            await s.refresh(order)
            return order

    @staticmethod
    def _assert_kds_actionable(order: Order) -> None:
        """Reject a kitchen action on an order that is already over.

        Shared by hold and resume so both refuse the same two terminal cases:
        the order's own lifecycle is finished, or the kitchen task has already
        been served (``done``).
        """
        if order.status not in KDS_ACTIONABLE_ORDER_STATUSES:
            raise ValidationError(
                f'订单当前状态为「{_status_label(order.status)}」，无法再联系后厨。'
            )
        if order.kds_status == "done":
            raise ValidationError('该订单已出餐，无法再联系后厨。')

    @staticmethod
    async def hold_kds_task(
        order_id: int,
        customer_id: int,
        reason: Optional[str],
        *,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        """Park the order's kitchen task (``-> held``) at the customer's request.

        Idempotent: an order already ``held`` is returned unchanged rather than
        recording a second transition, so a double-click or a resent message
        moves nothing. Raises :class:`ValidationError` for an illegal move (for
        example a task already ``done``).
        """
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(s, order_id)
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            if order.customer_id != customer_id:
                raise ForbiddenError('您只能操作自己的订单。')
            OrderService._assert_kds_actionable(order)
            if order.kds_status == "held":
                return order
            if "held" not in KDS_TRANSITIONS.get(order.kds_status, frozenset()):
                raise ValidationError(
                    f'后厨任务当前为「{_kds_status_label(order.kds_status)}」，无法挂起。'
                )
            previous = order.kds_status
            held_reason = (reason or "").strip()[:KDS_HELD_REASON_MAX_LENGTH] or None
            await OrderRepo.update_kds_status(s, order, "held", held_reason=held_reason)
            await AuditService.record(
                s,
                'order.kds_changed',
                entity_type='order',
                entity_id=order.id,
                branch_id=order.branch_id,
                actor=await UserRepo.get_customer_by_id(s, customer_id),
                details={'from': previous, 'to': 'held', 'reason': held_reason},
            )
            await s.refresh(order)
            return order

    @staticmethod
    async def resume_kds_task(
        order_id: int,
        customer_id: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        """Send a held kitchen task back to the queue (``held -> queued``).

        Raises :class:`ValidationError` when the task was not held -- there is
        nothing to resume.
        """
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(s, order_id)
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            if order.customer_id != customer_id:
                raise ForbiddenError('您只能操作自己的订单。')
            OrderService._assert_kds_actionable(order)
            if order.kds_status != "held":
                raise ValidationError('后厨任务并未挂起，无需恢复上菜。')
            await OrderRepo.update_kds_status(s, order, "queued")
            await AuditService.record(
                s,
                'order.kds_changed',
                entity_type='order',
                entity_id=order.id,
                branch_id=order.branch_id,
                actor=await UserRepo.get_customer_by_id(s, customer_id),
                details={'from': 'held', 'to': 'queued'},
            )
            await s.refresh(order)
            return order

    @staticmethod
    async def update_kds_status(
        order_id: int,
        new_status: str,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Order:
        """Advance an order's simulated KDS state -- the kitchen's side.

        The kitchen moves ``queued -> cooking -> done`` and can send a held task
        back to the queue (``held -> queued``). Legal moves come from
        :data:`KDS_TRANSITIONS`; branch scoping and the "still actionable" rule
        mirror the order-status flow. A move to the current state is a no-op.
        """
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(
                s, order_id, with_items_menu_item=True, with_branch=True
            )
            if order is None:
                raise NotFoundError(f'订单 #{order_id} 不存在。')
            ensure_branch_scope(staff, order.branch_id, message='您没有权限管理该门店的订单。')
            if order.status not in KDS_ACTIONABLE_ORDER_STATUSES:
                raise ValidationError(
                    f'订单当前状态为「{_status_label(order.status)}」，无法操作后厨。'
                )
            if order.kds_status == "done":
                raise ValidationError('该订单已出餐。')
            if new_status not in KDS_TRANSITIONS:
                raise ValidationError(
                    f'无效的后厨状态 {new_status}，必须是以下之一：{list(KDS_TRANSITIONS)}。'
                )
            if new_status == order.kds_status:
                return order
            if new_status not in KDS_TRANSITIONS.get(order.kds_status, frozenset()):
                raise ValidationError(
                    f'不允许将后厨任务从「{_kds_status_label(order.kds_status)}」变更为'
                    f'「{_kds_status_label(new_status)}」。'
                )
            previous = order.kds_status
            await OrderRepo.update_kds_status(s, order, new_status)
            await AuditService.record(
                s,
                'order.kds_changed',
                entity_type='order',
                entity_id=order.id,
                branch_id=order.branch_id,
                actor=staff,
                details={'from': previous, 'to': new_status},
            )
            await s.refresh(order)
            return order


__all__ = [
    "OrderService",
    "VALID_ORDER_STATUSES",
    "ORDER_TRANSITIONS",
    "KDS_TRANSITIONS",
    "OPEN_ORDER_STATUSES",
    "DEDUPLICATE_WINDOW_MINUTES",
]
