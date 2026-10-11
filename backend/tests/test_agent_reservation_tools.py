"""Task9 tool tests: ``check_table_availability_and_book`` is a thin adapter.

The tool must hold no business rules and no database access of its own: it
delegates all table selection / overlap / insert work to ReservationService
(verified here by monkeypatching the Service) and only translates the result --
and domain exceptions -- back into the legacy dict contract the reservation node
depends on.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from backend.app.agent.tools import table_tools
from backend.app.agent.tools.table_tools import check_table_availability_and_book
from backend.app.exceptions import (
    NotFoundError,
    ReservationRaceError,
    TableUnavailableError,
)
from backend.app.service.reservation import ReservationService

TOOL_SOURCE = Path(table_tools.__file__).read_text(encoding='utf-8')


def _patch(monkeypatch, *, reservation=None, exc=None):
    """Replace ReservationService.book_table and record how the tool calls it."""
    calls = {}

    async def _book_stub(customer_id, branch_id, start_time, party_size, **kwargs):
        calls['book'] = (customer_id, branch_id, start_time, party_size, kwargs)
        if exc is not None:
            raise exc
        return reservation

    monkeypatch.setattr(ReservationService, 'book_table', _book_stub)
    return calls


def _reservation():
    start = datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)
    table = SimpleNamespace(label='T1', seating_area='indoor')
    branch = SimpleNamespace(name='Downtown')
    return SimpleNamespace(
        id=42, status='confirmed', branch=branch, table=table, start_time=start
    )


async def test_adapter_has_no_database_access():
    """The tool must not open sessions or import SQLAlchemy itself."""
    assert 'AsyncSessionLocal' not in TOOL_SOURCE
    assert 'sqlalchemy' not in TOOL_SOURCE
    assert 'tstzrange' not in TOOL_SOURCE


async def test_success_delegates_and_returns_legacy_shape(monkeypatch):
    calls = _patch(monkeypatch, reservation=_reservation())
    result = await check_table_availability_and_book(
        customer_id=3,
        branch_id=1,
        start_time=datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc),
        party_size=2,
        seating_area='indoor',
    )
    assert result['success'] is True
    assert result['reservation_id'] == 42
    assert result['status'] == 'confirmed'
    assert result['branch_name'] == 'Downtown'
    assert result['table_label'] == 'T1'
    assert result['seating_area'] == 'indoor'
    assert result['date'] == '2026-10-20'
    assert result['time'] == '07:00 PM'
    assert result['party_size'] == 2
    assert result['duration_minutes'] == 90
    assert calls['book'][0] == 3
    assert calls['book'][1] == 1
    assert calls['book'][3] == 2


async def test_table_unavailable_sets_alternatives_flag(monkeypatch):
    msg = 'No tables currently available for 2 guests in the rooftop area at 07:00 PM.'
    _patch(monkeypatch, exc=TableUnavailableError(msg))
    result = await check_table_availability_and_book(
        customer_id=1,
        branch_id=1,
        start_time=datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc),
        party_size=2,
        seating_area='rooftop',
    )
    assert result['success'] is False
    assert result['error'] == msg
    assert result['alternatives_available'] is True


async def test_race_conflict_sets_race_flag(monkeypatch):
    exc = ReservationRaceError('That table was just reserved moments ago.')
    _patch(monkeypatch, exc=exc)
    result = await check_table_availability_and_book(
        customer_id=1,
        branch_id=1,
        start_time=datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc),
        party_size=2,
    )
    assert result['success'] is False
    assert result['error'] == exc.message
    assert result['race_conflict'] is True
    assert 'alternatives_available' not in result


async def test_branch_not_found_becomes_plain_error(monkeypatch):
    _patch(monkeypatch, exc=NotFoundError('Branch #5 not found.'))
    result = await check_table_availability_and_book(
        customer_id=1,
        branch_id=5,
        start_time=datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc),
        party_size=2,
    )
    assert result == {'success': False, 'error': 'Branch #5 not found.'}


async def test_naive_start_time_is_normalised_to_utc(monkeypatch):
    calls = _patch(monkeypatch, reservation=_reservation())
    await check_table_availability_and_book(
        customer_id=1,
        branch_id=1,
        start_time=datetime(2026, 10, 20, 19, 0),
        party_size=2,
    )
    passed_start = calls['book'][2]
    assert passed_start.tzinfo is not None
    assert passed_start.utcoffset().total_seconds() == 0
