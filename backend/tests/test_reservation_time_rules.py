"""Reservation time rules (OpenItems section 11).

``ReservationService.book_table`` used to check only whether a table was free --
the time itself was unvalidated, so a booking in the past, after closing, or
half a year away all went through, and the "that slot is full" reply pointed at
no concrete alternative. This suite pins the three rules that now apply to every
booking entry point (the customer form, the chat Agent and the staff reschedule):

* the start must be at least ``MIN_BOOKING_LEAD_MINUTES`` ahead and no more than
  ``MAX_BOOKING_DAYS`` out;
* the whole sitting must fit inside the branch's ``opening_hours`` (``HH:MM -
  HH:MM``), and an unparseable value is itself rejected;
* a full slot is explained with a nearby time that really is free.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import ValidationError
from backend.app.models.branch import Branch, Table
from backend.app.models.reservation import Reservation
from backend.app.opening_hours import (
    OPENING_HOURS_FORMAT_ERROR,
    format_clock,
    parse_opening_hours,
)
from backend.app.service.branch import BranchService
from backend.app.service.reservation import (
    MAX_BOOKING_DAYS,
    MIN_BOOKING_LEAD_MINUTES,
    ReservationService,
    current_wall_time,
    validate_bookable_slot,
)

VALID_HOURS = "10:00 - 22:00"


def _at(days, hour, minute=0):
    """A UTC slot ``days`` from now, at the given wall-clock hour."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


async def _make_branch(opening_hours=VALID_HOURS, *, tables=(("T1", "indoor", 2),)):
    async with transactional_session() as s:
        branch = Branch(
            name=f"__tr{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours=opening_hours,
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        table_ids = []
        for label, area, capacity in tables:
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


async def _cleanup(branch_id):
    async with transactional_session() as s:
        await s.execute(delete(Reservation).where(Reservation.branch_id == branch_id))
        await s.execute(delete(Branch).where(Branch.id == branch_id))


async def _book(branch_id, start, *, party_size=2, area="indoor", duration_minutes=90):
    return await ReservationService.book_table(
        customer_id=1,
        branch_id=branch_id,
        start_time=start,
        party_size=party_size,
        duration_minutes=duration_minutes,
        seating_area=area,
    )


# --------------------------------------------------------------------------- #
# parse_opening_hours
# --------------------------------------------------------------------------- #
def test_parse_opening_hours_reads_a_valid_range():
    assert parse_opening_hours("10:00 - 22:00") == (10 * 60, 22 * 60)
    assert parse_opening_hours("00:00 - 23:59") == (0, 23 * 60 + 59)
    # Single-digit hours and loose spacing are accepted.
    assert parse_opening_hours("9:30-18:00") == (9 * 60 + 30, 18 * 60)
    assert format_clock(9 * 60 + 30) == "09:30"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "10:00 AM - 10:00 PM",  # the old default -- no longer accepted
        "10:00",  # missing the closing time
        "22:00 - 10:00",  # overnight span
        "10:00 - 10:00",  # zero-length day
        "24:00 - 25:00",  # out of range
        "10:70 - 22:00",  # bad minutes
    ],
)
def test_parse_opening_hours_rejects_malformed_values(value):
    with pytest.raises(ValidationError) as excinfo:
        parse_opening_hours(value)
    assert excinfo.value.message == OPENING_HOURS_FORMAT_ERROR


def test_validate_bookable_slot_accepts_a_good_slot():
    # 20:00 + 90 minutes = 21:30, inside a 10:00-22:00 day.
    validate_bookable_slot(_at(1, 20), 90, VALID_HOURS)


def test_lead_time_boundary_is_strict():
    """18:45 -> 19:00 (exactly 15 minutes out) is refused; 16 minutes is fine."""
    now = _at(1, 18, 45)
    with pytest.raises(ValidationError):
        validate_bookable_slot(_at(1, 19, 0), 90, "00:00 - 23:59", now=now)
    validate_bookable_slot(_at(1, 19, 1), 90, "00:00 - 23:59", now=now)


def test_last_seating_is_close_minus_duration():
    """With a 90-minute sitting and a 22:00 close, 20:30 is the last start."""
    now = _at(1, 10, 0)
    validate_bookable_slot(_at(1, 20, 30), 90, VALID_HOURS, now=now)
    with pytest.raises(ValidationError):
        validate_bookable_slot(_at(1, 21, 0), 90, VALID_HOURS, now=now)


# --------------------------------------------------------------------------- #
# book_table enforces the window
# --------------------------------------------------------------------------- #
async def test_booking_in_the_past_is_rejected():
    branch_id, _ = await _make_branch()
    try:
        with pytest.raises(ValidationError):
            await _book(branch_id, _at(-1, 19))
    finally:
        await _cleanup(branch_id)


async def test_booking_inside_the_lead_time_is_rejected():
    branch_id, _ = await _make_branch()
    try:
        soon = current_wall_time() + timedelta(
            minutes=MIN_BOOKING_LEAD_MINUTES - 1
        )
        with pytest.raises(ValidationError):
            await _book(branch_id, soon)
    finally:
        await _cleanup(branch_id)


async def test_booking_a_slot_that_already_passed_locally_is_rejected():
    """The bug this rule exists to close: a slot hours in the local past is still
    "in the future" in UTC (the branch's local day runs ahead of UTC), so the
    comparison must use the local wall clock, not the UTC instant."""
    branch_id, _ = await _make_branch(opening_hours="00:00 - 23:59")
    try:
        with pytest.raises(ValidationError):
            await _book(branch_id, current_wall_time() - timedelta(hours=1))
    finally:
        await _cleanup(branch_id)


async def test_booking_too_far_ahead_is_rejected():
    branch_id, _ = await _make_branch()
    try:
        with pytest.raises(ValidationError):
            await _book(branch_id, _at(MAX_BOOKING_DAYS + 1, 19))
    finally:
        await _cleanup(branch_id)


async def test_booking_before_opening_is_rejected():
    branch_id, _ = await _make_branch()
    try:
        with pytest.raises(ValidationError):
            await _book(branch_id, _at(1, 8))
    finally:
        await _cleanup(branch_id)


async def test_booking_that_would_run_past_closing_is_rejected():
    branch_id, _ = await _make_branch()
    try:
        # 21:30 + 90 minutes = 23:00, past the 22:00 close.
        with pytest.raises(ValidationError):
            await _book(branch_id, _at(1, 21, 30))
    finally:
        await _cleanup(branch_id)


async def test_booking_with_unparseable_opening_hours_is_rejected():
    branch_id, _ = await _make_branch(opening_hours="mornings only")
    try:
        with pytest.raises(ValidationError):
            await _book(branch_id, _at(1, 19))
    finally:
        await _cleanup(branch_id)


async def test_booking_within_hours_succeeds():
    branch_id, table_ids = await _make_branch()
    try:
        reservation = await _book(branch_id, _at(1, 19))
        assert reservation.table_id == table_ids[0]
    finally:
        await _cleanup(branch_id)


# --------------------------------------------------------------------------- #
# the availability query agrees with booking
# --------------------------------------------------------------------------- #
async def test_availability_applies_the_same_window_rules():
    branch_id, _ = await _make_branch()
    try:
        with pytest.raises(ValidationError):
            await ReservationService.list_available_tables(
                branch_id, _at(-1, 19), party_size=2
            )
        with pytest.raises(ValidationError):
            await ReservationService.list_available_tables(
                branch_id, _at(1, 8), party_size=2
            )
        # A good slot still answers.
        assert await ReservationService.list_available_tables(
            branch_id, _at(1, 19), party_size=2
        )
    finally:
        await _cleanup(branch_id)


# --------------------------------------------------------------------------- #
# the staff reschedule shares the rules
# --------------------------------------------------------------------------- #
async def test_reschedule_into_a_closed_slot_is_rejected():
    branch_id, _ = await _make_branch()
    try:
        reservation = await _book(branch_id, _at(1, 19))
        with pytest.raises(ValidationError):
            await ReservationService.update_reservation(
                reservation.id, {"start_time": _at(1, 8)}
            )
        # The booking is untouched.
        async with transactional_session() as s:
            reloaded = await s.get(Reservation, reservation.id)
            assert reloaded.start_time.hour == 19
    finally:
        await _cleanup(branch_id)


# --------------------------------------------------------------------------- #
# concrete slot suggestions
# --------------------------------------------------------------------------- #
async def test_full_slot_suggests_a_genuinely_free_time():
    """The only table is busy at 19:00, so 19:00 is refused -- but the reply must
    point at a nearby time that really is bookable."""
    branch_id, _ = await _make_branch(tables=(("only", "indoor", 2),))
    try:
        await _book(branch_id, _at(2, 19))  # 19:00-20:30 taken
        with pytest.raises(Exception) as excinfo:
            await _book(branch_id, _at(2, 19))
        message = getattr(excinfo.value, "message", str(excinfo.value))
        assert "暂时没有可供" in message
        # 20:30 is the first free slot after the taken window; it must be named.
        assert "20:30" in message

        # ...and the suggestion is honest: booking it succeeds.
        later = await _book(branch_id, _at(2, 20, 30))
        assert later.status == "pending"
    finally:
        await _cleanup(branch_id)


# --------------------------------------------------------------------------- #
# stored data
# --------------------------------------------------------------------------- #
async def test_seeded_branches_have_parseable_opening_hours():
    async with transactional_session() as s:
        branches = (await s.execute(Branch.__table__.select())).mappings().all()
    assert branches
    for row in branches:
        parse_opening_hours(row["opening_hours"])


async def test_branch_service_rejects_a_bad_opening_hours_edit():
    branch_id, _ = await _make_branch()
    try:
        with pytest.raises(ValidationError):
            await BranchService.update_branch(branch_id, {"opening_hours": "whenever"})
        # A valid edit still goes through.
        updated = await BranchService.update_branch(
            branch_id, {"opening_hours": "11:00 - 23:30"}
        )
        assert updated.opening_hours == "11:00 - 23:30"
    finally:
        await _cleanup(branch_id)
