"""One-off dev-DB migration: widen the reservation exclusion constraint (Task17).

The constraint used to cover ``status = 'confirmed'`` only, so a *pending*
booking (the new default for chat bookings) and an already-*seated* party did
not block their table, letting the same slot be booked twice. This script
replaces it with one covering every occupying status and renames it to
``no_overlapping_active_table_reservations``.

It first backs up the reservation rows, then refuses to run if existing rows
would already violate the new constraint (better to look at the data than to get
a bare ALTER TABLE failure). Idempotent: re-running is a no-op once the new
constraint is in place.

Run from the repository root:

    ./.venv/Scripts/python.exe -m scripts.migrate_reservation_exclusion
"""

import asyncio
import datetime as dt
import json
from pathlib import Path

from sqlalchemy import text

from backend.app.database import AsyncSessionLocal
from backend.app.models.reservation import OCCUPYING_RESERVATION_STATUSES

OLD_CONSTRAINT = "no_overlapping_confirmed_table_reservations"
NEW_CONSTRAINT = "no_overlapping_active_table_reservations"

BACKUP = (
    Path(__file__).resolve().parent
    / "backups"
    / "dev_reservation_exclusion_migration_2026-10-13.json"
)

# Rendered from the model's tuple so the SQL predicate cannot drift from the
# one the ORM declares.
_OCCUPYING_SQL = ", ".join(f"'{status}'" for status in OCCUPYING_RESERVATION_STATUSES)
ADD_SQL = (
    f"ALTER TABLE reservations ADD CONSTRAINT {NEW_CONSTRAINT} "
    "EXCLUDE USING gist (table_id WITH =, booking_window WITH &&) "
    f"WHERE (status IN ({_OCCUPYING_SQL}))"
)

OVERLAP_SQL = f"""
SELECT a.id AS a_id, b.id AS b_id, a.table_id, a.status AS a_status,
       b.status AS b_status, a.start_time AS a_start, b.start_time AS b_start
FROM reservations a
JOIN reservations b
  ON a.table_id = b.table_id
 AND a.id < b.id
 AND a.booking_window && b.booking_window
WHERE a.status IN ({_OCCUPYING_SQL})
  AND b.status IN ({_OCCUPYING_SQL})
"""

CONSTRAINTS_SQL = """
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'reservations'::regclass AND contype = 'x'
ORDER BY conname
"""


async def main() -> None:
    async with AsyncSessionLocal() as s:
        before = (await s.execute(text(CONSTRAINTS_SQL))).all()
        print("constraints before:")
        for name, definition in before:
            print(f"  {name}: {definition}")

        if len(before) == 1 and before[0][0] == NEW_CONSTRAINT:
            print("already migrated; nothing to do.")
            return

        reservations = (await s.execute(text("SELECT * FROM reservations"))).all()
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        BACKUP.write_text(
            json.dumps(
                {
                    "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "database": "restaurant_ai",
                    "reservations": [dict(r._mapping) for r in reservations],
                },
                indent=2,
                default=str,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"backup written: {BACKUP} ({len(reservations)} reservations)")

        violations = (await s.execute(text(OVERLAP_SQL))).all()
        if violations:
            print(f"ABORT: {len(violations)} overlapping occupying bookings would block the ALTER:")
            for row in violations:
                print(f"  booking #{row.a_id} ({row.a_status}) vs #{row.b_id} ({row.b_status}) "
                      f"on table {row.table_id} @ {row.a_start}")
            return

        await s.execute(text(f"ALTER TABLE reservations DROP CONSTRAINT IF EXISTS {OLD_CONSTRAINT}"))
        await s.execute(text(f"ALTER TABLE reservations DROP CONSTRAINT IF EXISTS {NEW_CONSTRAINT}"))
        await s.execute(text(ADD_SQL))
        await s.commit()

        after = (await s.execute(text(CONSTRAINTS_SQL))).all()
        print("constraints after:")
        for name, definition in after:
            print(f"  {name}: {definition}")


if __name__ == "__main__":
    asyncio.run(main())
