"""Order placement tool: thin adapter over :class:`OrderService`.

Holds no business rules and no direct database access. Its only job is to keep
the caller-facing dict contract (``success`` / ``error`` plus the stock
shortfall fields) that the LangGraph order node depends on, while all real
work -- pricing, stock validation, atomic decrement, order persistence --
happens inside the Service via :meth:`OrderService.preview_order` and
:meth:`OrderService.place_order`.
"""

from typing import Any, Dict, List, Optional

from backend.app.exceptions import DomainError
from backend.app.service.order import OrderService

EMPTY_ORDER_ERROR = "订单不能为空，请指定想要点的菜品。"
MISSING_ADDRESS_ERROR = "外卖订单需要填写配送地址，请提供您的收货地址。"


def _first_shortfall(lines: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the first preview line whose requested quantity exceeds stock."""
    for line in lines:
        if line.get("shortfall"):
            return line
    return None


def _shortfall_response(short: Dict[str, Any]) -> Dict[str, Any]:
    """Reproduce the legacy out-of-stock / insufficient-stock response shapes."""
    name = short["name"]
    available = short["available_quantity"]
    if available == 0:
        return {
            "success": False,
            "error": f"「{name}」在本门店已售罄。",
            "out_of_stock_item": name,
            "available_quantity": 0,
        }
    return {
        "success": False,
        "error": (
            f"「{name}」库存不足，仅剩 {available} 份"
            f"（需要 {short['qty']} 份，缺 {short['shortfall']} 份）。"
            "请调整数量后重试。"
        ),
        "shortfall_item": name,
        "available_quantity": available,
        "shortfall": short["shortfall"],
    }


async def create_order_atomic(
    customer_id: int,
    branch_id: int,
    order_type: str,
    items: List[Dict[str, Any]],
    delivery_address: Optional[str] = None,
    special_notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Place an order through OrderService, preserving the legacy dict contract.

    Args:
        customer_id: Customer ID placing the order.
        branch_id: Branch ID from which the order is placed.
        order_type: "dine_in", "takeaway", or "delivery".
        items: List of dicts, each with ``menu_item_id``, ``quantity`` and
            optional ``selected_variants`` / ``item_notes``.
        delivery_address: Required if ``order_type == 'delivery'``.
        special_notes: Optional special instructions.

    Returns:
        A dict with ``success`` and either ``order_id`` / ``total_amount`` /
        ``order_type`` / ``status`` / ``items``, or a descriptive ``error``
        (plus, for stock problems, ``shortfall`` / ``available_quantity`` and
        ``shortfall_item`` or ``out_of_stock_item``).
    """
    if not items:
        return {"success": False, "error": EMPTY_ORDER_ERROR}
    if order_type == "delivery" and not delivery_address:
        return {"success": False, "error": MISSING_ADDRESS_ERROR}

    try:
        preview = await OrderService.preview_order(branch_id, items)
    except DomainError as exc:
        return {"success": False, "error": exc.message}

    short = _first_shortfall(preview["items"])
    if short is not None:
        return _shortfall_response(short)

    try:
        order = await OrderService.place_order(
            customer_id,
            branch_id,
            order_type,
            items,
            delivery_address=delivery_address,
            special_notes=special_notes,
            # The Agent is the only place an order can be submitted twice by
            # accident -- a message sent twice, or a small model emitting the
            # same tool call twice in one turn. Repeat submissions of a request
            # that is still open return the order that already exists (spec §9
            # 重复请求) instead of selling a second one.
            deduplicate=True,
        )
    except DomainError as exc:
        return {"success": False, "error": exc.message}

    order_items = [
        {
            "menu_item_id": line["menu_item_id"],
            "menu_item_name": line["name"],
            "quantity": line["qty"],
            "unit_price": line["unit_price"],
            "selected_variants": req.get("selected_variants") or [],
            "item_notes": req.get("item_notes"),
        }
        for line, req in zip(preview["items"], items)
    ]
    return {
        "success": True,
        "order_id": order.id,
        "status": order.status,
        "total_amount": order.total_amount,
        "order_type": order.order_type,
        "items": order_items,
    }

