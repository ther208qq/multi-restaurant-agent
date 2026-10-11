"""Status Check Node for Customer Orders and Reservations.

Reads the customer's recent orders and reservations through OrderService and
ReservationService (each owns its own session), so this node holds no query of
its own and no database-session handling.
"""

from typing import Any, Dict
from langchain_core.messages import AIMessage

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
        order_lines = ["📦 **Your Recent Orders:**"]
        for o in orders:
            status_emoji = {
                "placed": "⏳ Placed",
                "preparing": "🍳 Preparing",
                "ready": "🔔 Ready for Pickup/Delivery",
                "delivered": "✅ Delivered/Completed",
                "cancelled": "❌ Cancelled",
            }.get(o.status, o.status.title())

            order_lines.append(
                f"• Order #{o.id} ({o.order_type.replace('_', ' ').title()}): {status_emoji} — ${o.total_amount:.2f} ({len(o.items)} item(s))"
            )
        sections.append("\n".join(order_lines))

    if reservations:
        res_lines = ["🍽️ **Your Reservations:**"]
        for r in reservations:
            status_emoji = {
                "confirmed": "✅ Confirmed",
                "pending": "⏳ Pending",
                "seated": "🍷 Seated",
                "completed": "🏁 Completed",
                "cancelled": "❌ Cancelled",
            }.get(r.status, r.status.title())

            table_info = f"Table {r.table.label}" if r.table else "Table assignment pending"
            date_time = r.start_time.strftime("%b %d, %Y at %I:%M %p")
            res_lines.append(
                f"• Booking #{r.id}: {status_emoji} on {date_time} for {r.party_size} guests ({table_info})"
            )
        sections.append("\n".join(res_lines))

    if not sections:
        reply = "You don't have any active orders or reservations at this branch yet. Would you like to check out the menu or book a table?"
    else:
        reply = "\n\n".join(sections) + "\n\nCan I help you with anything else?"

    return {
        **state,
        "active_flow": "none",
        "last_reply": reply,
        "messages": [AIMessage(content=reply)],
    }
