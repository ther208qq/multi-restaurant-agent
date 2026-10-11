"""Reservation repository: semantic data access only.

Receives a session from the Service layer, encapsulates ``Reservation`` queries
and inserts (including the ``tstzrange`` encoding of the booking window and the
overlap probe used by the exclusion-constraint flow), never commits, and never
imports ``service`` / ``routes`` / ``agent``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.reservation import Reservation


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
    async def has_overlapping_confirmed(
        session: AsyncSession,
        table_id: int,
        start_time: datetime,
        end_time: datetime,
    ) -> bool:
        """True when a confirmed booking on ``table_id`` overlaps the slot.

        Encodes ``existing_start < new_end AND existing_end > new_start`` -- the
        application-side counterpart to the DB exclusion constraint, used to pick
        a free table before attempting the insert.
        """
        stmt = select(Reservation).where(
            Reservation.table_id == table_id,
            Reservation.status == "confirmed",
            Reservation.start_time < end_time,
            Reservation.end_time > start_time,
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none() is not None

    @staticmethod
    async def update_reservation(
        session: AsyncSession, reservation: Reservation, values: Dict[str, Any]
    ) -> Reservation:
        for key, value in values.items():
            setattr(reservation, key, value)
        await session.flush()
        return reservation


__all__ = ["ReservationRepo"]
