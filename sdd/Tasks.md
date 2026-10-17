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

> 2026-10-17 更正：Task31 删除经营诊断是对需求的误判。已从 git 提交和上一会话的完整文件/编辑记录中恢复经营异常诊断、同业对比、证据引用及其全部测试；Task20-30 恢复为已完成，Task32-37 的无功能损失清理保留。

## Task38 回归结果（2026-10-17，恢复诊断能力后重跑）

- 编译与导入：`python -m compileall -q backend/app backend/tests` 通过；`python -c "import backend.app.main"` 输出 `IMPORT_OK`。
- 诊断专项测试：`pytest backend/tests/test_diagnostics.py test_diagnosis_helpers.py test_diagnosis_acceptance.py test_branch_peers.py test_dish_daily.py test_stats.py -q` → **108 passed**。
- 后端全量测试：`python -m pytest backend/tests -q` → **613 passed**（约 108s），无 PostgreSQL / Ollama 相关跳过。
- 前端测试：`npm --prefix frontend test` → **25 passed**（diagnosisQuery 防抖/缓存/请求序号 18 项，evidenceRefs 证据引用可读化 7 项）。
- Task36 契约变更保留：管理员访问门店列表接口时需显式传 `branch_id`；`test_branch_menu_faq_routes.py` 中 5 处调用已按新契约更新。
