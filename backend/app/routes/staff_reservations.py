"""Staff Reservations Management Routes.

Thin HTTP layer: request parsing, branch scoping and response shaping only.
All reservation business rules (table selection, overlap probing, the
auto-confirm insert, staff updates) live in
:class:`backend.app.service.reservation.ReservationService`; domain exceptions
raised there are mapped to HTTP status codes by the global exception handler.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.reservation import Reservation
from backend.app.models.user import StaffUser
from backend.app.schemas.reservation import ReservationResponse, ReservationUpdate
from backend.app.service.reservation import ReservationService

router = APIRouter(prefix="/staff/reservations", tags=["Staff Reservations Management"])


def _format_reservation_response(r: Reservation) -> ReservationResponse:
    return ReservationResponse(
        id=r.id,
        customer_id=r.customer_id,
        branch_id=r.branch_id,
        table_id=r.table_id,
        party_size=r.party_size,
        start_time=r.start_time,
        end_time=r.end_time,
        duration_minutes=r.duration_minutes,
        seating_area_preference=r.seating_area_preference,
        status=r.status,
        notes=r.notes,
        created_at=r.created_at,
        table_label=r.table.label if r.table else None,
        branch_name=r.branch.name if r.branch else None,
        customer_name=r.customer.name if r.customer else None,
    )


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
    return [_format_reservation_response(r) for r in reservations]


@router.patch("/{res_id}", response_model=ReservationResponse)
async def update_reservation(
    res_id: int,
    payload: ReservationUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Update reservation status, assign table, or add staff notes."""
    values = {}
    if payload.status:
        values["status"] = payload.status
    if payload.table_id is not None:
        values["table_id"] = payload.table_id
    if payload.notes is not None:
        values["notes"] = payload.notes

    reservation = await ReservationService.update_reservation(
        res_id,
        values,
        staff=current_staff,
        session=db,
    )
    return _format_reservation_response(reservation)
