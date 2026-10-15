"""Surplus-inventory tool: thin adapter over :class:`GiftService`.

Keeps no business rule and no database access of its own -- it only shapes the
service's surplus inventory into the dict list the upsell offer node consumes.
"""

from typing import Any, Dict, List, Optional

from backend.app.service.gift import GiftService


async def get_surplus_inventory(
    branch_id: int, *, min_stock: Optional[int] = None
) -> Dict[str, Any]:
    """Dessert / drink items the branch can afford to give away.

    Returns ``{"success": True, "items": [{item_id, name, category,
    stock_quantity}, ...]}``, most abundant first, or an empty list when the
    branch has no surplus to offer.
    """
    kwargs = {} if min_stock is None else {"min_stock": min_stock}
    items: List[Dict[str, Any]] = await GiftService.get_surplus_inventory(
        branch_id, **kwargs
    )
    return {"success": True, "items": items}
