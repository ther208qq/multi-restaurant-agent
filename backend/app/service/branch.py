"""Branch and Table business service.

Owns the transaction boundary, applies branch-scope / permission rules, raises
domain exceptions, and is the single entry point reused by the API and Agent.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import ConflictError, ForbiddenError, NotFoundError
from backend.app.models.branch import Branch, Table
from backend.app.models.user import StaffUser
from backend.app.opening_hours import parse_opening_hours
from backend.app.repo.branch import BranchRepo
from backend.app.repo.reservation import ReservationRepo
from backend.app.service.audit import AuditService


def _check_opening_hours(values: Dict[str, Any]) -> None:
    """Reject an edit that would store an unparseable ``opening_hours``.

    The reservation service parses this field to reject out-of-hours bookings, so
    a bad value here would quietly break booking for the whole branch.
    """
    value = values.get("opening_hours")
    if value is not None:
        parse_opening_hours(value)


async def _reject_duplicate_label(
    session: AsyncSession,
    branch_id: int,
    label: str,
    *,
    exclude_id: Optional[int] = None,
) -> None:
    """Refuse a second table with the same name inside one branch.

    The label is what staff and customers call a table, so two of them in the
    same branch are indistinguishable -- and the auto-picker would treat both as
    valid candidates for one booking. Names may still repeat *across* branches.
    """
    existing = await BranchRepo.get_table_by_label(
        session, branch_id, label, exclude_id=exclude_id
    )
    if existing is not None:
        raise ConflictError(f"该门店已存在名为「{label}」的餐桌，请换一个编号。")


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
            await _reject_duplicate_label(s, values["branch_id"], values["label"])
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
            # Only a label that actually changes needs checking: the frontend
            # always sends the current label back, and a branch that already
            # holds duplicates (pre-guard rows) must still be editable.
            new_label = values.get("label")
            if new_label is not None and new_label != table.label:
                await _reject_duplicate_label(
                    s, table.branch_id, new_label, exclude_id=table_id
                )
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

    @staticmethod
    async def delete_table(
        staff: StaffUser,
        table_id: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> None:
        """Remove a table outright, unless it has bookings to its name.

        The ``reservations.table_id`` FK is ``ON DELETE CASCADE``, so a plain
        delete would silently take the table's booking history with it (and a
        completed booking is part of the record, not clutter). A table that has
        ever been booked is therefore refused with a 409 that points at the
        non-destructive alternative -- disabling it -- instead.
        """
        async with transactional_session(session) as s:
            table = await BranchRepo.get_table(s, table_id)
            if table is None:
                raise NotFoundError("餐桌不存在。")
            if staff.branch_id and table.branch_id != staff.branch_id:
                raise ForbiddenError("您没有权限访问该门店。")

            bookings = await ReservationRepo.count_by_table(s, table_id)
            if bookings:
                raise ConflictError(
                    f"餐桌 {table.label} 有 {bookings} 条预订记录，无法删除；"
                    "如需下线请改用「停用」。"
                )

            # Snapshot before the delete: after the flush the instance is gone.
            details = {
                "label": table.label,
                "seating_area": table.seating_area,
                "capacity": table.capacity,
            }
            branch_id = table.branch_id
            await BranchRepo.delete_table(s, table)
            await AuditService.record(
                s,
                "table.deleted",
                entity_type="table",
                entity_id=table_id,
                branch_id=branch_id,
                actor=staff,
                details=details,
            )


__all__ = ["BranchService"]
