Task1【无依赖】搭建 Repo / Service 基础结构、领域异常与会话/事务边界，并建立架构边界测试。

Task2【依赖 Task1】实现 Branch、Menu 与 FAQ 的 Repo/Service，统一菜单可用性规则。

Task3【依赖 Task2】将 Branch / Menu / FAQ 相关 API 迁移到 Service。

Task4【依赖 Task2】将 Agent 中 Branch / Menu / FAQ 的数据访问迁移到 Service，工具降为薄适配层。

Task5【依赖 Task1】实现 Order Repo 与库存原子操作 Repo。

Task6【依赖 Task5、Task2】实现 OrderService（下单、价格预估、状态流转、查询）。

Task7【依赖 Task6】将订单相关 API 与 Agent 下单工具/节点迁移到 OrderService。

Task8【依赖 Task1、Task2】实现 Reservation Repo/Service。

Task9【依赖 Task8】将预订相关 API 与 Agent 预订工具/节点迁移到 ReservationService。

Task10【依赖 Task1】实现 Auth Repo/Service。

Task11【依赖 Task10】将 Auth API 及鉴权依赖中的数据库访问迁移到 AuthService。

Task12【依赖 Task6、Task8】将客户活动查询（customer_orders API 与 status_node）迁移到 Order/Reservation Service。

Task13【依赖 Task3、Task4、Task7、Task9、Task11、Task12】清理残留的旧数据库访问与重复业务逻辑。

Task14【依赖 Task13】运行架构边界测试与全量回归测试。

Task15【依赖 Task6、Task7】完善订单生命周期：下单、确认、制作、出餐、交付、取消；实现合法状态转换及取消后的库存处理。

Task16【依赖 Task15】完善订单价格与明细：支持菜品规格加价、数量计算、金额计算及订单价格快照，确保菜品后续变价不影响历史订单。

Task17【无依赖】完善预订生命周期：查询可用餐桌、预订确认、改约、取消、入座、完成；处理时间冲突及并发预订。

Task18【依赖 Task15、Task17】完善权限与审计：后端校验用户、员工及门店权限；记录订单取消、库存修改、餐桌改派等关键操作及操作者。

Task19【依赖 Task15、Task16、Task17】实现经营统计与异常处理：统计订单量、销售额及菜品销量；处理重复请求、库存不足、预订冲突及操作失败，并编写对应测试。

Task20【无依赖】抽取诊断可复用判定：将窗口推导、有效经营日、日历对齐和形态判定从 Service 私有实现中抽出为公开纯函数；形态判定增加 direction 参数且默认 down，保持现有诊断输出不变；补充单元测试，并用黄金 payload 验证零行为变化。

Task21【依赖 Task20】菜品异常检测改为双向：按变化绝对值是否达到 CHANGE_THRESHOLD_PCT 产出 up / down 菜品 finding；跌幅达到 CRITICAL_CHANGE_PCT 标 critical，涨幅最高标 warning；上期排行缺失的菜品继续判为“无法判断”，不把 0 → N 当作上涨；补充升降对称测试。

Task22【依赖 Task20】新增菜品 × UTC 日聚合：在 StatsRepo 增加 dish_daily_totals，一次查询返回指定菜品集合的逐日销量；沿用销售状态口径、UTC 半开区间和门店权限，不新增表、不新增依赖、不改动现有聚合方法。

Task23【依赖 Task21、Task22】实现菜品逐日 Service 能力：构建指定菜品的逐日序列，区分真实零销量、无记录和可售性未知；复用方向化形态判定输出“单日冲高 / 整期上升 / 分散波动”或“单日塌方 / 整期缓降 / 分散波动”；单次批量查询，覆盖条数受 K 上限约束。

Task24【依赖 Task23】接入菜品逐日到诊断 payload：用逐日序列和形态替换菜品 finding 中 _dish_trend_gap 的 None，保持 f{N}.e{M} 编号和现有护栏不变；无 finding 时不发起查询、不增加 LLM 调用。

Task25【依赖 Task24】更新测试与黄金基线：改写 test_diagnostics.py 中硬断言“没有单品逐日数据”的测试，覆盖升降双向、真实零 / 无记录、上期缺失、形态标签、K 上限和证据引用；更新实测记录中的诊断基线并记录新增 finding 的差异。

Task26【无依赖】实现事实 / 推断 / 假设分离：将分析结果拆为确定性 facts、有证据引用的 inferences、无证据支撑且标注“未经证实”的 hypotheses；同步修改模型 prompt、JSON 解析、规则回退和前端分组展示；保留引用校验、数字白名单和禁用因果词护栏。

Task27【依赖 Task20】【可延后】实现同期同业对比 Service：复用 BranchService.list_branches 和 StatsService.get_branch_summary，对可见门店使用同一窗口和 MIN_BASELINE_DAYS 判定 ok / insufficient_baseline / no_data / zero_baseline / not_comparable，输出可比与不可比计数；不新增 repo 查询。

Task28【依赖 Task27】【可延后】接入同业对比入口：新增薄路由和响应 schema，门店绑定员工返回 not_comparable 及明确说明；前端增加独立对比入口；不改变现有诊断 payload 语义。

Task29【依赖 Task25、Task26、Task28（若执行）】横向回归与边界验收：验证黄金 payload、升降双向、架构边界、无异常不调模型、模型不可达回退、K 与同业数成本上限；确认 routes/ 与 agent/ 无数据库访问，新增能力只经 Service。

Task30【依赖 Task26】证据引用可读化：前端把 `f{N}.e{M}` 解析为“finding 标签 › evidence 标签”的可读来源，原始编号仅保留为锚点与悬停提示；证据表不再展示内部编号；补充纯函数单测与真实浏览器验证。

Task31【无依赖】删除经营异常诊断与同业对比功能全链路：移除后端 `service/diagnostics.py`、`service/branch_peers.py`、`agent/diagnosis.py`、`routes/staff_diagnostics.py`、`routes/staff_branch_peers.py`、`schemas/diagnostics.py`、`schemas/branch_peers.py`，以及 `routes/__init__.py` / `main.py` 中的注册；删除 `StatsRepo.dish_daily_totals` 与 `test_stats.py` 中对应测试；删除 5 个诊断相关后端测试文件；删除前端 `DiagnosisPanel`、`PeerComparison`、`diagnosisQuery`、`evidenceRefs` 及两个前端测试，移除 `AdminLayout` 的诊断标签与 `api.js` 的诊断接口；删除 `sdd/经营异常诊断Agent审计与Tasks.md`。验收：全库检索无残留引用，后端可正常导入，Task20-30 在进度表中标记为已回退。

Task32【依赖 Task31】删除确认无调用的死代码：删除 `GiftService.redeem_gift` 及其独占使用的 `GiftRepo.update_status`、仅导出未调用的 `is_surplus_category`，以及 `AuditService.record` 中无人传入的显式 `actor_type / actor_id / actor_name / actor_role` 参数分支；同步清理 `__all__`。验收：全库检索 0 调用，相关测试通过。

Task33【依赖 Task32】收敛门店权限校验：新增 `backend/app/service/scope.py`，提供统一的 `ensure_branch_scope` / `resolve_staff_branch`，替换 order、reservation、approval、menu、branch 中手写的越权判断（约 9 处），统一 403 文案；`list_tables` / `list_categories` 复用同一解析规则。验收：门店绑定员工的越权读写仍全部返回 403。

Task34【依赖 Task33】统一菜单序列化：合并 `menu.py` 中 `item_payload / variant_payload` 与 `_menu_item_payload` 两套重复的字段拼装，保留公开菜单与员工菜单各自的字段映射，更新 `test_cleanup_residual.py` 的检查。验收：两类菜单响应字段与现有契约一致。

Task35【依赖 Task33】合并审批状态查询：删除 `ApprovalService.get_approval_status`，单条订单详情改用 `get_approval_statuses([order_id])`；更新客户订单路由与相关测试。验收：订单详情与订单列表返回的审批状态一致。

Task36【依赖 Task33】移除魔法默认值：删除 `BranchService.list_tables` 与 `MenuService.list_categories` 中的 `staff.branch_id or branch_id or 1`，未绑定员工且未指定门店时显式报 400；核对前端调用点始终传入门店。验收：管理员与门店员工两个角色的列表页均正常加载。

Task37【依赖 Task31-Task36】清理残留与文档：确认 `routes/` 与 `agent/` 无 Repo 直连；更新 `README.md`、`ARCHITECTURE.md`、`sdd/Plan.md` 中已删除能力的描述；在本文档记录精简结果与回退说明。验收：架构边界测试通过，文档无失效引用。

Task38【依赖 Task37】全量回归：运行后端 pytest 与前端测试，把通过/失败结果记录到本文档；环境依赖（PostgreSQL / Ollama）导致的跳过需明确标注。验收：除环境限制外全部通过。

> 2026-10-17 追加：将经营诊断从“确定性报表 + 一次 LLM 总结”升级为只读调查 Agent。保留 `DiagnosisService` 与现有 `GET /staff/diagnosis`；使用 LangGraph/LangChain 预置的 `create_react_agent`，不手写 ReAct loop；通过 `recursion_limit` 和工具调用预算控制最大循环；Agent 自主选择时间区间，前端不再强制手调。

Task39【无依赖】定义诊断调查 Agent 的契约与接口骨架：在 `backend/app/schemas/diagnostics.py` 增加调查请求/响应模型（`question`、可选 `branch_id`、可选日期、`thread_id`、`answer`、`resolved_window`、`steps`、`evidence`、`hypotheses`、`report`、`budget`），约定 `POST /api/staff/diagnosis/investigate`；保留现有 `GET /staff/diagnosis` 不变。验收：schema 可导入、响应能表达自选时间区间/工具轨迹/证据引用/假设状态；不新增业务逻辑。

Task40【依赖 Task39】接入 LangGraph/LangChain 预置的 `create_react_agent`，不手写 planner/execute 循环：为现有 LLM 接缝提供 LangChain chat model 适配（优先新增 `langchain-openai` / `langchain-ollama`，或实现 `BaseChatModel` 包装现有 `llm_client`），构造带 tools 与 checkpointer 的诊断 Agent；用 `recursion_limit` 设置硬上限，并在工具包装层维护最多 5–6 次工具调用的预算，耗尽时强制收束。验收：现有 `langgraph` 依赖即可导入 `create_react_agent`；脚本化模型测试能完成多步工具调用并在上限停止；代码中不存在手写 ReAct loop。

Task41【依赖 Task39】实现核心只读诊断工具：`get_diagnosis`（`DiagnosisService.diagnose`）、`get_daily_series`（`StatsService.get_branch_summary` 的 `daily`）、`get_dish_daily`（`DishDailyService.get_dish_daily`）、`compare_peers`（`BranchPeerService.compare_branch_peers`）。工具由请求上下文构造，固定 staff / branch 范围，禁止 LLM 传入越权 `branch_id`；结果限制条数并生成稳定 evidence ref。验收：工具只调用 Service，不出现 select/execute/SQL；门店员工越权由 Service 返回 403；工具 schema 可被 `bind_tools` 识别；单元测试覆盖。

Task42【无依赖】让 Agent 自主选择时间区间：新增纯函数/工具 `resolve_time_range(expression)`，支持显式日期、今天/昨天、本周/上周、最近 7 天/最近 30 天、本月/上月及中英文表达；无明确区间时默认最近 7 天并标注 `source=default`；`本周`/`本月` 含未过完当天时标注 partial；同时返回相邻等长基线窗口。Agent 从用户问题中提取时间表达并调用该工具，前端不再要求手动选区间。验收：周一/月末/跨年/UTC 边界测试；歧义表达返回可解释结果；响应包含 `resolved_window` 与来源。

Task43【依赖 Task41】实现运营变更取证工具 `get_operational_changes`：包装 `AuditService.list_logs`，按调查窗口和门店筛选菜单下架、调价、库存调整、餐桌改派等记录；若现有 repo 仅支持最近 N 条，则补充 `start/end` 过滤（仅新增这一处 repo 查询）。验收：只返回窗口内记录、无越权、条数受限、证据可引用；测试覆盖。

Task44【依赖 Task40、Task41、Task42、Task43】实现调查接口与线程记忆：注册 `POST /api/staff/diagnosis/investigate`；从 JWT 注入 staff 并解析/校验 branch 范围；用 `create_react_agent` + `AsyncPostgresSaver` 按 `thread_id` 持久化调查状态，校验线程归属；返回步骤、证据、假设与报告；LLM 不可用或循环超限时返回确定性诊断 + 规则分析。验收：401/403/200；管理员可指定门店、门店员工固定本店；同一 `thread_id` 可续聊；LLM 离线不返回 500。

Task45【依赖 Task44】实现结论护栏与预算降级：复用/抽取现有 evidence ref 校验、数字白名单、禁止因果词与“未经证实”前缀；facts 只能来自工具结果，inferences 必须引用真实证据，无证据解释只能进入 hypotheses；预算或循环耗尽时输出已完成部分并明确说明未查项。验收：伪造 ref 被丢弃、输入外数字不出现、无证据因果不进入 inferences、耗尽时有明确说明；测试覆盖。

Task46【依赖 Task44】实现连续追问与区间切换：同一 thread 内支持“那周末呢”“改成上个月”“和别的门店比”等追问；窗口变化时复用或重新计算必要工具；线程隔离，员工只能访问自己的调查。验收：两轮以上对话测试；窗口与基线同步更新；越权 thread 返回 403/404。

Task47【依赖 Task45、Task46】前端诊断调查面板：在现有 `DiagnosisPanel` 旁增加提问框、示例问题、调查步骤时间线、假设板、报告分区（facts / inferences / hypotheses / checks）、当前 `resolved_window` 展示和连续追问；`api.js` 增加调查调用；保留原有一键诊断与证据表。验收：`npm run build` 通过；手工冒烟覆盖首轮调查、追问、LLM 失败提示；不破坏现有诊断展示。

Task48【依赖 Task47】黄金场景与全量回归：构造销售额下降由单品断崖、取消率上升、本店下降但同业平稳、数据不足四类场景；用脚本化 LLM 断言工具选择、证据引用与结论边界；跑后端 pytest、前端测试与构建；更新 README/ARCHITECTURE 中诊断与 Agent 描述（明确全局聊天入口仍未接入）。验收：场景全部通过；无证据引用/因果越界为 0；全量回归通过。

Task49【依赖 Task48】让调查 Agent 按真实数据覆盖期解析时间：新增门店订单日期范围查询，相对时间（含默认窗口）在最近数据已早于默认 7 天窗口时锚定到最近有数据的一天，显式日期保持不变；响应窗口与 notes 明确实际区间和数据覆盖范围。验收：仅含 2023 数据的评测库中，“最近/上个月”提问能命中 2023 数据，显式 2026 日期不被改写，全量回归通过。

Task50【依赖 Task49】删除未接线的旧多节点聊天图：移除 `agent/nodes/`、`agent/graph.py`、`agent/state.py`、`agent/flows.py`、`agent/labels.py`，以及只为旧节点服务的 `tools/faq_tools.py`、`order_tools.py`、`menu_tools.py`；删除/改写对应测试，保留仍被路由使用的 KDS、审批、offers、诊断、checkpointer 与 `llm.py`。验收：全库无旧图引用，后端可导入，全量测试通过，文档不再宣称存在客户聊天入口。

Task51【依赖 Task50】迁移到 LangChain `create_agent`：`langgraph.prebuilt.create_react_agent` 已弃用并将被移除；把诊断 Agent 的构造切换到 `langchain.agents.create_agent`，`prompt` 改为 `system_prompt`，保持 `checkpointer`、`recursion_limit`、工具预算与线程记忆语义不变；新增 `langchain` 依赖并更新 README/ARCHITECTURE。验收：不再导入 `create_react_agent`，诊断循环/追问/递归上限测试通过，全量回归通过。

> 2026-10-17 追加：诊断 Agent harness 改造。仅覆盖诊断调查 Agent，不引入通用多 Agent 框架；证据注册表第一阶段存 checkpoint state，不新增表；调查 API 只新增字段、不改现有契约；Task61-63 为可选 backlog，不阻塞核心 harness。

Task52【无依赖】建立诊断 Agent harness 基线：新增 `agent/harness.py` 的配置、运行 trace 与证据注册表契约；记录现有工具调用、工具耗时、输出字符数、token 估算、护栏丢弃数与降级原因；不改变现有行为。验收：契约单测；现有全量回归通过；基线报告可重复生成。

Task53【依赖 Task52】抽取 harness 运行时：把模型构建、工具注册/预算、checkpointer、超时与运行入口收敛到 harness；`run_investigation` 只保留鉴权、窗口解析与响应编排；保持现有响应契约。验收：无行为变化；诊断循环、追问、接口与全量回归通过。

Task54【依赖 Task53】上下文管理 middleware：用 `create_agent` 的 `wrap_model_call`/`before_model` 实现“当前轮原始工具结果 + 历史轮次调查备忘”；不修改 checkpoint 完整历史；加入 token/字符预算与 Ollama `num_ctx`。验收：多轮对话测试；超限时按预算降级；数字与 evidence ref 不丢失。

Task55【依赖 Task53】Thread 级证据注册表与稳定 ref：ref 增加 thread/turn 前缀；证据按 thread 持久化在 checkpoint state（第一阶段不新增表）；护栏校验 thread registry。验收：跨轮引用测试；旧 checkpoint 兼容；伪造 ref 仍被拒绝。

Task56【依赖 Task53】窗口一致性校验：所有日期工具的调用窗口必须与 `resolved_window` 一致；`steps` 记录每个工具实际窗口；不一致时返回可修正错误而非静默使用。验收：混合窗口、显式日期、覆盖期锚定测试。

Task57【依赖 Task53】结构化输出与有限修复：用 `create_agent(response_format=...)` 接 Pydantic 报告 schema；结构化结论被护栏丢弃时，把违规原因回传模型一次修复；仍失败回退确定性摘要。验收：结构化解析率提升；伪造引用/数字测试通过；API 仅新增字段。

Task58【依赖 Task53】工具输出结构化缩减：`get_dish_daily` 默认返回每道菜汇总、变化形态与异常日，完整逐日序列按需查询；为每个工具结果增加字符/token 预算与截断元数据。验收：单轮上下文体积明显下降；数据仍精确；工具与黄金场景测试通过。

Task59【依赖 Task53】超时与预算细节：用 `asyncio.timeout` 包住单次调查，给每个工具单独限时；`resolve_time_range` 不占 6 次工具预算；记录超时/失败并降级。验收：慢模型、慢工具、预算耗尽测试。

Task60【依赖 Task52、Task58】评估 harness：建立固定问题/场景数据集；用脚本化模型断言工具轨迹、窗口、证据引用和数字边界；输出工具选择、引用有效、数字幻觉、降级率、耗时与 token 指标；保留可选 live smoke。验收：可重复运行；CI 友好；指标报告落盘。

Task61【可选】【依赖 Task58、Task60】诊断维度扩展：在现有数据能力内增加预订健康度（预订/取消/未到店）、时段与星期分布、订单类型结构，以及“上周同星期/去年同期”基线；新增能力全部走 Service，并限制新增工具数量以适配预算。验收：新工具无越权、黄金场景通过、全量回归通过。

Task62【可选】【依赖 Task55】线程生命周期：前端持久化 thread_id 或增加服务端线程列表；为 checkpoint 增加保留时长/每线程上限清理；调查接口增加按员工的简单限流。验收：刷新后可续聊；旧线程可清理；限流测试通过。

Task63【可选】【依赖 Task59、Task61】主动诊断：定时执行确定性诊断扫描，仅在发现异常时调用 harness 生成调查摘要并告警；包含去重与失败降级。验收：调度、去重、告警、LLM 不可用降级均有测试。

## 实施进度

| Task | 状态 |
|---|---|
| Task20 | 已完成 |
| Task21 | 已完成 |
| Task22 | 已完成 |
| Task23 | 已完成 |
| Task24 | 已完成 |
| Task25 | 已完成 |
| Task26 | 已完成 |
| Task27 | 已完成 |
| Task28 | 已完成 |
| Task29 | 已完成 |
| Task30 | 已完成 |
| Task31 | 已撤销（已恢复诊断与同业对比能力） |
| Task32 | 已完成 |
| Task33 | 已完成 |
| Task34 | 已完成 |
| Task35 | 已完成 |
| Task36 | 已完成 |
| Task37 | 已完成 |
| Task38 | 已完成 |
| Task39 | 已完成 |
| Task40 | 已完成 |
| Task41 | 已完成 |
| Task42 | 已完成 |
| Task43 | 已完成 |
| Task44 | 已完成 |
| Task45 | 已完成 |
| Task46 | 已完成 |
| Task47 | 已完成 |
| Task48 | 已完成 |
| Task49 | 已完成 |
| Task50 | 已完成 |
| Task51 | 已完成 |
| Task52 | 已完成 |
| Task53 | 已完成 |
| Task54 | 已完成 |
| Task55 | 已完成 |
| Task56 | 已完成 |
| Task57 | 已完成 |
| Task58 | 已完成 |
| Task59 | 已完成 |
| Task60 | 已完成 |
| Task61 | 已完成（2026-10-17） |
| Task62 | 未开始（可选 backlog） |
| Task63 | 未开始（可选 backlog） |

> 2026-10-17 更正：Task31 删除经营诊断是对需求的误判。已从 git 提交和上一会话的完整文件/编辑记录中恢复经营异常诊断、同业对比、证据引用及其全部测试；Task20-30 恢复为已完成，Task32-37 的无功能损失清理保留。

## Task38 回归结果（2026-10-17，恢复诊断能力后重跑）

- 编译与导入：`python -m compileall -q backend/app backend/tests` 通过；`python -c "import backend.app.main"` 输出 `IMPORT_OK`。
- 诊断专项测试：`pytest backend/tests/test_diagnostics.py test_diagnosis_helpers.py test_diagnosis_acceptance.py test_branch_peers.py test_dish_daily.py test_stats.py -q` → **108 passed**。
- 后端全量测试：`python -m pytest backend/tests -q` → **613 passed**（约 108s），无 PostgreSQL / Ollama 相关跳过。
- 前端测试：`npm --prefix frontend test` → **25 passed**（diagnosisQuery 防抖/缓存/请求序号 18 项，evidenceRefs 证据引用可读化 7 项）。
- Task36 契约变更保留：管理员访问门店列表接口时需显式传 `branch_id`；`test_branch_menu_faq_routes.py` 中 5 处调用已按新契约更新。

## Task39-47 实现记录（2026-10-17）

- **Task39**：`schemas/diagnostics.py` 增加 `DiagnosisInvestigationRequest/Response`；`POST /api/staff/diagnosis/investigate` 注册，原有 `GET /staff/diagnosis` 未改。
- **Task40**：`agent/diagnosis_agent.py` 使用 LangGraph `create_react_agent`，无手写 planner/execute 循环；工具预算 6 次、`recursion_limit=24`；兼容 LangGraph 在递归上限返回 state（而非抛错）的行为，截断时回答置空并标记 `recursion_limit`。
- **Task41**：`agent/diagnosis_tools.py` 提供 6 个只读工具，全部走 Service；分支由请求上下文注入，schema 无 `branch_id`；证据 ref 命名空间化为 `dailyN.eM` / `dishN.<id>.eM` / `peersN.sM` / `opsN.eM`。
- **Task42**：`agent/time_range.py` 支持显式日期、今天/昨天、本周/上周、本周末/上周末、最近 7/30 天、本月/上月；无表达时默认最近 7 天（`source=default`），并返回相邻等长基线；周末表达优先于“上周/本周”整周匹配。
- **Task43**：`get_operational_changes` 包装 `AuditService.list_logs`，按窗口 + 门店 + 运营动作过滤，限制 30 条，结果带 `opsN.eM` ref。
- **Task44**：`run_investigation` 先鉴权再读数据；新线程 `diag-<staff>-<branch>-<uuid>`，续聊校验线程归属并禁止切换门店；`AsyncPostgresSaver` 持久化，测试用 `MemorySaver`；模型不可用/异常时降级为确定性诊断。
- **Task45**：结论护栏覆盖三类内容：事实/推断/核查必须引用真实 ref 且数字在白名单内；禁止因果词命中即丢弃；无证据解释强制进入 `hypotheses` 并标注“未经证实”；预算耗尽追加“未执行”核查项；回答文字单独校验，失败时替换为确定性摘要。
- **Task46**：同线程追问可切换区间（“那上个月呢”“上周末呢”“和别的门店比”），窗口与基线同步更新；跨员工/跨门店 thread 返回 403。
- **Task47**：新增 `InvestigationPanel`（提问框、示例、步骤时间线、报告分区、假设板、证据表、续聊）；`api.js` 增加 `investigateDiagnosis`；保留原一键诊断。前端构建与现有 25 项测试通过。
- 已验证的专项测试（上一轮）：`test_diagnosis_investigation_schema.py`、`test_diagnosis_agent_loop.py`、`test_diagnosis_tools.py`、`test_time_range.py`、`test_diagnosis_guardrails.py`、`test_diagnosis_investigation_route.py`、`test_diagnosis_followups.py`、`test_audit.py` 共 **67 passed**。
- 真实 LLM 首轮冒烟（隔离 eval 库，端口 8010）：登录 `eval_admin` 成功；首轮返回 `insufficient_data`，窗口 `2026-10-11..2026-10-17`（`source=question`），实际调用 `resolve_time_range, get_diagnosis, get_daily_series, get_operational_changes, compare_peers`，预算 5/6，无耗尽。
- 真实 LLM 追问冒烟：同一 `thread_id` 续聊“那上个月呢？”成功；窗口切换为 `2026-09-01..2026-09-30`，基线同步为相邻等长区间；调用 `resolve_time_range, get_diagnosis, get_daily_series, compare_peers`，预算 4/6；无 500。eval 公开数据集仅覆盖 2023 年，因此这两个 2026 窗口均无订单；护栏按预期丢弃了模型伪造的 `f0.e1` 等引用与区间外数字，并回退到确定性摘要——这正是无数据时的受控行为。
- **Task48 黄金场景**：新增 `backend/tests/test_diagnosis_golden_scenarios.py`，4 项全部通过（6.96s）：单品断崖（`get_diagnosis → get_dish_daily`，引用 `fN.eM` 与 `dish1.<id>.e3`）、取消率上升（`get_diagnosis → get_operational_changes`，引用取消归因拆分）、本店下降/同业平稳（`get_diagnosis → compare_peers`，独立 2025-06 窗口，先直接断言目标店下降且两个同业持平，再断言 `peers1.s1` 被采纳）、数据不足（规则报告、丢弃伪造引用、0 假设）。
- 真实 LLM 有数据冒烟（可复跑：`scripts/smoke_diagnosis_investigation.py`，eval 库 2023 数据，端口 8010）：首轮窗口 `2023-05-01..2023-05-31`（基线 `2023-03-31..2023-04-30`），调用 `resolve_time_range, get_diagnosis, get_daily_series, get_operational_changes`，预算 4/6，`status=ok`、`source=mixed`、丢弃 3 条无法验证结论；模型正确指出“本期并未下降而是上升”，引用 `f0.*`/`daily1.*`。追问“那这个月呢？”在同一 thread 内把窗口切到 `2023-06-01..2023-06-30`（基线 `2023-05-02..05-31`），4/6 次工具调用，无 500；模型本轮出现 `f0.e0/f0.e1` 伪造引用，护栏丢弃 11 条结论并将回答替换为确定性摘要。

## Task48 全量回归结果（2026-10-17）

- 后端全量：`python -m pytest backend/tests -q` → **668 passed**（约 120s）。首轮曾因同业对比工具的 `MAX_PEER_BRANCHES` 截断导致黄金场景前置断言失败；已改为直接调用 `DiagnosisService.diagnose` 验证目标店/同业，避免依赖工具选取顺序，复跑全绿。
- 前端测试：`npm --prefix frontend test` → **25 passed**。
- 前端构建：`npm --prefix frontend run build` → 成功（1910 modules，`index-*.js` 297.26 kB / gzip 80.20 kB）。
- 唯一告警：`langgraph.prebuilt.create_react_agent` 在 LangGraph 1.x 标记 deprecated（建议迁移到 `langchain.agents.create_agent`）。当前行为、预算与递归上限测试均通过；迁移已完成，见 Task51。


## Task49 实施记录（2026-10-17）

- **根因**：评测库仅覆盖 `2023-01-01..2023-06-30`，而 `resolve_time_range` 始终按今天（2026）解析相对时间；默认窗口与“最近”表达都落在 2026 年，诊断必然空数据。
- **新增覆盖期查询**：`StatsRepo.order_date_range` + `StatsService.get_data_coverage`，沿用 `resolve_branch_scope` 权限规则，返回门店首/末订单的 UTC 日期与 `has_data`。
- **相对时间锚定**：`time_range.coverage_anchor` 仅在最近数据早于默认 7 天窗口时锚定到数据截止日；`annotate_coverage` 标注 `source=coverage`，并在 `notes` 中写明数据覆盖范围和实际解析依据。
- **统一解析入口**：`resolve_investigation_window` 供 Agent 工具与确定性降级共用；请求显式日期、问题内显式日期保持原样；模型不可用时同样按覆盖期选择窗口。
- **提示与白名单**：提问上下文附带“今天 + 数据覆盖范围”，系统提示要求回答说明实际区间；`today` 与 coverage 纳入数字白名单，避免模型提到真实日期被护栏误杀。
- **窗口语义修正**：锚定后的历史窗口不再标记 `partial`，并移除“尚未过完的今天”提示；窗口超出覆盖期时明确说明超出部分无数据。
- **新增测试**：`test_time_range.py` 4 项（锚定阈值、覆盖注释、显式窗口不重标、显式日期识别）+ `test_diagnosis_tools.py` 1 项（2023 覆盖下默认/上个月/显式 2026）。
- **回归**：后端 `pytest backend/tests -q` → **673 passed**；前端 `npm test` → **25 passed**；`npm run build` 成功。
- **真实 eval 库冒烟（8000 端口）**：
  - “最近生意为什么下降？” → 窗口 `2023-06-24..2023-06-30`（`source=coverage`），工具实际返回 2023 数据，`status=ok`；
  - 同一 thread 追问“那上个月呢？” → 窗口 `2023-05-01..2023-05-31`（基线 `2023-03-31..2023-04-30`），`status=ok`；
  - `notes` 明确“数据库可用数据覆盖 2023-01-01 至 2023-06-30”。
- **边界**：显式传入 2026 日期仍按用户指定窗口执行，并提示不在数据覆盖范围，不静默改写。

## Task50 实施记录（2026-10-17）

- **删除的旧聊天图文件**：`backend/app/agent/nodes/`（6 个节点）、`graph.py`、`state.py`、`flows.py`、`labels.py`。这张 LangGraph `StateGraph` 从未被任何路由或 `main.py` 引用，只被自身与测试引用。
- **删除的孤立工具**：`agent/tools/faq_tools.py`、`order_tools.py`、`menu_tools.py`——删除节点后应用内已无调用方。
- **保留的在线 Agent 能力**：`diagnosis*.py`（调查 Agent）、`kds.py`、`approvals.py`、`offers.py`、`checkpointer.py`、`llm.py`、`llm_providers.py`、`time_range.py`，以及仍被路由使用的 `gift_tools.py`、`kds_tools.py`、`table_tools.py`。
- **测试调整**：
  - 删除仅覆盖已删除节点/适配器的 `test_agent_flow_drafts.py`、`test_agent_intent_router.py`、`test_agent_reservation_node.py`、`test_agent_branch_menu_faq.py`、`test_agent_order_tools.py`；
  - `test_stock_and_orders.py`、`test_duplicate_requests.py` 改为直接调用 `OrderService` / `MenuService`，继续覆盖库存、并发、重复请求与回滚；
  - `test_customer_activity.py`、`test_cleanup_residual.py` 移除 status/order 节点断言，保留路由与 Service 断言；
  - `test_architecture_boundaries.py`、`test_llm_provider.py` 改为扫描仍存在的 Agent 模块。
- **文档**：README / ARCHITECTURE 不再宣称存在客户聊天入口；`sdd/Spec.md` 与诊断审计文档补充旧图已删除说明。
- **回归**：
  - 定向测试 61 passed；
  - 后端全量 **597 passed**（较 Task49 的 673 少 76 项，均为随旧节点/适配器一并删除的测试）；
  - 前端 25 passed，`npm run build` 成功；
  - `compileall` 与 `import backend.app.main` 通过；8000 端口 reload 后接口返回 401（未带鉴权），服务正常。
- **边界**：未改任何在线业务逻辑；删除的代码在删除前已确认无路由引用。未提交代码。

## Task51 实施记录（2026-10-17）

- **背景**：`langgraph.prebuilt.create_react_agent` 在 LangGraph 1.x 已被标记弃用，并已迁移到 `langchain.agents.create_agent`。
- **依赖**：`backend/requirements.txt` 新增 `langchain>=1.0.0`；当前虚拟环境安装 `langchain 1.4.6`、`langgraph 1.2.14`。
- **改造**：`backend/app/agent/diagnosis_agent.py` 的导入改为 `from langchain.agents import create_agent`；`build_diagnosis_agent` 使用 `system_prompt=`（替代 `prompt=`），继续传入 `model`、带预算的工具、`checkpointer` 与 `name`。
- **语义保持**：`AsyncPostgresSaver` 线程记忆、`recursion_limit=24`、6 次工具预算、截断检测与确定性降级均未改变。
- **文档**：README / ARCHITECTURE 中的 Agent 实现名称更新为 LangChain `create_agent`。
- **回归**：诊断 Agent 循环、追问路由与调查接口测试 **14 passed**；全量回归与前端测试结果见下。
- **边界**：未改变 Agent 行为、工具集合或接口契约。

## Task52 实施记录（2026-10-17）

- **新增 `backend/app/agent/harness.py`**：
  - `HarnessConfig`：工具调用上限、递归上限（与现有 `diagnosis_agent.DEFAULT_*` 一致），以及为后续任务预留的字符 / token / 超时预算，默认 `None`（不启用）。
  - `RunTrace` / `ToolCallRecord`：记录工具调用名称、参数、状态（ok / error / budget_exhausted）、耗时、输出字符数、token 估算；汇总护栏丢弃数、证据数、回答长度、降级原因与预算快照。
  - `EvidenceRegistry` 协议 + `InMemoryEvidenceRegistry`：为 Task55 的 thread 级注册表提供契约，重复 ref 会被拒绝。
  - `estimate_tokens`：确定性估算（CJK ≈ 1 token/字符，其余 ≈ 4 字符/token）。
- **观测接入（不改变行为）**：`guard_tools` 增加可选 `trace`；`DiagnosisToolContext.trace`；`run_investigation` 创建/结束 trace 并记录预算与降级原因；`_compose_answer_and_report` 记录护栏丢弃与违规；`_fallback` 记录确定性诊断调用耗时及 `llm_unavailable` / `llm_call_failed`。HTTP 响应字段未变。
- **基线脚本**：`scripts/diagnosis_harness_baseline.py` 固定 3 个离线场景，不依赖数据库或真实 LLM，可重复生成报告。本次运行：
  - `multi_step`：2 次工具调用（get_diagnosis / get_daily_series），450 输出字符，约 123 tokens，0 条护栏丢弃，`source=mixed`；
  - `budget_exhausted`：1 次成功 + 2 次预算拒绝，降级原因 `recursion_limit` / `tool_call_limit`；
  - `guardrail_rejected`：丢弃 1 条结论，回答因“证据之外数字 88”被替换，`source=rule`。
- **测试**：新增 `backend/tests/test_harness.py` **8 passed**；诊断专项（harness / agent loop / tools / guardrails / route / followups / golden）**44 passed**；后端全量 **605 passed**（162s）。
- **边界**：trace 仅写入内部日志与基线脚本，不进入 API 响应；未提交代码。

## Task53 实施记录（2026-10-17）

- **运行时收敛到 `backend/app/agent/harness.py`**：
  - 契约（Task52）保持不变；新增运行时区段：`ToolCallBudget`、`guard_tools`、`build_diagnosis_chat_model`、`build_diagnosis_agent`、`message_text`、`AgentTurn`、`run_agent_turn` 全部从 `diagnosis_agent.py` 迁入。
  - 新增 `HarnessTurn` 与唯一入口 `run_harness_turn`：负责模型构建（失败返回 `model_available=False`）、共享工具预算、工具守卫注册、checkpointer 透传、单工具/整轮超时及带 `recursion_limit` 的 agent 运行。
  - `guard_tools` 新增可选 `timeout_seconds`（默认 `None`，行为不变）；超时记录为 `error` 工具调用并追加 `tool_timeout` 降级原因。
  - `build_diagnosis_agent` 新增可选 `tool_timeout_seconds`，其余参数与调用契约不变。
- **`run_investigation` 只保留编排**：鉴权、线程/门店校验、数据覆盖读取、问题构造、工具上下文、窗口解析与响应组装；模型/预算/agent/checkpointer/超时全部交给 `run_harness_turn`。模型不可用与调用异常仍走原确定性降级；HTTP 响应字段未变。
- **兼容层**：`agent/diagnosis_agent.py` 改为从 `harness` 重导出同名符号，旧测试与脚本的导入路径继续可用。
- **基线脚本**：`scripts/diagnosis_harness_baseline.py` 改用 `run_harness_turn`，输出与 Task52 完全一致（multi_step：2 次调用 / 450 字符 / 123 tokens；budget_exhausted：1 次成功 + 2 次拒绝，`recursion_limit` / `tool_call_limit`；guardrail_rejected：丢弃 1 条，回答因“证据之外的数字 88.0”被替换）。
- **测试**：`test_harness.py` 增加 3 项（工具超时、模型不可用早退、harness 持有 agent 构建与预算）；诊断专项（harness / agent loop / tools / guardrails / route / followups / golden）**47 passed**；后端全量 **608 passed**（160s）。
- **边界**：默认 `HarnessConfig` 不启用任何新超时/预算，响应契约与 Agent 行为不变；实现过程中 agent 构建异常由原先可能冒泡为 500 改为进入既有的“降级不 500”路径（与 `run_investigation` 文档契约一致）。未提交代码。

## Task54 实施记录（2026-10-17）

- **上下文 middleware（`harness.py`）**：
  - `compact_context()` 以 `HumanMessage` 切分轮次：最后一个问题起为当前轮，工具结果原样保留；更早轮次改写为 `[历史调查备忘]`，包含工具名/参数/状态、evidence ref 及其关联数字、最终回答摘要。
  - `build_context_middleware()` 使用 LangChain `@wrap_model_call`，只执行 `request.override(messages=...)`，**不写入 checkpoint**（完整历史无损）。
  - 预算降级顺序：先丢弃最旧的历史轮次并标注省略数量；仍超限时压缩当前轮 `ToolMessage`（保留 ref/数字，丢弃长列表，追加 `…[context-compacted]`）；仍超限记录 `context_budget_exceeded`，绝不删除当前问题。
  - 统计写入 `RunTrace.context`（before/after tokens 与 chars、轮次/丢弃/压缩计数、reasons），预算降级原因同时写入 `degrade_reasons`。
  - `build_diagnosis_agent` 默认挂载该 middleware，`run_harness_turn` 透传预算。
- **配置**：`OLLAMA_NUM_CTX=8192`、`DIAGNOSIS_CONTEXT_TOKEN_BUDGET=6000`、`DIAGNOSIS_CONTEXT_CHAR_BUDGET=24000`；`build_diagnosis_chat_model` 为 `ChatOllama` 显式传 `num_ctx`。
- **`run_investigation`**：仅把上述预算传入 `HarnessConfig`；HTTP 响应字段、证据列表与护栏逻辑不变（护栏仍基于完整工具结果）。
- **测试**：`test_harness.py` 新增 4 项（两轮记忆与 checkpoint 无损、token 预算丢弃最旧轮次、字符预算压缩当前工具结果且保留 ref/数字、Ollama `num_ctx`）；harness **15 passed**，诊断与 LLM provider 专项 **51 passed**；后端全量 **612 passed**（163s）。
- **基线**：`scripts/diagnosis_harness_baseline.py` 三个场景结果与 Task52/53 一致，trace 新增 `context` 统计（多步场景 `before_tokens=198`、无压缩）。
- **边界**：默认 `HarnessConfig` 的预算仍为 `None`；只有 `run_investigation` 显式传入 settings 预算。未提交代码。

## Task55 实施记录（2026-10-17）

- **稳定 ref 格式**：本地 ref 不变（工具仍发 `f0.e0` / `daily1.e1` / `dish1.5.d3`），线程级 ref 在轮次前加前缀（`t1.f0.e0`、`t2.daily1.e1`）。`qualify_evidence_ref` / `split_evidence_ref` / `qualify_refs_in_text` 负责转换；`REF_TOKEN_PATTERN` 把 `tN.ref` 当作一个完整 token，避免前缀数字被误判为回答里的业务数字。
- **Thread 级注册表（`harness.py`）**：
  - `ThreadEvidenceRegistry` 以限定 ref 为键保存 `local_ref`/`turn`/`label`/`value`/`detail`，并提供 `refs()`（仅限定 ref）与 `accepted_refs()`（限定 ref + 安全旧别名）。
  - 旧别名规则：当前轮存在的本地名映射到当前轮；只被一个历史轮次产生的本地名仍可解析；跨多轮复用的本地名在非当前轮时拒绝，避免把历史事实错配到当前轮。
  - 伪造 ref（`f9.e9`、`t1.f9.e9`、`t9.f0.e0`）不在注册表中，护栏仍会丢弃。
- **checkpoint 持久化（第一阶段不新增表）**：
  - `DiagnosisAgentState(AgentState)` 增加 `thread_evidence` 通道；`after_agent` middleware 在每轮结束时从 state messages 重建注册表并写入 checkpoint。
  - `run_agent_turn` 返回 `AgentTurn.thread_evidence` 与 `turn_index`（按 `HumanMessage` 计数）；`load_thread_evidence` 供不跑图的确定性降级路径读取，任何 checkpointer 异常都退化为空注册表。
  - 旧 checkpoint 兼容：缺少 `thread_evidence` 通道时，从已 checkpoint 的 `ToolMessage` JSON 重建（`collect_thread_evidence`），下一轮写入新通道；无需迁移步骤。
- **护栏与响应**：
  - 护栏改用 `registry.accepted_refs()` 校验 ref；数字白名单并入历史轮次的 evidence 数字/文本，跨轮引用旧数字不会被误判。
  - 响应 `evidence` 在保留原 `ref` 的同时新增 `thread_ref`（`t2.f...`），仅扩展 `ThreadDiagnosisEvidence`；普通 `GET /staff/diagnosis` 契约不变。
  - 历史备忘中的工具 ref、结构化引用与回答正文都会加上 `tN.` 前缀，模型在后续轮次可直接引用。
- **测试**：`test_harness.py` 新增 4 项（注册表限定/别名/伪造 ref、两轮持久化与备忘限定、旧 checkpoint 兼容、从 messages 重建）；`test_diagnosis_guardrails.py` 新增 5 项（跨轮引用、唯一旧别名、伪造跨轮 ref 被丢弃、回答伪造 ref 被替换、旧轮数字可引用）；`test_diagnosis_followups.py` 断言响应 evidence 的 `thread_ref == t2.<ref>`。harness **19 passed**；诊断与 LLM provider 专项 **75 passed**；后端全量 **621 passed**（162s）。
- **基线**：`scripts/diagnosis_harness_baseline.py` 三场景结果不变；trace 新增 `thread_evidence` 统计（基线脚本直接调用组合函数时为 `null`，不写 checkpoint）。
- **边界**：没有新增数据库表；HTTP 既有字段（含 `ref`）不变，仅新增可选 `thread_ref`；伪造 ref 的拒绝行为保持。未提交代码。

## Task56 实施记录（2026-10-17）

- **窗口一致性校验（`diagnosis_tools.py`）**：
  - 新增 `_check_window()`（在 `context.lock` 内读取/采纳窗口，避免同一条模型消息里的并行工具各自定下不同窗口）。所有带日期的工具（`get_diagnosis`、`get_daily_series`、`get_dish_daily`、`compare_peers`、`get_operational_changes`）在解析日期后、调用 Service 前校验。
  - 已解析窗口不一致时**不执行查询**，返回结构化可修正错误：`reason="window_mismatch"`、`requested_window`、`resolved_window`，并提示用已解析窗口重试。
  - `resolved_window` 尚未建立时（模型跳过了 `resolve_time_range`），第一个日期工具以自身窗口建立窗口（`source="agent"`，沿用相邻等长基线规则并附带数据覆盖说明），后续工具必须与之一致；响应因此始终只报告一个窗口。
  - 显式请求日期在 `run_investigation` 中预置为 `resolved_window`，即使模型从不调用 `resolve_time_range`，带日期的工具也不能改用别的区间。
  - `resolve_time_range` 若在本轮已有工具窗口后重新解析出不同区间，会在 notes 中说明“此前的工具窗口为 X，现已按本次解析更新”，最终响应采用最新解析结果。
- **steps 记录实际窗口**：每个工具步骤新增 `window`（`{start_date, end_date}`）；窗口不一致的错误步骤同时记录 `expected_window`（已解析窗口）。确定性降级路径的 `get_diagnosis` 步骤也带上窗口。`InvestigationStep` 新增可选 `window` / `expected_window`（`InvestigationStepWindow`），既有字段与 `ref` 契约不变。
- **提示词**：系统提示第 1 条明确“所有带日期的工具必须使用 `resolve_time_range` 返回的区间；收到 `window_mismatch` 时按 `resolved_window` 重试”。
- **测试**：`test_diagnosis_tools.py` 新增 4 项（混用窗口被拒绝且 Service 未调用、无 resolver 时首个工具确定窗口、显式请求日期锁窗、覆盖期锚定窗口）；`test_diagnosis_followups.py` 新增 1 项端到端测试（解析“上周”→错误窗口被拒→按 expected_window 重试成功）；schema 测试覆盖新增字段。工具专项 **12 passed**；诊断专项 **83 passed**；后端全量 **626 passed**（164s）。
- **基线**：`scripts/diagnosis_harness_baseline.py` 三个场景 trace 数字不变（多步：2 次调用 / 450 字符 / 123 tokens）。
- **边界**：未新增数据库表；既有响应字段不变，仅 `steps` 新增可选窗口字段；窗口不一致从“静默使用”改为可修正错误。未提交代码。

## Task57 实施记录（2026-10-17）

- **结构化报告 schema（`agent/diagnosis_report.py`）**：`AgentInvestigationReport`（`answer` + `facts` / `inferences` / `checks` 的 `AgentClaim` + `hypotheses` 的 `AgentHypothesis`）。`build_diagnosis_agent` 现在向 `create_agent(response_format=AgentInvestigationReport)` 传入 schema；LangChain 自动选择 ToolStrategy，把 schema 暴露为 `AgentInvestigationReport` 工具，并把校验后的对象写入 `structured_response`。
- **输出读取**：`AgentTurn` 新增 `structured`；`run_agent_turn` 优先读取 `structured_response`，序列化后作为 `answer`。结构化回答以报告工具的 ToolMessage 结束，不再被 `_stopped_before_final_answer` 误判为 recursion_limit。未产生结构化输出的脚本模型（既有测试）继续走原 JSON/纯文本路径，行为不变。
- **上下文备忘**：结构化最终回答是 tool call 而非 AIMessage 文本；`_find_final_answer` 识别报告工具调用并提取 answer 与引用 ref，历史轮次备忘不再丢失最终回答；报告工具本身不计入历史备忘的工具步骤。
- **护栏原因**：`validate_claims` / `validate_hypotheses` 新增可选 `problems` 收集器，逐条返回被丢弃原因（缺少 ref、伪造 ref、证据外数字、禁止归因、hypotheses 带 ref 等）；`_compose_answer_and_report` 聚合为 `problems`，并把 answer 违规也纳入。组合结果新增内部字段 `problems` / `answer_violations` / `refs` / `allowed_numbers` / `model_report`（不进入 HTTP 响应）。
- **一次修复**：`_repair_if_needed` 在存在违规时用普通模型调用（非 Agent turn、不消耗工具预算）把“上一版报告 + 违规原因 + 允许的 ref 与数字”发给模型一次；修复后的报告重新走同一套护栏。零残留则采用（trace `repair.succeeded=true`）；调用失败/非 JSON/仍有残留则回退确定性摘要（`answer_repair_failed`）。`RunTrace` 新增 `repair` 字段。
- **提示词**：系统提示第 7 条改为“结束后调用 `AgentInvestigationReport` 提交最终报告”，保留 ref/数字约束。
- **测试**：新增 `test_diagnosis_structured_output.py` 9 项（schema 序列化、结构化响应不被误判为上限、历史备忘保留结构化回答、伪造 ref 修复成功、修复后伪造数字仍被拒并回退、修复调用失败回退、API 字段不变、端点修复成功、端点修复失败回退）；`test_diagnosis_guardrails.py` 的两处泄露断言改为只渲染响应可见字段。诊断与 harness 专项 **140 passed**；后端全量 **635 passed**（165s）。
- **基线**：`scripts/diagnosis_harness_baseline.py` 三场景结果不变，trace 新增 `repair`（直接调用组合函数时为 `null`）。
- **边界**：未新增数据库表；HTTP 响应字段集合不变（仅内部组合结果新增字段）；伪造引用/数字仍被拒绝，修复失败时不会展示未校验内容。Provider 注意：ToolStrategy 会以 `tool_choice="required"` 绑定工具，`deepseek-chat` 等非思考模型支持该参数；思考/推理模型若拒绝该参数会进入确定性降级。未提交代码。

## Task58 实施记录（2026-10-17）

- **`get_dish_daily` 默认汇总**：新增可选参数 `include_daily`（默认 false）。默认每道菜只返回 `current_period` / `baseline_period`、`change_pct`、`shape` 与 `daily_summary`（天数、零销量天数与日期、最高/最低单日销量），以及 `notable_days`：最多 3 个零销量日 + 最高/最低单日，均为原始逐日行，带 `.d{原序列下标}` evidence_ref 与原因（“当天销量为 0”“窗口内最高/最低单日销量”）。不再返回 `daily`；notes 提示需要明细时传 `include_daily=true`。未返回的日期不再生成 evidence ref，证据列表随输出一起缩小。
- **按需完整序列**：`include_daily=true` 返回每道菜的完整 `daily`（每点带 ref，仍受 62 天结构性上限）。多道菜超出预算时，`_fit_dish_daily` 用二分搜索为所有菜取相同的逐日前缀长度，并设置 `daily_truncated`；不会让某一道菜挤占其他菜。
- **统一输出预算与元数据**：每个工具结果（含错误与 `window_mismatch`）统一经 `_bounded_result` 序列化，预算为 `MAX_TOOL_OUTPUT_CHARS=6000` 与 `MAX_TOOL_OUTPUT_TOKENS=3000`（token 使用 harness 的确定性 `estimate_tokens`）。超预算时只从指定顶层列表尾部整条移除（`findings` / `notes` / `daily` / `dishes` / `peers` / `changes`），不截断单条对象、不改写数字或 ref。所有结果新增 `meta`：`chars`、`tokens`（实际序列化后的值）、`budget_chars`、`budget_tokens`、`truncated`、`omitted`（按字段统计省略条数）；结构性条数上限也计入 `omitted`。
- **精确性**：预算与汇总都只在原始工具数据上计算；`daily_summary`、异常日、预算内保留的条目仍是精确值。`context.observe` 仍记录完整工具结果，护栏数字白名单不因展示缩减而丢失。
- **测试**：`test_diagnosis_tools.py` 16 项（新增 3 项：默认汇总与异常日、按需完整序列、输出预算与截断元数据；更新 ref/schema/错误 payload 断言）。62 天单菜品实测：默认汇总 **1404 字符**，`include_daily=true` **5811 字符**（约 4.1 倍差距），完整序列在预算内保留 57 天并报告 `omitted.daily=5`。诊断、harness、dish service 与架构边界专项 **98 passed**；后端全量 **639 passed**（166s）。
- **基线**：`scripts/diagnosis_harness_baseline.py` 三场景不变（多步 2 次调用 / 450 字符 / 123 tokens）。
- **边界**：未新增数据库表；HTTP 响应字段与工具名不变，仅工具 JSON 新增 `include_daily` 参数与 `meta` 字段；未提交代码。

## Task59 实施记录（2026-10-17）

- **整轮超时**：`run_harness_turn` 用 `asyncio.timeout` 包住一次 `run_agent_turn`（模型调用、工具循环与重试都计入），新增配置 `DIAGNOSIS_TURN_TIMEOUT_SECONDS=90.0`。超时后记录 `turn_timeout` 降级，`run_investigation` 捕获后保留上一轮证据并回退确定性诊断：`status="llm_unavailable"`、`reason_code="turn_timeout"`，notes 明确说明“超过 N 秒，未进行多步调查”。`HarnessConfig` 的默认值仍为 `None`，未显式配置的调用方（基线脚本、旧测试）行为不变。
- **单工具超时**：`guard_tools` 对每次工具调用单独使用 `asyncio.timeout`，新增配置 `DIAGNOSIS_TOOL_TIMEOUT_SECONDS=10.0`。超时不再向上抛出中断整轮，而是返回 `{"status":"timeout","tool":...,"message":"工具执行超时，已跳过…"}` 观察结果，trace 记 `status="timeout"` 与 `tool_timeout` 降级；模型可基于已有结果继续回答（提示中要求说明“尚未检查”的内容）。模型可见文本不写具体秒数，避免护栏数字白名单无法解释该数字。
- **失败降级**：工具异常同样转成 `{"status":"error","tool":...,"message":"Type: ..."}` 观察结果，trace 记 `error` 与 `tool_failed`，不再让单个坏探针变成整轮失败。`RunTrace.summary()` 新增 `tool_timeouts`；`tool_failures` 仍只统计 `error`。
- **免费导航工具**：`FREE_TOOL_NAMES={"resolve_time_range"}`。`ToolCallBudget.consume` 仍把它写入 `calls`（“已检查工具”判断和缺失工具提示使用），但不增加 `used`；预算耗尽后仍可调用。响应中的 `tool_calls_used` 因此只统计数据探针：先 resolve 再 get_diagnosis 记 1（followup 测试已按此更新）。
- **测试（新增 4 项）**：慢模型在 `run_harness_turn` 与路由层均触发 `turn_timeout` 并回退确定性答案；慢工具在轮内降级为 timeout 观察，模型收到该观察后仍给出最终回答；`resolve_time_range` 免费且预算耗尽后仍可用；工具失败改为观察结果。专项 **73 passed**；后端全量 **643 passed**（165.63s）。
- **基线**：`scripts/diagnosis_harness_baseline.py` 三场景数值不变（多步 2 次调用 / 450 字符 / 123 tokens；预算耗尽 used=1、refusals=2）；trace 新增 `tool_timeouts` 且均为 0。
- **边界**：未新增数据库表；HTTP 响应字段与状态枚举不变；`tool_calls_used` 语义按任务要求改为“数据探针次数”，`resolve_time_range` 不再计入；超时/失败仅记录在 trace 日志与工具观察中，不新增响应字段；未提交代码。

## Task60 实施记录（2026-10-17）

- **固定数据集**：新增 `backend/app/agent/diagnosis_eval_dataset.json`（`version=1`，报告记录文件 SHA-256）。9 个场景共 10 轮：单品下钻、同业对比、运营变更、同线程窗口切换、编造数字、伪造引用、预算耗尽、工具超时/失败降级、错误窗口重试。每轮固定问题、脚本化工具轨迹（含参数）、结构化最终报告与 `expect` 断言；数据集加载时校验场景 id、步骤结构、`expect.tools` 与脚本步骤一致，以及窗口基线相邻等长。所有产品工具名都被至少一个场景覆盖。
- **脚本化模型与 fixture 工具**：新增 `backend/app/agent/diagnosis_evaluation.py`。`ScriptedTurnModel` 按队列逐次返回 `AIMessage`（工具调用或 `AgentInvestigationReport` 结构化调用），记录模型调用次数与输入 token 估算；`build_fixture_tools` 用生产工具名/schema 包装固定 payload，并复刻 `window_mismatch` 拒绝、evidence ref 生成、`record_step`/`observe`/`add_evidence` 与可注入的 timeout/raise 故障。每轮都走真实 `run_harness_turn`（预算、超时、上下文中件、checkpoint、thread evidence）和生产 `_compose_answer_and_report` 护栏，因此指标与员工实际看到的报告一致。
- **指标**：逐轮输出工具轨迹与工具选择、日期工具窗口匹配率与不一致次数、引用总数/有效/缺失/有效率、数字总数/幻觉列表/幻觉率、护栏丢弃与回答校验、降级原因、工具失败/超时/预算拒绝、预算使用/耗尽、耗时、模型调用、提示/工具/回答/上下文 token、线程证据数。汇总输出通过率、工具选择准确率、窗口准确率、引用有效率、数字幻觉率、降级率、护栏介入率、平均/总耗时、token 总量与均值、模型调用数。
- **CLI 与落盘**：新增 `scripts/diagnosis_eval.py`，默认写入 `sdd/diagnosis_eval_report.json`，任一场景失败时退出码非 0；支持 `--dataset`、`--output`、`--print-json`，`--live` 可额外调用既有 `scripts/smoke_diagnosis_investigation.py` 做真实模型冒烟。离线运行无数据库、无 provider、无凭证，便于 CI。
- **实测（本机）**：9/9 场景、10/10 轮通过；工具选择准确率 **100%**，窗口准确率 **94.74%**（错误窗口重试场景 1/2），引用有效率 **90%**（`invalid_ref` 场景 0%），数字幻觉率 **5.26%**（`invented_number` 场景），降级率 **40%**，护栏介入率 **20%**；总耗时 **611.6 ms**（平均 61.2 ms/轮），token 合计 **46709**（模型输入 41722 / 工具输出 4672 / 回答 315，平均 4670.9/轮），模型调用 39 次。报告已落盘到 `sdd/diagnosis_eval_report.json`。
- **测试**：新增 `backend/tests/test_diagnosis_eval.py` 7 项（数据集一致性、全部场景通过、干净场景引用/数字零缺陷、护栏场景被计量、伪造数字/引用评分、错误期望显式失败、报告落盘与哈希）。诊断/harness/架构专项 **69 passed**；后端全量 **650 passed**（168.17s）；Task52 基线脚本三场景数值不变。
- **边界**：仅新增评估模块、固定数据集、CLI、测试与指标报告，不修改诊断 Agent、工具、护栏或 HTTP 契约；未新增数据库表；数据集与离线运行不访问数据库或 provider；live smoke 保持可选；未提交代码。

## Task61 实施记录（2026-10-17）

- **Service（`backend/app/service/business_profile.py`）**：新增 `BusinessProfileService.get_business_profile()`，一次调用回答四个维度。窗口与既有统计一致：UTC 闭区间日期、默认最近 7 天、最长 366 天；通过 `resolve_branch_scope` 做强门店校验（跨店 `ForbiddenError`、无门店 `ValidationError`、门店不存在 `NotFoundError`），分支信息经 `BranchRepo.get_branch` 读取，避免嵌套 Service 提交。
- **口径（写入 payload notes，不静默假设）**：
  - 预订健康度：`booked_count`、按状态分组的 `by_status`（含人数）、`cancelled_count`/`cancellation_rate_pct`、`arrived_count`（seated+completed）、`pending_count`/`confirmed_count`、`due_count`（预订时间已过且非取消）、`no_show_count`/`no_show_party_size`/`no_show_rate_pct`；数据没有独立“未到店”状态，故 `no_show_basis="suspected"` 且分母排除已取消与未来预订。
  - 时段/星期分布：按 `BUSINESS_TIMEZONE`（默认 `Asia/Shanghai`）划分，返回补零的 7 行星期分布与 24 行时段分布，附 `calendar_days`、`avg_orders_per_calendar_day`、`share_pct`；窗口本身仍为 UTC 自然日，首尾可能各含半个本地日（notes 说明）。
  - 订单类型结构：`dine_in`/`takeaway`/`delivery` 的成交单量、销售额、取消数与占比。
  - 基线：`last_week_same_weekday`（整体前移 7 天）与 `last_year_same_period`（前移 1 年，2 月 29 日回退 2 月 28 日），给出单量/销售额变化比例；基线为零时 `status="zero_baseline"`、比例为 `null`，不伪造 0%。
- **Repo**：`StatsRepo.order_type_totals`（按 `Order.order_type` 聚合成交/取消）与 `StatsRepo.weekday_hour_totals`（`timezone(name, created_at)` 后用 `isodow`/`hour` 分组）；`ReservationRepo.reservation_status_totals`（按状态与“预订时间是否已过”分组，含人数合计）。全部只读聚合，Agent 不接触 SQL。
- **工具（`get_business_profile`）**：仅接受 `start_date`/`end_date`，不接收 `branch_id`；经 `_check_window` 与已解析窗口校验（不一致时不查询并返回可修正错误），调用 Service 后生成 `profileN.t1/w1/h1/r1/b1/b2` 六个 ref。工具结果除 `refs` 映射外，还携带同六个 ref 的紧凑 `evidence` 记录，使线程证据注册表能从 ToolMessage 重建（与 `get_daily_series` 的逐日 ref 做法一致），后续轮次可引用上一轮的 `profileN.*`。工具数仍为 7 个，预算内一个工具覆盖全部新维度。
- **其他接入**：`DATE_TOOLS` 与生产/评估工具清单包含新工具；系统提示列出该能力；前端步骤时间线新增“查看预订与时段结构”标签；`requirements.txt` 显式声明 `tzdata>=2024.1`（Windows 上 `ZoneInfo("Asia/Shanghai")` 依赖）。
- **测试**：新增 `backend/tests/test_business_profile.py` 8 项（类型/星期/时段结构与上海时区换算、仅取消窗口、预订健康度区分取消/到店/疑似未到店、全未来窗口无未到店、精确的周/年基线、闰日回退、门店越权与参数校验）。`test_diagnosis_tools.py` 新增 2 项并断言工具消息携带六个 evidence 记录；`test_diagnosis_golden_scenarios.py` 新增黄金场景（服务真实读写 test DB，断言工具轨迹、预算 1 次、`dropped=0`、六条 `profile1.*` 证据、`no_show_count=1`、`cancelled_count=1` 与基线引用）；离线评估数据集新增 `business_profile` 场景（线程证据 ≥6、引用与小数字段零缺陷），`CLEAN_SCENARIOS` 同步。
- **实测**：离线评估 **10/10 场景、11/11 轮通过**；工具选择准确率 **100%**，窗口准确率 **95%**，引用有效率 **92%**，数字幻觉率 **3.23%**，降级率 **36%**，护栏介入率 **18%**；报告已重新落盘到 `sdd/diagnosis_eval_report.json`。专项测试 **135 passed**（业务画像/工具/黄金场景/评估 38 + 诊断/harness/架构 97）；后端全量 **661 passed**（172.66s）；`scripts/diagnosis_harness_baseline.py` 三场景 trace 数值不变。
- **边界**：未新增数据库表；调查 API 既有字段与工具名集合只增不改（步骤时间线仅多一个工具名）；新工具不接收 `branch_id`、强制门店作用域且只读；新能力全部经 Service；未提交代码。
