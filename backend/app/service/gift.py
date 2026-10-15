"""Gift / surplus-inventory business service.

Holds the single definition of two rules shared by the booking upsell flow
(feature 2) and the order-approval flow (feature 3):

* **what counts as surplus inventory** -- a dessert or drink category item that
  still holds :data:`SURPLUS_STOCK_THRESHOLD` or more units at the branch, so
  giving one away does not eat into what the branch needs to sell; and
* **how a voucher is issued** -- take one unit of the item's stock atomically,
  then record the ``gifts`` row, so a voucher always corresponds to inventory
  that was actually reserved.

The availability query (:meth:`GiftService.get_surplus_inventory`) owns its
transaction. :func:`issue_gift` deliberately does not: it is called from inside
the booking / approval transaction so that "reserve the table / place the order
*and* issue the voucher" either both happen or neither does.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import ConflictError, NotFoundError, ValidationError
from backend.app.models.gift import Gift
from backend.app.repo.gift import GiftRepo
from backend.app.repo.menu import MenuRepo
from backend.app.service.audit import AuditService

# The upsell treats a dessert / drink item as "surplus" when the branch still
# holds at least this many units of it. Matched as a substring against the
# category name so both "饮品" and a branch's own "特色饮品" qualify.
SURPLUS_CATEGORY_KEYWORDS = ("甜点", "饮品")
SURPLUS_STOCK_THRESHOLD = 5


def is_surplus_category(category_name: Optional[str]) -> bool:
    """Whether a category counts toward the surplus (giftable) pool."""
    if not category_name:
        return False
    return any(keyword in category_name for keyword in SURPLUS_CATEGORY_KEYWORDS)


def surplus_payload(item: Any, category_name: str, stock_quantity: int) -> Dict[str, Any]:
    """The gift-candidate shape the Agent offer and the API response share."""
    return {
        "item_id": item.id,
        "name": item.name,
        "category": category_name,
        "stock_quantity": stock_quantity,
    }


async def issue_gift(
    s: AsyncSession,
    *,
    branch_id: int,
    item_id: int,
    source_type: str,
    source_id: int,
    actor: Optional[Any] = None,
) -> Gift:
    """Reserve one unit of ``item_id`` and record the voucher -- same transaction.

    Runs inside the caller's open transaction so the voucher and the booking /
    order it belongs to commit together. Raises :class:`NotFoundError` for an
    unknown item, :class:`ValidationError` when the item belongs to another
    branch, and :class:`ConflictError` when it is unavailable or out of stock.
    """
    item = await MenuRepo.get_item(s, item_id, with_category=True)
    if item is None:
        raise NotFoundError(f"赠品菜品 #{item_id} 不存在。")
    category = item.category
    if category is None or category.branch_id != branch_id:
        raise ValidationError("该菜品不属于本门店，无法作为赠品。")
    if not item.is_available:
        raise ConflictError(f"「{item.name}」当前不可供应，无法作为赠品。")
    if not await MenuRepo.try_decrement_stock(s, item_id, branch_id, 1):
        raise ConflictError(f"「{item.name}」库存不足，无法作为赠品。")

    gift = await GiftRepo.create_gift(
        s,
        {
            "branch_id": branch_id,
            "item_id": item_id,
            "source_type": source_type,
            "source_id": source_id,
            "status": "pending",
        },
    )
    # Attach the item we already fetched, so the response builder can read its
    # name without firing a lazy load in async context.
    gift.item = item
    await AuditService.record(
        s,
        "gift.issued",
        entity_type="gift",
        entity_id=gift.id,
        branch_id=branch_id,
        actor=actor,
        details={
            "item_id": item_id,
            "item_name": item.name,
            "source_type": source_type,
            "source_id": source_id,
        },
    )
    return gift


class GiftService:
    @staticmethod
    async def get_surplus_inventory(
        branch_id: int,
        *,
        min_stock: int = SURPLUS_STOCK_THRESHOLD,
        session: Optional[AsyncSession] = None,
    ) -> List[Dict[str, Any]]:
        """Giftable dessert / drink items at the branch, most abundant first."""
        async with transactional_session(session) as s:
            rows = await MenuRepo.list_surplus_candidates(
                s,
                branch_id,
                category_keywords=list(SURPLUS_CATEGORY_KEYWORDS),
                min_stock=min_stock,
            )
            return [surplus_payload(item, name, qty) for item, name, qty in rows]

    @staticmethod
    async def redeem_gift(
        gift_id: int, *, session: Optional[AsyncSession] = None
    ) -> Gift:
        """Mark an issued voucher as collected by the guest."""
        async with transactional_session(session) as s:
            gift = await GiftRepo.get_gift(s, gift_id, with_item=True)
            if gift is None:
                raise NotFoundError(f"赠品 #{gift_id} 不存在。")
            if gift.status == "redeemed":
                return gift
            updated = await GiftRepo.update_status(s, gift, "redeemed")
            await AuditService.record(
                s,
                "gift.redeemed",
                entity_type="gift",
                entity_id=gift.id,
                branch_id=gift.branch_id,
                details={"item_id": gift.item_id},
            )
            return updated


__all__ = [
    "GiftService",
    "issue_gift",
    "is_surplus_category",
    "surplus_payload",
    "SURPLUS_CATEGORY_KEYWORDS",
    "SURPLUS_STOCK_THRESHOLD",
]
