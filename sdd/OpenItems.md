# 待办清单（Open Items）

> 建档：2026-10-11；**本轮（2026-10-12）已全部完成**，见「二、本轮完成」。上一轮 4 个 bug 的修复记录见文末。

## 一、原始待决策项（均已完成，保留原始描述以便追溯）

1. **给测试独立数据库** —— 已完成。见「二、本轮完成」第 1 条。
2. **清理本轮测试数据** —— 已完成。见第 2 条。
3. **重启前后端做正式验收** —— 已完成。见第 3 条。
4. **公开菜单 API 是否也隐藏空分类** —— 已完成（是，改为隐藏）。见第 4 条。
5. **更新 `ARCHITECTURE.md:103`** —— 已完成。见第 5 条。

## 二、本轮完成（2026-10-12）

1. **测试独立数据库** ✅
   - 新建 `restaurant_ai_test`；`backend/tests/conftest.py` 在**任何 `backend.app` import 之前**从 `.env`/环境读取原始 `DATABASE_URL`、把库名换成本测试库再写回 `os.environ`，因此引擎/校验器在导入时即指向测试库。
   - session 级 autouse fixture：`DROP DATABASE IF EXISTS ... CREATE DATABASE`（先 `pg_terminate_backend` 断开占用连接）→ `init_db()` → `seed_data()`。
   - 两道保险：`TEST_DATABASE_NAME` 必须以 `_test` 结尾；基础 URL 若本身已等于测试 URL 直接拒绝启动——防止误把开发库当测试库清空。
   - `backend/app/seed.py` 的「已 seed 则跳过」守卫由 `select(Branch)` + `scalar_one_or_none()` 改为 `select(Branch).limit(1)` + `scalar_one_or_none()`；原写法在有 2+ 分支时抛 `MultipleResultsFound`（与 `ARCHITECTURE.md` 第 96 行记载的修法一致，此前代码漏了 `.limit(1)`，本轮补齐）。
   - 结果：**258 passed**，连跑两次一致；跑测试前后开发库行数不变（9 预约 / 163 订单 / 11 顾客 / 4 分支）——开发库不再被 pytest 触碰。
2. **清理本轮（2026-10-11）测试数据** ✅
   - 实际范围与最初记录不同（原文列的 #381/#405、#533/#562、255/256/308 与本轮真实数据对不上），改为按「2026-10-11 创建」这一口径，**先备份后删除**。
   - 备份：`scripts/backups/dev_cleanup_2026-10-12.json`（含所删全部行）。脚本：`scripts/cleanup_dev_test_data.py`。
   - 删除：137 订单（含 4 条测试顾客订单）+ 143 条订单明细；预约 #409 / #410 / #412；9 个测试邮箱顾客 `acceptance_*` / `sticky_*` / `datefix_*` / `menucheck_*`（id 255,256,257,258,259,308,309,334,335）。事务内按外键序 `order_items → orders → reservations → customers`。
   - 保留：10-07 / 10-08 的 26 订单、预约 #1/#6/#9/#18/#19/#21、种子 customer 1（以及更早遗留、不在本轮范围的 `testuser_flow@example.com`，id 2）。
   - 复核：0 孤儿行。
3. **重启前后端** ✅ —— 后端 `uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000`（:8000，启动无连接池超时）；前端 `npm run dev`（Vite :5173，绑定 IPv6 `::1`）。冒烟：`/docs` `/openapi.json` `/api/branches` 均 200，前端 `/` 200。
4. **公开菜单 API 隐藏空分类** ✅ —— 在 `MenuService.get_branch_menu` 的 `_menu_payload` 里跳过空分类；该函数被公开 API（`routes/branches.py`）与 Agent 菜单工具共用，故两侧行为统一；员工侧 `MenuService.list_categories` 仍可见。新增测试 `test_get_branch_menu_hides_empty_categories`。
5. **`ARCHITECTURE.md` LLM 段更新** ✅ —— 已把过时的 mock `OllamaDeepSeekClient.generate()` 表述改为现状（`agent/llm_providers.py`：Ollama vs 任意 OpenAI 兼容端点 + `rule_based_fallback`，仅在实连端点手工验证）。

## 三、提交拆分建议（沿用，仍未提交）

工作区叠着两批改动：
- **SDD 分层重构**：新增 `backend/app/repo/`、`backend/app/service/`、`exceptions.py`、`error_handlers.py`，以及 routes / agent 工具与节点的下沉改造（含 `sdd/` 规格文档）。
- **bug 修复 + 本轮收尾**：`agent/flows.py`、各 `nodes/*.py`、`tools/menu_tools.py`、`tests/conftest.py`、`seed.py`、`service/menu.py`、`ARCHITECTURE.md` 及新增测试。

两批在 `intent_router.py` / `reservation_node.py` / `order_node.py` / `menu_tools.py` 上重叠，按文件拆会把中间态拆坏，故仍建议合并为单次提交。

## 四、上一轮已完成（4 个 bug，均已单测 + 实跑验证）

| # | 问题 | 修法 |
|---|---|---|
| 1（致命） | `reservation_node` 日期写死 `2026-09-16`，相对日期全订到过去 | 改为动态 UTC 当天 |
| 2 | `"order" in "Where is my order?"` 抢先命中，状态查询被答成菜单 | 词边界正则 + status 优先 |
| 3 | 半成品草稿永久劫持线程，无过期/取消 | 新增 `agent/flows.py`（`stamp`/`is_stale`/`is_cancel`/`pending_flow`）+ 路由重写 + 落库打时间戳 |
| 4 | 空分类渲染成裸 `[__route_test_cat]` 泄漏给顾客 | 渲染跳过空分类 + 删脏行 |

修复完成时全量测试结果：**258 passed**（本轮在第 1 项完成后已可安全重跑，仍为 258 passed）。
