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

## 三、提交拆分建议（**已提交 2026-10-12**）

最终拆成 3 个 commit（原建议的 2 个合并方案作废，因为工作区里其实还叠着「测试库隔离 + 文档」这一批无关改动）：

| commit | 内容 |
|---|---|
| `5cd9055` | 测试库隔离（`conftest.py`）+ `OpenItems.md` / `ARCHITECTURE.md` 文档更新 |
| `cf7f81f` | 全栈中文化（前端 + 后端 + 测试断言，含新增 `backend/app/labels.py`） |
| `dc98409` | 开发库本地化脚本 + `.gitignore` 忽略 `scripts/backups/` |

提交信息用英文，沿用仓库既有 4 个 commit 的约定。**未 push**，分支为 `main`。

以下是当时（提交前）对工作区内容的记录，保留以便追溯。工作区叠着两批改动：
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

---

## 五、全栈汉化（2026-10-12 完成）

前后端所有展示文案改为中文：前端 UI、聊天快捷词与预填、Agent 系统提示与回复、后端错误信息、FAQ 政策、种子数据。**专名保留原文**（`GourmetBistro`、`BistroAI`、`DeepSeek R1`、登录账号、枚举键值）。

- 新增 **`backend/app/labels.py`** 作为枚举→中文标签的唯一来源。它放在 `app/` 直下（而非 `agent/` 下），因为 `tests/test_architecture_boundaries.py` 禁止 `service` 目录 import `agent`，而 `service/reservation.py` 需要 `seating_area_label`。`agent/labels.py` 退化为纯转发，保持单一事实来源。
- 顺带修掉汉化中引入的重复显示：餐桌标签原本内嵌座位区，与预订回复追加的 `（室内）` 叠成 `T2（室内）（室内）`；现种子标签改为纯标识符（`T1`/`P1`/`R1`…）。
- 开发库**原地改名为中文**（未 drop 重建，保留历史）：36 行重命名，删除 2 个 `__route_test_branch` 残留门店；**26 笔订单 / 6 笔预订 / 2 位顾客全部保留**。备份 `scripts/backups/dev_seed_localize_2026-10-12.json`，脚本 `scripts/localize_dev_seed.py`。另清理冒烟测试产生的预订 #429（备份 `scripts/backups/smoke_test_reservation_2026-10-12.json`）。
- 测试：**274 passed**。

## 六、待解决问题（2026-10-12 汉化复查发现，**尚未修复**）

> 明天处理。能力清单另见 [`Capabilities.md`](./Capabilities.md)。其中 #1 / #4 / #5 已在 Task17 处理，见第七节。

| # | 严重度 | 问题 | 证据 / 位置 |
|---|---|---|---|
| 1 | **高** | **顾客「我的记录」状态标签缺 key**，会显示英文原文 | `frontend/src/components/customer/MyHistoryModal.jsx:38` 的 `STATUS_LABELS` 为订单/预订共用，缺 `placed` / `seated` / `completed`。后端枚举见 `service/order.py:28`、`service/reservation.py:34`。**开发库 27 笔订单中 21 笔为 `placed`**（另 cancelled 6、ready 1），即顾客点完单打开「我的记录」看到的就是英文 `placed` |
| 2 | **高** | **离线 LLM 降级形同虚设** | `agent/llm_providers.py` 的 `rule_based_fallback` 用英文字面量 `"classify intent" in system_prompt` 判断是否走意图分类分支，而 `intent_router_node` 实际发中文提示（「你是餐厅 AI 助手的意图分类系统。」）→ 断网时路由拿不到 JSON，`extract_json_from_text` 返回 `None`，意图落到 `general` 只回问候语。`tests/test_llm_provider.py` 用英文提示测试，**覆盖不到真实路径** |
| 3 | 中 | **前端从不校验 token** | `frontend/src/App.jsx` 只从 localStorage 恢复会话；`api.js` 里 `customerMe` / `staffMe` 定义了但无任何组件调用。token 过期后界面仍显示已登录，首个请求才失败 |
| 4 | 低 | `ReservationsManager` 缺 `pending` 标签 | `frontend/src/components/admin/ReservationsManager.jsx:75`。预订目前会自动确认，实操碰不到 |
| 5 | 观察 | **订位无确认步骤**（订单两步、订位一步，设计不一致） | `agent/nodes/reservation_node.py` 抽齐字段即 `confirmed`；`order_node.py` 则先出摘要等用户回「确认」。需产品决策，非 bug |
| 6 | 观察 | **门店无法在界面新建/编辑** | `api.js` 有 `createBranch` / `updateBranch`，但无任何界面调用；菜品只能改「是否在售」与库存，改不了名称/描述/价格/规格；分类/菜品/餐桌均无删除接口。新部署只能依赖种子数据 |

另：`MyHistoryModal` 与 `OrdersManager` 对同一订单状态措辞不一致（员工侧 `placed`→「已下单」，顾客侧无该键），修 #1 时建议把两侧标签统一成一套。

### 环境提醒

`uvicorn --reload` 在 Windows 上**本会话多次漏掉文件变更**（WatchFiles 只捕获到一次 `seed.py` 改动，其余靠手动杀进程重启才生效）。改完后端文件后请**手动重启**，不要依赖自动重载。`taskkill /PID` 会被 Git Bash 当路径处理，需用 `MSYS_NO_PATHCONV=1` 或 `powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"`。

---

## 七、Task17 收尾（2026-10-13）

Task17「完善预订生命周期」已实现：查询可用餐桌、预订确认、改约、取消、入座、完成，以及时间冲突与并发预订处理。据此关闭第六节的 3 项：

| # | 结论 |
|---|---|
| 1 | **已确认无需再改**：`MyHistoryModal.jsx` 的 `STATUS_LABELS` 现已覆盖 `placed`/`seated`/`completed`（含 `pending`），汉化提交（`cf7f81f`）时已补齐；第六节该行的描述已过时。 |
| 4 | **已修复**：`ReservationsManager` 补上 `pending` 标签、徽章与筛选项，并新增「确认」按钮（预订现在会停在 `pending`）。 |
| 5 | **已决策并落地**：聊天订位落库为 `pending`，由员工在后台确认；顾客侧仍是「一步落库」，但回复改为「预订已提交，等待门店确认」。 |

- **状态机**：新增 `RESERVATION_TRANSITIONS`（与订单同构），非法转换抛 `ValidationError`（400）；`cancelled → pending` 恢复前校验原时段是否仍空闲，否则 409。`completed` 为终态。
- **占用语义扩大**：排他约束由 `WHERE status='confirmed'` 扩为 `WHERE status IN ('pending','confirmed','seated')`，重命名为 `no_overlapping_active_table_reservations`；应用层查空桌与约束用同一状态集（`OCCUPYING_RESERVATION_STATUSES`），修掉了「pending / 已入座时段可被重复预订」的漏洞。**开发库已就地 ALTER**（备份 `scripts/backups/dev_reservation_exclusion_migration_2026-10-13.json`，脚本 `scripts/migrate_reservation_exclusion.py`，迁移前 0 冲突行，共 10 笔预订）。
- **改约**：`PATCH /staff/reservations/{id}` 现接受 `start_time` / `party_size` / `duration_minutes` / `seating_area_preference`；变更会重算 `booking_window`，自动保留原桌或重选空桌；显式指定被占用的餐桌则 409。已入座可改派餐桌，已取消/已完成不可改约。
- **查空桌**：新增 `GET /staff/reservations/availability`；底层 `ReservationService.list_available_tables` 与 Agent 的「换个时间/区域」提示文案（`_unavailable_message`）共用同一实现。
- **前端**：`ReservationsManager`（确认按钮/标签/筛选项）、`MyHistoryModal`（待确认/已确认提示）、`ChatWidget`（预订徽章改「待门店确认」）、`BranchSelector`（提示语）同步更新。
- 测试：新增 `backend/tests/test_reservation_lifecycle.py`（状态机、改约、查空桌、占用与并发），并更新既有预订测试；全量 **353 passed**。

**遗留（不在 Task17 范围）**：Agent 仍不能取消/修改已落库的预订（`update_reservation` 仅员工路由可用）；若需顾客自助取消，需另开任务加工具与消息流。
