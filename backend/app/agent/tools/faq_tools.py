"""Branch FAQ Agent Tool — thin adapter over :class:`FAQService`.

Holds no business data and no direct data access. The per-branch policies now
live in the Service; this tool only adapts the result / domain exception to the
``{"error": ...}`` shape the FAQ node expects.
"""

from typing import Any, Dict

from backend.app.exceptions import DomainError
from backend.app.service.faq import FAQService


async def get_branch_faq(branch_id: int) -> Dict[str, Any]:
    """Opening hours, address, contact, and dining policies for a branch."""
    try:
        return await FAQService.get_branch_info(branch_id)
    except DomainError as exc:
        return {"error": exc.message}
