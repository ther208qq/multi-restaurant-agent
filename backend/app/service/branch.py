"""Branch and Table business service.

Owns the transaction boundary, applies branch-scope / permission rules, raises
domain exceptions, and is the single entry point reused by the API and Agent.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, NotFoundError
from backend.app.models.branch import Branch, Table
from backend.app.models.user import StaffUser
from backend.app.opening_hours import parse_opening_hours
from backend.app.repo.branch import BranchRepo
from backend.app.service.audit import AuditService


def _check_opening_hours(values: Dict[str, Any]) -> None:
    """Reject an edit that would store an unparseable ``opening_hours``.

    The reservation service parses this field to reject out-of-hours bookings, so
    a bad value here would quietly break booking for the whole branch.
    """
    value = values.get("opening_hours")
    if value is not None:
        parse_opening_hours(value)


class BranchService:
    @staticmethod
    async def list_active_branches(
        *, session: Optional[AsyncSession] = None
    ) -> List[Branch]:
        """Active branches with their tables (public listing / Agent branch list)."""
        async with transactional_session(session) as s:
            return await BranchRepo.list_branches(s, active_only=True)

    @staticmethod
    async def list_branches(
        staff: StaffUser, *, session: Optional[AsyncSession] = None
    ) -> List[Branch]:
        """Branches visible to a staff member (scope-limited when branch-bound)."""
        async with transactional_session(session) as s:
            return await BranchRepo.list_branches(s, branch_id=staff.branch_id)

    @staticmethod
    async def get_branch(
        branch_id: int,
        *,
        active_only: bool = False,
        session: Optional[AsyncSession] = None,
    ) -> Optional[Branch]:
        """Load a branch by id, or ``None`` when absent (callers tolerate a miss)."""
        async with transactional_session(session) as s:
            return await BranchRepo.get_branch(s, branch_id, active_only=active_only)

    @staticmethod
    async def create_branch(
        values: Dict[str, Any],
        *,
        actor: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Branch:
        async with transactional_session(session) as s:
            _check_opening_hours(values)
            branch = await BranchRepo.create_branch(s, values)
            await AuditService.record(
                s,
                "branch.created",
                entity_type="branch",
                entity_id=branch.id,
                branch_id=branch.id,
                actor=actor,
                details={"name": branch.name},
            )
            return branch

    @staticmethod
    async def update_branch(
        branch_id: int,
        values: Dict[str, Any],
        *,
        actor: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Branch:
        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id)
            if branch is None:
                raise NotFoundError("门店不存在。")
            _check_opening_hours(values)
            before = {key: getattr(branch, key) for key in values if hasattr(branch, key)}
            updated = await BranchRepo.update_branch(s, branch, values)
            if before:
                await AuditService.record(
                    s,
                    "branch.updated",
                    entity_type="branch",
                    entity_id=branch_id,
                    branch_id=branch_id,
                    actor=actor,
                    details={"changes": {key: [before[key], getattr(updated, key)] for key in before}},
                )
            return updated

    @staticmethod
    async def list_tables(
        staff: StaffUser,
        branch_id: Optional[int] = None,
        *,
        session: Optional[AsyncSession] = None,
    ) -> List[Table]:
        async with transactional_session(session) as s:
            resolved_branch_id = staff.branch_id or branch_id or 1
            return await BranchRepo.list_tables(s, resolved_branch_id)

    @staticmethod
    async def create_table(
        staff: StaffUser,
        values: Dict[str, Any],
        *,
        session: Optional[AsyncSession] = None,
    ) -> Table:
        if staff.branch_id and values.get("branch_id") != staff.branch_id:
            raise ForbiddenError("您没有权限访问该门店。")
        async with transactional_session(session) as s:
            table = await BranchRepo.create_table(s, values)
            await AuditService.record(
                s,
                "table.created",
                entity_type="table",
                entity_id=table.id,
                branch_id=table.branch_id,
                actor=staff,
                details={
                    "label": table.label,
                    "seating_area": table.seating_area,
                    "capacity": table.capacity,
                },
            )
            return table

    @staticmethod
    async def update_table(
        staff: StaffUser,
        table_id: int,
        values: Dict[str, Any],
        *,
        session: Optional[AsyncSession] = None,
    ) -> Table:
        async with transactional_session(session) as s:
            table = await BranchRepo.get_table(s, table_id)
            if table is None:
                raise NotFoundError("餐桌不存在。")
            if staff.branch_id and table.branch_id != staff.branch_id:
                raise ForbiddenError("您没有权限访问该门店。")
            before = {key: getattr(table, key) for key in values if hasattr(table, key)}
            updated = await BranchRepo.update_table(s, table, values)
            if before:
                await AuditService.record(
                    s,
                    "table.updated",
                    entity_type="table",
                    entity_id=table_id,
                    branch_id=updated.branch_id,
                    actor=staff,
                    details={"changes": {key: [before[key], getattr(updated, key)] for key in before}},
                )
            return updated


__all__ = ["BranchService"]
