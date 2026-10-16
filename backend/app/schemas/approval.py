"""Order-approval Schemas (``sdd/AgentFeatures.md`` §5).

The staff board's view of a pending decision: the customer's request, the agent's
recognised intent and its inventory-based gift suggestion, and the gated order.
The decision endpoints return the resulting approval in the same shape.
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict

from backend.app.schemas.order import OrderResponse


class GiftCandidate(BaseModel):
    """One item a manager may give away, with its current branch stock."""

    item_id: int
    name: str
    category: Optional[str] = None
    stock_quantity: int

    @classmethod
    def from_payload(cls, item: dict) -> "GiftCandidate":
        return cls(**item)


class GiftSuggestion(BaseModel):
    """The agent's gift proposal for a pending approval."""

    message: str
    recommended_item: GiftCandidate
    candidates: List[GiftCandidate] = []

    @classmethod
    def from_payload(cls, payload: dict) -> "GiftSuggestion":
        return cls(
            message=payload["message"],
            recommended_item=GiftCandidate.from_payload(payload["recommended_item"]),
            candidates=[
                GiftCandidate.from_payload(c) for c in payload.get("candidates", [])
            ],
        )


class ApprovalDecision(BaseModel):
    """The body of an approve call: the manager's explicitly chosen gift."""

    gift_id: int


class ApprovalResponse(BaseModel):
    """One approval as the staff board sees it."""

    id: int
    order_id: int
    remark: Optional[str] = None
    intent: str
    status: str
    created_at: datetime
    order: Optional[OrderResponse] = None
    suggestion: Optional[GiftSuggestion] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_approval(
        cls, approval: Any, *, suggestion: Optional[GiftSuggestion] = None
    ) -> "ApprovalResponse":
        # The gated order is read from the instance dict so an unloaded
        # relationship never triggers a lazy load in async context.
        order = approval.__dict__.get("order")
        return cls(
            id=approval.id,
            order_id=approval.order_id,
            remark=approval.remark,
            intent=approval.intent,
            status=approval.status,
            created_at=approval.created_at,
            order=OrderResponse.from_order(order) if order is not None else None,
            suggestion=suggestion,
        )


__all__ = [
    "ApprovalDecision",
    "ApprovalResponse",
    "GiftCandidate",
    "GiftSuggestion",
]
