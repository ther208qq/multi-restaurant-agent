"""Store-anomaly diagnosis response schemas.

Mirrors the split used by :mod:`backend.app.schemas.stats`: the Service returns a
plain dict and the route attaches this model as ``response_model``, so the shape
is declared once and enforced at the edge.

``status`` is the field the UI branches on: ``ok`` means the Service had enough
history to judge, ``insufficient_data`` means it deliberately *refused* to judge
(see ``notes`` for why). A refusal is a normal answer, not an error -- treating a
missing baseline as a -100% drop is exactly the failure this feature exists to
avoid.
"""

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

# ``ok``: findings were judged (possibly none). ``insufficient_data``: the
# Service refused to judge because the baseline could not support a comparison.
DIAGNOSIS_STATUSES = ("ok", "insufficient_data")


class DiagnosisEvidence(BaseModel):
    """One observed fact, addressable by the Agent's reasoning.

    ``ref`` is the stable handle an Agent ``evidence_ref`` must match; a reason
    citing a ref that is not in the payload is dropped rather than rendered.
    """

    ref: str
    label: str
    value: Any = None
    detail: str = ""


class DiagnosisFinding(BaseModel):
    """One metric judged anomalous, with the facts behind the judgement."""

    metric: str
    label: str
    direction: str  # "down" | "up"
    severity: str  # "warning" | "critical"
    current: float
    previous: float
    change_pct: float
    menu_item_id: Optional[int] = None
    evidence: List[DiagnosisEvidence] = []


class DiagnosisPeriod(BaseModel):
    """The window a comparison was drawn from, plus how much of it was active."""

    start_date: date
    end_date: date
    order_count: int
    cancelled_count: int
    revenue: float
    average_order_value: float
    units_sold: int
    active_days: int


class DiagnosisClaim(BaseModel):
    """One sentence from the Agent, tied to the evidence that supports it.

    ``evidence_refs`` are checked against the payload the Agent was given: a
    claim citing a ref that does not resolve is dropped rather than rendered, so
    the references reaching the UI are always ones the Service actually produced.
    """

    text: str
    evidence_refs: List[str] = []


class DiagnosisAnalysis(BaseModel):
    """The Agent's reading of the findings: facts, causes, and what is unverifiable.

    ``facts`` are written deterministically from the Service payload; ``causes``
    and ``checks`` may come from the LLM, and ``source`` says which produced
    them so a rule-based fallback is never mistaken for a fuller analysis.
    """

    status: str   # "ok" | "no_findings" | "insufficient_data"
    source: str   # "llm" | "rule"
    dropped: int = 0
    facts: List[DiagnosisClaim] = []
    causes: List[DiagnosisClaim] = []
    checks: List[DiagnosisClaim] = []


class DiagnosisResponse(BaseModel):
    """Deterministic anomalies for one branch and window, with the Agent's reading.

    ``baseline_period`` is the immediately preceding window of equal length, so
    the two ranges never overlap and always sit side by side. ``analysis`` is
    absent only when the caller rendered the Service payload on its own.
    """

    branch_id: int
    branch_name: str
    status: str
    notes: List[str] = []
    current_period: DiagnosisPeriod
    baseline_period: DiagnosisPeriod
    thresholds: Dict[str, float] = {}
    findings: List[DiagnosisFinding] = []
    analysis: Optional[DiagnosisAnalysis] = None


__all__ = [
    "DIAGNOSIS_STATUSES",
    "DiagnosisAnalysis",
    "DiagnosisClaim",
    "DiagnosisEvidence",
    "DiagnosisFinding",
    "DiagnosisPeriod",
    "DiagnosisResponse",
]
