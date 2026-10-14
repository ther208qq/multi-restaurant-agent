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

---

## 八、Task18 收尾（2026-10-13）

Task18「完善权限与审计」已实现：后端校验用户 / 员工 / 门店权限；关键操作连同操作者写入审计日志。

### 权限（`service/` 层，API 与 Agent 共用）

审计与权限都放在 Service 层而不是路由，路由只负责「谁在调用」。逐端点复核后的结论：

| 端端点 | 结论 |
|---|---|
| `GET /staff/reservations/availability` | **本次修复**：原先不带任何门店校验，带 `branch_id` 的员工可查任意门店的空桌（越权读）。现由 `ReservationService.list_available_tables(..., staff=...)` 校验，跨门店 `403`；Agent 调用时 `staff=None`，不受影响。 |
| `GET /staff/orders`、`/staff/reservations`、`/staff/menu/categories`、`/staff/branches/tables` | 本来就用 `staff.branch_id or branch_id` 收敛到本门店，无需改。 |
| `PATCH /staff/orders/{id}/status`、`PATCH /staff/reservations/{id}`、`PUT /staff/menu/stock`、`PATCH /staff/menu/items/{id}`、`POST /staff/menu/{categories,items}`、`POST|PATCH /staff/branches/tables` | 本来就有门店校验，无需改。 |
| `POST|PATCH /staff/branches` | 本来就有 `require_admin`（`role != admin` → `403 需要管理员权限`）。 |
| 顾客侧 `/orders/me`、`/reservations/me`、`POST /chat` | 由 JWT 注入的 `customer_id` 决定范围，无越权路径。 |

### 审计（新增 `audit_logs` + `AuditService`）

- **表**：`audit_logs`（actor_type/id/name/role、branch_id、action、entity_type/id、details JSONB、created_at）。操作者**冗余存储**而非外键，账号删除后记录仍可读。
- **写入语义**：`AuditService.record(session, ...)` **不开自己的事务**，而是加入被审计操作的事务——业务回滚则不落审计行。测试专门覆盖「非法状态转换」「库存不足被拒」两种情况下不留记录。读取侧的 `AuditService.list_logs(staff, ...)` 才是常规 Service 入口，按门店收敛。
- **覆盖的操作**：`order.created`、`order.status_changed`（含取消与反取消，记录 from/to）、`reservation.created`、`reservation.status_changed`、`reservation.rescheduled`、`reservation.table_reassigned`（含「无桌预订入座时自动派桌」）、`stock.updated`（from/to）、`menu_item.created/updated`、`menu_category.created`、`branch.created/updated`、`table.created/updated`。一次请求的多个操作各记一行（例如同时改约 + 改派餐桌）。
- **读取接口**：新增 `GET /staff/audit-logs?branch_id=&action=&entity_type=&entity_id=&limit=`（limit ≤ 200），门店收敛逻辑与其它员工接口一致；门店 id 传别的门店会被忽略。
- **前端**：`AdminLayout` 新增「操作日志」页签，`AuditLogManager` 支持门店 / 操作类型 / 条数筛选，展示时间、操作者、操作、对象与变更前后值。

### 其它

- 修掉一个潜在坑：`init_db()` 依赖调用方先 import 模型才能让 `Base.metadata` 有表。应用启动时靠路由间接 import 到了全部模型，因此一直没暴露；本次以脚本调用 `init_db()` 建表时才发现 `create_all()` 建了个寂寞。已在 `init_db()` 内部 import 模型注册表（`backend/app/models/__init__.py`），写入 `ARCHITECTURE.md` 的工程日志。
- **开发库**：`audit_logs` 通过 `create_all()` 增量建出（未 drop 重建，订单 28 / 预订 10 不变）。冒烟测试用 `downtown_staff` 把门店 1 的餐桌 P1 容量 2→4→2 改了个来回（数据已复原），留下 2 行真实审计记录（id 1、2）。
- 测试：新增 `backend/tests/test_audit.py`（17 条：各类操作的记录与操作者、失败不留痕、门店收敛、接口 401/403），全量 **370 passed**；前端 `npm run build` 通过。

**遗留**：审计表**没有自动清理/归档**（长期运行会持续增长）；操作日志页只读，无导出。均不在 Task18 范围。

---

## 九、Task19 收尾（2026-10-13）

Task19「经营统计与异常处理」已实现：按门店和日期统计订单量、销售额及菜品销量；处理重复请求、库存不足、预订冲突与操作失败，并配有测试。

### 经营统计

- **新增** `backend/app/repo/stats.py`（聚合 SQL）、`backend/app/service/stats.py`（口径与门店收敛）、`backend/app/schemas/stats.py`、`backend/app/routes/staff_stats.py`（`GET /staff/stats`）、前端 `StatsDashboard.jsx` + `AdminLayout`「经营统计」页签。
- **口径**（两个是决策而非实现细节，故放在 Service 层）：
  1. **已取消订单不算销售**：不计入订单量/销售额/销量/菜品排行，单独用 `cancelled_count` 汇报；「取消后又恢复」的订单重新计入——数字描述的是区间内订单的**当前状态**，与订单看板看到的一致。
  2. **一天 = 一个 UTC 日**：库里是 `timestamptz`，`date_trunc('day', ...)` 会跟着服务器 TimeZone 走，故聚合前先 `timezone('UTC', ...)`；区间为左闭右开的 UTC 窗口，`end_date` 当天包含在内，默认最近 7 天，最长 366 天。
- 销售额与菜品排行取订单行上的**快照单价**（Task16 落库的含规格加价价格），后续改价不会改写历史统计。
- 权限：带门店的员工统计本门店（不传 `branch_id` 默认本门店），统计别的门店 `403`；不带门店的超级管理员必须显式传 `branch_id`，否则 `400 请指定要统计的门店。`
- **开发库无 schema 变更**（未新增列/表），实跑冒烟见下。

### 重复请求与异常处理

- **新增** `backend/app/repo/locks.py`（`pg_advisory_xact_lock` 顾问锁）与两个 Service 内的查重：`OrderService._find_duplicate_order`、`ReservationService._find_duplicate_booking`；两个 Agent 工具（`create_order_atomic`、`check_table_availability_and_book`）以 `deduplicate=True` 提交。
- **判重口径不同，原因不同**：
  - **订单**是两步的（摘要 → 用户回「确认」），第二次确认可用**内容**识别：门店 + 用餐方式 + 配送地址 + 菜品行（菜品、数量、规格名、行备注），与**已落库的行**比较而不是与请求 id 比较。
  - **订位**是一步落库的，没有确认回合可锚定，只能以**请求本身**为身份：同一顾客 + 同时段 + 同人数 + 同时长。这条尤其必要——排除约束拦不住它：重复提交会落到**另一张**空桌，等于给同一拨人占两张桌。
  - 两者都限 5 分钟窗口（`DEDUPLICATE_WINDOW_MINUTES`）且限「仍未完结」：订单已取消/已送达、订位已取消/已完成时不吸收重复，会新建。
  - 查重与写入之间用**顾问锁**（按请求上锁，不是按顾客）串行化：否则查重与插入是两个事务，两次并发提交会互相看不到对方而各插一行；按请求上锁意味着不相关的订单仍可并发。
- **`place_order` 改成两趟**：第一趟解析并校验每一行（不写），第二趟才扣库存 + 插单。好处是查重不消耗库存，且「第二行不合格」在第一行扣减**之前**就中止（而不是扣一半再回滚）。
- **失败不留痕**（新增测试逐条断言）：多行订单第二行库存不足、并发下 `try_decrement_stock` 落空导致中途失败、餐桌被别人占用、非法状态转换——四种情况都保证 **无订单行、无库存变动、无审计行**。
- 重复的 `PATCH /staff/orders/{id}/status` 本身幂等（同状态直接返回，不再写审计）。

### 测试

- 新增 `backend/tests/test_stats.py`（16 条：聚合口径、取消/恢复、门店与日期收敛、UTC 跨日、区间边界、排行上限、空区间、校验与 403/401/422）与 `backend/tests/test_duplicate_requests.py`（13 条：订单/订位的重复提交、不同请求仍新建、取消后新建、窗口外新建、并发重复只建一单、失败不留痕、状态请求幂等）。
- 全量 **399 passed**（370 + 29）；前端 `npm run build` 通过。
- **改动既有测试 1 处**：`test_reservation_exclusion.py::test_graceful_handling_when_table_taken` 原先用 `customer_id=1` 占满天台桌，再用**同一顾客**请求同一时段——现在这正好是「重复提交」，会被查重返回已存在的那笔，测不到「无空桌」提示。改为让**另一个顾客**占桌，测试意图不变（也更贴近现实）。`test_stock_and_orders.py::test_concurrent_orders_for_last_unit` 未改：相同顾客的并发同款订单仍会先被工具**预览**判为库存不足（首次已扣光最后一件），原先的断言原样通过。

### 冒烟（开发库实跑）

后端 `uvicorn --reload --port 8010` 起在开发库上：`admin` 与 `downtown_staff` 登录后
`GET /api/staff/stats?branch_id=1&start_date=2026-10-01&end_date=2026-10-13` 返回
`order_count=22 / cancelled_count=6 / revenue=446.50 / units_sold=37`，排行与每日明细正常；
带门店的员工不传 `branch_id` 默认本门店、查别的门店 `403`、无 token `401`。**只读接口，未改开发库数据**；测完已停后端（原先未运行）。

### 遗留（不在 Task19 范围）

1. **订单查重在预览之后**：若首次提交把库存用尽（如最后一件），第二次提交会被工具**预览**判为「库存不足」而不是返回原订单——没有重复下单/重复扣减，但顾客会看到失败提示。要修需把查重提前到预览之前（代价是判重签名改用未解析的请求规格，判定会变弱）。
2. 统计**只按 UTC 日**分桶，没有按门店时区偏移；跨时区门店的「今天」会差一天。
3. 统计页无导出（CSV/打印），条形图为纯 CSS（未引入图表库）。
4. 顾问锁是**单库级别**的：将来多实例部署时它仍在 PostgreSQL 内生效（本来就是库级锁），但若把查重挪到应用缓存里就会失效——目前无此计划。

---

## 十、顾客自助下单与预约（2026-10-14 完成）

### 目标

改前前端**没有任何下单/订位表单**，交易只能走聊天。本次补上「侧栏购物车 + 结算弹窗」与「预约表单」，交易入口变为三条（见 `Capabilities.md` 第 0 节）。

**查证结论**：Service 层与 schema 都已具备，**只缺顾客侧 HTTP 路由**，不动 Service、不动数据库、不做迁移。

**已定的三项决策**：① 侧栏购物车 + 结算弹窗；② 预约只选区域、由系统自动配桌（顾客不能指定桌号）；③ 直接下单**不**套用 5 分钟去重。

> 完整方案（含验证步骤、scope guard）见会话计划文件
> `C:\Users\ther\.claude\plans\silly-wishing-cosmos.md`，本节只记进度。

### 完成情况（2026-10-14）

在前一版半截重构（`_loaded` / `from_order` / `from_reservation` / `parse_slot` 等）之上补齐顾客侧接口与前端界面。**全量 `pytest`：414 passed**（含新增 15 个用例），`npm run build` 通过。

后端在既有半截重构之上补齐（改动 7 个既有文件 + 新增 2 个测试文件）：

| 文件 | 已做的改动 |
|---|---|
| `schemas/order.py` | 新增模块级 `_loaded(obj, name)`（从实例 `__dict__` 读关系，避免异步上下文触发懒加载）；`OrderItemResponse.from_item()`；`OrderResponse.from_order(order, *, customer_name=None)` |
| `schemas/reservation.py` | 新增 `_loaded`、`parse_slot(date, time)`（`YYYY-MM-DD`+`HH:MM` → UTC datetime，格式错抛 `ValidationError`，文案与员工端原内联版一致）、`SLOT_FORMAT_ERROR`；`ReservationCreate.to_start_time()`；`ReservationAvailabilityItem.from_table()`；`ReservationResponse.from_reservation(r, *, customer_name=None)` |
| `schemas/__init__.py` | 导出 `ReservationAvailabilityItem`、`parse_slot` |
| `routes/staff_orders.py` | 删掉本地 `_format_order_response`，两处改用 `OrderResponse.from_order(...)` |
| `routes/staff_reservations.py` | 删掉本地两个 `_format_*`，改用 schema 构造器；可用性查询的 `datetime.strptime` 内联解析换成 `parse_slot` |
| `repo/order.py` | `list_orders` 新增 `with_items_menu_item` 选项（镜像 `get_order` 已有参数） |
| `service/order.py` | `get_order`/`list_orders` 改为 `with_items_menu_item=True`（这样行回执能带菜名） |

### 本次新增

- **`schemas/order.py`**：新增 `OrderPreviewLine` / `OrderPreviewResponse`（形状取自 `OrderService.preview_order` 返回），并在 `schemas/__init__.py` 导出。
- **`routes/customer_orders.py`**：新增 4 个顾客侧路由 `POST /orders/preview`、`POST /orders`、`GET /reservations/availability`、`POST /reservations`，全部 `Depends(get_current_customer)`，`customer_id` 只从 JWT 取；下单 `deduplicate=False`、订位 `deduplicate=True`。
- **后端测试**：`backend/tests/test_customer_ordering.py`（8 例）、`test_customer_booking.py`（7 例）——覆盖预览不扣库存、下单原子扣减 + 审计、价格以数据库为准（伪造 `price_delta` 被忽略）、外卖缺地址 400、库存不足 409 且不留痕、伪造顾客 id 被忽略、重复订位返回同一笔、撞车 409、坏日期 400、未知门店 404、未登录 401。
- **前端**：`api.js` 四个方法；`utils/datetime.js`（墙上时间格式化）；`MenuExplorer` 加购物车 + 规格单选（`VariantPicker` / `MenuItemActions` 两个小部件）；新建 `CartPanel.jsx`、`CheckoutModal.jsx`（实时预览报价 + 单品备注）、`ReservationModal.jsx`（实时空桌数，系统自动选桌）；`Navbar` 加「预订座位」；`App.jsx` 提升 `cart` 状态并在**切换门店时清空**、下单后**重拉菜单**（库存已变）。
- **既有时间显示**：`MyHistoryModal.jsx`、`ReservationsManager.jsx` 的 `toLocaleString()` 改为走 `utils/datetime.js`（墙上时间），与聊天回复口径一致。
- **文档**：`Capabilities.md` 第 0 节改写为「三条交易通道」，顾客侧能力表补 7 行；`README.md` 补顾客侧能力与时间语义限制。
- 验证后**单次提交**（英文 + `Co-Authored-By`），仍在 `main`，不纳入 `sdd/Spec.md`、`sdd/Tasks.md` 的既有工作区改动。

### 关键坑（实现时别踩）

- **`from_order`/`from_reservation` 读关系一律走 `_loaded`**：新路由用 `OrderService.get_order(order.id)`（已带 `with_items_menu_item=True`）回读后再构造，否则 `branch_name`/`customer_name` 为空、甚至异步懒加载报错。
- **`deduplicate` 有意不一致**：下单 `False`（顾客可能真的想再来一份），订位 `True`（重复订位会让同一拨人**占住两张不同的桌**，排除约束拦不住它）——这正是 Task19 给 Agent 加该保护的原因。
- **时间语义**：沿用既有约定，前端把输入当**墙上时间**直接给 `requested_date`+`requested_time`，由 `parse_slot` 按 UTC 存；不做时区换算。
- **架构边界测试会扫字面量**：`routes/` 下不能出现 `select(`、`.execute(`、`.commit(`，也不能 import `backend.app.repo` / `backend.app.agent.tools`。
- 本次改动**动了既有员工路由与 repo/service**，所以 `pytest` 必须全量跑（不是只跑新用例）。

---

## 十一、预订时间校验与时段推荐（待实现，2026-10-14 记录）

> 结论：**当前未实现**。`ReservationService.book_table` 只校验「餐桌是否有空」（排除约束 + 探测），
> **不校验时间是否合理**——过去时间、营业时间、最远可订天数一概不管；「推荐时间」也只是一句泛泛的占位文案。

### 现状（证据）

| 检查项 | 现状 | 位置 |
|---|---|---|
| 人数 ≥ 1 | ✅ 有 | `service/reservation.py:230` |
| 时间格式可解析 | ✅ 有，但只补时区、不判早晚 | `_coerce_start_time` `service/reservation.py:95` |
| 门店存在且启用 | ✅ 有（与营业时段无关） | `service/reservation.py:237` |
| **过去时间** | ❌ 不校验 | `book_table` 内无 `now()` 比较（唯一的 `now()` 在 `:335`，是判重窗口） |
| **营业时间** | ❌ 不生效 | `Branch.opening_hours` 是自由文本（种子 `"10:00 - 22:00"` / `"11:00 - 23:00"`），全库从未解析 |
| **最远可订天数** | ❌ 无 | — |
| **时段推荐** | ⚠️ 只推「其它区域」；时间上只有固定一句「今晚更早或更晚的时间段也有空位」，不计算 | `_unavailable_message` `service/reservation.py:359` |

`schema/reservation.py` 的 `ReservationCreate` 无任何 `validator`（`grep` → none）。

### 已定口径（本轮确认）

1. **范围：全部实现** —— 过去时间 + 营业时间（含最晚入座）+ 最远可订天数 + 具体时段推荐。
2. **`opening_hours` 规范成可解析格式 `HH:MM - HH:MM`** —— 解析失败即拒绝预订；同步改种子数据与「员工编辑门店」的校验。**不加结构化列、不做迁移**（那要改表）。

### 实现要点（待做）

全部落在 **Service 层**（`service/reservation.py`）。`book_table` 被 **顾客表单 / 聊天 Agent / 员工改约** 三处共用，改一处三入口同时生效：

1. **时间窗口校验**（`book_table` 开头，`:230` 附近）：
   - 拒绝 `start_time <= now`（或「至少提前 N 分钟」，N 待定，默认 0）；
   - 拒绝超出最远可订天数（默认 30 天，待定）。
2. **营业时间校验**：
   - 新增解析器：`opening_hours`（`HH:MM - HH:MM`）→ `(open, close)`；解析失败抛 `ValidationError`。
   - **最晚入座规则**（待定，建议）：`start_time + duration_minutes <= close`（必须留足用餐时长），否则拒绝并提示。
   - 跨零点写法（如 `18:00 - 02:00`）暂不支持——要么明确拒绝，要么单列一条处理，先记着。
3. **真正的时段推荐**（改写 `_unavailable_message` 或新增 `suggest_slots`）：
   - 在请求日**同一营业日内**按固定步长（如 30 分钟）前后扫描，算出**具体有位的时段**（复用 `_free_tables`），给出「19:00 已满，18:00 或 20:30 有位」这类结论。
   - 保留既有的「其它区域」提示（`_free_tables(..., 'any', ...)`）。
4. **`list_available_tables` 同步受约束**：顾客的 `GET /reservations/availability` 与员工查空桌要走同一时间窗口，否则前端显示「有位」、提交却被拒，自相矛盾。

### 测试（待补）

- 过去时间 → 400；超最远天数 → 400。
- 打烊后 / 未开门 / 最晚入座留不足时长 → 400；`opening_hours` 解析失败也拒绝。
- 满座时推荐命中：「某时段满」返回的建议里包含**真的有空**的时段。
- 三入口一致：顾客路由、Agent 工具、员工改约走同一规则。
- `opening_hours` 规范化后，种子数据与「员工编辑门店」校验同步。

### 影响面 / scope guard

- 改 `service/reservation.py`（三入口共用）+ `seed.py`（营业时间格式）+ 员工编辑门店的前端校验/文档。
- **不加 DB 列、不做迁移**。
- 需**全量**跑 `pytest`（动了共用 Service），并重启后端（Windows `--reload` 会漏改，见第六节环境提醒）。
