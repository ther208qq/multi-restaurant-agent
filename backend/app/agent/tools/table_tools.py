"""Table Availability and Auto-Confirm Reservation Tool.

Thin adapter over :class:`backend.app.service.reservation.ReservationService`:
it normalises tool arguments and converts the service's domain exceptions into
the dict shape the Agent nodes expect. All booking rules (candidate selection,
overlap probing, the exclusion-constraint-guarded insert) live in the service;
this module never opens a session or touches SQLAlchemy directly.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.app.exceptions import (
    DomainError,
    ReservationRaceError,
    TableUnavailableError,
)
from backend.app.service.reservation import ReservationService


async def check_table_availability_and_book(
    customer_id: int,
    branch_id: int,
    start_time: datetime,
    party_size: int,
    duration_minutes: int = 90,
    seating_area: Optional[str] = "indoor",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Find an eligible table and auto-confirm reservation, or explain why not.

    Returns a dict with ``success`` plus either the confirmed reservation
    details, or an ``error`` string — flagged ``alternatives_available`` when
    another seating area is free, or ``race_conflict`` when a concurrent booking
    won the database exclusion-constraint race.
    """
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    try:
        reservation = await ReservationService.book_table(
            customer_id=customer_id,
            branch_id=branch_id,
            start_time=start_time,
            party_size=party_size,
            duration_minutes=duration_minutes,
            seating_area=seating_area,
            notes=notes,
        )
    except TableUnavailableError as exc:
        return {"success": False, "error": exc.message, "alternatives_available": True}
    except ReservationRaceError as exc:
        return {"success": False, "error": exc.message, "race_conflict": True}
    except DomainError as exc:
        return {"success": False, "error": exc.message}

    return {
        "success": True,
        "reservation_id": reservation.id,
        "status": reservation.status,
        "branch_name": reservation.branch.name,
        "table_label": reservation.table.label,
        "seating_area": reservation.table.seating_area,
        "date": reservation.start_time.strftime("%Y-%m-%d"),
        "time": reservation.start_time.strftime("%I:%M %p"),
        "party_size": party_size,
        "duration_minutes": duration_minutes,
    }
