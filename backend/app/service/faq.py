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
        "parking": "正门提供代客泊车服务；主街沿线也可停车。",
        "dress_code": "商务休闲装，请勿穿着沙滩装。",
        "cancellation_policy": "可在用餐前 1 小时之前修改或取消预订。",
        "payment_methods": "柜台结账或送餐时可使用现金、信用卡/借记卡支付。",
        "delivery_radius": "市中心店周边 7 英里内可配送。",
    },
    2: {
        "parking": "店后有专属顾客停车场。",
        "dress_code": "休闲至正装均可。",
        "cancellation_policy": "可在用餐前 1 小时之前修改或取消预订。",
        "payment_methods": "柜台结账或送餐时可使用现金、信用卡/借记卡支付。",
        "delivery_radius": "北山店周边 10 英里内可配送。",
    },
}

_DEFAULT_POLICY: Dict[str, str] = {
    "parking": "门店附近有路边停车位。",
    "dress_code": "休闲着装即可。",
    "cancellation_policy": "用餐前 1 小时之前可免费取消。",
    "payment_methods": "柜台结账或送餐时可使用现金或银行卡支付。",
    "delivery_radius": "门店周边常规配送范围。",
}


class FAQService:
    @staticmethod
    async def get_branch_info(
        branch_id: int, *, session: Optional[AsyncSession] = None
    ) -> Dict[str, Any]:
        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id)
            if branch is None:
                raise NotFoundError(f"门店 #{branch_id} 不存在。")
            return {
                "branch_id": branch.id,
                "branch_name": branch.name,
                "address": branch.address,
                "phone": branch.phone,
                "opening_hours": branch.opening_hours,
                "policies": _BRANCH_POLICIES.get(branch.id, _DEFAULT_POLICY),
            }


__all__ = ["FAQService"]
