"""Order-approval model (``sdd/AgentFeatures.md`` §5).

When an order's ``special_notes`` read as a request for a freebie, the order is
not placed outright: it enters ``pending_approval`` and one ``OrderApproval`` row
records what the customer asked for so a manager can decide. On approval a gift
is issued and the order proceeds to ``placed``; on rejection the order is
cancelled and its stock released.

``remark`` snapshots the customer's own words; ``intent`` is the agent's reading
of them (today only ``gift_request``); ``status`` walks
``pending -> approved | rejected``.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from backend.app.database import Base

# Approval lifecycle.
APPROVAL_STATUSES = ("pending", "approved", "rejected")

# The agent-recognised intents that route an order into approval. Only one today;
# kept as a named constant so the intent node and the Service agree on the code.
GIFT_REQUEST_INTENT = "gift_request"


class OrderApproval(Base):
    __tablename__ = "order_approvals"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The customer's ``special_notes`` as written -- the "customer request" the
    # manager reads on the approval card.
    remark = Column(Text, nullable=True)
    intent = Column(String(50), nullable=False, default=GIFT_REQUEST_INTENT)
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # The order this approval gates (unidirectional: reaches the order only).
    order = relationship("Order")


__all__ = ["OrderApproval", "APPROVAL_STATUSES", "GIFT_REQUEST_INTENT"]
