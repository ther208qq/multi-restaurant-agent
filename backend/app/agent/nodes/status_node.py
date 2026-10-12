"""Status Check Node for Customer Orders and Reservations.

Reads the customer's recent orders and reservations through OrderService and
ReservationService (each owns its own session), so this node holds no query of
its own and no database-session handling.
"""

from typing import Any, Dict
from langchain_core.messages import AIMessage

from backend.app.agent.labels import (
    ORDER_STATUS_LABELS,
    RESERVATION_STATUS_LABELS,
    order_type_label,
)
from backend.app.service.order import OrderService
from backend.app.service.reservation import ReservationService


async def status_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve customer's active orders and upcoming reservations scoped strictly to their account."""
    customer_id = state.get("customer_id")
    branch_id = state.get("branch_id")

    # 1. Fetch recent orders for this customer at this branch
    orders = await OrderService.list_orders(
        customer_id=customer_id, branch_id=branch_id, limit=3
    )

    # 2. Fetch upcoming reservations for this customer at this branch
    reservations = await ReservationService.list_reservations(
        customer_id=customer_id, branch_id=branch_id, descending=True, limit=3
    )

    sections = []

    if orders:
        order_lines = ["📦 **您的近期订单：**"]
        for o in orders:
            status_label = ORDER_STATUS_LABELS.get(o.status, o.status)
            order_lines.append(
                f"• 订单 #{o.id}（{order_type_label(o.order_type)}）：{status_label} — ${o.total_amount:.2f}（{len(o.items)} 项）"
            )
        sections.append("\n".join(order_lines))

    if reservations:
        res_lines = ["🍽️ **您的预订：**"]
        for r in reservations:
            status_label = RESERVATION_STATUS_LABELS.get(r.status, r.status)
            table_info = f"餐桌 {r.table.label}" if r.table else "尚未分配餐桌"
            date_time = r.start_time.strftime("%Y-%m-%d %H:%M")
            res_lines.append(
                f"• 预订 #{r.id}：{status_label}，{date_time}，{r.party_size} 位（{table_info}）"
            )
        sections.append("\n".join(res_lines))

    if not sections:
        reply = "您在本门店暂时没有进行中的订单或预订。需要查看菜单或预订座位吗？"
    else:
        reply = "\n\n".join(sections) + "\n\n还需要其他帮助吗？"

    return {
        **state,
        "active_flow": "none",
        "last_reply": reply,
        "messages": [AIMessage(content=reply)],
    }
