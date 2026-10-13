"""Staff Audit Log Routes.

Read-only view of the operation trail written by the Service layer (spec §9
"权限与审计"). Thin HTTP layer: request parsing, branch scoping and response
shaping only; the branch-scope rule itself lives in
:class:`backend.app.service.audit.AuditService`.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.audit import AuditLogResponse
from backend.app.service.audit import AuditService, audit_log_payload

router = APIRouter(prefix="/staff/audit-logs", tags=["Staff Audit Log"])


@router.get("", response_model=List[AuditLogResponse])
async def list_audit_logs(
    branch_id: Optional[int] = Query(None),
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[int] = Query(None),
    limit: int = Query(50),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List recent audited operations, newest first (branch-scoped for staff)."""
    logs = await AuditService.list_logs(
        current_staff,
        branch_id=branch_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        session=db,
    )
    return [audit_log_payload(log) for log in logs]
