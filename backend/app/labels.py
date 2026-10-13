"""Chinese labels for the canonical enum codes used across the backend.

The database and the API deal in canonical codes (``dine_in``, ``placed``,
``rooftop``, ...). Both the Agent nodes and the Service layer need to render
them as Chinese in user-facing text, so the mapping lives here once — in a leaf
module that imports nothing — instead of being re-spelled per layer.
"""

ORDER_TYPE_LABELS = {
    "dine_in": "堂食",
    "takeaway": "外带",
    "delivery": "外卖",
}

ORDER_STATUS_LABELS = {
    "placed": "⏳ 已下单",
    "confirmed": "📋 已确认",
    "preparing": "🍳 制作中",
    "ready": "🔔 待取餐/配送",
    "delivered": "✅ 已送达/已完成",
    "cancelled": "❌ 已取消",
}

RESERVATION_STATUS_LABELS = {
    "confirmed": "✅ 已确认",
    "pending": "⏳ 待确认",
    "seated": "🍷 已入座",
    "completed": "🏁 已完成",
    "cancelled": "❌ 已取消",
}

SEATING_AREA_LABELS = {
    "indoor": "室内",
    "outdoor": "户外露台",
    "rooftop": "天台花园",
}


def order_type_label(order_type: str) -> str:
    """Chinese label for an order type, falling back to the raw code."""
    return ORDER_TYPE_LABELS.get(order_type, order_type)


def seating_area_label(area: str) -> str:
    """Chinese label for a seating area, falling back to the raw code."""
    return SEATING_AREA_LABELS.get(area, area)


__all__ = [
    "ORDER_TYPE_LABELS",
    "ORDER_STATUS_LABELS",
    "RESERVATION_STATUS_LABELS",
    "SEATING_AREA_LABELS",
    "order_type_label",
    "seating_area_label",
]
