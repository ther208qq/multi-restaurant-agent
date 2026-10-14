"""Task17 tests: reservation lifecycle -- transitions, reschedule, availability.

Spec §9: the reservation lifecycle is ``pending -> confirmed -> seated ->
completed``. A booking starts ``pending`` (staff confirm it), ``cancelled`` is
reachable from any non-terminal status and releases the table slot, ``completed``
is terminal, and a cancelled booking can be restored to ``pending`` only while
its slot is still free. Pending / confirmed / seated all *occupy* their table, so
a slot can never be double-booked -- enforced by the application probe and, as
the backstop, by the PostgreSQL exclusion constraint.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, func

from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    NotFoundError,
    TableUnavailableError,
    ValidationError,
)
from backend.app.models.branch import Branch, Table
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer
from backend.app.repo.reservation import ReservationRepo
from backend.app.service.reservation import (
    OCCUPYING_RESERVATION_STATUSES,
    RESERVATION_TRANSITIONS,
    VALID_RESERVATION_STATUSES,
    ReservationService,
)

CHAIN = ("pending", "confirmed", "seated", "completed")


def _slot(days=500, hour=19, minute=0):
    # ``days`` is a per-test slot discriminator, not a literal day count: fold it
    # into the bookable horizon (1..25 days) so every slot stays inside the
    # 30-day booking window the service now enforces.
    offset = days % 25 + 1
    return (datetime.now(timezone.utc) + timedelta(days=offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


async def _make_branch(specs):
    """Create an isolated branch with tables; specs = [(label, area, capacity)]."""
    async with transactional_session() as s:
        branch = Branch(
            name='__rl_branch', address='x', phone='x',
            opening_hours='10:00 - 23:59', is_active=True,
        )
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
        customer = Customer(name="__rl", email=f"{uuid4().hex}@x.com", password_hash="x")
        s.add(customer)
        await s.flush()
        return customer.id


async def _cleanup(branch_id, customer_id=None):
    async with transactional_session() as s:
        await s.execute(delete(Reservation).where(Reservation.branch_id == branch_id))
        await s.execute(delete(Branch).where(Branch.id == branch_id))
        if customer_id is not None:
            await s.execute(delete(Customer).where(Customer.id == customer_id))


async def _book(branch_id, customer_id, days=500, hour=19, party_size=2, area="indoor"):
    return await ReservationService.book_table(
        customer_id=customer_id,
        branch_id=branch_id,
        start_time=_slot(days, hour),
        party_size=party_size,
        seating_area=area,
    )


async def _status(reservation_id):
    async with transactional_session() as s:
        reservation = await ReservationRepo.get_reservation(s, reservation_id)
        return reservation.status if reservation else None


async def _reach(reservation_id, target):
    """Drive a fresh booking to ``target`` along the legal chain."""
    for status in CHAIN[1 : CHAIN.index(target) + 1]:
        await ReservationService.update_reservation(reservation_id, {"status": status})


def test_lifecycle_vocabulary_and_matrix_are_complete():
    assert set(VALID_RESERVATION_STATUSES) == set(CHAIN) | {"cancelled"}
    assert set(RESERVATION_TRANSITIONS) == set(VALID_RESERVATION_STATUSES)
    # ``completed`` is terminal and only a cancelled booking can be restored.
    assert RESERVATION_TRANSITIONS["completed"] == frozenset()
    assert RESERVATION_TRANSITIONS["cancelled"] == frozenset({"pending"})
    # Every occupying status is a real status, and none of them is terminal.
    assert set(OCCUPYING_RESERVATION_STATUSES) <= set(VALID_RESERVATION_STATUSES)
    assert RESERVATION_TRANSITIONS["seated"] == frozenset({"completed", "cancelled"})


async def test_booking_starts_pending_and_holds_its_table():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        assert reservation.status == "pending"
        assert reservation.table_id == table_ids[0]
    finally:
        await _cleanup(branch_id, customer_id)


async def test_pending_booking_occupies_the_table():
    """A pending booking must block the slot -- not just a confirmed one."""
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        first = await _book(branch_id, customer_id)
        assert first.status == "pending"
        with pytest.raises(TableUnavailableError):
            await _book(branch_id, customer_id)
    finally:
        await _cleanup(branch_id, customer_id)


async def test_happy_path_pending_confirmed_seated_completed():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        for status in ("confirmed", "seated", "completed"):
            updated = await ReservationService.update_reservation(
                reservation.id, {"status": status}
            )
            assert updated.status == status
    finally:
        await _cleanup(branch_id, customer_id)


async def test_same_status_is_a_noop():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        again = await ReservationService.update_reservation(
            reservation.id, {"status": "pending"}
        )
        assert again.status == "pending"
    finally:
        await _cleanup(branch_id, customer_id)


ILLEGAL_TRANSITIONS = [
    ("pending", "seated"),  # must be confirmed first
    ("pending", "completed"),
    ("confirmed", "completed"),  # must be seated first
    ("confirmed", "pending"),  # no going backwards
    ("seated", "confirmed"),
    ("seated", "pending"),
    ("completed", "cancelled"),  # terminal
    ("completed", "seated"),
    ("completed", "pending"),
    ("cancelled", "confirmed"),  # only -> pending allowed
    ("cancelled", "seated"),
    ("cancelled", "completed"),
]


@pytest.mark.parametrize("start,target", ILLEGAL_TRANSITIONS)
async def test_illegal_transition_is_rejected_and_status_unchanged(start, target):
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        if start == "cancelled":
            await ReservationService.update_reservation(
                reservation.id, {"status": "cancelled"}
            )
        elif start != "pending":
            await _reach(reservation.id, start)
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"status": target}
            )
        assert await _status(reservation.id) == start
    finally:
        await _cleanup(branch_id, customer_id)


@pytest.mark.parametrize("start", ["pending", "confirmed", "seated"])
async def test_cancel_from_any_non_terminal_state_releases_the_slot(start):
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        if start != "pending":
            await _reach(reservation.id, start)

        cancelled = await ReservationService.update_reservation(
            reservation.id, {"status": "cancelled"}
        )
        assert cancelled.status == "cancelled"
        # The table is free again, so the same slot can be booked anew.
        replacement = await _book(branch_id, customer_id)
        assert replacement.table_id == reservation.table_id
    finally:
        await _cleanup(branch_id, customer_id)


async def test_restore_cancelled_booking_when_slot_is_free():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        await ReservationService.update_reservation(reservation.id, {"status": "cancelled"})
        restored = await ReservationService.update_reservation(
            reservation.id, {"status": "pending"}
        )
        assert restored.status == "pending"
    finally:
        await _cleanup(branch_id, customer_id)


async def test_restore_cancelled_booking_conflicts_when_slot_was_retaken():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id)
        await ReservationService.update_reservation(reservation.id, {"status": "cancelled"})
        # Someone else takes the freed slot on the same table.
        await _book(branch_id, customer_id)

        with pytest.raises(ConflictError):
            await ReservationService.update_reservation(
                reservation.id, {"status": "pending"}
            )
        assert await _status(reservation.id) == "cancelled"
    finally:
        await _cleanup(branch_id, customer_id)


async def test_seating_a_booking_without_a_table_assigns_one():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        async with transactional_session() as s:
            orphan = Reservation(
                customer_id=customer_id,
                branch_id=branch_id,
                table_id=None,
                party_size=2,
                start_time=_slot(days=501, hour=19),
                end_time=_slot(days=501, hour=20, minute=30),
                duration_minutes=90,
                seating_area_preference="indoor",
                status="confirmed",
                booking_window=func.tstzrange(
                    _slot(days=501, hour=19), _slot(days=501, hour=20, minute=30), "[)"
                ),
            )
            s.add(orphan)
            await s.flush()
            orphan_id = orphan.id

        seated = await ReservationService.update_reservation(
            orphan_id, {"status": "seated"}
        )
        assert seated.status == "seated"
        assert seated.table_id == table_ids[0]
    finally:
        await _cleanup(branch_id, customer_id)


# --------------------------------------------------------------------------- #
# Availability query
# --------------------------------------------------------------------------- #


async def test_list_available_tables_filters_by_area_and_party_size():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "outdoor", 6)])
    try:
        all_tables = await ReservationService.list_available_tables(
            branch_id, _slot(days=510), party_size=2
        )
        assert {t.id for t in all_tables} == set(table_ids)

        indoor = await ReservationService.list_available_tables(
            branch_id, _slot(days=510), party_size=2, seating_area="indoor"
        )
        assert [t.id for t in indoor] == [table_ids[0]]

        big_party = await ReservationService.list_available_tables(
            branch_id, _slot(days=510), party_size=3
        )
        assert [t.id for t in big_party] == [table_ids[1]]
    finally:
        await _cleanup(branch_id)


async def test_list_available_tables_excludes_occupied_slots():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        await _book(branch_id, customer_id, days=511)
        free = await ReservationService.list_available_tables(
            branch_id, _slot(days=511), party_size=2, seating_area="indoor"
        )
        assert len(free) == 1

        # A booked-out area yields an empty list, not an error.
        await _book(branch_id, customer_id, days=511)
        assert await ReservationService.list_available_tables(
            branch_id, _slot(days=511), party_size=2, seating_area="indoor"
        ) == []
    finally:
        await _cleanup(branch_id, customer_id)


async def test_list_available_tables_rejects_unknown_branch_and_bad_party_size():
    with pytest.raises(NotFoundError):
        await ReservationService.list_available_tables(
            999999, _slot(days=512), party_size=2
        )
    with pytest.raises(ValidationError):
        await ReservationService.list_available_tables(1, _slot(days=512), party_size=0)


# --------------------------------------------------------------------------- #
# Reschedule / re-table
# --------------------------------------------------------------------------- #


async def test_reschedule_keeps_the_table_and_frees_the_old_slot():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=520, hour=19)
        moved = await ReservationService.update_reservation(
            reservation.id,
            {"start_time": _slot(days=520, hour=21)},
        )
        assert moved.start_time.hour == 21
        assert moved.end_time.hour == 22 and moved.end_time.minute == 30
        assert moved.table_id == table_ids[0]

        # The old window is genuinely released (booking_window moved with it),
        # and the new one blocks as expected.
        await _book(branch_id, customer_id, days=520, hour=19)
        with pytest.raises(TableUnavailableError):
            await _book(branch_id, customer_id, days=520, hour=21)
    finally:
        await _cleanup(branch_id, customer_id)


async def test_reschedule_repicks_a_table_when_the_current_one_is_taken():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        first = await _book(branch_id, customer_id, days=521, hour=19)
        pinned_table = first.table_id
        second = await _book(branch_id, customer_id, days=521, hour=21)
        # Pin the later booking onto the same table, so that table is busy at 21:00.
        await ReservationService.update_reservation(second.id, {"table_id": pinned_table})

        # Moving the first booking into the second one's window must land on the
        # table that is still free instead of being refused.
        moved = await ReservationService.update_reservation(
            first.id, {"start_time": _slot(days=521, hour=21)}
        )
        assert moved.table_id != pinned_table
        assert moved.table_id in table_ids
    finally:
        await _cleanup(branch_id, customer_id)


async def test_reschedule_onto_a_pinned_busy_table_conflicts():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        first = await _book(branch_id, customer_id, days=522, hour=19)
        await _book(branch_id, customer_id, days=522, hour=21)

        with pytest.raises(ConflictError):
            await ReservationService.update_reservation(
                first.id,
                {"start_time": _slot(days=522, hour=21), "table_id": table_ids[0]},
            )
        assert await _status(first.id) == "pending"
    finally:
        await _cleanup(branch_id, customer_id)


async def test_reschedule_reports_unavailable_when_nothing_is_free():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        first = await _book(branch_id, customer_id, days=523, hour=19)
        await _book(branch_id, customer_id, days=523, hour=21)
        with pytest.raises(TableUnavailableError):
            await ReservationService.update_reservation(
                first.id, {"start_time": _slot(days=523, hour=21)}
            )
    finally:
        await _cleanup(branch_id, customer_id)


async def test_reschedule_grows_the_party_onto_a_bigger_table():
    branch_id, table_ids = await _make_branch([("Small", "indoor", 2), ("Big", "indoor", 6)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=524)
        assert reservation.table_id == table_ids[0]
        moved = await ReservationService.update_reservation(
            reservation.id, {"party_size": 5}
        )
        assert moved.party_size == 5
        assert moved.table_id == table_ids[1]
    finally:
        await _cleanup(branch_id, customer_id)


@pytest.mark.parametrize("start", ["seated", "completed", "cancelled"])
async def test_reschedule_is_rejected_outside_the_reschedulable_states(start):
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=525)
        if start == "cancelled":
            await ReservationService.update_reservation(
                reservation.id, {"status": "cancelled"}
            )
        else:
            await _reach(reservation.id, start)
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"start_time": _slot(days=525, hour=22)}
            )
    finally:
        await _cleanup(branch_id, customer_id)


async def test_table_change_is_allowed_while_seated():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=526)
        await _reach(reservation.id, "seated")
        moved = await ReservationService.update_reservation(
            reservation.id, {"table_id": table_ids[1]}
        )
        assert moved.table_id == table_ids[1]
        assert moved.status == "seated"
    finally:
        await _cleanup(branch_id, customer_id)


async def test_table_change_is_rejected_for_cancelled_bookings():
    branch_id, table_ids = await _make_branch([("A", "indoor", 2), ("B", "indoor", 2)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=527)
        await ReservationService.update_reservation(reservation.id, {"status": "cancelled"})
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"table_id": table_ids[1]}
            )
    finally:
        await _cleanup(branch_id, customer_id)


async def test_table_change_validates_capacity_branch_and_existence():
    branch_id, table_ids = await _make_branch([("Small", "indoor", 2), ("Big", "indoor", 6)])
    other_branch_id, other_table_ids = await _make_branch([("Far", "indoor", 4)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=528, party_size=4)
        assert reservation.table_id == table_ids[1]

        # Too small for the party.
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"table_id": table_ids[0]}
            )
        # Belongs to another branch.
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"table_id": other_table_ids[0]}
            )
        # Does not exist.
        with pytest.raises(NotFoundError):
            await ReservationService.update_reservation(
                reservation.id, {"table_id": 999999}
            )
    finally:
        await _cleanup(branch_id, customer_id)
        await _cleanup(other_branch_id)


async def test_reschedule_validates_party_size_and_duration():
    branch_id, table_ids = await _make_branch([("A", "indoor", 4)])
    customer_id = await _make_customer()
    try:
        reservation = await _book(branch_id, customer_id, days=529)
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(reservation.id, {"party_size": 0})
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"duration_minutes": 0}
            )
    finally:
        await _cleanup(branch_id, customer_id)


async def test_booking_rejects_non_positive_party_size():
    branch_id, _ = await _make_branch([("A", "indoor", 2)])
    try:
        with pytest.raises(ValidationError):
            await _book(branch_id, 1, days=530, party_size=0)
    finally:
        await _cleanup(branch_id)
