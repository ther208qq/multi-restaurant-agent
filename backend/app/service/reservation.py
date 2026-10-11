"""Reservation business service.

Owns the transaction boundary and holds the single implementation of the table
booking / reservation-management rules shared by the staff reservation API and
the Agent booking flow: candidate-table selection, overlap probing, the
auto-confirm insert guarded by the database exclusion constraint, and branch
scoping of staff updates.

Raises domain exceptions (never HTTP types); the API maps them to status codes
and the Agent turns them into user-facing replies.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ForbiddenError,
    NotFoundError,
    ReservationRaceError,
    TableUnavailableError,
    ValidationError,
)
from backend.app.models.branch import Table
from backend.app.models.reservation import Reservation
from backend.app.models.user import StaffUser
from backend.app.repo.branch import BranchRepo
from backend.app.repo.reservation import ReservationRepo

VALID_RESERVATION_STATUSES = (
    "pending",
    "confirmed",
    "seated",
    "completed",
    "cancelled",
)

RACE_CONFLICT_MESSAGE = (
    "That table was just reserved moments ago by another guest. "
    "Please choose an alternative time slot or seating area."
)


class ReservationService:
    @staticmethod
    async def list_reservations(
        *,
        staff: Optional[StaffUser] = None,
        customer_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        status: Optional[str] = None,
        date: Optional[str] = None,
        descending: bool = False,
        limit: Optional[int] = None,
        session: Optional[AsyncSession] = None,
    ) -> List[Reservation]:
        """List reservations, scoped to the staff member's branch when bound.

        ``date`` (``YYYY-MM-DD``) is matched against the start time rendered as
        ``%Y-%m-%d`` exactly as the API did before, so timezone behaviour is
        unchanged. ``descending`` / ``limit`` let the customer-activity and
        Agent status call sites reuse this one query instead of hand-rolling it.
        """
        async with transactional_session(session) as s:
            effective_branch = branch_id
            if staff is not None and staff.branch_id:
                effective_branch = staff.branch_id
            reservations = await ReservationRepo.list_reservations(
                s,
                customer_id=customer_id,
                branch_id=effective_branch,
                status=status,
                with_table=True,
                with_customer=True,
                with_branch=True,
                descending=descending,
                limit=limit,
            )
            if date:
                reservations = [
                    r
                    for r in reservations
                    if r.start_time.strftime("%Y-%m-%d") == date
                ]
            return reservations

    @staticmethod
    async def update_reservation(
        reservation_id: int,
        values: Dict[str, Any],
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Reservation:
        """Staff update of status / assigned table / notes, with branch scoping."""
        async with transactional_session(session) as s:
            reservation = await ReservationRepo.get_reservation(
                s, reservation_id, with_table=True, with_customer=True, with_branch=True
            )
            if reservation is None:
                raise NotFoundError(f'Reservation #{reservation_id} not found.')
            if (
                staff is not None
                and staff.branch_id
                and reservation.branch_id != staff.branch_id
            ):
                raise ForbiddenError(
                    'You do not have permission to manage reservations for this branch.'
                )

            changes = {}
            new_status = values.get('status')
            if new_status is not None:
                if new_status not in VALID_RESERVATION_STATUSES:
                    raise ValidationError(f"Invalid status '{new_status}'. Must be one of {list(VALID_RESERVATION_STATUSES)}.")
                changes['status'] = new_status
            if values.get('table_id') is not None:
                changes['table_id'] = values['table_id']
            if values.get('notes') is not None:
                changes['notes'] = values['notes']
            if changes:
                await ReservationRepo.update_reservation(s, reservation, changes)
            return reservation

    @staticmethod
    async def book_table(
        customer_id: int,
        branch_id: int,
        start_time: datetime,
        party_size: int,
        duration_minutes: int = 90,
        seating_area: Optional[str] = 'indoor',
        notes: Optional[str] = None,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Reservation:
        """Find a free best-fit table and auto-confirm the booking."""
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        end_time = start_time + timedelta(minutes=duration_minutes)
        area_filter = seating_area.lower() if seating_area else 'any'

        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id, active_only=True)
            if branch is None:
                raise NotFoundError(f'Branch #{branch_id} not found.')

            chosen_table = await ReservationService._pick_free_table(
                s, branch_id, party_size, area_filter, start_time, end_time
            )
            if chosen_table is None:
                raise TableUnavailableError(
                    await ReservationService._unavailable_message(s, branch_id, party_size, area_filter, start_time, end_time)
                )

            try:
                reservation = await ReservationRepo.create_reservation(
                    s,
                    customer_id=customer_id,
                    branch_id=branch_id,
                    table_id=chosen_table.id,
                    party_size=party_size,
                    start_time=start_time,
                    end_time=end_time,
                    duration_minutes=duration_minutes,
                    seating_area_preference=chosen_table.seating_area,
                    status='confirmed',
                    notes=notes,
                )
            except IntegrityError:
                await s.rollback()
                raise ReservationRaceError(RACE_CONFLICT_MESSAGE)
            reservation.branch = branch
            reservation.table = chosen_table
            return reservation

    @staticmethod
    async def _pick_free_table(
        s, branch_id, party_size, area_filter, start_time, end_time
    ):
        candidates = await BranchRepo.list_candidate_tables(
            s, branch_id, min_capacity=party_size, seating_area=area_filter
        )
        for table in candidates:
            if not await ReservationRepo.has_overlapping_confirmed(
                s, table.id, start_time, end_time
            ):
                return table
        return None

    @staticmethod
    async def _unavailable_message(
        s, branch_id, party_size, area_filter, start_time, end_time
    ):
        alt_areas = []
        if area_filter != 'any':
            others = await BranchRepo.list_candidate_tables(
                s, branch_id, min_capacity=party_size
            )
            for table in others:
                if not await ReservationRepo.has_overlapping_confirmed(
                    s, table.id, start_time, end_time
                ):
                    alt_areas.append(f"{table.seating_area.capitalize()} (Table {table.label})")
        if alt_areas:
            alternatives = f" However, we have availability in our {', '.join(set(alt_areas))} area(s)."
        else:
            alternatives = " We also have availability earlier or later in the evening."
        return (
            f"No tables currently available for {party_size} guests in the {area_filter} area "
            f"at {start_time.strftime('%I:%M %p')}.{alternatives}"
        )


__all__ = ["ReservationService", "VALID_RESERVATION_STATUSES"]

