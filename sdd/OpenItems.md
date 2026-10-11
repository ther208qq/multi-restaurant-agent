# 待办清单（Open Items）

> 记录时间：2026-10-11。上一轮已修完 4 个 bug（见文末），以下为**未完成/待决策**事项。

## 一、待你决策的改进项（按建议优先级）

1. **给测试独立数据库** —— 推荐优先做，约 30–45 分钟。
   - 现状：`backend/tests/conftest.py` 用 `AsyncSessionLocal`，整套测试跑在**开发库 `restaurant_ai`** 上，与 uvicorn / 浏览器同一个库。
   - 危害（已实证）：
     - `backend/tests/test_reservation_exclusion.py` 会**删除** branch 1 在「明天 19:00 ±2h」「+5 天 20:00」「+6 天 20:00」三个窗口内的真实预约，再以 `customer_id=1` 重建。**跑一次 pytest 就可能毁掉真实验收预约。**
     - `test_stock_and_orders.py` / `test_agent_order_tools.py` 真调 `create_order_atomic` 且部分不清理，往开发库泄漏订单。
     - 泄漏行正是此前 Bug 4「`__route_test_cat` 空分类泄漏到顾客菜单」的根因。
   - 方案：新建 `restaurant_ai_test`，在 `conftest.py` **任何 import 之前**覆盖 `DATABASE_URL`（引擎在导入时按 `.env` 建好，晚改无效），启动时建表 + 跑 `seed_data()`（新库为空，现有测试依赖种子数据）。风险点：需跑全量收敛「数据不存在」类连带失败。
2. **清理本轮测试数据** —— 约 10 分钟。
   - orders #533 / #562，reservations #381 / #405，测试账号 `acceptance_*` / `sticky_*` / `datefix_*` / `menucheck_*`，customers 255 / 256 / 308。
3. **重启前后端做正式验收** —— 启动约 5 分钟，走查另计。
   - 当前前后端均已停止（`:3000` / `:5173` / `:8000` 无监听），按此前要求停的。
   - 注意：启动后端必须带 `--reload`（Windows 下 psycopg 异步连接池在 Proactor 事件循环上会挂），但该 reloader 在本机会「检测到变更却不重启子进程」，验证前务必杀掉干净重启，别信 `--reload`。
4. **公开菜单 API 是否也隐藏空分类** —— 约 10 分钟。
   - 目前只改了 Agent 侧渲染（`agent/tools/menu_tools.py` 跳过空分类）；`routes/branches.py` 的公开菜单 API 仍会返回空分类。
5. **更新 `ARCHITECTURE.md:103`** —— 约 5 分钟。仍写着 mock `OllamaDeepSeekClient.generate()`，已过时（现为 `agent/llm_providers.py`）。

## 二、提交拆分建议

工作区同时叠着两批改动，尚未提交（本次提交前状态）：
- **SDD 分层重构**：新增 `backend/app/repo/`、`backend/app/service/`、`exceptions.py`、`error_handlers.py`，以及 routes / agent 工具与节点的下沉改造（含 `sdd/` 规格文档）。
- **本轮 4 个 bug 修复**：`agent/flows.py`（新增）、`nodes/intent_router.py`、`nodes/reservation_node.py`、`nodes/order_node.py`、`tools/menu_tools.py` 及 3 个新测试文件。

两批在 `intent_router.py` / `reservation_node.py` / `order_node.py` / `menu_tools.py` 上**重叠**，按文件拆会把中间态拆坏（例如 `test_agent_branch_menu_faq.py` 含 Bug 4 的测试，若先提交重构会导致该测试在中间态失败），因此本次合并为单次提交。后续如需拆分，建议按 hunk 拆或先补一次「重构后」基线提交。

## 三、上一轮已完成（4 个 bug，均已单测 + 实跑验证）

| # | 问题 | 修法 |
|---|---|---|
| 1（致命） | `reservation_node` 日期写死 `2026-09-16`，相对日期全订到过去 | 改为动态 UTC 当天 |
| 2 | `"order" in "Where is my order?"` 抢先命中，状态查询被答成菜单 | 词边界正则 + status 优先 |
| 3 | 半成品草稿永久劫持线程，无过期/取消 | 新增 `agent/flows.py`（`stamp`/`is_stale`/`is_cancel`/`pending_flow`）+ 路由重写 + 落库打时间戳 |
| 4 | 空分类渲染成裸 `[__route_test_cat]` 泄漏给顾客 | 渲染跳过空分类 + 删脏行 |

修复完成时全量测试结果：**258 passed**。（此后未再重跑——因为第 1 项问题，重跑会删真实预约。）
