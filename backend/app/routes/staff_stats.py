"""Staff Business Statistics Routes.

Thin HTTP layer over :class:`backend.app.service.stats.StatsService`: query
parsing and response shaping only. What counts as a sale, how a day is bounded
and which branch a staff member may aggregate are Service-layer rules shared with
any future Agent-facing statistics tool.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.stats import BranchStatsResponse
from backend.app.service.stats import (
    DEFAULT_TOP_ITEMS,
    StatsService,
)

router = APIRouter(prefix="/staff/stats", tags=["Staff Statistics"])


@router.get("", response_model=BranchStatsResponse)
async def get_branch_stats(
    branch_id: Optional[int] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    top: int = Query(DEFAULT_TOP_ITEMS),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Order volume, revenue and dish sales for a branch and date range.

    ``start_date`` / ``end_date`` are inclusive UTC dates and default to the last
    seven days. A branch-bound staff member may only query their own branch.
    """
    return await StatsService.get_branch_summary(
        branch_id,
        start_date,
        end_date,
        top_limit=top,
        staff=current_staff,
        session=db,
    )
