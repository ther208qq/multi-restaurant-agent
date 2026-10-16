# 门店经营异常诊断 Agent · 审计与 Tasks（2026-10-16）

> 本次只做**只读审计 + Tasks 编写**，不修改任何代码。确认后再按 Task 顺序逐个实现。
> 所有结论以仓库实际代码为准，逐条给出文件路径 + 符号名 + 行号；不假设功能一定存在。

---

## 第一步：只读审计

### A. 现有统计能力盘点

**`StatsService.get_branch_summary`** —— `backend/app/service/stats.py:70-181`（唯一的对外统计入口）

| 返回字段 | 行号 | 口径 |
|---|---|---|
| `order_count` | :148 | 排除 `cancelled` 的单量 |
| `cancelled_count` | :156 | 单独汇报 |
| `revenue` | :149 | `sum(total_amount)`，排除 `cancelled` |
| `average_order_value` | :158-160 | `revenue / order_count`，`order_count=0` 时为 `0.0` |
| `units_sold` | :124 | 全部菜品份数（**不受** `top_limit` 影响） |
| `top_items[]` | :162-171 | 每项 `menu_item_id` / `name` / `quantity` / `revenue` / `order_count` |
| `daily[]` | :172-180 | 逐 UTC 日的 `order_count` / `revenue` / `cancelled_count` |

关键口径常量：`SOLD_ORDER_STATUSES` :39（非 `cancelled` 全部）、`CANCELLED_ORDER_STATUSES` :40、`DEFAULT_RANGE_DAYS = 7` :42、`MAX_RANGE_DAYS = 366` :43、`DEFAULT_TOP_ITEMS = 10` :44、`MAX_TOP_ITEMS = 50` :45。
「一天 = 一个 UTC 日」：`_day_start` :64-66、区间左闭右开且 `end_date` 含当天 :108-109。
门店权限收敛（branch-bound 员工只能统计本店，越权 403）：:89-97。

**`StatsRepo`** —— `backend/app/repo/stats.py`
`order_totals` :28-60、`daily_totals` :62-106（`_UTC_DAY` :23）、`units_sold` :108-130、`dish_totals` :132-181（按份数降序 + `limit`）。
全部按 `branch_id` + `created_at ∈ [start, end)` 过滤，**没有任何按小时/按时段/按 `order_type` 的分组**。

**API / Schema / 前端 / 测试**
`GET /staff/stats` —— `backend/app/routes/staff_stats.py:27-48`；
`BranchStatsResponse` / `StatsDishSales` / `StatsDailyPoint` —— `backend/app/schemas/stats.py:28-46 / :18-25 / :9-15`；
`StatsDashboard.jsx` —— `frontend/src/components/admin/StatsDashboard.jsx`（KPI 卡片 + 条形图 + 每日明细，`api.getStats` 见 `api.js:324-331`）；
`backend/tests/test_stats.py` —— 含现成的**回填历史**手法 `_place(..., created_at=...)` :77-97（`UPDATE orders SET created_at=...`）。

### B. 五项指标的可得性 —— **全部现成，零新增查询**

| 指标 | 现有字段 | 位置 | 结论 |
|---|---|---|---|
| 订单量 | `order_count` | `service/stats.py:148` | ✅ 已有 |
| 销售额 | `revenue` | :149 | ✅ 已有 |
| 客单价 | `average_order_value` | :158-160 | ✅ 已有 |
| 菜品销量 | `top_items[].quantity` / `units_sold` | :162-171 / :124 | ✅ 已有 |
| 订单取消 | `cancelled_count` | :156 | ✅ 已有 |

### C. 历史对比能力

- **可以 —— 按日期区间**：`start_date`/`end_date` 为任意值（:99-106），「本期 vs 上一个等长窗口」**直接可算**（调两次，日期平移）。
- **可以 —— 按门店**：`branch_id` 参数。
- **可以 —— 按日**：`daily[]`（:172-180）给出逐日序列。
- **不可以 —— 按时段（午市/晚市/按小时）**：`_UTC_DAY` 只截到「日」（`repo/stats.py:23`），没有任何按小时聚合。
- **不可以 —— 按渠道（堂食/外卖）**：`Order.order_type` 字段存在（`models/order.py:31`），但 `StatsRepo` 所有聚合都不按它分组。
- **⚠️ 菜品跨窗口对比有边界**：`top_items` 受 `top_limit` 限制（上限 50，:45）且**只列榜内菜品**。某菜若跌出榜单，会被误读为「销量归零」。用 `top_limit=50` 可缓解，但**必须在结论里标明**，不能当成 100% 跌幅。

### D. 数据量现实（**本次审计最重要的发现**）

**`backend/app/seed.py` 不创建任何订单。** 该文件只建门店（:26 / :33）、菜品（:77-183）、餐桌、顾客（:214）——全文没有 `Order(` / `OrderItem(`。

后果：**开发库里的订单只来自手工冒烟**，通常集中在同一两天。

- 「本期 vs 上期」在刚种子的库上，上期几乎必然是**空窗口**（`order_count == 0`）。
- 此时算「环比 −100%」是**假异常**，绝不能报警。
- 所以 MVP **必须**内置「基线不足即拒绝判定」的规则（见 T1），而不是先算出来再补救。

好消息：**测试不受此限**。`test_stats.py:92-96` 证明了可以用 `UPDATE orders SET created_at=...` 回填跨窗口历史，新测试照此构造即可。

### E. 哪些异常可以可靠检测

| 异常 | 可检测 | 前提 |
|---|---|---|
| 订单量骤降/骤升 | ✅ | 上期 `order_count > 0` |
| 销售额骤降/骤升 | ✅ | 上期 `revenue > 0` |
| 客单价异常 | ✅ | 上期 `order_count > 0` |
| 取消率异常 | ✅ | 上期 `order_count + cancelled_count > 0` |
| 菜品销量异常 | ⚠️ 有条件 | **该菜须在上期 `top_items` 内**，否则只报「无法判断」 |
| 时段异常（午市/晚市） | ❌ | 需新增按小时聚合（超出 MVP） |
| 渠道异常（堂食/外卖） | ❌ | 需新增按 `order_type` 聚合（超出 MVP） |

### F. 哪些原因**因缺数据而无法**实现 —— 一律不得编造

| 常见原因 | 为什么本系统说不了 |
|---|---|
| 天气 / 节假日 / 竞对 / 商圈客流 | 库里**没有任何外部数据** |
| 排班 / 人手不足 | 无排班表；`staff_users` 只有 `username` / `role` / `branch_id` |
| 促销 / 折扣效果 | 无促销实体；`orders` 无折扣字段，`total_amount` 是终值（`models/order.py:35`） |
| 评价 / 差评 | 不存在 |
| 售罄导致流失 | `menu_item_stocks` 只有**当前**余量（`models/menu.py:67`），**无历史**，无法回溯「那天是否卖光」 |

**能做的归因仅限于系统内可交叉验证的事实**，例如：

1. 销售额跌而订单量持平 → 客单价问题（证据：两者变化方向背离）。
2. 订单量跌而取消数未涨 → 不是「流失到取消」，而是**进店量**问题（证据：取消率未同步上升）。
3. 某菜销量跌而整店单量持平 → **单品**问题而非门店问题（证据：该菜在 `top_items` 的份额）。
4. 取消数上升 → 可再用**审计日志**区分顾客取消与员工取消：`order.status_changed` 的 `details` 带 `'by': 'customer'`（`service/order.py:598-606`），员工取消走 `service/order.py:529-537`。

第 4 条是项目里**真实存在的第二证据源**（`AuditService.list_logs`，`service/audit.py:111-137`；repo 见 `repo/audit.py:46-67`），MVP 应当用上——这是「收集证据」而非「把数字丢给 LLM」的关键差别。

### G. 可直接复用的现有模块

| 复用对象 | 位置 |
|---|---|
| 统计口径与聚合 | `StatsService.get_branch_summary`（**整段复用，不重写任何 SUM/COUNT**） |
| 门店权限收敛 | `service/stats.py:89-97`（传 `staff=` 即生效，不重复实现） |
| Agent 子流程范式 | `agent/kds.py`（规则优先 + LLM 兜底）、`agent/approvals.py`（LLM 优先 + 规则兜底） |
| LLM 接缝 | `agent/llm.py`：`llm_client.generate(prompt, system_prompt, temperature)` :69-77、`extract_json_from_text` :28-56 |
| 薄工具契约 | `agent/tools/kds_tools.py`（`success`/`error`/`code` + 域异常转译） |
| 路由范式 | `routes/customer_agent.py`（「业务接口顺带咨询 Agent」，**不是**聊天入口） |
| 审计取证 | `AuditService.list_logs`（`service/audit.py:111-137`） |
| 前端范式 | `StatsDashboard.jsx` 的门店/区间选择与 KPI 卡片；`AdminLayout.jsx:83-89` 加页签 |

---

### H. 最小可行版本需要新增什么

**约束下的分层落位**（依据 `backend/tests/test_architecture_boundaries.py:31-36`）：
`service` 禁 import `routes`/`agent`/`fastapi`；`agent` 禁 import `repo`/`routes`，且**不得出现** `select(` / `.execute(` / `.commit(` / `AsyncSessionLocal`（:40，字符串扫描）。

```
GET /staff/diagnosis
  routes/staff_diagnostics.py     ← 薄：解析参数 + 调 Agent 子流程
    agent/diagnosis.py            ← LLM 只做「归因表述」，不做计算、不做状态变更
      service/diagnostics.py      ← 确定性异常检测（纯 Python，无 LLM）
        StatsService.get_branch_summary ×2   ← 本期 + 上期，整段复用
        AuditService.list_logs               ← 取消归因取证
```

**新增 5 个文件**

| 文件 | 内容 |
|---|---|
| `backend/app/schemas/diagnostics.py` | 响应模型 |
| `backend/app/service/diagnostics.py` | 确定性异常检测 + 证据装配 |
| `backend/app/agent/diagnosis.py` | 原因分析与建议组织（LLM + 规则兜底） |
| `backend/app/routes/staff_diagnostics.py` | `GET /staff/diagnosis` |
| `frontend/src/components/admin/DiagnosisPanel.jsx` | 诊断页签 |

**改动 5 个文件**

`backend/app/routes/__init__.py`（导出新 router）、`backend/app/main.py:20/:88-99`（注册）、`frontend/src/api.js`（`getDiagnosis`）、`frontend/src/components/admin/AdminLayout.jsx`（加页签）、`backend/tests/`（新增 `test_diagnostics.py`）。

**明确不改**：`StatsService` / `StatsRepo` / `agent/` 现有模块 / 任何状态机。
**明确不加**：新 repo 查询（**MVP 零 repo 改动**）、按小时或按渠道聚合、聊天入口、多 Agent、向量库、记忆。

---

## 第二步：最小 Tasks

> 按依赖顺序排列，每个 Task 独立可验证；**上一个 Task 测试通过才进入下一个**。

### T1 —— 确定性异常检测（service 层）

- **实现内容**：新增 `backend/app/service/diagnostics.py` 与 `backend/app/schemas/diagnostics.py`。核心入口 `DiagnosisService.diagnose(branch_id, start_date, end_date, *, staff=None, session=None)`：调两次 `StatsService.get_branch_summary`（本期窗口 + 紧邻的上一个等长窗口），对订单量 / 销售额 / 客单价 / 取消率 / 菜品销量计算环比，输出结构化 `{"period": ..., "baseline": ..., "findings": [...]}`，每条 finding 含 `metric` / `current` / `previous` / `change_pct` / `severity` / `evidence`。
  **基线规则（本 Task 的核心）**：本期与上期均为空 → 只返回 `insufficient_data`，`findings` 为空；上期 `order_count == 0` → 单量/销售额/客单价/取消率**一律不判定**；某菜不在上期 `top_items` → **不判定**（记 `item_baseline_missing`），绝不当作 −100%。
- **依赖**：无
- **验收标准**：① 给定构造的两段窗口，稳定输出预期 findings；② 上期空窗时 `findings == []` 且带 `insufficient_data`；③ 门店越权由 `StatsService` 抛 `ForbiddenError`，本层**不重复实现**权限；④ 无异常时 `findings == []` 且**不带** `insufficient_data`。
- **对应测试**：新增 `backend/tests/test_diagnostics.py`，用 `test_stats.py:92-96` 的回填手法造跨窗口订单：正常跌幅→有 finding；上期空窗→不判定；某菜上期不在榜→不判定；越权→`ForbiddenError`；无异常→空 findings。

### T2 —— 证据装配（service 层，复用现有）

- **实现内容**：在 `diagnostics.py` 内为每条 finding 装 `evidence`：指标的本期/上期值、`daily[]` 中该指标的逐日序列（用于区分「单日塌方」与「整期缓降」）；**取消类异常**额外调 `AuditService.list_logs(branch_id=..., action='order.status_changed')`，统计 `details.by == 'customer'` 与员工取消的比数。
- **依赖**：T1
- **验收标准**：① 每条 finding 的 `evidence` 非空；② 取消类异常的 evidence 含顾客/员工取消比数；③ 证据不足时 evidence **显式写明缺什么**（如 `"missing": "该菜品不在上期排行内"`），不用空字段冒充。
- **对应测试**：扩充 `test_diagnostics.py`。

### T3 —— Agent 原因分析与建议（agent 层）

- **实现内容**：新增 `backend/app/agent/diagnosis.py`。`analyze_findings(findings, period_info)` 把 T1/T2 的**结构化**结果交给 `llm_client.generate`，system prompt 强制三段式输出：**已确认事实 / 可能原因 / 待核查事项**，且每条原因必须带 `evidence_ref` 指向输入中的 evidence 下标。LLM 不可达或输出不合法 → 退回**规则式归因**（只输出可由指标关系直接推出的那几条，见审计 §F 的 1/2/3）。**LLM 不做任何计算，也不做任何状态变更。**
- **依赖**：T1、T2
- **验收标准**：① 原因引用的 `evidence_ref` 不存在时**丢弃该原因**；② LLM 不可达时规则式归因仍可用且接口不报错；③ `findings` 为空时**直接返回「本期无异常」，不调 LLM**；④ 输出中**不含任何输入里没有的数字**；⑤ 明确区分「已确认事实」与「可能原因」两个字段。
- **对应测试**：`test_diagnostics.py` 新增，用 `monkeypatch.setattr(llm_client, 'generate', ...)` stub（照 `test_cleanup_residual.py:84-88` 手法）：正常归因；非法 `evidence_ref` 被丢弃；LLM 抛异常 → 规则兜底；空 findings 不调 LLM（断言 stub 未被调用）。

### T4 —— 接口（routes 层）

- **实现内容**：新增 `backend/app/routes/staff_diagnostics.py`，`GET /staff/diagnosis?branch_id=&start_date=&end_date=`，`Depends(get_current_staff)`，返回诊断响应；在 `routes/__init__.py` 导出、`main.py:20/:88-99` 注册。路由保持极薄：只解析参数、调 `agent/diagnosis.py`、返回。
- **依赖**：T3
- **验收标准**：① 无 token → 401；② branch-scoped 员工请求别门店 → 403；③ 正常请求 200 且结构符合 schema；④ 空数据窗口 → 200 且 `insufficient_data`（**不是** 500、**不是**假异常）。
- **对应测试**：新增 route 测试（`async_client` + `_staff_headers`，照 `test_branch_menu_faq_routes.py:23-27`）。

### T5 —— 前端诊断页签

- **实现内容**：`api.js` 加 `getDiagnosis(branchId, startDate, endDate)`；新增 `DiagnosisPanel.jsx`（门店/区间选择复用 `StatsDashboard.jsx:45-70` 写法；每条异常一张卡片：指标与变化 / 严重度 / 证据 / 可能原因 / 建议 / 待核查）；`AdminLayout.jsx` 加「经营诊断」页签。
- **依赖**：T4
- **验收标准**：① 可选门店与区间并加载；② `insufficient_data` 时显示明确的「**数据不足，暂不判定**」提示，不是空白也不是假异常；③ 失败时有错误横幅；④ `npm run build` 通过。
- **对应测试**：无自动化单测（项目前端无测试框架），以 `npm run build` + 手工冒烟为准。

---

## 风险与限制（如实记录）

1. **开发库当前没有任何订单种子**（审计 §D）。首次真机冒烟**预期会看到「数据不足，暂不判定」**——这是**正确行为**，不是 bug。要看到真实诊断，需先用回填或手工下单造出跨窗口数据。
2. **菜品对比受 `top_limit` 边界影响**（审计 §C），MVP 用 `top_limit=50` 并在 evidence 中标注；跌出榜单的菜品报「无法判断」。
3. **归因上限**：天气/节假日/竞对/排班/促销/售罄原因**一律不可说**（审计 §F）。Agent 输出必须停在这条线内，宁可少说。
4. **时段与渠道维度的异常不在 MVP 内**（需新增聚合查询，超出最小范围）。
5. **不做任何自动处置**：不调价、不促销、不改库存、不动订单状态——只输出诊断与建议。

---

## 不做（明确排除）

- 不恢复聊天入口，不重新接入旧 LangGraph 图（`agent/graph.py` 保持现状，不接线）。
- 不新增多 Agent、记忆、向量库或任何新框架。
- 不新增 repo 查询方法（MVP 零 repo 改动）。
- 不做按小时/按时段/按渠道聚合。
- 不做自动处置与任何「预计提升 X%」的承诺。
