"""Staff Order-Approval Routes (``sdd/AgentFeatures.md`` §5).

The manager's side of the HITL gate. Thin HTTP layer: it scopes to the staff's
branch, asks the agent for the gift suggestion shown on each card, and shapes the
response; the decisions themselves live in :class:`ApprovalService`, and the
gift's re-validation lives in ``GiftService`` (reached through ``issue_gift``).

The suggestion is recomputed here, live, rather than stored: what a manager sees
is what the branch holds *now* -- and the approve call re-checks it anyway, so a
stale display can never leak a gift that is no longer available.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.approvals import build_gift_suggestion
from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.approval import (
    ApprovalDecision,
    ApprovalResponse,
    GiftSuggestion,
)
from backend.app.service.approval import ApprovalService

router = APIRouter(prefix="/staff/approvals", tags=["Staff Approvals"])


async def _suggestion_for(order) -> Optional[GiftSuggestion]:
    """The agent's live gift proposal for a gated order's branch, if any."""
    if order is None:
        return None
    payload = await build_gift_suggestion(order.branch_id)
    return GiftSuggestion.from_payload(payload) if payload else None


@router.get("", response_model=List[ApprovalResponse])
async def list_approvals(
    status: Optional[str] = Query(None, description="pending | approved | rejected"),
    branch_id: Optional[int] = Query(None),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """The approval queue, oldest first, scoped to the staff member's branch.

    A branch-restricted staff member always sees their own branch; a super admin
    sees the optional ``?branch_id`` filter (or every branch). Pending items carry
    the agent's gift suggestion.
    """
    effective_branch = current_staff.branch_id or branch_id
    approvals = await ApprovalService.list_approvals(
        branch_id=effective_branch, status=status, session=db
    )
    result: List[ApprovalResponse] = []
    for approval in approvals:
        # Only an undecided request needs a suggestion; a closed one is history.
        suggestion = await _suggestion_for(
            approval.__dict__.get("order") if approval.status == "pending" else None
        )
        result.append(ApprovalResponse.from_approval(approval, suggestion=suggestion))
    return result


@router.post("/{approval_id}/approve", response_model=ApprovalResponse)
async def approve_approval(
    approval_id: int,
    payload: ApprovalDecision,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Approve the request and gift ``gift_id`` (re-validated against live stock)."""
    approval, _order, _gift = await ApprovalService.approve(
        approval_id, payload.gift_id, staff=current_staff, session=db
    )
    fresh = await ApprovalService.get_approval(approval.id, session=db)
    return ApprovalResponse.from_approval(fresh)


@router.post("/{approval_id}/reject", response_model=ApprovalResponse)
async def reject_approval(
    approval_id: int,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Reject the request: the order is cancelled and its stock released."""
    approval, _order = await ApprovalService.reject(
        approval_id, staff=current_staff, session=db
    )
    fresh = await ApprovalService.get_approval(approval.id, session=db)
    return ApprovalResponse.from_approval(fresh)
