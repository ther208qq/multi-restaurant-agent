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

# Simulated Kitchen Display System states (AgentFeatures §3).
KDS_STATUS_LABELS = {
    "queued": "🕐 待制作",
    "cooking": "🍳 制作中",
    "held": "⏸ 已挂起",
    "done": "✅ 已出餐",
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

# Audited operations (see ``service/audit.py``). Read-only views render these.
AUDIT_ACTION_LABELS = {
    "order.created": "下单",
    "order.status_changed": "订单状态变更",
    "order.kds_changed": "后厨任务状态变更",
    "reservation.created": "新建预订",
    "reservation.status_changed": "预订状态变更",
    "reservation.rescheduled": "预改约",
    "reservation.table_reassigned": "改派餐桌",
    "stock.updated": "库存调整",
    "menu_category.created": "新建菜品分类",
    "menu_item.created": "新建菜品",
    "menu_item.updated": "修改菜品",
    "branch.created": "新建门店",
    "branch.updated": "修改门店",
    "table.created": "新建餐桌",
    "table.updated": "修改餐桌",
    "gift.issued": "发放赠品",
    "gift.redeemed": "核销赠品",
}

AUDIT_ENTITY_LABELS = {
    "order": "订单",
    "reservation": "预订",
    "menu_item": "菜品",
    "menu_category": "菜品分类",
    "branch": "门店",
    "table": "餐桌",
    "gift": "赠品",
}


def order_type_label(order_type: str) -> str:
    """Chinese label for an order type, falling back to the raw code."""
    return ORDER_TYPE_LABELS.get(order_type, order_type)


def audit_action_label(action: str) -> str:
    """Chinese label for an audited action, falling back to the raw code."""
    return AUDIT_ACTION_LABELS.get(action, action)


def audit_entity_label(entity_type: str) -> str:
    """Chinese label for an audited entity type, falling back to the raw code."""
    return AUDIT_ENTITY_LABELS.get(entity_type, entity_type)


def seating_area_label(area: str) -> str:
    """Chinese label for a seating area, falling back to the raw code."""
    return SEATING_AREA_LABELS.get(area, area)


__all__ = [
    "ORDER_TYPE_LABELS",
    "ORDER_STATUS_LABELS",
    "RESERVATION_STATUS_LABELS",
    "SEATING_AREA_LABELS",
    "AUDIT_ACTION_LABELS",
    "AUDIT_ENTITY_LABELS",
    "order_type_label",
    "seating_area_label",
    "audit_action_label",
    "audit_entity_label",
]
