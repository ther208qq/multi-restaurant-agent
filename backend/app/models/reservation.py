"""Reservation Model with PostgreSQL Exclusion Constraint Guard."""

from datetime import datetime, timedelta, timezone
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, TSTZRANGE
from sqlalchemy.orm import relationship

from backend.app.database import Base

# Statuses whose booking holds the table for its slot. The DB exclusion
# constraint below and the application-side overlap probe (``ReservationRepo``)
# both treat exactly these as occupying, so a *pending* booking blocks its
# table just like a confirmed one and an already-*seated* party keeps its table
# instead of silently freeing it for a new booking. Cancelling / completing a
# reservation drops it out of this set and releases the slot.
OCCUPYING_RESERVATION_STATUSES = ("pending", "confirmed", "seated")

# Rendered into the constraint's SQL predicate (a plain text() clause, so the
# tuple above cannot be bound as a parameter).
_OCCUPYING_STATUS_SQL = ", ".join(f"'{status}'" for status in OCCUPYING_RESERVATION_STATUSES)


class Reservation(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    table_id = Column(Integer, ForeignKey("tables.id", ondelete="CASCADE"), nullable=True, index=True)

    party_size = Column(Integer, nullable=False, default=2)
    start_time = Column(DateTime(timezone=True), nullable=False, index=True)
    end_time = Column(DateTime(timezone=True), nullable=False, index=True)
    duration_minutes = Column(Integer, nullable=False, default=90)
    seating_area_preference = Column(String(50), nullable=True, default="indoor")  # indoor, outdoor, rooftop, any

    # Status: pending (customer booked via Agent, awaiting staff confirmation),
    # confirmed, seated, completed, cancelled
    status = Column(String(50), nullable=False, default="pending", index=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # PostgreSQL TSTZRANGE for exclusion constraint
    booking_window = Column(TSTZRANGE, nullable=False)

    __table_args__ = (
        # Exclude concurrent overlapping table usage for every occupying status
        # (pending / confirmed / seated), so double-booking is rejected by the
        # database rather than by Python.
        ExcludeConstraint(
            ("table_id", "="),
            ("booking_window", "&&"),
            where=text(f"status IN ({_OCCUPYING_STATUS_SQL})"),
            name="no_overlapping_active_table_reservations",
            using="gist",
        ),
    )

    # Relationships
    customer = relationship("Customer", back_populates="reservations")
    branch = relationship("Branch", back_populates="reservations")
    table = relationship("Table", back_populates="reservations")

    @classmethod
    def calculate_window(cls, start: datetime, duration_minutes: int = 90):
        """Calculate start, end, and postgres range representation."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(minutes=duration_minutes)
        return start, end
