"""Agent sub-flow for the store-anomaly diagnosis (``sdd/经营异常诊断Agent审计与Tasks.md`` §T3).

The Service (:mod:`backend.app.service.diagnostics`) has already decided *what*
is anomalous and assembled the facts behind each judgement. This module answers
*why* and *what to do about it* -- and nothing else. It performs no calculation,
opens no session, and changes no business state.

The split is deliberate, and it is the whole point of the feature:

* **已确认事实 (facts)** are written here, in Python, straight from the payload.
  The model never authors them, so a fact cannot drift away from its evidence.
* **有证据的推断 (inferences)** and **待核查事项 (checks)** are the model's job,
  but only as prose *over the evidence*. Each one must cite an ``evidence_ref``
  that exists in the payload; an item citing a ref that does not resolve is
  dropped whole. An item may not introduce a number the payload does not contain,
  and may not blame weather, holidays, competitors, staffing, promotions or
  sell-outs -- the system holds no data for any of those, so the validator
  refuses them rather than trusting the prompt to avoid them.
* **未经证实的假设 (hypotheses)** take no evidence refs at all. They are the
  model's speculative explanations, prefixed with ``未经证实：`` by this module
  (never by the model) and rendered in their own section, so a guess can never be
  read as an evidenced cause. The same number and forbidden-keyword guards apply.
* When the model is unreachable, or its answer does not survive validation, the
  deterministic :func:`rule_causes` take over as inferences. They state only what
  the metric relationships already imply, and each still cites real evidence.
  The rules do not invent hypotheses; that bucket is empty in fallback mode.
* **No findings means no call at all.** There is nothing to explain, so the model
  is never consulted -- a quiet day costs nothing and a refusal cannot be talked
  into a cause.

The interesting failure this guards against is not a hallucinated sentence; it is
a *plausible* one. "客流下降可能与周边竞品活动有关" reads perfectly and would be
routine in a generic analytics tool, but nothing in this system observed it. It
belongs in hypotheses -- labelled unverified -- not in inferences.
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.app.agent.llm import extract_json_from_text, llm_client
from backend.app.service.diagnostics import (
    TREND_DIFFUSE,
    TREND_GRADUAL,
    TREND_GRADUAL_UP,
    TREND_SINGLE_DAY,
    TREND_SINGLE_DAY_UP,
)

# Shapes that were actually classified by the Service. Anything else under a
# "逐日走势" label -- ``None``, the diffuse bucket -- is not a confirmed fact.
_CLASSIFIED_SHAPES = (
    TREND_SINGLE_DAY,
    TREND_GRADUAL,
    TREND_SINGLE_DAY_UP,
    TREND_GRADUAL_UP,
)

# What the analysis concludes about itself. ``no_findings`` and
# ``insufficient_data`` both mean "no causes were produced", but for opposite
# reasons -- a quiet period versus an unjudgeable one -- and the UI has to tell
# them apart, so they stay distinct here.
ANALYSIS_OK = "ok"
ANALYSIS_NO_FINDINGS = "no_findings"
ANALYSIS_INSUFFICIENT = "insufficient_data"

# Who wrote the inferences and checks. ``rule`` is not a degraded mode to hide:
# when the model is unreachable the analysis is narrower, and the caller is told
# so.
SOURCE_LLM = "llm"
SOURCE_RULE = "rule"

# Added by the validator, never taken from the model, so an unverified guess is
# labelled as such no matter what the model wrote.
HYPOTHESIS_PREFIX = "未经证实："

# Causes this system has no data to support (audit §F). A cause mentioning any of
# these asserts something nothing in the database observed, so it is discarded --
# including the honest-sounding "可能受天气影响". The separation matters: a
# *check* may ask a human to go and look, but a *cause* may not claim knowledge.
FORBIDDEN_CAUSE_KEYWORDS = (
    # 天气
    "天气", "下雨", "雨天", "暴雨", "台风", "气温", "降温", "高温", "气候",
    # 节假日 / 日历
    "节假日", "假期", "节日", "春节", "国庆", "五一", "元旦", "中秋", "周末",
    "节后", "节前",
    # 竞对 / 商圈
    "竞对", "竞争对手", "同行", "隔壁", "对面", "商圈", "客流被分流",
    # 排班 / 人手
    "排班", "人手", "人员不足", "员工不足", "请假", "缺勤", "离职", "招不到人",
    # 促销 / 价格策略
    "促销", "打折", "折扣", "优惠活动", "满减", "降价", "做活动", "补贴",
    # 售罄 / 供货
    "售罄", "卖光", "卖完", "断货", "缺货", "无货", "供货",
)

# An evidence ref as ``_attach_evidence_refs`` writes it. Stripped before numbers
# are read out of a sentence, so "见 f0.e1" does not register 0 and 1 as claims.
_REF_PATTERN = re.compile(r"f\d+\.e\d+")

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

# Tolerance when matching a number in prose against the payload: the payload is
# rounded to two decimals, so "33.33" must match a stored 33.3333.
_NUMBER_TOLERANCE = 0.011

_ANALYSIS_SYSTEM_PROMPT = (
    "你是门店经营异常的分析助手。用户会给你一组**已经确认的**证据，"
    "每条证据前面有一个 evidence_ref 标识。\n"
    "请把结论分成三类：有证据支持的推断 inferences、没有证据支持的可能解释 hypotheses、"
    "需要人工核实的信息 checks。输出 JSON：\n"
    '{"inferences": [{"text": "有证据支持的推断", "evidence_refs": ["f0.e1"]}], '
    '"hypotheses": [{"text": "没有证据支持的可能解释"}], '
    '"checks": [{"text": "需要进一步核实的信息", "evidence_refs": ["f0.e1"]}]}\n'
    "硬性要求：\n"
    "1. 每条 inference、每条 check 都必须引用至少一个真实存在的 evidence_ref。"
    "引用不存在的 ref、或引用空列表，该条会被直接丢弃。\n"
    "2. hypotheses 不要写 evidence_ref：它代表目前没有证据支持的可能解释，"
    "系统会自动加上「未经证实」标注。\n"
    "3. 三类文字里都不要写任何数字（包括百分比、金额、天数）。"
    "需要指代数值时用对应证据的 evidence_ref，不要复述它。\n"
    "4. 禁止把原因归咎于天气、节假日、竞争对手、排班或人手、促销与折扣、菜品售罄 —— "
    "系统没有采集这些数据，提到它们的内容会被丢弃。\n"
    "5. 只做分析与建议，不给出任何会自动执行的处置动作。\n"
    "6. 只输出 JSON，不要输出任何其他文字。\n"
    "如果证据不足以支持任何 inference，就返回空的 inferences 列表，不要硬凑；"
    "没有可说的假设时返回空的 hypotheses 列表。"
)


# --------------------------------------------------------------------------- #
# Reading the Service payload
# --------------------------------------------------------------------------- #
def _findings(diagnosis: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(diagnosis.get("findings") or [])


def _refs_of(finding: Dict[str, Any]) -> List[str]:
    """Every evidence ref on one finding, in order."""
    return [item["ref"] for item in finding.get("evidence") or [] if item.get("ref")]


def _evidence_index(diagnosis: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every citable fact, keyed by ref -- the only refs a cause may use."""
    return {
        item["ref"]: item
        for finding in _findings(diagnosis)
        for item in finding.get("evidence") or []
        if item.get("ref")
    }


def _collect_numbers(value: Any, out: Set[float]) -> None:
    """Every number anywhere in the payload, recursively."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.add(float(value))
    elif isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_numbers(item, out)


def _allowed_numbers(diagnosis: Dict[str, Any]) -> Set[float]:
    """The numbers a cause is permitted to mention: exactly the payload's own.

    Percentages, totals, thresholds and day counts all arrive in the payload, so
    a legitimate sentence that quotes one passes. A number that is *not* here was
    invented, and the item carrying it is dropped.
    """
    numbers: Set[float] = set()
    _collect_numbers(diagnosis, numbers)
    return numbers


def _number_is_known(number: float, allowed: Set[float]) -> bool:
    return any(abs(number - candidate) <= _NUMBER_TOLERANCE for candidate in allowed)


def _numbers_in_text(text: str) -> List[float]:
    """Numbers a sentence actually asserts, ignoring any evidence refs it cites."""
    return [float(token) for token in _NUMBER_PATTERN.findall(_REF_PATTERN.sub(" ", text))]


def _asserts_forbidden_cause(text: str) -> bool:
    return any(keyword in text for keyword in FORBIDDEN_CAUSE_KEYWORDS)


# --------------------------------------------------------------------------- #
# 已确认事实 -- written here, never by the model
# --------------------------------------------------------------------------- #
def _facts(diagnosis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The confirmed facts, phrased from the payload alone.

    Kept deterministic on purpose: these are the statements a manager will act
    on, and they must be reproducible from the statistics without a model in the
    loop. Each one carries the refs of the evidence that produced it.
    """
    facts: List[Dict[str, Any]] = []
    for finding in _findings(diagnosis):
        direction = "下降" if finding.get("direction") == "down" else "上升"
        severity = "严重" if finding.get("severity") == "critical" else "需关注"
        facts.append(
            {
                "text": (
                    f"{finding['label']}{direction} {abs(finding['change_pct'])}%"
                    f"（本期 {finding['current']}，上期 {finding['previous']}，{severity}）。"
                ),
                "evidence_refs": _refs_of(finding),
            }
        )
        shape = _shape_fact(finding)
        if shape is not None:
            facts.append(shape)

    # The Service's own caveats are confirmed facts about the analysis: a window
    # that is still running, or a dish the ranking could not compare, is as real
    # as the numbers.
    for note in diagnosis.get("notes") or []:
        facts.append({"text": note, "evidence_refs": []})
    return facts


def _shape_citation(
    finding: Dict[str, Any], *, include_diffuse: bool = False
) -> Optional[Tuple[str, str]]:
    """The Service's shape verdict on a finding, as ``(shape, ref)``.

    Only the *classified* shapes count by default. The Service also emits a
    "not computable" placeholder under the same label -- for a per-order average,
    or for a dish the per-request daily cap left out -- and that belongs in the
    checklist, not in the confirmed facts, so it is filtered out here by the
    value being a known shape rather than ``None``.

    ``include_diffuse`` lets a *check* use the ``分散波动`` verdict too: it is
    still a classified shape and tells the reader where to look, even though it
    was deliberately kept out of the confirmed facts.
    """
    for item in finding.get("evidence") or []:
        if not item.get("ref") or not item["label"].endswith("逐日走势"):
            continue
        value = item.get("value")
        if value in _CLASSIFIED_SHAPES or (
            include_diffuse and value == TREND_DIFFUSE
        ):
            return value, item["ref"]
    return None


def _shape_fact(finding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A second fact when the Service classified the shape of the movement."""
    citation = _shape_citation(finding)
    if citation is None:
        return None
    shape, ref = citation
    direction = "上升" if finding.get("direction") == "up" else "下降"
    return {
        "text": f"{finding['label']}的{direction}形态为「{shape}」。",
        "evidence_refs": [ref],
    }


# --------------------------------------------------------------------------- #
# 规则式归因 -- the fallback, and the floor under the model
# --------------------------------------------------------------------------- #
def _by_metric(diagnosis: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for finding in _findings(diagnosis):
        grouped.setdefault(finding["metric"], []).append(finding)
    return grouped


def rule_causes(diagnosis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Inferences the metric relationships already imply, with no model involved.

    Every one of these is a relationship between findings the Service produced,
    not a guess about the world: a basket that shrank while traffic held, a drop
    that cancellations did not accompany, a fall confined to individual dishes.
    The audit (§F) lists exactly this family as what the data can support, and
    nothing outside it is said.
    """
    grouped = _by_metric(diagnosis)
    causes: List[Dict[str, Any]] = []

    # Revenue down *and* average order value down: the shortfall is partly a
    # smaller basket, not only fewer baskets.
    if "revenue" in grouped and "average_order_value" in grouped:
        aov = grouped["average_order_value"][0]
        causes.append(
            {
                "text": (
                    "销售额与客单价同时下降，说明缺口有一部分来自每单金额变小，"
                    "而不完全是单量变少。"
                ),
                "evidence_refs": _refs_of(aov),
            }
        )

    # Volume down with no cancellation rise: whatever moved is upstream of the
    # cancellation step, which is the one thing the data can actually rule out.
    if "order_count" in grouped and "cancelled_rate" not in grouped:
        volume = grouped["order_count"][0]
        causes.append(
            {
                "text": (
                    "订单量下降期间取消率没有同步上升，因此流失不是发生在下单之后被取消这一环，"
                    "更可能出现在下单之前。"
                ),
                "evidence_refs": _refs_of(volume),
            }
        )

    # Individual dishes moved while the store's volume held: a dish problem (or a
    # dish-level movement) rather than a store-wide one. A rise is described the
    # same neutral way as a fall -- the system does not know whether growth is an
    # anomaly, so it must not call it a "problem".
    dishes = grouped.get("dish_sales") or []
    if dishes and "order_count" not in grouped:
        fell = any(dish.get("direction") == "down" for dish in dishes)
        rose = any(dish.get("direction") == "up" for dish in dishes)
        if rose and not fell:
            text = (
                "整店订单量没有明显变化，而这些菜品销量明显上升，"
                "变化更可能集中在个别菜品而不是全店客流。"
            )
        elif fell and rose:
            text = (
                "整店订单量没有明显变化，而这些菜品销量有升有降，"
                "变化更可能集中在个别菜品而不是全店客流。"
            )
        else:
            text = (
                "整店订单量没有明显变化，而这些菜品销量明显下滑，"
                "问题更可能集中在个别菜品而不是全店客流。"
            )
        causes.append(
            {
                "text": text,
                "evidence_refs": [ref for dish in dishes for ref in _refs_of(dish)],
            }
        )

    # Cancellations up: say which side moved, in words, from the split the
    # Service already counted.
    if "cancelled_rate" in grouped:
        cancellations = grouped["cancelled_rate"][0]
        split = next(
            (
                item
                for item in cancellations.get("evidence") or []
                if isinstance(item.get("value"), dict)
            ),
            None,
        )
        if split is not None:
            sides = []
            if split["value"].get("customer"):
                sides.append("顾客主动取消")
            if split["value"].get("staff"):
                sides.append("员工在后台取消")
            if split["value"].get("unattributed"):
                sides.append("未标明操作者的取消")
            if sides:
                causes.append(
                    {
                        "text": (
                            "取消率上升，其中包含" + "、".join(sides) + "。"
                            "这几类的处理方向不同：顾客取消偏需求侧，"
                            "员工取消偏运营侧。"
                        ),
                        "evidence_refs": [split["ref"]],
                    }
                )
    return causes


def rule_checks(diagnosis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What a human still has to go and look at, given what is not in the data.

    Each check is tied to the gap that produced it -- a metric with no daily
    breakdown, an attribution keyed on a different clock -- so the list is a
    chore, not a set of open-ended suspicions.
    """
    grouped = _by_metric(diagnosis)
    checks: List[Dict[str, Any]] = []

    for finding in _findings(diagnosis):
        metric = finding["metric"]
        if metric == "dish_sales":
            shape_citation = _shape_citation(finding, include_diffuse=True)
            if shape_citation is not None:
                shape, ref = shape_citation
                if shape in (TREND_SINGLE_DAY, TREND_SINGLE_DAY_UP):
                    text = (
                        "该菜品的变化集中在单日，先核查该日的实际营业与出单情况"
                        "（是否售罄、是否漏单或停售）。"
                        if shape == TREND_SINGLE_DAY
                        else "该菜品的上升集中在单日，先核查该日是否有促销、"
                        "团购或活动性大单。"
                    )
                elif shape in (TREND_GRADUAL, TREND_GRADUAL_UP):
                    text = (
                        "该菜品本期多数天数低于上期水平，属于持续性变化，"
                        "建议按周复核而不是只看某一天。"
                        if shape == TREND_GRADUAL
                        else "该菜品本期多数天数高于上期水平，属于持续性变化，"
                        "建议按周复核备货与出餐能力。"
                    )
                else:
                    text = "该菜品销量波动分散在多日，建议按日核对出单与可售情况。"
                checks.append({"text": text, "evidence_refs": [ref]})
            else:
                # The dish was not covered by the per-dish daily request (the
                # per-request cap), so say the shape is unknown rather than guess.
                if finding.get("direction") == "up":
                    shape_options = "单日冲高还是整期上升"
                else:
                    shape_options = "单日塌方还是整期缓降"
                checks.append(
                    {
                        "text": (
                            f"本次没有展开该菜品的逐日明细，无法判断是{shape_options}，"
                            "需要人工核查它在该区间的每日出单情况。"
                        ),
                        "evidence_refs": _refs_of(finding),
                    }
                )
        elif metric == "average_order_value":
            checks.append(
                {
                    "text": (
                        "客单价变化可能来自菜品结构变化，现有统计没有拆解到品类，"
                        "需要核查该区间的菜品构成。"
                    ),
                    "evidence_refs": _refs_of(finding),
                }
            )
        elif metric == "cancelled_rate":
            checks.append(
                {
                    "text": (
                        "取消归因按「取消动作发生的时间」统计，与按订单创建时间统计的取消数"
                        "口径不同，区间边界处需要人工核对。"
                    ),
                    "evidence_refs": _refs_of(finding),
                }
            )

    order_volume = (grouped.get("order_count") or [None])[0]
    if order_volume is not None:
        citation = _shape_citation(order_volume)
        if citation is not None:
            shape, ref = citation
            checks.append(
                {
                    "text": (
                        "本期缺口集中在单日，先核查该日的实际营业情况"
                        "（是否整日未营业、是否漏单）。"
                        if shape == TREND_SINGLE_DAY
                        else "本期每一天都不如上期水平，属于持续性变化，"
                        "建议按周复核而不是只看某一天。"
                    ),
                    "evidence_refs": [ref],
                }
            )

    for note in diagnosis.get("notes") or []:
        if "尚未过完" in note:
            checks.append({"text": note + "建议去掉今天后重新核对。", "evidence_refs": []})
    return checks


# --------------------------------------------------------------------------- #
# 模型路径 -- and the validator that decides whether to keep its answer
# --------------------------------------------------------------------------- #
def _evidence_brief(diagnosis: Dict[str, Any]) -> str:
    """The evidence, refs first, as the model's whole view of the world.

    The model never sees the raw statistics on their own: every number it could
    quote arrives attached to the ref it must cite.
    """
    lines: List[str] = []
    for index, finding in enumerate(_findings(diagnosis)):
        lines.append(
            f"异常 {index + 1}：{finding['label']}（{finding['direction']}，"
            f"{finding['severity']}）"
        )
        for item in finding.get("evidence") or []:
            lines.append(
                f"  {item.get('ref')} | {item.get('label')} | {item.get('value')} | "
                f"{item.get('detail')}"
            )
    return "\n".join(lines)


def _accept_claims(
    items: Any, *, refs: Dict[str, Any], allowed: Set[float]
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep only the claims that cite real evidence and assert nothing invented.

    A claim is dropped, whole, when it cites a ref that does not exist, cites
    nothing at all, blames a cause the system cannot see, or carries a number the
    payload does not contain. Partial credit is not offered: a sentence that is
    half-evidenced is a sentence whose other half is a guess.
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        text = str(item.get("text") or "").strip()
        cited = [ref for ref in item.get("evidence_refs") or [] if isinstance(ref, str)]
        if (
            not text
            or not cited
            or any(ref not in refs for ref in cited)
            or _asserts_forbidden_cause(text)
            or any(
                not _number_is_known(number, allowed)
                for number in _numbers_in_text(text)
            )
        ):
            dropped += 1
            continue
        kept.append({"text": text, "evidence_refs": cited})
    return kept, dropped


def _accept_hypotheses(
    items: Any, *, allowed: Set[float]
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep only the guesses that stay guesses.

    A hypothesis is the one bucket allowed to name a cause the system cannot see,
    which is exactly why it is guarded at least as tightly as an inference: it
    may cite no evidence ref at all (citing one would let a guess borrow a
    fact's authority), it may not carry a number the payload does not contain,
    and it is subject to the same forbidden-keyword list. The ``未经证实：``
    prefix is added here rather than trusted to the model, so the label survives
    even when the model forgets it.
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        text = str(item.get("text") or "").strip()
        if text.startswith(HYPOTHESIS_PREFIX):
            text = text[len(HYPOTHESIS_PREFIX) :].strip()
        cited = item.get("evidence_refs") or []
        if isinstance(cited, str):
            cited = [cited]
        if (
            not text
            or cited
            or _asserts_forbidden_cause(text)
            or any(
                not _number_is_known(number, allowed)
                for number in _numbers_in_text(text)
            )
        ):
            dropped += 1
            continue
        kept.append({"text": HYPOTHESIS_PREFIX + text, "evidence_refs": []})
    return kept, dropped


async def _llm_analysis(
    diagnosis: Dict[str, Any], refs: Dict[str, Any], allowed: Set[float]
) -> Optional[Dict[str, Any]]:
    """The model's inferences, hypotheses and checks, or ``None`` if unusable."""
    raw = await llm_client.generate(
        prompt=_evidence_brief(diagnosis), system_prompt=_ANALYSIS_SYSTEM_PROMPT
    )
    parsed = extract_json_from_text(raw)
    if not isinstance(parsed, dict):
        return None

    inferences, dropped_inferences = _accept_claims(
        parsed.get("inferences"), refs=refs, allowed=allowed
    )
    hypotheses, dropped_hypotheses = _accept_hypotheses(
        parsed.get("hypotheses"), allowed=allowed
    )
    checks, dropped_checks = _accept_claims(
        parsed.get("checks"), refs=refs, allowed=allowed
    )
    if not inferences:
        # An answer that offers no usable inference is no better than no answer,
        # and the rules can still say something true. Fall through.
        return None
    return {
        "source": SOURCE_LLM,
        "dropped": dropped_inferences + dropped_hypotheses + dropped_checks,
        "inferences": inferences,
        "hypotheses": hypotheses,
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _no_findings_analysis(diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    """The answer when the Service judged nothing -- produced without a model.

    A refusal and a quiet period are different results and are labelled
    differently, but neither has a cause to explain, so neither calls out. The
    Service's own notes become the checklist: they are precisely the things a
    human would have to supply before this could be answered at all.
    """
    # ``ANALYSIS_INSUFFICIENT`` deliberately equals the Service's
    # ``insufficient_data`` status: the Agent passes the Service's verdict
    # through rather than re-deciding it. ``no_findings`` is the Agent's own --
    # the Service returns ``ok`` there, which is true but says nothing about
    # whether there is anything to explain.
    refused = diagnosis.get("status") == ANALYSIS_INSUFFICIENT
    return {
        "status": ANALYSIS_INSUFFICIENT if refused else ANALYSIS_NO_FINDINGS,
        "source": SOURCE_RULE,
        "dropped": 0,
        "facts": [],
        "inferences": [],
        "hypotheses": [],
        "checks": [
            {"text": note, "evidence_refs": []} for note in diagnosis.get("notes") or []
        ],
    }


async def analyze_diagnosis(diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    """Explain and advise on a :class:`DiagnosisService` payload.

    Returns ``{"status", "source", "dropped", "facts", "inferences",
    "hypotheses", "checks"}``:

    * ``facts`` are deterministic and always come from the payload;
    * ``inferences`` / ``checks`` come from the model when it is reachable and
      cite only real evidence, and from :func:`rule_causes` /
      :func:`rule_checks` otherwise;
    * ``hypotheses`` are the model's un-evidenced guesses, every one prefixed
      with ``未经证实：``; the rule fallback emits none;
    * ``source`` says which of the two happened, so a narrower analysis is never
      mistaken for a fuller one;
    * ``dropped`` counts the model's claims that were thrown away.

    The model is not consulted at all when there is nothing to explain.
    """
    if not _findings(diagnosis):
        return _no_findings_analysis(diagnosis)

    facts = _facts(diagnosis)
    refs = _evidence_index(diagnosis)
    allowed = _allowed_numbers(diagnosis)

    llm_result: Optional[Dict[str, Any]] = None
    try:
        llm_result = await _llm_analysis(diagnosis, refs, allowed)
    except Exception:  # defensive: a degraded seam must not fail the request
        llm_result = None

    if llm_result is not None:
        return {"status": ANALYSIS_OK, "facts": facts, **llm_result}
    return {
        "status": ANALYSIS_OK,
        "source": SOURCE_RULE,
        "dropped": 0,
        "facts": facts,
        "inferences": rule_causes(diagnosis),
        "hypotheses": [],
        "checks": rule_checks(diagnosis),
    }


__all__ = [
    "ANALYSIS_INSUFFICIENT",
    "ANALYSIS_NO_FINDINGS",
    "ANALYSIS_OK",
    "FORBIDDEN_CAUSE_KEYWORDS",
    "HYPOTHESIS_PREFIX",
    "SOURCE_LLM",
    "SOURCE_RULE",
    "analyze_diagnosis",
    "rule_causes",
    "rule_checks",
]
