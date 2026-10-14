"""Staff Reservations Management Routes.

Thin HTTP layer: request parsing, branch scoping and response shaping only.
All reservation business rules (table selection, the availability query, overlap
probing, the exclusion-constraint-guarded insert, lifecycle transitions and
rescheduling) live in :class:`backend.app.service.reservation.ReservationService`;
domain exceptions raised there are mapped to HTTP status codes by the global
exception handler.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.reservation import (
    ReservationAvailabilityItem,
    ReservationResponse,
    ReservationUpdate,
    parse_slot,
)
from backend.app.service.reservation import ReservationService

router = APIRouter(prefix="/staff/reservations", tags=["Staff Reservations Management"])


@router.get("", response_model=List[ReservationResponse])
async def list_staff_reservations(
    branch_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_filter: Optional[str] = Query(None, alias="date"),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List reservations, filterable by branch, status, and date."""
    reservations = await ReservationService.list_reservations(
        staff=current_staff,
        branch_id=branch_id,
        status=status_filter,
        date=date_filter,
        session=db,
    )
    return [ReservationResponse.from_reservation(r) for r in reservations]


@router.get("/availability", response_model=List[ReservationAvailabilityItem])
async def list_table_availability(
    branch_id: int = Query(...),
    date: str = Query(..., description="YYYY-MM-DD"),
    time: str = Query(..., description="HH:MM (24-hour)"),
    party_size: int = Query(2),
    duration_minutes: int = Query(90),
    seating_area: Optional[str] = Query(None),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List the tables bookable at a given slot (read-only availability query)."""
    start_time = parse_slot(date, time)

    tables = await ReservationService.list_available_tables(
        branch_id=branch_id,
        start_time=start_time,
        party_size=party_size,
        duration_minutes=duration_minutes,
        seating_area=seating_area,
        staff=current_staff,
        session=db,
    )
    return [ReservationAvailabilityItem.from_table(t) for t in tables]


@router.patch("/{res_id}", response_model=ReservationResponse)
async def update_reservation(
    res_id: int,
    payload: ReservationUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Update reservation status / table / notes, or reschedule it."""
    values = {}
    if payload.status:
        values["status"] = payload.status
    if payload.table_id is not None:
        values["table_id"] = payload.table_id
    if payload.notes is not None:
        values["notes"] = payload.notes
    if payload.start_time is not None:
        values["start_time"] = payload.start_time
    if payload.party_size is not None:
        values["party_size"] = payload.party_size
    if payload.duration_minutes is not None:
        values["duration_minutes"] = payload.duration_minutes
    if payload.seating_area_preference is not None:
        values["seating_area_preference"] = payload.seating_area_preference

    reservation = await ReservationService.update_reservation(
        res_id,
        values,
        staff=current_staff,
        session=db,
    )
    return ReservationResponse.from_reservation(reservation)
