"""Audit service: writing and reading the "who did what" trail (spec §9).

Two distinct responsibilities, deliberately split:

* :meth:`AuditService.record` **never opens its own transaction**. It joins the
  one the audited operation is already running in, so the business write and its
  audit row commit or roll back together — a rejected stock adjustment or a
  rolled-back order cancellation leaves no misleading audit entry behind. It is
  therefore called with the session from inside another Service's
  ``transactional_session`` block, not as a standalone entry point.
* :meth:`AuditService.list_logs` **is** a normal Service entry point: it owns its
  transaction and applies the branch-scope rule (a branch-bound staff member
  only ever sees their own branch's trail).

The actor is resolved from the ORM object that performed the operation
(:class:`StaffUser` / :class:`Customer`), or passed explicitly for operations
that only carry an id (Agent tool calls).
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import ValidationError
from backend.app.labels import audit_action_label, audit_entity_label
from backend.app.models.audit import AuditLog
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.audit import AuditRepo

MAX_AUDIT_LIMIT = 200


def actor_fields(actor: Optional[Any]) -> Dict[str, Any]:
    """Map an acting user object onto the audit row's denormalized columns."""
    if isinstance(actor, StaffUser):
        return {
            "actor_type": "staff",
            "actor_id": actor.id,
            "actor_name": actor.username,
            "actor_role": actor.role,
        }
    if isinstance(actor, Customer):
        return {
            "actor_type": "customer",
            "actor_id": actor.id,
            "actor_name": actor.name,
            "actor_role": None,
        }
    return {"actor_type": "system", "actor_id": None, "actor_name": None, "actor_role": None}


def audit_log_payload(log: AuditLog) -> Dict[str, Any]:
    """Serialize an audit row for the API, with Chinese action/entity labels."""
    return {
        "id": log.id,
        "created_at": log.created_at,
        "actor_type": log.actor_type,
        "actor_id": log.actor_id,
        "actor_name": log.actor_name,
        "actor_role": log.actor_role,
        "branch_id": log.branch_id,
        "action": log.action,
        "action_label": audit_action_label(log.action),
        "entity_type": log.entity_type,
        "entity_label": audit_entity_label(log.entity_type),
        "entity_id": log.entity_id,
        "details": log.details or {},
    }


class AuditService:
    @staticmethod
    async def record(
        session: AsyncSession,
        action: str,
        *,
        entity_type: str,
        entity_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        actor: Optional[Any] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> AuditLog:
        """Write one audit row on the caller's session (no transaction of its own).

        ``actor`` takes precedence when given; otherwise the explicit
        ``actor_type`` / ``actor_id`` / ``actor_name`` / ``actor_role`` values are
        used, so an Agent tool that only knows the customer id can still attribute
        the operation.
        """
        fields = actor_fields(actor) if actor is not None else {
            "actor_type": actor_type or "system",
            "actor_id": actor_id,
            "actor_name": actor_name,
            "actor_role": actor_role,
        }
        return await AuditRepo.record(
            session,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
            details=details or {},
            **fields,
        )

    @staticmethod
    async def list_logs(
        staff: StaffUser,
        *,
        branch_id: Optional[int] = None,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        limit: int = 50,
        session: Optional[AsyncSession] = None,
    ) -> List[AuditLog]:
        """Recent audit rows, newest first, limited to what ``staff`` may see."""
        if limit < 1:
            raise ValidationError("查询条数至少为 1。")
        limit = min(limit, MAX_AUDIT_LIMIT)
        # Scope: a branch-bound staff member always reads their own branch,
        # whatever branch_id the request asked for.
        effective_branch = staff.branch_id or branch_id
        async with transactional_session(session) as s:
            return await AuditRepo.list_logs(
                s,
                branch_id=effective_branch,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                limit=limit,
            )


__all__ = [
    "AuditService",
    "actor_fields",
    "audit_log_payload",
    "MAX_AUDIT_LIMIT",
]
