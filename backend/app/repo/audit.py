"""Audit log repository: semantic data access only.

Receives a session from the Service layer, encapsulates the insert and the
filtered listing, never commits, and never imports ``service`` / ``routes`` /
``agent``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.audit import AuditLog


class AuditRepo:
    @staticmethod
    async def record(
        session: AsyncSession,
        *,
        action: str,
        entity_type: str,
        entity_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        actor_type: str = "system",
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> AuditLog:
        log = AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
            details=details,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
        )
        session.add(log)
        await session.flush()
        return log

    @staticmethod
    async def list_logs(
        session: AsyncSession,
        *,
        branch_id: Optional[int] = None,
        action: Optional[str] = None,
        actions: Optional[List[str]] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[AuditLog]:
        """Most recent first.

        ``start`` is inclusive and ``end`` exclusive, matching the UTC day
        half-open ranges used by the statistics layer. ``actions`` is an
        alternative to the single ``action`` value for investigations that look
        for any of several operational changes in one query.
        """
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if branch_id is not None:
            stmt = stmt.where(AuditLog.branch_id == branch_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if actions:
            stmt = stmt.where(AuditLog.action.in_(actions))
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if start is not None:
            stmt = stmt.where(AuditLog.created_at >= start)
        if end is not None:
            stmt = stmt.where(AuditLog.created_at < end)
        res = await session.execute(stmt)
        return list(res.scalars().all())


__all__ = ["AuditRepo"]
