"""Order business service.

Owns the transaction boundary and holds the single implementation of the order
business rules shared by the staff/customer order APIs and the Agent ordering
flow: price estimation, stock validation, atomic placement, status transitions
and cancellation / un-cancellation stock reversal.

Raises domain exceptions (never HTTP types); the API maps them to status codes
and the Agent turns them into user-facing replies.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.models.order import Order
from backend.app.models.user import StaffUser
from backend.app.repo.menu import MenuRepo
from backend.app.repo.order import OrderRepo

VALID_ORDER_STATUSES = ("placed", "preparing", "ready", "delivered", "cancelled")


def _variant_delta(variant: Dict[str, Any]) -> float:
    return float(variant.get("delta") or variant.get("price_delta") or 0.0)


def _unit_price(
    base_price: float, selected_variants: Optional[List[Dict[str, Any]]]
) -> float:
    return float(base_price) + sum(
        _variant_delta(v) for v in (selected_variants or [])
    )


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
                "Order cannot be empty. Please specify items to order."
            )
        async with transactional_session(session) as s:
            lines: List[Dict[str, Any]] = []
            total_amount = 0.0
            has_shortfall = False
            for req in items:
                item_id = req.get("menu_item_id")
                qty = req.get("quantity", 1)
                menu_item = await MenuRepo.get_item(s, item_id)
                if menu_item is None:
                    raise NotFoundError(f"Item #{item_id} does not exist.")
                stock = await MenuRepo.get_stock(s, item_id, branch_id)
                available = stock.stock_quantity if stock else 0
                unit_price = _unit_price(
                    menu_item.base_price, req.get("selected_variants")
                )
                shortfall = max(0, qty - available)
                if shortfall:
                    has_shortfall = True
                subtotal = round(unit_price * qty, 2)
                if not shortfall:
                    total_amount += unit_price * qty
                lines.append(
                    {"menu_item_id": item_id, "name": menu_item.name, "qty": qty, "unit_price": unit_price, "subtotal": subtotal, "available_quantity": available, "shortfall": shortfall})
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
        session: Optional[AsyncSession] = None,
    ) -> Order:
        # Validate stock, atomically decrement, and persist the order.
        if not items:
            raise ValidationError(
                "Order cannot be empty. Please specify items to order."
            )
        if order_type == "delivery" and not delivery_address:
            raise ValidationError(
                "Delivery orders require a delivery address. Please provide your address."
            )
        async with transactional_session(session) as s:
            processed: List[Dict[str, Any]] = []
            total_amount = 0.0
            for req in items:
                item_id = req.get('menu_item_id')
                qty = req.get('quantity', 1)
                if qty <= 0:
                    raise ValidationError(f'Quantity for item #{item_id} must be at least 1.')
                menu_item = await MenuRepo.get_item(s, item_id)
                if menu_item is None:
                    raise NotFoundError(f'Item #{item_id} does not exist.')
                if not menu_item.is_available:
                    raise ConflictError(f"'{menu_item.name}' is currently unavailable.")
                stock = await MenuRepo.get_stock_for_update(s, item_id, branch_id)
                available = stock.stock_quantity if stock else 0
                if available < qty:
                    raise ConflictError(f'Insufficient stock for {menu_item.name}. Only {available} available (requested {qty}, short by {qty - available}).')
                variants = req.get('selected_variants', []) or []
                unit_price = _unit_price(menu_item.base_price, variants)
                if not await MenuRepo.try_decrement_stock(s, item_id, branch_id, qty):
                    raise ConflictError(f'Stock changed for {menu_item.name}. Please retry.')
                total_amount += unit_price * qty
                processed.append({'menu_item_id': item_id, 'quantity': qty, 'unit_price': unit_price, 'selected_variants': variants, 'item_notes': req.get('item_notes')})
            order = await OrderRepo.create_order(
                s,
                {
                    'customer_id': customer_id,
                    'branch_id': branch_id,
                    'order_type': order_type,
                    'status': 'placed',
                    'delivery_address': delivery_address if order_type == 'delivery' else None,
                    'special_notes': special_notes,
                    'total_amount': round(total_amount, 2),
                },
            )
            for p in processed:
                await OrderRepo.add_order_item(s, {'order_id': order.id, **p})
            return order

    @staticmethod
    async def get_order(
        order_id: int, *, session: Optional[AsyncSession] = None
    ) -> Order:
        async with transactional_session(session) as s:
            order = await OrderRepo.get_order(
                s, order_id, with_items=True, with_customer=True, with_branch=True
            )
            if order is None:
                raise NotFoundError(f'Order #{order_id} not found.')
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
                with_items=True,
                with_customer=True,
                with_branch=True,
                limit=limit,
            )

    @staticmethod
    async def _apply_status_stock_effects(s, order: Order, new_status: str) -> None:
        if order.status != 'cancelled' and new_status == 'cancelled':
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
                raise ConflictError(f'Cannot un-cancel order: insufficient stock for item(s): {", ".join(unique)}.')
        elif order.status == 'cancelled' and new_status != 'placed':
            raise ValidationError("A cancelled order can only be un-cancelled to 'placed' status.")

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
                raise NotFoundError(f'Order #{order_id} not found.')
            if staff is not None and staff.branch_id and order.branch_id != staff.branch_id:
                raise ForbiddenError('You do not have permission to manage orders for this branch.')
            if new_status not in VALID_ORDER_STATUSES:
                raise ValidationError(f'Invalid status {new_status}. Must be one of {list(VALID_ORDER_STATUSES)}.')
            if new_status == order.status:
                return order
            await OrderService._apply_status_stock_effects(s, order, new_status)
            await OrderRepo.update_order_status(s, order, new_status)
            # ``updated_at`` is server-generated (``onupdate``), so SQLAlchemy
            # expires it after the UPDATE; refresh here, inside the async
            # boundary, to keep the returned object fully populated.
            await s.refresh(order)
            return order


__all__ = ["OrderService", "VALID_ORDER_STATUSES"]
