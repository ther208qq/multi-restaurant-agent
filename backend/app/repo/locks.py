"""Transaction-scoped advisory locks used to de-duplicate concurrent requests.

A repeated submission of the *same* logical request -- a chat message sent twice,
a tool call the model emitted twice in one turn, a retried HTTP request -- would
otherwise run the duplicate check and the insert in two interleaved transactions
and create both rows. PostgreSQL advisory locks close that window: the lock is
keyed on a hash of the request's identity, taken with ``pg_advisory_xact_lock``
inside the caller's transaction, and released automatically on commit or
rollback, so it is never leaked by an error path.

Repo-layer only: the Service layer decides *when* a request needs serialising,
this module only knows how.
"""

import hashlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def lock_key(scope: str, *parts: Any) -> int:
    """A stable signed 64-bit advisory-lock key for ``scope`` plus ``parts``.

    ``pg_advisory_xact_lock`` takes a ``bigint``; hashing keeps the key a fixed
    size whatever the parts look like, and ``hashlib`` (rather than PostgreSQL's
    ``hashtext``) keeps it deterministic across sessions and database versions.
    """
    raw = "|".join([scope, *(str(part) for part in parts)])
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def lock_request(session: AsyncSession, key: int) -> None:
    """Block until this transaction holds the advisory lock for ``key``.

    A second transaction asking for the same key waits here until the first one
    commits, which is exactly what makes "look for an existing row, then insert"
    safe against a concurrent duplicate.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


__all__ = ["lock_key", "lock_request"]
