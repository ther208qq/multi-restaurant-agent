"""Gift (voucher) Schemas."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class GiftResponse(BaseModel):
    """One issued gift voucher."""

    id: int
    branch_id: int
    item_id: int
    item_name: Optional[str] = None
    source_type: str
    source_id: int
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_gift(cls, gift: Any) -> "GiftResponse":
        # ``item`` is read from the instance dict so an unloaded relationship
        # never triggers a lazy load in async context (see schemas/order.py).
        item = gift.__dict__.get("item")
        return cls(
            id=gift.id,
            branch_id=gift.branch_id,
            item_id=gift.item_id,
            item_name=item.name if item else None,
            source_type=gift.source_type,
            source_id=gift.source_id,
            status=gift.status,
            created_at=gift.created_at,
        )
