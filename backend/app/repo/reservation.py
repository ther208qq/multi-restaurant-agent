"""Reservation repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``Reservation`` queries
and inserts (including the ``tstzrange`` encoding of the booking window and the
overlap probe used by the exclusion-constraint flow), never commits, and never
imports ``service`` / ``routes`` / ``agent``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.reservation import OCCUPYING_RESERVATION_STATUSES, Reservation


class ReservationRepo:
    @staticmethod
    async def create_reservation(
        session: AsyncSession,
        *,
        customer_id: int,
        branch_id: int,
        table_id: Optional[int],
        party_size: int,
        start_time: datetime,
        end_time: datetime,
        duration_minutes: int,
        seating_area_preference: Optional[str],
        status: str,
        notes: Optional[str],
    ) -> Reservation:
        """Insert a reservation, deriving its ``booking_window`` from the slot.

        The ``[)`` range is what the PostgreSQL ``EXCLUDE USING gist`` constraint
        compares, so double-booking is rejected by the database, not by Python.
        """
        reservation = Reservation(
            customer_id=customer_id,
            branch_id=branch_id,
            table_id=table_id,
            party_size=party_size,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            seating_area_preference=seating_area_preference,
            status=status,
            notes=notes,
            booking_window=func.tstzrange(start_time, end_time, "[)"),
        )
        session.add(reservation)
        await session.flush()
        return reservation

    @staticmethod
    async def get_reservation(
        session: AsyncSession,
        reservation_id: int,
        *,
        with_table: bool = False,
        with_customer: bool = False,
        with_branch: bool = False,
        for_update: bool = False,
    ) -> Optional[Reservation]:
        stmt = select(Reservation).where(Reservation.id == reservation_id)
        options = []
        if with_table:
            options.append(selectinload(Reservation.table))
        if with_customer:
            options.append(selectinload(Reservation.customer))
        if with_branch:
            options.append(selectinload(Reservation.branch))
        if options:
            stmt = stmt.options(*options)
        if for_update:
            # Serialise concurrent edits of the *same* booking (two staff members
            # rescheduling it at once); cross-booking races are settled by the
            # exclusion constraint instead.
            stmt = stmt.with_for_update()
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_reservations(
        session: AsyncSession,
        *,
        customer_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        status: Optional[str] = None,
        with_table: bool = True,
        with_customer: bool = False,
        with_branch: bool = False,
        descending: bool = False,
        limit: Optional[int] = None,
    ) -> List[Reservation]:
        stmt = select(Reservation)
        options = []
        if with_table:
            options.append(selectinload(Reservation.table))
        if with_customer:
            options.append(selectinload(Reservation.customer))
        if with_branch:
            options.append(selectinload(Reservation.branch))
        if options:
            stmt = stmt.options(*options)

        if customer_id is not None:
            stmt = stmt.where(Reservation.customer_id == customer_id)
        if branch_id is not None:
            stmt = stmt.where(Reservation.branch_id == branch_id)
        if status is not None:
            stmt = stmt.where(Reservation.status == status)

        stmt = stmt.order_by(
            Reservation.start_time.desc() if descending else Reservation.start_time.asc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def reservation_status_totals(
        session: AsyncSession,
        *,
        branch_id: int,
        start: datetime,
        end: datetime,
        now: datetime,
    ) -> List[Dict[str, Any]]:
        """Reservation counts and party sizes per status, split by slot elapsed.

        A reservation belongs to the window by its **start time** (when the
        guest was expected), not by when it was created. ``start_passed`` is
        ``True`` once the booked slot is in the past relative to ``now``: the
        Service needs that split to distinguish a booking that has not happened
        yet from one whose slot came and went while still ``pending`` /
        ``confirmed``.
        """
        passed = (Reservation.start_time < now).label("start_passed")
        stmt = (
            select(
                Reservation.status.label("status"),
                passed,
                func.count().label("reservation_count"),
                func.coalesce(func.sum(Reservation.party_size), 0).label(
                    "party_size"
                ),
            )
            .where(
                Reservation.branch_id == branch_id,
                Reservation.start_time >= start,
                Reservation.start_time < end,
            )
            .group_by(Reservation.status, passed)
            .order_by(Reservation.status, passed)
        )
        res = await session.execute(stmt)
        return [
            {
                "status": row.status,
                "start_passed": bool(row.start_passed),
                "reservation_count": int(row.reservation_count or 0),
                "party_size": int(row.party_size or 0),
            }
            for row in res.all()
        ]

    @staticmethod
    async def find_open_duplicate(
        session: AsyncSession,
        *,
        customer_id: int,
        branch_id: int,
        start_time: datetime,
        party_size: int,
        duration_minutes: int,
        statuses: Sequence[str],
        since: datetime,
    ) -> Optional[Reservation]:
        """This customer's still-holding booking for the very same slot, if any.

        Feeds the duplicate-request check: a booking that is still occupying its
        table (pending / confirmed / seated) and matches the slot exactly is the
        same request submitted twice, not a second table.
        """
        stmt = (
            select(Reservation)
            .options(
                selectinload(Reservation.table), selectinload(Reservation.branch)
            )
            .where(
                Reservation.customer_id == customer_id,
                Reservation.branch_id == branch_id,
                Reservation.status.in_(tuple(statuses)),
                Reservation.start_time == start_time,
                Reservation.party_size == party_size,
                Reservation.duration_minutes == duration_minutes,
                Reservation.created_at >= since,
            )
            .order_by(Reservation.id.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def has_overlapping_active(
        session: AsyncSession,
        table_id: int,
        start_time: datetime,
        end_time: datetime,
        *,
        exclude_reservation_id: Optional[int] = None,
    ) -> bool:
        """True when an occupying booking on ``table_id`` overlaps the slot.

        Encodes ``existing_start < new_end AND existing_end > new_start`` and
        counts every *occupying* status (pending / confirmed / seated) -- the
        application-side counterpart to the DB exclusion constraint, used to pick
        a free table before attempting a write. ``exclude_reservation_id`` lets a
        booking move to a new slot without colliding with its own current row.
        """
        stmt = (
            select(Reservation.id)
            .where(
                Reservation.table_id == table_id,
                Reservation.status.in_(OCCUPYING_RESERVATION_STATUSES),
                Reservation.start_time < end_time,
                Reservation.end_time > start_time,
            )
            .limit(1)
        )
        if exclude_reservation_id is not None:
            stmt = stmt.where(Reservation.id != exclude_reservation_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none() is not None

    @staticmethod
    async def count_by_table(session: AsyncSession, table_id: int) -> int:
        """How many bookings -- of any status -- reference ``table_id``.

        Every status counts, history included: the FK from ``reservations`` to
        ``tables`` is ``ON DELETE CASCADE``, so a hard delete of the table would
        take its past bookings with it. Callers use this to refuse instead.
        """
        stmt = select(func.count()).select_from(Reservation).where(
            Reservation.table_id == table_id
        )
        res = await session.execute(stmt)
        return int(res.scalar_one())

    @staticmethod
    async def update_reservation(
        session: AsyncSession, reservation: Reservation, values: Dict[str, Any]
    ) -> Reservation:
        for key, value in values.items():
            setattr(reservation, key, value)
        await session.flush()
        return reservation

    @staticmethod
    async def reschedule_reservation(
        session: AsyncSession,
        reservation: Reservation,
        *,
        start_time: datetime,
        end_time: datetime,
        duration_minutes: int,
        values: Optional[Dict[str, Any]] = None,
    ) -> Reservation:
        """Move a booking to a new slot, keeping ``booking_window`` in step.

        ``booking_window`` is the column the exclusion constraint compares, so it
        has to be rewritten together with the slot; both land in the same UPDATE,
        which means the constraint always sees a consistent row.
        """
        reservation.start_time = start_time
        reservation.end_time = end_time
        reservation.duration_minutes = duration_minutes
        reservation.booking_window = func.tstzrange(start_time, end_time, "[)")
        for key, value in (values or {}).items():
            setattr(reservation, key, value)
        await session.flush()
        return reservation


__all__ = ["ReservationRepo"]
