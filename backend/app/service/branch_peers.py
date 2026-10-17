"""Same-window peer comparison across the branches a caller can see (Task27).

Answers Q3 -- "is this branch's drop its own problem, or is every branch moving?"
-- without touching the diagnosis contract. The diagnosis payload has no
cross-branch field, so this capability lives in its own Service method and is
consumed by its own entry point (Task28), not folded into a finding.

What it deliberately does *not* do:

* **It does not re-report the target branch's totals.** They already exist in
  the diagnosis payload as ``f0`` / ``f1``; repeating them here would invite two
  readings of one fact. The target appears only as an identity and a
  comparability verdict.
* **It does not relax the baseline gate for anyone.** Each branch is judged on
  its own window pair with the same :data:`MIN_BASELINE_DAYS` and the same
  active-day definition the diagnosis uses. A branch that is not comparable is
  labelled, never given a percentage.
* **It does not conclude "everyone is down".** The summary states counts only;
  whether a sample is large enough to talk about a common trend is not a
  threshold this system has (the contract leaves it open), so no such judgement
  is made.

All numbers come from the public statistics entry point. No query is written
here, and no new repository method is needed.
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import NotFoundError
from backend.app.models.user import StaffUser
from backend.app.service.branch import BranchService
from backend.app.service.diagnostics import (
    MIN_BASELINE_DAYS,
    active_days,
    has_rows,
    relative_change_pct,
    resolve_window,
)
from backend.app.service.stats import StatsService, resolve_branch_scope

# Cost bound per request: every compared branch costs two summaries (current and
# baseline), and each summary issues several aggregate queries. Ten matches the
# per-dish daily cover, which is the existing precedent for a per-request cap.
# The value is provisional -- the contract leaves the exact number open.
MAX_PEER_BRANCHES = 10

# One branch's verdict. ``not_comparable`` is not a branch-level data problem:
# it is the whole comparison being impossible because there is no peer to
# compare with (a branch-bound staff member, or a one-branch system).
PEER_OK = "ok"
PEER_INSUFFICIENT_BASELINE = "insufficient_baseline"
PEER_NO_DATA = "no_data"
PEER_ZERO_BASELINE = "zero_baseline"
PEER_NOT_COMPARABLE = "not_comparable"


def _judge(current: Dict[str, Any], baseline: Dict[str, Any]) -> Tuple[str, str]:
    """How one branch stands, and why, in that order of certainty.

    The ordering matters. A branch with no record at all this window cannot be
    compared at all; a baseline with zero sales cannot produce a percentage
    (writing ``-100%`` would turn "no baseline" into "collapsed"); and only then
    does the active-day gate apply. A baseline of zero is a more specific answer
    than "too few active days", so it is reported first.
    """
    if not has_rows(current):
        return PEER_NO_DATA, "本期没有任何订单记录，无法判断变化。"
    if baseline["order_count"] == 0:
        return PEER_ZERO_BASELINE, "上期没有成交订单，无法计算变化比例。"
    baseline_active_days = active_days(baseline)
    if baseline_active_days < MIN_BASELINE_DAYS:
        return (
            PEER_INSUFFICIENT_BASELINE,
            f"上期有效经营天数仅 {baseline_active_days} 天"
            f"（少于 {MIN_BASELINE_DAYS} 天），样本不足以判断趋势。",
        )
    return PEER_OK, "两期数据均满足基线要求，可以同期比较。"


def _peer_entry(
    branch_id: int, branch_name: str, status: str, current: Dict[str, Any],
    baseline: Dict[str, Any], verdict: str,
) -> Dict[str, Any]:
    """One comparable-or-not row. ``change_pct`` exists only when it is meaningful."""
    value: Optional[float] = None
    if status == PEER_OK:
        change = relative_change_pct(current["order_count"], baseline["order_count"])
        value = round(change, 2) if change is not None else None
    return {
        "branch_id": branch_id,
        "branch_name": branch_name,
        "status": status,
        "current_active_days": active_days(current),
        "baseline_active_days": active_days(baseline),
        "change_pct": value,
        "verdict": verdict,
    }


class BranchPeerService:
    @staticmethod
    async def compare_branch_peers(
        branch_id: Optional[int] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        *,
        staff: StaffUser,
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Compare the target branch with its visible peers over one window pair.

        ``branch_id`` may be omitted for a branch-bound staffer, whose own branch
        is the target. Scope is inherited from the existing services: an
        out-of-scope id raises :class:`ForbiddenError` before anything is read,
        and a missing branch raises :class:`NotFoundError`.

        Returns ``{"status", "target", "window", "peers", "comparable_count",
        "incomparable_count", "summary", "notes"}``. Counts cover every branch
        that was actually judged, target included -- the target is a sample in
        the "how many branches are comparable?" count, even though its totals
        are not repeated. It is simply not listed again among ``peers``.
        """
        start, end, baseline_start, baseline_end = resolve_window(start_date, end_date)
        target_id = resolve_branch_scope(branch_id, staff)

        visible = await BranchService.list_branches(staff, session=session)
        target = next((branch for branch in visible if branch.id == target_id), None)
        if target is None:
            raise NotFoundError(f"门店 #{target_id} 不存在。")

        window = {
            "start_date": start,
            "end_date": end,
            "baseline_start_date": baseline_start,
            "baseline_end_date": baseline_end,
        }
        notes: List[str] = []

        other_branches = [branch for branch in visible if branch.id != target_id]
        if not other_branches:
            # Saying "no change to report" here would be a lie by omission: the
            # caller can see only this branch, so no peer comparison exists.
            summary = (
                "您只能看到本店，无法进行同业对比。"
                if staff.branch_id
                else "当前没有其他可见门店，无法进行同业对比。"
            )
            return {
                "status": PEER_NOT_COMPARABLE,
                "target": {
                    "branch_id": target.id,
                    "branch_name": target.name,
                    "status": PEER_NOT_COMPARABLE,
                },
                "window": window,
                "peers": [],
                "comparable_count": 0,
                "incomparable_count": 1,
                "summary": summary,
                "notes": notes,
            }

        # Target first, then peers by id (the listing's own order), so the cap
        # can never silently drop the branch the caller asked about.
        selected = [target] + other_branches
        if len(selected) > MAX_PEER_BRANCHES:
            dropped = len(selected) - MAX_PEER_BRANCHES
            selected = selected[:MAX_PEER_BRANCHES]
            notes.append(
                f"可见门店共 {len(visible)} 家，本次仅比较前 {MAX_PEER_BRANCHES} 家"
                f"（含目标店），其余 {dropped} 家未纳入判定与计数。"
            )

        judged: List[Dict[str, Any]] = []
        target_status = PEER_NOT_COMPARABLE
        for branch in selected:
            current = await StatsService.get_branch_summary(
                branch.id,
                start,
                end,
                # Only order volume is compared; asking for one dish keeps the
                # ranking aggregate as small as the public entry point allows.
                top_limit=1,
                staff=staff,
                session=session,
            )
            baseline = await StatsService.get_branch_summary(
                branch.id,
                baseline_start,
                baseline_end,
                top_limit=1,
                staff=staff,
                session=session,
            )
            status, verdict = _judge(current, baseline)
            if branch.id == target_id:
                target_status = status
                continue
            judged.append(
                _peer_entry(branch.id, branch.name, status, current, baseline, verdict)
            )

        comparable_count = (1 if target_status == PEER_OK else 0) + sum(
            1 for peer in judged if peer["status"] == PEER_OK
        )
        judged_count = len(selected)
        incomparable_count = judged_count - comparable_count
        summary = (
            f"本次共判定 {judged_count} 家可见门店：可比 {comparable_count} 家，"
            f"不可比 {incomparable_count} 家。本结果只陈述逐店可比性，"
            "不判断变化是否普遍。"
        )

        return {
            "status": PEER_OK,
            "target": {
                "branch_id": target.id,
                "branch_name": target.name,
                "status": target_status,
            },
            "window": window,
            "peers": judged,
            "comparable_count": comparable_count,
            "incomparable_count": incomparable_count,
            "summary": summary,
            "notes": notes,
        }


__all__ = [
    "BranchPeerService",
    "MAX_PEER_BRANCHES",
    "PEER_INSUFFICIENT_BASELINE",
    "PEER_NO_DATA",
    "PEER_NOT_COMPARABLE",
    "PEER_OK",
    "PEER_ZERO_BASELINE",
]
