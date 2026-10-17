"""Staff peer-comparison route: query parsing and response shaping only.

The judgement lives in :class:`backend.app.service.branch_peers.BranchPeerService`,
including scope enforcement and the ``not_comparable`` verdict for a branch-bound
staff member. This module does not decide anything -- in particular it does not
turn "no visible peer" into an empty success, because that would read as "no
other branch is affected".

The path sits beside ``/staff/diagnosis`` rather than inside its payload: the
diagnosis contract is unchanged by this feature.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.branch_peers import BranchPeerResponse
from backend.app.service.branch_peers import BranchPeerService

router = APIRouter(prefix="/staff/diagnosis/peers", tags=["Staff Diagnosis Peers"])


@router.get("", response_model=BranchPeerResponse)
async def get_branch_peers(
    branch_id: Optional[int] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Compare one branch with the same-window peers the caller can see.

    ``branch_id`` may be omitted by a branch-bound staff member, whose own branch
    is the target. Two answers are *normal*, not errors: an admin may get
    ``status = "ok"`` with per-branch verdicts, and a branch-bound staff member
    gets ``status = "not_comparable"`` with an explicit explanation -- never an
    empty peer list that could be mistaken for "nothing else moved".

    ``start_date`` / ``end_date`` default to the last seven days and use the same
    equal-length baseline window as the diagnosis.
    """
    return await BranchPeerService.compare_branch_peers(
        branch_id, start_date, end_date, staff=current_staff, session=db
    )
