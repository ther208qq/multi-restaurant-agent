"""Audit log model.

Records who did what, to which entity, in which branch (spec §9 "权限与审计").
One row per audited operation, written inside the *same* transaction as the
operation it describes: a rolled-back order cancellation leaves no audit row,
and a committed one always has one.

The actor is denormalized (``actor_type`` / ``actor_name`` / ``actor_role``)
rather than kept as a foreign key, so the trail stays readable after a staff
account or customer is deleted, and so the same table can record both human
actors and system/agent ones.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from backend.app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Actor: "staff" | "customer" | "system".
    actor_type = Column(String(20), nullable=False, index=True)
    actor_id = Column(Integer, nullable=True)
    actor_name = Column(String(150), nullable=True)
    actor_role = Column(String(50), nullable=True)

    # Branch the operation belongs to; NULL for operations not tied to one.
    branch_id = Column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # e.g. "order.status_changed", "stock.updated", "reservation.table_reassigned"
    action = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(50), nullable=False, index=True)
    entity_id = Column(Integer, nullable=True, index=True)

    details = Column(JSONB, nullable=True)


__all__ = ["AuditLog"]
