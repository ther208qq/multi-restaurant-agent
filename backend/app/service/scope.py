"""Shared branch-scope permission rules.

A branch-bound staff member may only read or act on their own branch; an
unscoped staff member (admin) must name the branch explicitly. Every Service
that takes a staff actor and a branch id uses these helpers instead of
hand-writing the same comparison, so the rule exists exactly once.
"""

from typing import Optional

from backend.app.exceptions import ForbiddenError, ValidationError
from backend.app.models.user import StaffUser

DEFAULT_FORBIDDEN_MESSAGE = "您没有权限访问该门店。"


def ensure_branch_scope(
    staff: Optional[StaffUser],
    branch_id: int,
    *,
    message: str = DEFAULT_FORBIDDEN_MESSAGE,
) -> None:
    """Reject a branch-bound staff member acting outside their own branch."""
    if staff is not None and staff.branch_id and branch_id != staff.branch_id:
        raise ForbiddenError(message)


def resolve_staff_branch(
    staff: Optional[StaffUser],
    branch_id: Optional[int],
    *,
    required_message: str = "请指定门店。",
) -> int:
    """The branch a request may target, applying the staff scope rule.

    A branch-bound staff member defaults to their own branch and may not name
    another one; an unscoped staff member must name a branch.
    """
    if staff is not None and staff.branch_id:
        if branch_id is not None and branch_id != staff.branch_id:
            raise ForbiddenError(DEFAULT_FORBIDDEN_MESSAGE)
        return staff.branch_id
    if branch_id is None:
        raise ValidationError(required_message)
    return branch_id


__all__ = ["ensure_branch_scope", "resolve_staff_branch"]
