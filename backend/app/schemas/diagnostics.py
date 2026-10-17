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
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

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


class ThreadDiagnosisEvidence(DiagnosisEvidence):
    """Investigation evidence with its thread-stable, turn-qualified handle.

    ``ref`` stays the short local name a tool emitted (``f0.e0``,
    ``daily1.e1``), so existing clients keep working. ``thread_ref`` adds the
    turn prefix (``t2.f0.e0``) that remains valid when the same investigation
    continues into later turns.
    """

    thread_ref: Optional[str] = None


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
    """The Agent's reading of the findings, split by how much is known.

    ``facts`` are written deterministically from the Service payload;
    ``inferences`` and ``checks`` may come from the LLM but must cite real
    evidence, and ``source`` says which produced them so a rule-based fallback
    is never mistaken for a fuller analysis. ``hypotheses`` are explanations the
    system holds no evidence for: each is prefixed with ``未经证实：`` and carries
    no refs, so the UI can keep it visibly apart from an evidenced inference.
    """

    status: str   # "ok" | "no_findings" | "insufficient_data"
    source: str   # "llm" | "rule"
    dropped: int = 0
    facts: List[DiagnosisClaim] = []
    inferences: List[DiagnosisClaim] = []
    hypotheses: List[DiagnosisClaim] = []
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


# ---------------------------------------------------------------------------
# Read-only investigation Agent contract (Task39)
#
# The deterministic ``GET /staff/diagnosis`` payload above is unchanged and
# remains the single source of computed facts. The investigation endpoint wraps
# it in a conversation: the caller asks a question, and the Agent decides which
# tools to call and which time window the question is about (Task42). These
# models therefore describe *the investigation process* -- what was asked, which
# window was resolved, what the Agent did, and what it is still unsure about --
# rather than adding a second kind of finding.
# ---------------------------------------------------------------------------

WINDOW_SOURCES = ("question", "agent", "explicit", "default")
INVESTIGATION_STATUSES = (
    "ok",
    "insufficient_data",
    "budget_exhausted",
    "llm_unavailable",
)

# A hypothesis is never presented as a finding. ``unverified`` is the default
# and the honest state for an explanation the tools could not test; only
# ``supported``/``rejected`` may be reached after an evidence-backed check.
HYPOTHESIS_STATUSES = ("unverified", "supported", "rejected")


class DiagnosisInvestigationRequest(BaseModel):
    """One turn in a diagnosis investigation.

    Dates are optional on purpose: the Agent resolves the window from the
    question (Task42), so a caller that does not pass them is not forced to pick
    a range by hand. ``thread_id`` resumes a previous investigation; omit it on
    the first turn and the response supplies the new id.
    """

    question: str = Field(min_length=1, max_length=500)
    branch_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    thread_id: Optional[str] = Field(default=None, max_length=128)


class InvestigationWindow(BaseModel):
    """The comparison window the Agent actually used, plus where it came from.

    ``baseline_*`` is the adjacent equal-length window, the same invariant the
    deterministic diagnosis uses. ``source`` tells the UI whether the caller,
    the question, the Agent, or the default supplied the range, so an inferred
    window is never mistaken for one the user chose. ``partial`` marks windows
    that include a day still in progress (e.g. "this week" asked on a Tuesday).
    """

    start_date: date
    end_date: date
    baseline_start_date: date
    baseline_end_date: date
    source: Literal["question", "agent", "explicit", "default", "coverage"] = "default"
    expression: Optional[str] = None
    partial: bool = False
    notes: List[str] = []


class InvestigationStepWindow(BaseModel):
    """A date range a step refers to, as ``YYYY-MM-DD``.

    ``window`` on a step is the range the tool actually used. On a rejected
    call it is what the tool was asked for, and ``expected_window`` is the
    turn's resolved range the caller should retry with.
    """

    start_date: date
    end_date: date


class InvestigationStep(BaseModel):
    """One tool call the Agent made, in order.

    ``summary`` is human-readable ("近 7 天销售额下降 18%，主因是……") while
    ``arguments`` is the exact tool input. Failed calls stay in the trace with
    ``status="error"`` instead of disappearing: a reported "not checked" is part
    of the investigation's honesty.

    ``window`` records the range a dated tool actually queried (Task56), so a
    trace can be audited against ``resolved_window`` even when a call was
    rejected. ``expected_window`` is present only for a ``window_mismatch``.
    """

    index: int
    tool: str
    arguments: Dict[str, Any] = {}
    status: Literal["ok", "error"] = "ok"
    summary: str = ""
    evidence_refs: List[str] = []
    window: Optional[InvestigationStepWindow] = None
    expected_window: Optional[InvestigationStepWindow] = None


class InvestigationBudget(BaseModel):
    """How much of the Agent's bounded loop was used.

    Both limits are enforcement, not advice: ``recursion_limit`` is passed to
    LangGraph and ``tool_calls_limit`` is enforced by the tool wrapper. An
    exhausted budget is reported so the answer can name what was left unchecked.
    """

    tool_calls_used: int = 0
    tool_calls_limit: int = 6
    recursion_limit: int = 24
    exhausted: bool = False
    reason: Optional[str] = None


class InvestigationHypothesis(BaseModel):
    """An explanation under investigation, explicitly not a finding.

    ``status`` moves only when a tool result tests the explanation: an
    unevidenced plausible story stays ``unverified``. ``evidence_refs`` may be
    empty exactly when the status is ``unverified``.
    """

    text: str
    status: Literal["unverified", "supported", "rejected"] = "unverified"
    evidence_refs: List[str] = []
    note: str = ""


class InvestigationReport(BaseModel):
    """The Agent's written reading, split by how much is known.

    Same guardrail shape as :class:`DiagnosisAnalysis`: ``facts`` are derived
    from tool results, ``inferences`` must cite real evidence refs, and
    ``checks`` are the next things worth looking at. Hypotheses are kept on the
    response as :class:`InvestigationHypothesis` objects because they carry an
    investigation status that this report's claims do not.
    """

    status: Literal["ok", "insufficient_data"]
    source: Literal["llm", "rule", "mixed"]
    dropped: int = 0
    facts: List[DiagnosisClaim] = []
    inferences: List[DiagnosisClaim] = []
    checks: List[DiagnosisClaim] = []


class DiagnosisInvestigationResponse(BaseModel):
    """One investigated answer: conversation handle, evidence, and trace.

    ``diagnosis`` is the deterministic payload the Agent worked from (absent
    when it never ran the diagnosis tool). ``steps`` and ``evidence`` let a
    reviewer audit the reasoning; ``answer`` is what the staff member reads.
    """

    thread_id: str
    branch_id: int
    branch_name: str
    status: Literal["ok", "insufficient_data", "budget_exhausted", "llm_unavailable"]
    answer: str
    resolved_window: Optional[InvestigationWindow] = None
    steps: List[InvestigationStep] = []
    evidence: List[ThreadDiagnosisEvidence] = []
    hypotheses: List[InvestigationHypothesis] = []
    report: InvestigationReport
    diagnosis: Optional[DiagnosisResponse] = None
    budget: InvestigationBudget = InvestigationBudget()
    notes: List[str] = []


__all__ = [
    "DIAGNOSIS_STATUSES",
    "HYPOTHESIS_STATUSES",
    "INVESTIGATION_STATUSES",
    "WINDOW_SOURCES",
    "DiagnosisAnalysis",
    "DiagnosisClaim",
    "DiagnosisEvidence",
    "DiagnosisFinding",
    "DiagnosisInvestigationRequest",
    "DiagnosisInvestigationResponse",
    "DiagnosisPeriod",
    "DiagnosisResponse",
    "InvestigationBudget",
    "InvestigationHypothesis",
    "InvestigationReport",
    "InvestigationStep",
    "InvestigationStepWindow",
    "InvestigationWindow",
    "ThreadDiagnosisEvidence",
]
