"""Branch and Table repository: semantic data access only.

Receives a session from the Service layer, encapsulates queries and updates,
never commits, and never imports ``service`` / ``routes`` / ``agent``.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.branch import Branch, Table


class BranchRepo:
    @staticmethod
    async def list_branches(
        session: AsyncSession,
        *,
        active_only: bool = False,
        branch_id: Optional[int] = None,
    ) -> List[Branch]:
        stmt = (
            select(Branch)
            .options(selectinload(Branch.tables))
            .order_by(Branch.id.asc())
        )
        if active_only:
            stmt = stmt.where(Branch.is_active == True)  # noqa: E712
        if branch_id is not None:
            stmt = stmt.where(Branch.id == branch_id)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_branch(
        session: AsyncSession,
        branch_id: int,
        *,
        active_only: bool = False,
    ) -> Optional[Branch]:
        stmt = select(Branch).where(Branch.id == branch_id)
        if active_only:
            stmt = stmt.where(Branch.is_active == True)  # noqa: E712
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def create_branch(session: AsyncSession, values: Dict[str, Any]) -> Branch:
        branch = Branch(**values)
        session.add(branch)
        await session.flush()
        return branch

    @staticmethod
    async def update_branch(
        session: AsyncSession, branch: Branch, values: Dict[str, Any]
    ) -> Branch:
        for key, value in values.items():
            setattr(branch, key, value)
        await session.flush()
        return branch

    @staticmethod
    async def list_tables(session: AsyncSession, branch_id: int) -> List[Table]:
        stmt = (
            select(Table)
            .where(Table.branch_id == branch_id)
            .order_by(Table.label.asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_candidate_tables(
        session: AsyncSession,
        branch_id: int,
        *,
        min_capacity: int,
        seating_area: Optional[str] = None,
    ) -> List[Table]:
        """Seatable tables for ``min_capacity`` guests, best-fit (smallest) first.

        ``seating_area`` of ``None``/``"any"`` does not filter by area.
        """
        stmt = (
            select(Table)
            .where(
                Table.branch_id == branch_id,
                Table.is_available == True,  # noqa: E712
                Table.capacity >= min_capacity,
            )
            .order_by(Table.capacity.asc())
        )
        if seating_area and seating_area != "any":
            stmt = stmt.where(Table.seating_area == seating_area)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_table(session: AsyncSession, table_id: int) -> Optional[Table]:
        res = await session.execute(select(Table).where(Table.id == table_id))
        return res.scalar_one_or_none()

    @staticmethod
    async def get_table_by_label(
        session: AsyncSession,
        branch_id: int,
        label: str,
        *,
        exclude_id: Optional[int] = None,
    ) -> Optional[Table]:
        """Any table in ``branch_id`` labelled ``label``, optionally skipping one id.

        Returns the first match rather than erroring on several: the guard this
        backs is what *stops* duplicates, so it must not itself blow up on a
        database that already has one.
        """
        stmt = select(Table).where(Table.branch_id == branch_id, Table.label == label)
        if exclude_id is not None:
            stmt = stmt.where(Table.id != exclude_id)
        res = await session.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def create_table(session: AsyncSession, values: Dict[str, Any]) -> Table:
        table = Table(**values)
        session.add(table)
        await session.flush()
        return table

    @staticmethod
    async def update_table(
        session: AsyncSession, table: Table, values: Dict[str, Any]
    ) -> Table:
        for key, value in values.items():
            setattr(table, key, value)
        await session.flush()
        return table

    @staticmethod
    async def delete_table(session: AsyncSession, table: Table) -> None:
        await session.delete(table)
        await session.flush()


__all__ = ["BranchRepo"]
