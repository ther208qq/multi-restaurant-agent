"""Audit log response schemas."""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class AuditLogResponse(BaseModel):
    """One audited operation, with Chinese labels for the raw codes."""

    id: int
    created_at: datetime
    actor_type: str  # staff | customer | system
    actor_id: Optional[int] = None
    actor_name: Optional[str] = None
    actor_role: Optional[str] = None
    branch_id: Optional[int] = None
    action: str
    action_label: str
    entity_type: str
    entity_label: str
    entity_id: Optional[int] = None
    details: Dict[str, Any] = {}


__all__ = ["AuditLogResponse"]
