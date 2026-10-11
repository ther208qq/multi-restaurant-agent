"""Branch FAQ business service.

Aggregates a branch's contact/opening info with its dining policies. The
per-branch policy data lives here (business data), not in the Agent layer.
"""

from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import NotFoundError
from backend.app.repo.branch import BranchRepo

# Per-branch dining policies (lookup table keyed by branch id).
_BRANCH_POLICIES: Dict[int, Dict[str, str]] = {
    1: {
        "parking": "Valet parking available at the front entrance; street parking available on Main St.",
        "dress_code": "Smart casual. No beachwear.",
        "cancellation_policy": "Reservations can be modified or cancelled up to 1 hour before dining.",
        "payment_methods": "Cash, credit/debit cards accepted at the counter or upon delivery.",
        "delivery_radius": "Delivers within 7 miles of Downtown Central City.",
    },
    2: {
        "parking": "Dedicated customer parking lot behind the building.",
        "dress_code": "Casual to elegant.",
        "cancellation_policy": "Reservations can be modified or cancelled up to 1 hour before dining.",
        "payment_methods": "Cash, credit/debit cards accepted at the counter or upon delivery.",
        "delivery_radius": "Delivers within 10 miles of North Hills.",
    },
}

_DEFAULT_POLICY: Dict[str, str] = {
    "parking": "Street parking available nearby.",
    "dress_code": "Casual.",
    "cancellation_policy": "Free cancellation up to 1 hour prior.",
    "payment_methods": "Cash or card at counter/delivery.",
    "delivery_radius": "Standard city delivery radius.",
}


class FAQService:
    @staticmethod
    async def get_branch_info(
        branch_id: int, *, session: Optional[AsyncSession] = None
    ) -> Dict[str, Any]:
        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id)
            if branch is None:
                raise NotFoundError(f"Branch #{branch_id} not found.")
            return {
                "branch_id": branch.id,
                "branch_name": branch.name,
                "address": branch.address,
                "phone": branch.phone,
                "opening_hours": branch.opening_hours,
                "policies": _BRANCH_POLICIES.get(branch.id, _DEFAULT_POLICY),
            }


__all__ = ["FAQService"]
