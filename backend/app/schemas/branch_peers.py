"""HTTP contract for the same-window peer comparison (Task28).

The Service (:mod:`backend.app.service.branch_peers`) owns every judgement; this
module only fixes the wire shape so a field rename cannot silently reach the UI.
The response is deliberately separate from :class:`DiagnosisResponse`: peer
comparison is its own entry, and the diagnosis payload's semantics do not change
because this endpoint exists.

``change_pct`` is optional for a reason: it is present only for a branch whose
baseline passed the gate. A missing value is "not comparable", never zero -- the
UI has to render a dash, not a percentage.
"""

from datetime import date
from typing import Dict, List, Optional

from pydantic import BaseModel

# The verdict a branch can carry. ``not_comparable`` is the whole comparison
# being impossible (no visible peer), not a data problem in one branch.
PEER_STATUSES = (
    "ok",
    "insufficient_baseline",
    "no_data",
    "zero_baseline",
    "not_comparable",
)


class PeerBranch(BaseModel):
    """The target branch as an identity and a verdict -- no totals repeated."""

    branch_id: int
    branch_name: str
    status: str


class PeerWindow(BaseModel):
    start_date: date
    end_date: date
    baseline_start_date: date
    baseline_end_date: date


class PeerEntry(BaseModel):
    branch_id: int
    branch_name: str
    status: str
    current_active_days: int
    baseline_active_days: int
    change_pct: Optional[float] = None
    verdict: str


class BranchPeerResponse(BaseModel):
    status: str   # "ok" | "not_comparable"
    target: PeerBranch
    window: PeerWindow
    peers: List[PeerEntry] = []
    comparable_count: int
    incomparable_count: int
    summary: str
    notes: List[str] = []


__all__ = [
    "PEER_STATUSES",
    "BranchPeerResponse",
    "PeerBranch",
    "PeerEntry",
    "PeerWindow",
]
