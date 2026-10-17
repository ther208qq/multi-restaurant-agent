"""Staff Store-Anomaly Diagnosis Routes.

Thin HTTP layer over the diagnosis sub-flow: query parsing and response shaping
only. Nothing is decided here. The deterministic detection is
:class:`backend.app.service.diagnostics.DiagnosisService`; the cause analysis is
:mod:`backend.app.agent.diagnosis`. The route hands the caller's identity to both
and passes their results through unchanged.

Branch scope follows the statistics service: a branch-bound staff member may only
diagnose their own branch, and the resulting ``ForbiddenError`` is mapped to 403
by the global handlers. The route does not re-check it, so there is exactly one
implementation of the rule to keep correct.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.diagnosis import analyze_diagnosis
from backend.app.agent.diagnosis_investigation import run_investigation
from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.diagnostics import (
    DiagnosisInvestigationRequest,
    DiagnosisInvestigationResponse,
    DiagnosisResponse,
)
from backend.app.service.diagnostics import DiagnosisService

router = APIRouter(prefix="/staff/diagnosis", tags=["Staff Diagnosis"])


@router.get("", response_model=DiagnosisResponse)
async def get_branch_diagnosis(
    branch_id: Optional[int] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Diagnose one branch's window against the equal-length window before it.

    ``start_date`` / ``end_date`` are inclusive UTC dates and default to the last
    seven days. Two answers are *normal*, not errors: ``status`` is
    ``insufficient_data`` when the history cannot support a comparison (the
    response explains why, with no findings), and ``findings`` is empty when the
    period was judged and simply showed no anomaly. Neither ever becomes a 500,
    and neither is dressed up as a collapse.
    """
    diagnosis = await DiagnosisService.diagnose(
        branch_id, start_date, end_date, staff=current_staff, session=db
    )
    # The Agent adds ``analysis``; every other field is the Service's, untouched.
    return {**diagnosis, "analysis": await analyze_diagnosis(diagnosis)}


@router.post("/investigate", response_model=DiagnosisInvestigationResponse)
async def investigate_branch_diagnosis(
    payload: DiagnosisInvestigationRequest,
    request: Request,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Ask the read-only investigation Agent a question about this branch.

    The Agent picks the window (dates are optional), calls the bounded set of
    read-only tools, and answers. ``thread_id`` from a previous response resumes
    the same investigation; ownership and branch are checked before the graph
    runs. An unavailable model yields the deterministic diagnosis with
    ``status="llm_unavailable"`` rather than an error.
    """
    return await run_investigation(
        payload,
        current_staff,
        db,
        checkpointer=getattr(request.app.state, "checkpointer", None),
    )
