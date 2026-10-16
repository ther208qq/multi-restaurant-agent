# Agent 嵌入业务流：三个功能的实施方案（待实现）

> 建档：2026-10-14。本文是「去除顾客聊天入口后，把 `agent/` 模块嵌入具体业务端点」的实施方案，
> 供后续按 **2 → 1 → 3** 顺序实现。
>
> **当前状态（2026-10-16）：功能2、功能1、功能3 全部完成（见 §0.1）。**

## 0. 已定决策（记录，不再反复确认）

| 决策点 | 结论 |
|---|---|
| 前端技术栈 | **React**（本项目，非 Vue 3；axios 拦截器改为 fetch 封装内拦截） |
| 企业微信 | **不接**；B 端通知一律走 REST 轮询，只把接口写好 |
| KDS | **模拟层**（订单上加字段 + 工具读写），不接真实 KDS |
| 落地方式 | 业务端点内嵌 Agent（不是恢复顾客聊天） |
| 实现顺序 | **功能2 → 功能1 → 功能3** |

### 0.1 实现状态

| 功能 | 状态 | 备注 |
|---|---|---|
| **功能2 预订逼单** | ✅ 已完成（2026-10-15，commit `ddcd0cf`） | 详见 `sdd/OpenItems.md` §十三 |
| **功能1 KDS 订单请求/挂起** | ✅ 已完成（2026-10-15） | 详见 `sdd/OpenItems.md` §十四 |
| 功能3 HITL 审批 | ✅ 已完成（2026-10-16） | 详见 `sdd/OpenItems.md` §十五；tasks A–E 逐项提交 `daca295`→`300aba4` |

**功能2 落地时被动确认的三点（2026-10-15，用户裁定"暂时这样"）**：

1. **API 可调用 Agent**：`Spec.md` §6 写「API 只调用 Service……不调用 Agent」，与本节 §1「Agent 只在具体接口内被调用」冲突；
   按**较新的本文**处理——`routes/customer_orders.py` 直接 import `agent.offers`。现有边界扫描
   （`tests/test_architecture_boundaries.py`）只禁 `routes → agent.tools`，故该写法通过。
2. **话术不过 LLM**：功能2 的 offer 话术由 `agent/offers.py::compose_offer_message` 本地确定性拼装，
   未调 `llm_client`（该接口在预订弹窗里随每次改字段触发，走 LLM 会有 45s 超时与不可测输出）。
   `llm_client` 的 seam 留给功能1/3 的意图识别。
3. **`GET /reservations/availability` 响应改为对象** `{tables, agent_offer}`（原为裸数组；数组无法带字段）。
   前端与既有用例已同步；员工侧 `/staff/reservations/availability` 未动。

## 1. 总体思路

三个功能都建立在已保留的 `backend/app/agent/`（LangGraph graph/nodes/tools + Postgres
checkpointer + `llm_providers`）之上。Agent 只在**具体接口内**被调用；断网时靠
`rule_based_fallback` 兜底，保证接口仍有合理返回。

## 2. 公共改动（一次性）

- `agent/` 内新增/复用「工具注册 + 子图」，三功能共用 `llm_client` 与离线降级。
- 新增 `gifts` 表作赠品核销凭证：
  `id, branch_id, item_id, source_type(order|reservation), source_id, status(pending|redeemed), created_at`。
  接受逼单或审批通过时落一条并扣库存。

## 3. 功能1：KDS 订单请求/挂起（模拟 KDS）

- **接口**：`POST /api/agent/order/{order_id}/request`（顾客鉴权，body `{message}`）→ `{reply}`。
- **Agent**：意图节点 → `query_kds_status(order_id)` → `suspend_kds_task(order_id, reason)`（可选 `resume`）。
- **数据模型**：`Order` 新增 `kds_status`（`queued|cooking|held|done`）+ `kds_held_reason`。
- **前端**：顾客「我的记录」/订单加「联系后厨」按钮 → Dialog 输入 → POST → 展示回复（如「已通知后厨，热菜推迟下锅」）。

## 4. 功能2：预订逼单（最先做）

- **接口**：`GET /reservations/availability` 满席时不再只返回空，改带 `agent_offer` 字段
  （`{message, alternative_times, gift_item}`）；`POST /reservations/accept-offer` 接受安排。
- **Agent**：满席时 `get_surplus_inventory(branch_id)` + 复用现成 `_suggest_times` 生成话术。
- **默认口径（可调）**：「过剩库存」= 甜点/饮品类目且 `stock_quantity >= 阈值（默认 5）`。
- **前端**：`ReservationModal` 拦截到 `agent_offer` → offer Modal（话术 + 备选时间 + [接受此安排]）→ 接受后带标记重新提交落库 + 核销赠品。

## 5. 功能3：HITL 审批（最后做）

- **接口**：`POST /orders` 对 `special_notes` 走意图识别；判定「索要赠品」→ 订单落
  `pending_approval`（新增状态）+ 建审批记录，返回 202 + `approval_id`。
  B 端：`GET /staff/approvals`、`POST /staff/approvals/{id}/approve`、`/reject`。
- **数据模型**：`order_approvals` 表（`id, order_id, remark, intent, status, created_at`）；订单新增状态 `pending_approval`。
- **挂起/唤醒**：用 **DB 状态 + 恢复接口**（比 LangGraph interrupt 简单、跨请求不持活图）；
  审批通过时追加赠品行、落 `gifts` 核销、订单转 `placed`。
- **前端**：C 端结算页提交后轮询 `GET /orders/{id}`；B 端 `AdminLayout` 加「审批」页（轮询 `GET /staff/approvals`，卡片 + [同意]/[拒绝]）。

## 6. 接口汇总

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/agent/order/{id}/request` | 顾客给后厨留言/改单 |
| GET | `/reservations/availability` | 满席时附带 `agent_offer` |
| POST | `/reservations/accept-offer` | 接受逼单安排（备选时间 + 赠品） |
| POST | `/orders` | 备注意图识别，赠品转审批 |
| GET | `/staff/approvals` | 店长拉取待审批 |
| POST | `/staff/approvals/{id}/approve` · `/reject` | 审批闭环 |

## 7. 数据模型变更汇总

- `Order`：+`kds_status`、`kds_held_reason`、新状态 `pending_approval`。
- 新表：`gifts`、`order_approvals`。

## 8. 验收要点（每功能一致）

接口 200/401/403/400/404 正确；离线降级可用；`gifts`/审批/KDS 状态流转正确；前端对应弹窗闭环；
全量 `pytest` + `npm run build` 通过。
