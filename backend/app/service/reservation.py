"""Reservation business service.

Owns the transaction boundary and holds the single implementation of the table
booking / reservation-management rules shared by the staff reservation API and
the Agent booking flow: candidate-table selection, the availability query,
overlap probing, the exclusion-constraint-guarded insert, rescheduling and the
reservation lifecycle.

Lifecycle (spec §9): ``pending -> confirmed -> seated -> completed``. A booking
made through the Agent starts ``pending`` so that a staff member confirms it
(``pending -> confirmed``); ``cancelled`` is reachable from any non-terminal
status and releases the table slot, ``completed`` is terminal, and a cancelled
booking may be restored to ``pending`` (staff undoing a mistake) after checking
that its slot is still free.

Concurrency: the DB exclusion constraint covers every *occupying* status
(pending / confirmed / seated), so a double booking is rejected by the database
and surfaced here as :class:`ReservationRaceError` (409). Rescheduling probes
the target table first for a precise message and keeps the constraint as the
backstop.

Raises domain exceptions (never HTTP types); the API maps them to status codes
and the Agent turns them into user-facing replies.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ReservationRaceError,
    TableUnavailableError,
    ValidationError,
)
from backend.app.labels import RESERVATION_STATUS_LABELS, seating_area_label
from backend.app.models.branch import Table
from backend.app.models.reservation import OCCUPYING_RESERVATION_STATUSES, Reservation
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

# Legal status moves. Forward-only along the lifecycle, plus cancellation from
# any non-terminal status and the single ``cancelled -> pending`` restore. A
# status absent from the map (or an empty set) has no outgoing transitions.
RESERVATION_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "pending": frozenset({"confirmed", "cancelled"}),
    "confirmed": frozenset({"seated", "cancelled"}),
    "seated": frozenset({"completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset({"pending"}),
}

# Statuses a booking can be rescheduled / re-tabled from: the slot is still in
# the future and nobody is sitting at the table yet.
RESCHEDULABLE_STATUSES = frozenset({"pending", "confirmed"})

RACE_CONFLICT_MESSAGE = (
    "该餐桌刚刚被其他顾客预订。请选择其他时间段或座位区域。"
)

RESTORE_CONFLICT_MESSAGE = (
    "无法恢复该预订：原时段已被其他预订占用。请改约到其他时间段。"
)


def _status_label(status: str) -> str:
    """Chinese label for a reservation status, falling back to the raw code."""
    return RESERVATION_STATUS_LABELS.get(status, status)


def _coerce_start_time(value: Any) -> datetime:
    """Accept a ``datetime`` or an ISO-8601 string, always returning UTC-aware."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValidationError("预订时间格式不正确。")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


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
    async def list_available_tables(
        branch_id: int,
        start_time: datetime,
        party_size: int,
        duration_minutes: int = 90,
        seating_area: Optional[str] = None,
        *,
        session: Optional[AsyncSession] = None,
    ) -> List[Table]:
        """Free tables for a slot, best-fit (smallest capacity) first.

        The read-only counterpart of :meth:`book_table`: it answers "what could I
        book at this time?" for the staff API and for the Agent's unavailable-slot
        explanation, using the same occupancy rule (every *occupying* status
        blocks its table).
        """
        if party_size < 1:
            raise ValidationError("就餐人数至少为 1 位。")
        start = _coerce_start_time(start_time)
        end = start + timedelta(minutes=duration_minutes)
        area_filter = seating_area.lower() if seating_area else "any"

        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id, active_only=True)
            if branch is None:
                raise NotFoundError(f"门店 #{branch_id} 不存在。")
            return await ReservationService._free_tables(
                s, branch_id, party_size, area_filter, start, end
            )

    @staticmethod
    async def _free_tables(
        s: AsyncSession,
        branch_id: int,
        party_size: int,
        area_filter: str,
        start_time: datetime,
        end_time: datetime,
        *,
        exclude_reservation_id: Optional[int] = None,
    ) -> List[Table]:
        """Candidate tables with no occupying booking overlapping the slot."""
        candidates = await BranchRepo.list_candidate_tables(
            s, branch_id, min_capacity=party_size, seating_area=area_filter
        )
        free: List[Table] = []
        for table in candidates:
            if not await ReservationRepo.has_overlapping_active(
                s,
                table.id,
                start_time,
                end_time,
                exclude_reservation_id=exclude_reservation_id,
            ):
                free.append(table)
        return free

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
        """Find a free best-fit table and record the booking as ``pending``.

        The booking already holds its table (it is an occupying status), but it
        waits for staff confirmation before it counts as confirmed.
        """
        if party_size < 1:
            raise ValidationError("就餐人数至少为 1 位。")
        start_time = _coerce_start_time(start_time)
        end_time = start_time + timedelta(minutes=duration_minutes)
        area_filter = seating_area.lower() if seating_area else 'any'

        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id, active_only=True)
            if branch is None:
                raise NotFoundError(f'门店 #{branch_id} 不存在。')

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
                    status='pending',
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
        free = await ReservationService._free_tables(
            s, branch_id, party_size, area_filter, start_time, end_time
        )
        return free[0] if free else None

    @staticmethod
    async def _unavailable_message(
        s, branch_id, party_size, area_filter, start_time, end_time
    ):
        alt_areas = []
        if area_filter != 'any':
            free_elsewhere = await ReservationService._free_tables(
                s, branch_id, party_size, 'any', start_time, end_time
            )
            for table in free_elsewhere:
                alt_areas.append(f"{seating_area_label(table.seating_area)}（餐桌 {table.label}）")
        if alt_areas:
            alternatives = f" 不过，以下座位还有空位：{'、'.join(sorted(set(alt_areas)))}。"
        else:
            alternatives = " 今晚更早或更晚的时间段也有空位。"
        area_text = (
            f"{seating_area_label(area_filter)}区域"
            if area_filter != "any"
            else "本门店"
        )
        return (
            f"{start_time.strftime('%H:%M')} {area_text}暂时没有可供 {party_size} 位就餐的餐桌。"
            f"{alternatives}"
        )

    @staticmethod
    async def update_reservation(
        reservation_id: int,
        values: Dict[str, Any],
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Reservation:
        """Update a reservation: status transition, reschedule, table or notes.

        ``values`` may carry any of ``status``, ``table_id``, ``notes``,
        ``start_time``, ``party_size``, ``duration_minutes`` and
        ``seating_area_preference``. Status changes are checked against the
        transition matrix; time / party changes re-derive the booking window and
        either keep the current table (when it still fits and is free) or pick a
        new free one, rejecting the move with a 409 when the target is taken.
        """
        async with transactional_session(session) as s:
            reservation = await ReservationRepo.get_reservation(
                s,
                reservation_id,
                with_table=True,
                with_customer=True,
                with_branch=True,
                for_update=True,
            )
            if reservation is None:
                raise NotFoundError(f'预订 #{reservation_id} 不存在。')
            if (
                staff is not None
                and staff.branch_id
                and reservation.branch_id != staff.branch_id
            ):
                raise ForbiddenError(
                    '您没有权限管理该门店的预订。'
                )

            changes: Dict[str, Any] = {}
            new_status = values.get('status')
            new_table_id = values.get('table_id')
            new_party_size = values.get('party_size')
            new_duration = values.get('duration_minutes')
            new_start_raw = values.get('start_time')
            new_area = values.get('seating_area_preference')

            # --- 1. Reschedule / re-table -------------------------------------
            reschedule = (
                new_start_raw is not None
                or new_party_size is not None
                or new_duration is not None
                or new_area is not None
            )
            if reschedule and reservation.status not in RESCHEDULABLE_STATUSES:
                raise ValidationError(
                    f'只能改约「{_status_label("pending")}」或「{_status_label("confirmed")}」'
                    f'的预订（当前状态：{_status_label(reservation.status)}）。'
                )
            if new_table_id is not None and reservation.status not in OCCUPYING_RESERVATION_STATUSES:
                raise ValidationError(
                    f'「{_status_label(reservation.status)}」的预订不能改派餐桌。'
                )

            start_time = reservation.start_time
            end_time = reservation.end_time
            duration_minutes = reservation.duration_minutes
            party_size = reservation.party_size
            table = None
            if reschedule or new_table_id is not None:
                if new_start_raw is not None:
                    start_time = _coerce_start_time(new_start_raw)
                party_size = new_party_size if new_party_size is not None else party_size
                if party_size < 1:
                    raise ValidationError("就餐人数至少为 1 位。")
                if new_party_size is not None:
                    changes['party_size'] = party_size
                duration_minutes = new_duration if new_duration is not None else duration_minutes
                if duration_minutes < 1:
                    raise ValidationError("用餐时长至少为 1 分钟。")
                end_time = start_time + timedelta(minutes=duration_minutes)
                area_filter = (
                    new_area.lower()
                    if new_area
                    else (reservation.table.seating_area if reservation.table else 'any')
                )
                table = await ReservationService._resolve_target_table(
                    s,
                    reservation,
                    table_id=new_table_id,
                    party_size=party_size,
                    area_filter=area_filter,
                    start_time=start_time,
                    end_time=end_time,
                )

            # --- 2. Status transition -----------------------------------------
            if new_status is not None:
                if new_status not in VALID_RESERVATION_STATUSES:
                    raise ValidationError(
                        f"无效的状态「{new_status}」，必须是以下之一：{list(VALID_RESERVATION_STATUSES)}。"
                    )
                if new_status != reservation.status:
                    if new_status not in RESERVATION_TRANSITIONS.get(reservation.status, frozenset()):
                        raise ValidationError(
                            f'不允许将预订从「{_status_label(reservation.status)}」'
                            f'变更为「{_status_label(new_status)}」。'
                        )
                    changes['status'] = new_status
                    if new_status == 'seated' and table is None and reservation.table_id is None:
                        # A booking without a table can still be seated: take any
                        # free one for its own slot.
                        changes['table_id'] = (
                            await ReservationService._require_free_table(
                                s,
                                reservation.branch_id,
                                party_size,
                                reservation.seating_area_preference or 'any',
                                start_time,
                                end_time,
                                exclude_reservation_id=reservation.id,
                            )
                        ).id
                    elif new_status == 'pending' and reservation.status == 'cancelled':
                        await ReservationService._ensure_slot_still_free(s, reservation)

            if table is not None:
                changes['table_id'] = table.id
                changes['seating_area_preference'] = table.seating_area

            if values.get('notes') is not None:
                changes['notes'] = values['notes']

            # --- 3. Persist ---------------------------------------------------
            moved = reschedule or new_table_id is not None
            if moved or changes:
                try:
                    if moved:
                        await ReservationRepo.reschedule_reservation(
                            s,
                            reservation,
                            start_time=start_time,
                            end_time=end_time,
                            duration_minutes=duration_minutes,
                            values=changes,
                        )
                    else:
                        await ReservationRepo.update_reservation(s, reservation, changes)
                except IntegrityError:
                    await s.rollback()
                    raise ReservationRaceError(RACE_CONFLICT_MESSAGE)
            # Re-read so the caller gets the reloaded table / branch relationship
            # (a reschedule may have swapped the table) rather than a stale object.
            return await ReservationRepo.get_reservation(
                s,
                reservation_id,
                with_table=True,
                with_customer=True,
                with_branch=True,
            )

    @staticmethod
    async def _resolve_target_table(
        s: AsyncSession,
        reservation: Reservation,
        *,
        table_id: Optional[int],
        party_size: int,
        area_filter: str,
        start_time: datetime,
        end_time: datetime,
    ) -> Table:
        """The table the booking should occupy, or a conflict/validation error."""
        if table_id is not None:
            table = await BranchRepo.get_table(s, table_id)
            if table is None:
                raise NotFoundError(f'餐桌 #{table_id} 不存在。')
            if table.branch_id != reservation.branch_id:
                raise ValidationError('该餐桌不属于本门店。')
            if not table.is_available:
                raise ValidationError(f'餐桌 {table.label} 已停用。')
            if table.capacity < party_size:
                raise ValidationError(
                    f'餐桌 {table.label} 最多容纳 {table.capacity} 位，无法安排 {party_size} 位。'
                )
            if await ReservationRepo.has_overlapping_active(
                s, table.id, start_time, end_time, exclude_reservation_id=reservation.id
            ):
                raise ConflictError(
                    f'餐桌 {table.label} 在 {start_time.strftime("%H:%M")} 该时段已被占用，'
                    f'请选择其他餐桌或时间。'
                )
            return table

        current = reservation.table
        if (
            current is not None
            and current.is_available
            and current.capacity >= party_size
            and (area_filter == 'any' or current.seating_area == area_filter)
            and not await ReservationRepo.has_overlapping_active(
                s, current.id, start_time, end_time, exclude_reservation_id=reservation.id
            )
        ):
            return current

        table = await ReservationService._require_free_table(
            s,
            reservation.branch_id,
            party_size,
            area_filter,
            start_time,
            end_time,
            exclude_reservation_id=reservation.id,
        )
        return table

    @staticmethod
    async def _require_free_table(
        s: AsyncSession,
        branch_id: int,
        party_size: int,
        area_filter: str,
        start_time: datetime,
        end_time: datetime,
        *,
        exclude_reservation_id: Optional[int] = None,
    ) -> Table:
        free = await ReservationService._free_tables(
            s,
            branch_id,
            party_size,
            area_filter,
            start_time,
            end_time,
            exclude_reservation_id=exclude_reservation_id,
        )
        if not free:
            raise TableUnavailableError(
                await ReservationService._unavailable_message(
                    s, branch_id, party_size, area_filter, start_time, end_time
                )
            )
        return free[0]

    @staticmethod
    async def _ensure_slot_still_free(s: AsyncSession, reservation: Reservation) -> None:
        """Guard the ``cancelled -> pending`` restore against a retaken slot."""
        if reservation.table_id is None:
            return
        if await ReservationRepo.has_overlapping_active(
            s,
            reservation.table_id,
            reservation.start_time,
            reservation.end_time,
            exclude_reservation_id=reservation.id,
        ):
            raise ConflictError(RESTORE_CONFLICT_MESSAGE)


__all__ = [
    "ReservationService",
    "VALID_RESERVATION_STATUSES",
    "RESERVATION_TRANSITIONS",
    "OCCUPYING_RESERVATION_STATUSES",
    "RESCHEDULABLE_STATUSES",
]
