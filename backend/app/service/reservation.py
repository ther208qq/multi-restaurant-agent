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

Time rules (spec §11): a booking must start at least
:data:`MIN_BOOKING_LEAD_MINUTES` ahead and no more than :data:`MAX_BOOKING_DAYS`
out, and fit inside the branch's ``opening_hours`` -- enforced by
:func:`validate_bookable_slot` from every entry point (booking, the availability
query and the staff reschedule) so the three cannot disagree.

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
from backend.app.opening_hours import format_clock, parse_opening_hours
from backend.app.repo.branch import BranchRepo
from backend.app.repo.locks import lock_key, lock_request
from backend.app.repo.reservation import ReservationRepo
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService

# How long a booking that still holds its table keeps absorbing repeat
# submissions of itself (spec §9 重复请求). The Agent books in one step -- there
# is no confirmation turn to anchor a request id on -- so the identity has to be
# the request itself: the same customer asking for the same slot, party size and
# duration again is the same request, not a second table.
DEDUPLICATE_WINDOW_MINUTES = 5

# Booking-window rules (spec §11), enforced by every entry point that can create
# or move a booking -- the customer form, the chat Agent and the staff reschedule
# -- so a slot one surface accepts is accepted by all. A slot must be at least
# ``MIN_BOOKING_LEAD_MINUTES`` ahead (no booking the immediate past) and no more
# than ``MAX_BOOKING_DAYS`` out (no booking half a year away).
MIN_BOOKING_LEAD_MINUTES = 15
MAX_BOOKING_DAYS = 30

# How the "that slot is full" reply looks for a concrete nearby time. It scans
# outward from the requested time in fixed steps, up to a bounded radius, and
# reports the first few times that really do have a free table.
SUGGESTION_STEP_MINUTES = 30
SUGGESTION_RADIUS_MINUTES = 240
MAX_TIME_SUGGESTIONS = 3

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


def current_wall_time() -> datetime:
    """The current time in the wall-clock frame bookings are stored in.

    Bookings are stored as the wall clock the guest typed, labelled UTC
    (``parse_slot``). Comparing them against the *real* UTC instant lets a
    "today 18:00" booking through hours after 18:00 has passed locally, because
    the branch's local time runs ahead of UTC. Taking "now" from the server's
    local wall clock keeps both sides of every time comparison in the same frame,
    so at 19:00 a booking for 18:00 is refused. Assumes the server runs in the
    branch's timezone; a multi-timezone deployment would need a per-branch
    offset.
    """
    return datetime.now().replace(tzinfo=timezone.utc)


def validate_bookable_slot(
    start_time: datetime,
    duration_minutes: int,
    opening_hours: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> None:
    """Reject a slot outside the bookable window or the branch's opening hours.

    Three rules (spec §11): the start must be *more* than
    :data:`MIN_BOOKING_LEAD_MINUTES` ahead, no further than
    :data:`MAX_BOOKING_DAYS` out, and the whole sitting
    (``start_time + duration_minutes``) must fit inside the branch's opening
    hours. "Now" is the local wall clock (:func:`current_wall_time`) so a slot
    that has already passed locally is refused even when UTC is still behind it.
    Raises :class:`ValidationError` otherwise.

    Shared by ``book_table``, ``list_available_tables`` and the staff reschedule
    path, so a slot the availability query offers is one ``book_table`` accepts --
    otherwise the form would show "tables free" and then refuse the submission.
    """
    now = now or current_wall_time()
    # Strictly more than the lead: at 18:45 a 19:00 seating (exactly 15 minutes
    # out) is already too close, per the "book ahead" rule.
    if start_time <= now + timedelta(minutes=MIN_BOOKING_LEAD_MINUTES):
        raise ValidationError(
            f"预订时间需比当前时间晚 {MIN_BOOKING_LEAD_MINUTES} 分钟以上。"
        )
    if start_time > now + timedelta(days=MAX_BOOKING_DAYS):
        raise ValidationError(
            f"最多只能预订未来 {MAX_BOOKING_DAYS} 天内的座位。"
        )
    open_minutes, close_minutes = parse_opening_hours(opening_hours)
    start_minutes = start_time.hour * 60 + start_time.minute
    if start_minutes < open_minutes:
        raise ValidationError(
            f"该门店 {format_clock(open_minutes)} 开门，请选择营业时间内的时段。"
        )
    if start_minutes + duration_minutes > close_minutes:
        raise ValidationError(
            f"该门店 {format_clock(close_minutes)} 打烊，"
            f"无法安排 {duration_minutes} 分钟的用餐，请选择更早的时段。"
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
    async def list_available_tables(
        branch_id: int,
        start_time: datetime,
        party_size: int,
        duration_minutes: int = 90,
        seating_area: Optional[str] = None,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> List[Table]:
        """Free tables for a slot, best-fit (smallest capacity) first.

        The read-only counterpart of :meth:`book_table`: it answers "what could I
        book at this time?" for the staff API and for the Agent's unavailable-slot
        explanation, using the same occupancy rule (every *occupying* status
        blocks its table). When called from the staff API (``staff`` given), the
        branch-scope rule applies: a branch-bound staff member may only inspect
        their own branch.
        """
        if staff is not None and staff.branch_id and branch_id != staff.branch_id:
            raise ForbiddenError("您没有权限访问该门店。")
        if party_size < 1:
            raise ValidationError("就餐人数至少为 1 位。")
        start = _coerce_start_time(start_time)
        end = start + timedelta(minutes=duration_minutes)
        area_filter = seating_area.lower() if seating_area else "any"

        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id, active_only=True)
            if branch is None:
                raise NotFoundError(f"门店 #{branch_id} 不存在。")
            # The read-only query applies the same window rules as booking, so it
            # cannot advertise a table for a slot ``book_table`` would refuse.
            validate_bookable_slot(start, duration_minutes, branch.opening_hours)
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
        deduplicate: bool = False,
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

            # Spec §11: reject a slot in the past, too far ahead, or outside the
            # branch's opening hours before touching any table.
            validate_bookable_slot(start_time, duration_minutes, branch.opening_hours)

            if deduplicate:
                existing = await ReservationService._find_duplicate_booking(
                    s,
                    customer_id=customer_id,
                    branch_id=branch_id,
                    start_time=start_time,
                    party_size=party_size,
                    duration_minutes=duration_minutes,
                )
                if existing is not None:
                    # The same request, already booked: hand back the original
                    # booking rather than hold a second table for it.
                    existing.branch = branch
                    return existing

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
            # Audit trail (spec §9): the booking holds its table from the moment
            # it is created -- attribute it to the customer who booked it.
            await AuditService.record(
                s,
                'reservation.created',
                entity_type='reservation',
                entity_id=reservation.id,
                branch_id=branch_id,
                actor=await UserRepo.get_customer_by_id(s, customer_id),
                details={
                    'status': reservation.status,
                    'party_size': party_size,
                    'duration_minutes': duration_minutes,
                    'table_id': chosen_table.id,
                    'start_time': start_time.isoformat(),
                },
            )
            return reservation

    @staticmethod
    async def _find_duplicate_booking(
        s: AsyncSession,
        *,
        customer_id: int,
        branch_id: int,
        start_time: datetime,
        party_size: int,
        duration_minutes: int,
    ) -> Optional[Reservation]:
        """This customer's still-holding booking for the same slot, if any.

        Guards against a repeat submission (spec §9 重复请求): without it, a chat
        message sent twice, a retried request, or a model that emits the same tool
        call twice would hold *two* tables for one party -- the exclusion
        constraint cannot catch that, because the second booking lands on a
        different free table.

        The look-up runs under an advisory lock keyed on the request, so two
        simultaneous submissions of the same booking cannot both miss each other
        and both insert.
        """
        await lock_request(
            s,
            lock_key(
                "reservation-request",
                customer_id,
                branch_id,
                start_time.isoformat(),
                party_size,
                duration_minutes,
            ),
        )
        since = datetime.now(timezone.utc) - timedelta(
            minutes=DEDUPLICATE_WINDOW_MINUTES
        )
        return await ReservationRepo.find_open_duplicate(
            s,
            customer_id=customer_id,
            branch_id=branch_id,
            start_time=start_time,
            party_size=party_size,
            duration_minutes=duration_minutes,
            statuses=OCCUPYING_RESERVATION_STATUSES,
            since=since,
        )

    @staticmethod
    async def _pick_free_table(
        s, branch_id, party_size, area_filter, start_time, end_time
    ):
        free = await ReservationService._free_tables(
            s, branch_id, party_size, area_filter, start_time, end_time
        )
        return free[0] if free else None

    @staticmethod
    async def _suggest_times(
        s, branch_id, party_size, area_filter, start_time, duration_minutes, opening_hours
    ) -> List[str]:
        """Concrete nearby start times that still have a free table (spec 11).

        Steps outward from the requested time, returning up to
        ``MAX_TIME_SUGGESTIONS`` ``HH:MM`` labels whose whole sitting fits inside
        the opening hours. Empty when nothing nearby is free.
        """
        try:
            open_minutes, close_minutes = parse_opening_hours(opening_hours)
        except ValidationError:
            return []
        requested = start_time.hour * 60 + start_time.minute
        base = start_time.replace(second=0, microsecond=0)
        found: List[str] = []
        for step in range(SUGGESTION_STEP_MINUTES, SUGGESTION_RADIUS_MINUTES + 1, SUGGESTION_STEP_MINUTES):
            for candidate in (requested - step, requested + step):
                if candidate < open_minutes or candidate + duration_minutes > close_minutes:
                    continue
                candidate_dt = base.replace(hour=candidate // 60, minute=candidate % 60)
                free = await ReservationService._free_tables(
                    s, branch_id, party_size, area_filter, candidate_dt,
                    candidate_dt + timedelta(minutes=duration_minutes),
                )
                if free:
                    found.append(candidate_dt.strftime("%H:%M"))
                if len(found) >= MAX_TIME_SUGGESTIONS:
                    return found
        return found

    @staticmethod
    async def _unavailable_message(
        s, branch_id, party_size, area_filter, start_time, end_time
    ):
        duration_minutes = int((end_time - start_time).total_seconds() // 60)
        branch = await BranchRepo.get_branch(s, branch_id)
        slot_hints = (
            await ReservationService._suggest_times(
                s, branch_id, party_size, area_filter, start_time,
                duration_minutes, branch.opening_hours,
            )
            if branch is not None
            else []
        )
        alt_areas = []
        if area_filter != 'any':
            free_elsewhere = await ReservationService._free_tables(
                s, branch_id, party_size, 'any', start_time, end_time
            )
            for table in free_elsewhere:
                alt_areas.append(f"{seating_area_label(table.seating_area)}（餐桌 {table.label}）")
        hints = []
        if slot_hints:
            hints.append(f"该时段前后仍空着的时段：{'、'.join(slot_hints)}")
        if alt_areas:
            hints.append(f"其它座位区域还有空位：{'、'.join(sorted(set(alt_areas)))}")
        if hints:
            alternatives = " " + "；".join(hints) + "。"
        else:
            alternatives = " 请换个时间或座位区域再试。"
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

            # Snapshot the pre-change state for the audit trail (§9).
            previous = {
                'status': reservation.status,
                'table_id': reservation.table_id,
                'table_label': reservation.table.label if reservation.table else None,
                'start_time': reservation.start_time,
                'party_size': reservation.party_size,
                'duration_minutes': reservation.duration_minutes,
            }

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
            seated_table = None
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
                if new_start_raw is not None or new_duration is not None:
                    validate_bookable_slot(start_time, duration_minutes, reservation.branch.opening_hours)
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
                        seated_table = await ReservationService._require_free_table(
                            s,
                            reservation.branch_id,
                            party_size,
                            reservation.seating_area_preference or 'any',
                            start_time,
                            end_time,
                            exclude_reservation_id=reservation.id,
                        )
                        changes['table_id'] = seated_table.id
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

            # --- 4. Audit trail (spec §9) --------------------------------------
            # Recorded after the write succeeds, on the same transaction: a race
            # rejection above rolls back and leaves no audit row behind.
            await ReservationService._record_update_audit(
                s,
                reservation_id=reservation_id,
                branch_id=reservation.branch_id,
                staff=staff,
                previous=previous,
                changes=changes,
                moved=moved,
                start_time=start_time,
                party_size=party_size,
                duration_minutes=duration_minutes,
                new_area=new_area,
                new_table=table or seated_table,
            )
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
    async def _record_update_audit(
        s: AsyncSession,
        *,
        reservation_id: int,
        branch_id: int,
        staff: Optional[StaffUser],
        previous: Dict[str, Any],
        changes: Dict[str, Any],
        moved: bool,
        start_time: datetime,
        party_size: int,
        duration_minutes: int,
        new_area: Optional[str],
        new_table: Optional[Table],
    ) -> None:
        """Write the audit rows for one staff reservation update.

        A single request can be several auditable operations at once (a
        reschedule *and* a table reassignment *and* a status move), so each one
        gets its own row rather than one overloaded entry.
        """
        if changes.get('status') is not None:
            await AuditService.record(
                s,
                'reservation.status_changed',
                entity_type='reservation',
                entity_id=reservation_id,
                branch_id=branch_id,
                actor=staff,
                details={'from': previous['status'], 'to': changes['status']},
            )

        if moved:
            rescheduled: Dict[str, Any] = {}
            if start_time != previous['start_time']:
                rescheduled['start_time'] = [
                    previous['start_time'].isoformat(),
                    start_time.isoformat(),
                ]
            if party_size != previous['party_size']:
                rescheduled['party_size'] = [previous['party_size'], party_size]
            if duration_minutes != previous['duration_minutes']:
                rescheduled['duration_minutes'] = [
                    previous['duration_minutes'],
                    duration_minutes,
                ]
            if new_area is not None:
                rescheduled['seating_area_preference'] = new_area.lower()
            if rescheduled:
                await AuditService.record(
                    s,
                    'reservation.rescheduled',
                    entity_type='reservation',
                    entity_id=reservation_id,
                    branch_id=branch_id,
                    actor=staff,
                    details=rescheduled,
                )

        # A table move can come from an explicit re-table, from a reschedule that
        # picked a different table, or from seating a booking that had none.
        new_table_id = changes.get('table_id', previous['table_id'])
        if new_table_id != previous['table_id']:
            await AuditService.record(
                s,
                'reservation.table_reassigned',
                entity_type='reservation',
                entity_id=reservation_id,
                branch_id=branch_id,
                actor=staff,
                details={
                    'from_table_id': previous['table_id'],
                    'from_table_label': previous['table_label'],
                    'to_table_id': new_table_id,
                    'to_table_label': new_table.label if new_table else None,
                },
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
    "DEDUPLICATE_WINDOW_MINUTES",
    "MIN_BOOKING_LEAD_MINUTES",
    "MAX_BOOKING_DAYS",
    "validate_bookable_slot",
    "current_wall_time",
]
