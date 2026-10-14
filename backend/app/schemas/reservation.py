"""Reservation Schemas."""

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict

from backend.app.exceptions import ValidationError

SLOT_FORMAT_ERROR = '日期或时间格式不正确，日期为 YYYY-MM-DD，时间为 HH:MM。'


def _loaded(obj: Any, name: str) -> Any:
    """Read an already-loaded relationship without triggering a lazy load."""
    return obj.__dict__.get(name)


def parse_slot(date: str, time: str) -> datetime:
    """Combine a ``YYYY-MM-DD`` date and an ``HH:MM`` time into a UTC datetime.

    Wall-clock input is stored as-is and treated as UTC -- the convention the
    staff form and the Agent's booking node already follow, so a slot entered in
    one surface is comparable with a booking made in another.
    """
    try:
        return datetime.strptime(f'{date} {time}', '%Y-%m-%d %H:%M').replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        raise ValidationError(SLOT_FORMAT_ERROR)


class ReservationCreate(BaseModel):
    branch_id: int
    party_size: int = 2
    requested_date: str  # YYYY-MM-DD
    requested_time: str  # HH:MM (24-hour)
    duration_minutes: int = 90
    seating_area_preference: Optional[str] = "indoor"  # indoor, outdoor, rooftop, any
    notes: Optional[str] = None

    def to_start_time(self) -> datetime:
        return parse_slot(self.requested_date, self.requested_time)


class ReservationUpdate(BaseModel):
    status: Optional[str] = None  # pending, confirmed, seated, completed, cancelled
    table_id: Optional[int] = None
    notes: Optional[str] = None
    # Reschedule fields. ``start_time`` is an ISO-8601 timestamp; a new slot
    # re-derives the booking window and re-checks table availability.
    start_time: Optional[datetime] = None
    party_size: Optional[int] = None
    duration_minutes: Optional[int] = None
    seating_area_preference: Optional[str] = None


class ReservationAvailabilityItem(BaseModel):
    """One bookable table for a requested slot."""

    table_id: int
    label: str
    seating_area: Optional[str] = None
    capacity: int

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_table(cls, table: Any) -> "ReservationAvailabilityItem":
        """Build the availability row from a ``Table`` ORM object."""
        return cls(
            table_id=table.id,
            label=table.label,
            seating_area=table.seating_area,
            capacity=table.capacity,
        )


class ReservationResponse(BaseModel):
    id: int
    customer_id: int
    branch_id: int
    table_id: Optional[int] = None
    party_size: int
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    seating_area_preference: Optional[str] = None
    status: str
    notes: Optional[str] = None
    created_at: datetime
    table_label: Optional[str] = None
    branch_name: Optional[str] = None
    customer_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_reservation(
        cls, reservation: Any, *, customer_name: Optional[str] = None
    ) -> "ReservationResponse":
        """Build the response from a ``Reservation`` ORM object.

        Shared by the staff reservation manager and the customer's own booking
        endpoints. ``customer_name`` is passed by the customer endpoints (read
        from the authenticated caller) and otherwise falls back to the loaded
        relationship.
        """
        table = _loaded(reservation, "table")
        branch = _loaded(reservation, "branch")
        customer = _loaded(reservation, "customer")
        return cls(
            id=reservation.id,
            customer_id=reservation.customer_id,
            branch_id=reservation.branch_id,
            table_id=reservation.table_id,
            party_size=reservation.party_size,
            start_time=reservation.start_time,
            end_time=reservation.end_time,
            duration_minutes=reservation.duration_minutes,
            seating_area_preference=reservation.seating_area_preference,
            status=reservation.status,
            notes=reservation.notes,
            created_at=reservation.created_at,
            table_label=table.label if table else None,
            branch_name=branch.name if branch else None,
            customer_name=customer_name
            if customer_name is not None
            else (customer.name if customer else None),
        )
