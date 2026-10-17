# Agent Map

> 复习地图（不是设计文档，那是 ARCHITECTURE.md）。
> 把 backend/app/agent/ 的代码映射到两个基础模型上，以后读任何 agent 都用同一套框架。

## 0. 两个心智模型（不要更多）

本项目所有 agent 都是这两个模型的应用或轻量变体，没有第三种。

```text
模型 A - Tool-Calling（工具调用环）
  LLM --决定调哪个工具--> Tool --薄封装--> Service --> DB
  约束：LLM 永远不碰 DB，只触发固定工具

模型 B - Agent + Workflow（LangGraph）
  State + Node + Edge 定骨架，LLM 只负责"不确定的那一步决策"
```

四个 agent 面对两个模型的映射：

| Agent 面 | 入口路由 | 核心文件 | 属于 |
|---|---|---|---|
| 顾客联系后厨 KDS | POST /api/agent/order/{id}/request | kds.py + tools/kds_tools.py | 模型 A（规则优先，LLM 兜底） |
| 顾客礼物审批 | POST /api/orders、/staff/approvals | approvals.py + tools/gift_tools.py | 模型 A（LLM 优先，规则兜底） |
| 顾客预订逼单 | GET /api/reservations/availability | offers.py + tools/gift_tools、table_tools | 模型 A 变体（纯规则措辞，不调 LLM） |
| 诊断-一次性 | GET /api/staff/diagnosis | diagnosis.py | 单次 LLM + 护栏（不是 loop） |
| 诊断-多轮调查 | POST /api/staff/diagnosis/investigate | diagnosis_investigation.py + harness.py + diagnosis_tools.py + diagnosis_report.py | 模型 B（LangGraph ReAct） |

共享底座（不属于某个面）：llm.py + llm_providers.py（模型接入）、time_range.py（时间窗解析）、checkpointer.py（多轮记忆）。

---

## 1. 必要性体检（事实，不是感觉）

判断依据：全仓 import 图 + scripts/ + CI 实况（本仓库没有任何 CI，只有 docker-compose.yml）。

| 模块 | 行数 | 在请求热路径 | 谁在用 | 判定 |
|---|---|---|---|---|
| harness.py | 1657 | 是 | investigation + tools + eval/baseline 脚本 | 必留（职责混了 6 类，可拆） |
| diagnosis_tools.py | 1206 | 是 | investigation | 必留（LLM 唯一的"手"） |
| diagnosis_investigation.py | 1088 | 是 | route | 必留（鉴权 + 组装） |
| diagnosis.py | 812 | 是 | GET 路由 + investigation | 必留（分析 + 护栏） |
| time_range.py | 467 | 是 | tools + investigation | 必留 |
| diagnosis_report.py | 177 | 是 | harness + investigation | 必留（结构化 schema） |
| llm.py + llm_providers.py | 310 | 是 | kds / approvals / diagnosis | 必留 |
| kds.py + approvals.py + offers.py | 458 | 是 | 顾客路由 | 必留 |
| tools/（kds / gift / table） | 217 | 是 | 上述 helper | 必留 |
| checkpointer.py | 53 | 是 | main.py | 必留 |
| diagnosis_evaluation.py | 1278 | 否 | 仅 scripts/diagnosis_eval.py + 1 个测试 | 看约束（无 CI，只在手动跑时活） |
| InMemoryEvidenceRegistry / EvidenceRecord | ~90 | 否 | 仅 test_harness.py | 可降为测试内部 |

两个硬结论：

1. diagnosis_agent.py 曾是死代码（全仓无人 import），已删除（2026-10-21）。注意别和 harness.py 里的 build_diagnosis_agent 函数混淆。
2. 没有 CI。diagnosis_evaluation.py 这 1278 行只在你手动跑 `scripts/diagnosis_eval.py` 时才活。要引用 sdd/diagnosis_eval_report.json 的指标就留，否则是"写了没接进流程"的重量。

盔甲部分全部有测试背书（修复环、跨轮证据、compact_context 都被 test_diagnosis_guardrails / structured_output / harness 覆盖），不是没人管的死重量。

---

## 2. 骨架版（快乐路径）

7000 行的核心控制流其实是这十几行。真实返回的 dict 里多出来的每个键，都能追溯到一块盔甲。

    async def investigate(question, staff, session, thread_id=None):
        branch = resolve_staff_branch(staff)   # 身份决定门店
        window = resolve_window(question)      # 时间窗解析
        ctx    = Ctx(staff, session, branch, window)
        model  = build_chat_model()            # 一个模型
        tools  = build_readonly_tools(ctx)     # 7 个只读工具，每个薄封装一个 Service
        agent  = create_agent(model, tools, response_format=Report)  # LangGraph 自动跑 ReAct 循环
        turn   = await agent.ainvoke({'messages': [question]}, thread_id)
        report = turn.structured_response       # {answer, facts, inferences, checks}
        return {'thread_id': thread_id, 'branch_name': branch.name,
                'answer': report.answer, 'resolved_window': window,
                'steps': ctx.steps, 'evidence': ctx.evidence,
                'report': report, 'diagnosis': ctx.diagnosis}

---

## 3. 每块盔甲正好回答一个「保证题」

原则：**只有当你需要承兑某个保证时，对应代码才有必要存在。** 不问，就可以删。

| 盔甲代码 | 它回答的问题 | 不问就能删？ |
|---|---|---|
| 数字白名单 + ref 校验 + FORBIDDEN_CAUSE_KEYWORDS | 怎么保证模型不编造原因/数字？ | 是 |
| 「未经证实：」前缀 + hypotheses 分桶 | 怎么区分推断和猜测？ | 是 |
| _repair_if_needed 修复环 | 模型第一次答错怎么办？ | 是（收益最小） |
| tool budget / recursion_limit / timeout | 模型会不会死循环、卡死？ | 是 |
| _fallback + llm_unavailable | 模型挂了接口会 500 吗？ | 是 |
| checkpointer + ThreadEvidenceRegistry | 追问「那周末呢」时它记得上一轮吗？ | 是（除非要追问） |
| compact_context 备忘压缩 | 聊几十轮上下文不爆吗？ | 是 |
| diagnosis_evaluation.py | 你怎么证明它可靠（有没有数据）？ | 可移出仓库 |

结论：**热路径骨架约 300 行是必须的；剩下约 7000 行是你主动选择承兑的一串保证。** 它们不是过度工程，而是"当有人问怎么保证 X 时能拿得出的答案"。唯一真正过度的是三块：修复环（复杂度换很小收益）、跨轮 ref（只有追问场景才用）、以及无 CI 时的手动评测。

---

## 4. 命名对照与阅读顺序

这套文件名前缀全是 diagnosis，看不出层次。按职责对照：

| 文件 | 真实职责 |
|---|---|
| diagnosis_investigation.py | 编排：鉴权 + 解析窗口 + 调 harness + 组装响应 |
| harness.py | 运行时：建模型 / 建图 / 预算 / 超时 / 上下文压缩 / 证据注册表 / RunTrace |
| diagnosis_tools.py | 7 个只读工具（Service 薄封装） |
| diagnosis_report.py | 结构化输出 schema + 修复 prompt |
| diagnosis.py | 分析与护栏（facts / inferences / checks + 校验器） |
| diagnosis_evaluation.py | 离线评测（不在请求路径） |

从外到内的阅读顺序：
1. routes/staff_diagnostics.py（入口）
2. diagnosis_investigation.py 的 run_investigation（控制流）
3. harness.py 的 run_harness_turn / build_diagnosis_agent（图怎么建）
4. diagnosis_tools.py 的 build_diagnosis_tools（工具）
5. diagnosis.py 的 analyze_diagnosis（护栏）


