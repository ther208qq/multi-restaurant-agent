"""Gift (promotional voucher) model.

A ``Gift`` is the redemption voucher created when a customer accepts a booking
upsell offer (feature 2) or when a manager approves an order that asked for a
freebie (feature 3). Issuing one takes the item's branch stock by one, so the
voucher is a real reservation of inventory rather than a loose promise.

``status`` tracks whether the guest has actually collected it: ``pending`` from
issue until a staff member hands it over, then ``redeemed``.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import relationship

from backend.app.database import Base

# What created the voucher: a table booking (an accepted upsell offer) or an
# order (a manager-approved freebie). ``source_id`` points at the matching row.
GIFT_SOURCE_TYPES = ("order", "reservation")

# Voucher lifecycle.
GIFT_STATUSES = ("pending", "redeemed")


class Gift(Base):
    __tablename__ = "gifts"

    id = Column(Integer, primary_key=True, index=True)
    branch_id = Column(
        Integer,
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("menu_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type = Column(String(20), nullable=False)  # "order" | "reservation"
    source_id = Column(Integer, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships (unidirectional: the voucher reaches its branch and item).
    branch = relationship("Branch")
    item = relationship("MenuItem")


__all__ = ["Gift", "GIFT_SOURCE_TYPES", "GIFT_STATUSES"]
