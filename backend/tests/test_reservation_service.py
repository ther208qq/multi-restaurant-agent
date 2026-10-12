"""Task8 tests: ReservationService booking, listing and staff updates."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ReservationRaceError,
    TableUnavailableError,
    ValidationError,
)
from backend.app.models.branch import Branch, Table
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.reservation import ReservationRepo
from backend.app.service.reservation import ReservationService

ADMIN = StaffUser(username='admin', role='admin', branch_id=None)


def _staff(branch_id):
    return StaffUser(username=f's{branch_id}', role='staff', branch_id=branch_id)


def _slot(days=400, hour=19, minute=0):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


async def _make_branch(specs):
    """Create an isolated branch with tables; specs = [(label, area, capacity)]."""
    async with transactional_session() as s:
        branch = Branch(name='__rs_branch', address='x', phone='x', is_active=True)
        s.add(branch)
        await s.flush()
        table_ids = []
        for label, area, capacity in specs:
            table = Table(
                branch_id=branch.id,
                label=label,
                seating_area=area,
                capacity=capacity,
                is_available=True,
            )
            s.add(table)
            await s.flush()
            table_ids.append(table.id)
        return branch.id, table_ids


async def _make_customer():
    async with transactional_session() as s:
        customer = Customer(name="__rs", email=f"{uuid4().hex}@x.com", password_hash="x")
        s.add(customer)
        await s.flush()
        return customer.id


async def _cleanup(branch_id, customer_id=None):
    async with transactional_session() as s:
        await s.execute(delete(Reservation).where(Reservation.branch_id == branch_id))
        await s.execute(delete(Branch).where(Branch.id == branch_id))
        if customer_id is not None:
            await s.execute(delete(Customer).where(Customer.id == customer_id))


async def test_book_table_auto_confirms_and_assigns_table():
    branch_id, table_ids = await _make_branch([("I1", "indoor", 2)])
    try:
        reservation = await ReservationService.book_table(
            customer_id=1,
            branch_id=branch_id,
            start_time=_slot(),
            party_size=2,
            seating_area="indoor",
        )
        assert reservation.status == "confirmed"
        assert reservation.table_id == table_ids[0]
        assert reservation.branch.name == "__rs_branch"
        assert reservation.table.label == "I1"
    finally:
        await _cleanup(branch_id)


async def test_book_table_branch_not_found():
    with pytest.raises(NotFoundError):
        await ReservationService.book_table(
            customer_id=1,
            branch_id=999999,
            start_time=_slot(),
            party_size=2,
        )


async def test_book_table_picks_smallest_fitting_table():
    branch_id, table_ids = await _make_branch(
        [("Big", "indoor", 6), ("Small", "indoor", 2)]
    )
    try:
        reservation = await ReservationService.book_table(
            customer_id=1,
            branch_id=branch_id,
            start_time=_slot(),
            party_size=2,
            seating_area="indoor",
        )
        assert reservation.table_id == table_ids[1]
    finally:
        await _cleanup(branch_id)


async def test_book_table_same_slot_uses_other_table():
    branch_id, _ = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    try:
        first = await ReservationService.book_table(
            customer_id=1, branch_id=branch_id, start_time=_slot(), party_size=2
        )
        second = await ReservationService.book_table(
            customer_id=1, branch_id=branch_id, start_time=_slot(), party_size=2
        )
        assert first.table_id != second.table_id
    finally:
        await _cleanup(branch_id)


async def test_book_table_no_table_in_area_raises():
    branch_id, _ = await _make_branch([("I1", "indoor", 2)])
    try:
        with pytest.raises(TableUnavailableError) as exc_info:
            await ReservationService.book_table(
                customer_id=1,
                branch_id=branch_id,
                start_time=_slot(),
                party_size=2,
                seating_area="rooftop",
            )
        assert "暂时没有可供" in exc_info.value.message
        assert isinstance(exc_info.value, ConflictError)
    finally:
        await _cleanup(branch_id)


async def test_book_table_race_raises(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    async def _boom(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception("exclusion"))

    monkeypatch.setattr(ReservationRepo, "create_reservation", _boom)
    branch_id, _ = await _make_branch([("I1", "indoor", 2)])
    try:
        with pytest.raises(ReservationRaceError):
            await ReservationService.book_table(
                customer_id=1, branch_id=branch_id, start_time=_slot(), party_size=2
            )
    finally:
        await _cleanup(branch_id)


async def test_list_reservations_scopes_and_filters():
    branch_id, _ = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    other_branch_id, _ = await _make_branch([("C", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        await ReservationService.book_table(
            customer_id=customer_id, branch_id=branch_id,
            start_time=_slot(days=401, hour=18), party_size=2,
        )
        await ReservationService.book_table(
            customer_id=customer_id, branch_id=other_branch_id,
            start_time=_slot(days=402, hour=18), party_size=2,
        )

        scoped = await ReservationService.list_reservations(staff=_staff(branch_id))
        assert len(scoped) == 1
        assert scoped[0].branch_id == branch_id

        mine = await ReservationService.list_reservations(customer_id=customer_id)
        assert len(mine) == 2

        by_status = await ReservationService.list_reservations(
            customer_id=customer_id, status="confirmed"
        )
        assert len(by_status) == 2

        target_date = _slot(days=401).strftime("%Y-%m-%d")
        by_date = await ReservationService.list_reservations(
            customer_id=customer_id, date=target_date
        )
        assert len(by_date) == 1
        assert by_date[0].start_time.strftime("%Y-%m-%d") == target_date
    finally:
        await _cleanup(branch_id, customer_id)
        await _cleanup(other_branch_id)


async def test_update_reservation_not_found():
    with pytest.raises(NotFoundError):
        await ReservationService.update_reservation(999999, {"status": "seated"})


async def test_update_reservation_forbids_other_branch():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    other_branch_id, _ = await _make_branch([("C", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await ReservationService.book_table(
            customer_id=customer_id, branch_id=branch_id,
            start_time=_slot(days=403, hour=18), party_size=2,
        )
        with pytest.raises(ForbiddenError):
            await ReservationService.update_reservation(
                reservation.id, {"status": "seated"}, staff=_staff(other_branch_id)
            )
    finally:
        await _cleanup(branch_id, customer_id)
        await _cleanup(other_branch_id)


async def test_update_reservation_invalid_status():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await ReservationService.book_table(
            customer_id=customer_id, branch_id=branch_id,
            start_time=_slot(days=404, hour=18), party_size=2,
        )
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"status": "bogus"}
            )
    finally:
        await _cleanup(branch_id, customer_id)


async def test_update_reservation_applies_changes():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "indoor", 4)])
    customer_id = await _make_customer()
    try:
        reservation = await ReservationService.book_table(
            customer_id=customer_id, branch_id=branch_id,
            start_time=_slot(days=405, hour=18), party_size=2,
        )
        updated = await ReservationService.update_reservation(
            reservation.id,
            {"status": "seated", "table_id": table_ids[1], "notes": "window seat"},
            staff=_staff(branch_id),
        )
        assert updated.status == "seated"
        assert updated.table_id == table_ids[1]
        assert updated.notes == "window seat"
    finally:
        await _cleanup(branch_id, customer_id)
