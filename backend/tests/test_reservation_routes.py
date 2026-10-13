"""Task9 route tests: staff reservation endpoints served through ReservationService.

These verify the migrated API layer keeps its previous HTTP behavior (status
codes, branch scoping, status/date filters, staff updates) while all booking and
management rules now live in the Service.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.branch import Branch, Table
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer, StaffUser
from backend.app.service.reservation import ReservationService


def _staff_headers(staff_id: int) -> dict:
    token = create_access_token(
        {'sub': str(staff_id), 'type': 'staff', 'username': f'__rr{staff_id}', 'role': 'staff'}
    )
    return {'Authorization': f'Bearer {token}'}


def _slot(days: int, hour: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


async def _make_branch(label: str) -> int:
    async with transactional_session() as s:
        branch = Branch(name='__rr_branch', address='x', phone='x', is_active=True)
        s.add(branch)
        await s.flush()
        s.add(
            Table(
                branch_id=branch.id,
                label=label,
                seating_area='indoor',
                capacity=4,
                is_available=True,
            )
        )
        await s.flush()
        return branch.id


async def _make_customer() -> int:
    async with transactional_session() as s:
        c = Customer(
            name='__rr',
            email=f'__rr{datetime.now().timestamp()}@x.com',
            password_hash='x',
        )
        s.add(c)
        await s.flush()
        return c.id


async def _make_staff(branch_id) -> int:
    async with transactional_session() as s:
        staff = StaffUser(
            username=f'__rr_s_{branch_id}',
            password_hash='x',
            role='staff',
            branch_id=branch_id,
        )
        s.add(staff)
        await s.flush()
        return staff.id


async def _book(branch_id: int, customer_id: int, days: int = 320, hour: int = 18):
    return await ReservationService.book_table(
        customer_id=customer_id,
        branch_id=branch_id,
        start_time=_slot(days, hour),
        party_size=2,
    )


async def _cleanup(branch_id, customer_id=None, staff_id=None):
    async with transactional_session() as s:
        await s.execute(delete(Reservation).where(Reservation.branch_id == branch_id))
        await s.execute(delete(Branch).where(Branch.id == branch_id))
        if staff_id is not None:
            await s.execute(delete(StaffUser).where(StaffUser.id == staff_id))
        if customer_id is not None:
            await s.execute(delete(Customer).where(Customer.id == customer_id))


async def test_list_reservations_requires_auth(async_client):
    resp = await async_client.get('/api/staff/reservations')
    assert resp.status_code == 401


async def test_list_reservations_scoped_to_staff_branch(async_client):
    b1 = await _make_branch('R1')
    b2 = await _make_branch('R2')
    customer = await _make_customer()
    staff_id = await _make_staff(b1)
    try:
        r1 = await _book(b1, customer, days=321, hour=18)
        r2 = await _book(b2, customer, days=322, hour=18)
        resp = await async_client.get(
            '/api/staff/reservations', headers=_staff_headers(staff_id)
        )
        assert resp.status_code == 200
        ids = [r['id'] for r in resp.json()]
        assert r1.id in ids
        assert r2.id not in ids
    finally:
        await _cleanup(b1, customer, staff_id)
        await _cleanup(b2)


async def test_list_reservations_admin_filters_by_branch_and_status(
    async_client, staff_auth_headers
):
    b1 = await _make_branch('R3')
    customer = await _make_customer()
    try:
        r1 = await _book(b1, customer, days=323, hour=18)

        by_branch = await async_client.get(
            f'/api/staff/reservations?branch_id={b1}', headers=staff_auth_headers
        )
        assert by_branch.status_code == 200
        assert any(r['id'] == r1.id for r in by_branch.json())

        pending = await async_client.get(
            f'/api/staff/reservations?branch_id={b1}&status=pending',
            headers=staff_auth_headers,
        )
        assert any(r['id'] == r1.id for r in pending.json())

        seated = await async_client.get(
            f'/api/staff/reservations?branch_id={b1}&status=seated',
            headers=staff_auth_headers,
        )
        assert all(r['id'] != r1.id for r in seated.json())
    finally:
        await _cleanup(b1, customer)


async def test_list_reservations_filters_by_date(async_client, staff_auth_headers):
    b1 = await _make_branch('R4')
    customer = await _make_customer()
    try:
        r1 = await _book(b1, customer, days=324, hour=18)
        target = _slot(324, 18).strftime('%Y-%m-%d')
        same_day = await async_client.get(
            f'/api/staff/reservations?branch_id={b1}&date={target}',
            headers=staff_auth_headers,
        )
        assert any(r['id'] == r1.id for r in same_day.json())

        other = (_slot(324, 18) + timedelta(days=1)).strftime('%Y-%m-%d')
        other_day = await async_client.get(
            f'/api/staff/reservations?branch_id={b1}&date={other}',
            headers=staff_auth_headers,
        )
        assert all(r['id'] != r1.id for r in other_day.json())
    finally:
        await _cleanup(b1, customer)


async def test_patch_reservation_updates_status_and_notes(
    async_client, staff_auth_headers
):
    b1 = await _make_branch('R5')
    customer = await _make_customer()
    try:
        r1 = await _book(b1, customer, days=325, hour=18)
        # A fresh booking is pending: confirm it first, then seat it.
        confirmed = await async_client.patch(
            f'/api/staff/reservations/{r1.id}',
            json={'status': 'confirmed'},
            headers=staff_auth_headers,
        )
        assert confirmed.status_code == 200
        assert confirmed.json()['status'] == 'confirmed'

        resp = await async_client.patch(
            f'/api/staff/reservations/{r1.id}',
            json={'status': 'seated', 'notes': 'window seat'},
            headers=staff_auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['status'] == 'seated'
        assert body['notes'] == 'window seat'
        assert body['branch_name'] == '__rr_branch'
    finally:
        await _cleanup(b1, customer)


async def test_patch_reservation_not_found(async_client, staff_auth_headers):
    resp = await async_client.patch(
        '/api/staff/reservations/999999999',
        json={'status': 'seated'},
        headers=staff_auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()['detail']


async def test_patch_reservation_invalid_status(async_client, staff_auth_headers):
    b1 = await _make_branch('R6')
    customer = await _make_customer()
    try:
        r1 = await _book(b1, customer, days=326, hour=18)
        resp = await async_client.patch(
            f'/api/staff/reservations/{r1.id}',
            json={'status': 'bogus'},
            headers=staff_auth_headers,
        )
        assert resp.status_code == 400
    finally:
        await _cleanup(b1, customer)


async def test_patch_reservation_forbidden_for_other_branch(async_client):
    b1 = await _make_branch('R7')
    b2 = await _make_branch('R8')
    customer = await _make_customer()
    staff_id = await _make_staff(b2)
    try:
        r1 = await _book(b1, customer, days=327, hour=18)
        resp = await async_client.patch(
            f'/api/staff/reservations/{r1.id}',
            json={'status': 'seated'},
            headers=_staff_headers(staff_id),
        )
        assert resp.status_code == 403
    finally:
        await _cleanup(b1, customer, staff_id)
        await _cleanup(b2)


async def test_patch_reservation_reschedules_the_booking(async_client, staff_auth_headers):
    """Task17: the staff API can move a booking to a new slot."""
    b1 = await _make_branch('R9')
    customer = await _make_customer()
    try:
        r1 = await _book(b1, customer, days=328, hour=18)
        new_start = _slot(328, 21)
        resp = await async_client.patch(
            f'/api/staff/reservations/{r1.id}',
            json={'start_time': new_start.isoformat(), 'party_size': 4},
            headers=staff_auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['party_size'] == 4
        assert datetime.fromisoformat(body['start_time']).hour == 21
        assert body['status'] == 'pending'
    finally:
        await _cleanup(b1, customer)


async def test_patch_reservation_rejects_illegal_transition(async_client, staff_auth_headers):
    b1 = await _make_branch('R10')
    customer = await _make_customer()
    try:
        r1 = await _book(b1, customer, days=329, hour=18)
        resp = await async_client.patch(
            f'/api/staff/reservations/{r1.id}',
            json={'status': 'completed'},  # pending must be confirmed/seated first
            headers=staff_auth_headers,
        )
        assert resp.status_code == 400
    finally:
        await _cleanup(b1, customer)


async def test_table_availability_requires_auth(async_client):
    resp = await async_client.get(
        '/api/staff/reservations/availability?branch_id=1&date=2030-01-01&time=19:00'
    )
    assert resp.status_code == 401


async def test_table_availability_lists_bookable_tables(
    async_client, staff_auth_headers
):
    b1 = await _make_branch('R11')
    try:
        target = _slot(330, 18)
        resp = await async_client.get(
            f'/api/staff/reservations/availability?branch_id={b1}'
            f'&date={target.strftime("%Y-%m-%d")}&time={target.strftime("%H:%M")}'
            f'&party_size=4',
            headers=staff_auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [t['label'] for t in body] == ['R11']
        assert body[0]['seating_area'] == 'indoor'
        assert body[0]['capacity'] == 4

        # Nothing fits a party of 10 at that branch.
        too_big = await async_client.get(
            f'/api/staff/reservations/availability?branch_id={b1}'
            f'&date={target.strftime("%Y-%m-%d")}&time={target.strftime("%H:%M")}'
            f'&party_size=10',
            headers=staff_auth_headers,
        )
        assert too_big.json() == []
    finally:
        await _cleanup(b1)


async def test_table_availability_rejects_bad_datetime(async_client, staff_auth_headers):
    b1 = await _make_branch('R12')
    try:
        resp = await async_client.get(
            f'/api/staff/reservations/availability?branch_id={b1}&date=not-a-date&time=19:00',
            headers=staff_auth_headers,
        )
        assert resp.status_code == 400
    finally:
        await _cleanup(b1)


async def test_table_availability_unknown_branch_is_404(async_client, staff_auth_headers):
    resp = await async_client.get(
        '/api/staff/reservations/availability?branch_id=999999&date=2030-01-01&time=19:00',
        headers=staff_auth_headers,
    )
    assert resp.status_code == 404
