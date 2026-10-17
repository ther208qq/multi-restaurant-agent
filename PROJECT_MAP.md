# 项目地图 PROJECT_MAP

> 复习地图（不是设计文档，那是 `ARCHITECTURE.md`；也不是产品介绍，那是 `README.md`）。
> 这份地图把整个仓库的代码归到两个轴：**核心代码**（快乐路径上不可省的骨架）与**防御性代码**
> （为承兑某个保证而存在的盔甲）。后端 / 前端各一部分，防御性代码按功能分组。
> 只深读 agent 子系统的旧地图见 [`AGENT_MAP.md`](./AGENT_MAP.md)，两者互相链接、不合并。

## 0. 怎么读这份地图

### 三个心智模型（不要更多）

```text
模型 A - Tool-Calling（工具调用环）
  LLM --决定调哪个工具--> Tool --薄封装--> Service --> DB
  约束：LLM 永远不碰 DB，只触发固定工具

模型 B - Agent + Workflow（LangGraph / LangChain create_agent）
  State + 预置 ReAct 循环做骨架，LLM 只决定「下一步调哪个工具」

模型 C - React 单状态视图（前端）
  一个 App.jsx 持有全部状态，无路由库，两视图（customer / admin）切换
```

四个 agent 面 + 前端，落到三个模型：

| Agent 面 | 入口路由 | 核心文件 | 属于 |
|---|---|---|---|
| 顾客联系后厨 KDS | `POST /api/agent/order/{id}/request` | `agent/kds.py` + `tools/kds_tools.py` | 模型 A（规则优先，LLM 兜底） |
| 顾客礼物审批 | `POST /api/orders`、`/staff/approvals` | `agent/approvals.py` + `tools/gift_tools.py` | 模型 A（LLM 优先，规则兜底） |
| 顾客预订逼单 | `GET /api/reservations/availability` | `agent/offers.py` + `tools/gift_tools`/`table_tools` | 模型 A 变体（纯规则措辞，不调 LLM） |
| 诊断-一次性 | `GET /api/staff/diagnosis` | `agent/diagnosis.py` | 单次 LLM + 护栏（不是 loop） |
| 诊断-多轮调查 | `POST /api/staff/diagnosis/investigate` | `agent/diagnosis_investigation.py` + `harness.py` + `diagnosis_tools.py` | 模型 B（LangChain `create_agent` ReAct） |
| 前端 | Vite dev server | `App.jsx` + 各组件 | 模型 C |

共享底座（不属于某个面）：`agent/llm.py` + `llm_providers.py`（模型接入）、`agent/time_range.py`
（时间窗解析）、`agent/checkpointer.py`（多轮记忆）。

### 核心 vs 防御性

- **核心代码**：请求不经过它就无法完成的骨架（路由、Service 业务规则、Repo 查询、模型/Schema、
  组件渲染、`api.js`）。
- **防御性代码**：请求能正常走，但它去掉后某个「保证」就不再成立（LLM 输出可信、并发不超卖、
  越权被拒、模型挂了不 500、长对话不爆上下文…）。判定标准只有一条——**去掉它，哪个保证会被打破？
  没有保证被打破，就是无效防御，该删。**

---

## 1. 后端核心代码（`backend/app`）

### 1.1 分层与调用链

```
HTTP
 └─ route (app/routes/*)            薄：解析参数 + Depends(get_db) + Depends(鉴权) + 整形响应
     ├─ 鉴权依赖 (auth/security.py)  JWT 解码 → AuthService.get_customer/get_staff
     ├─ [边缘 agent 钩子]            kds / offers / approvals（经 tools 调 Service）
     └─ Service.static_method(...)   app/service/*  ← 持有事务边界
         └─ async with transactional_session(s) as s:
             ├─ Repo.method(s, ...)  app/repo/*  ← 只查询/flush，从不 commit
             ├─ 其他 Service 调用     例：AuditService.record(s, ...) 加入同一事务
             └─ raise DomainError    → 由 error_handlers 映射为 HTTP 状态码
```

**四条铁律**（由 `tests/test_architecture_boundaries.py` 静态 import 扫描强制）：

1. Service 持有事务边界（`database.transactional_session`）；Repo 只收 session、不 commit。
2. `repo` 禁 import `service`/`routes`/`agent`/`fastapi`；`service` 禁 import `routes`/`agent`/`fastapi`。
3. `routes` 只调 Service，不碰 Repo、DB、agent 业务 tools；`agent` 只经 Tool 调 Service，不碰 Repo/DB。
4. 关键业务约束由确定性代码或数据库保证，**不依赖 LLM**（`sdd/Constitution.md`）。

Agent helper 的两个钩子点：
- **路由边缘**：`customer_orders.py`（offers/approvals）、`customer_agent.py`（kds）、
  `staff_approvals.py`（礼物建议）——业务接口「顺带咨询 Agent」，不是聊天入口。
- **诊断路由内部**：`GET /staff/diagnosis` 调 `DiagnosisService.diagnose` 后交
  `agent.diagnosis.analyze_diagnosis`（纯函数，无 session）；`POST /staff/diagnosis/investigate`
  整体交给 `agent.diagnosis_investigation.run_investigation`。

### 1.2 模块清单

**根包**（`backend/app/`）

| 模块 | 行数 | 一行职责 |
|---|---|---|
| `config.py` | 91 | Pydantic `Settings`（DB URL / LLM provider / JWT / 诊断预算） |
| `database.py` | 84 | async engine + `Base` + `get_db` + `transactional_session`（事务边界）+ `init_db` |
| `error_handlers.py` | 56 | 领域异常 → HTTP 状态码 + `{"detail"}` |
| `exceptions.py` | 65 | 领域异常层级（`NotFound/Validation/Conflict/Auth/Forbidden/TableUnavailable/ReservationRace`） |
| `labels.py` | 114 | 中文码表（订单/预订/KDS/审计/区域） |
| `opening_hours.py` | 58 | `HH:MM - HH:MM` 解析与校验 |
| `main.py` | 118 | FastAPI app、lifespan、CORS、14 个 router 挂载、`/health` |
| `seed.py` | 234 | 开发库种子脚本（门店/菜品/餐桌/账号） |

**routes**（14 个文件，全部前缀 `/api`）——见 §1.3 端点表。

**service**（14 个文件，业务规则 + 事务 + 审计）

| 模块 | 行数 | 职责 |
|---|---|---|
| `order.py` | 763 | 下单/价格/库存/生命周期/去重 + 模拟 KDS |
| `reservation.py` | 1019 | 订桌、可用性、排除约束插入、改约、生命周期 |
| `diagnostics.py` | 1247 | 确定性异常检测 `DiagnosisService` + 单品逐日 `DishDailyService` |
| `stats.py` | 241 | 经营统计聚合 + 门店范围 |
| `approval.py` | 269 | HITL 审批（建挂起单 / 通过 / 拒绝） |
| `branch.py` | 248 | 门店/餐桌 CRUD + 范围 + 审计 |
| `menu.py` | 287 | 菜单/库存、可用性规则 |
| `business_profile.py` | 470 | 预订健康度/星期/时段/订单类型/基线（诊断维度扩展） |
| `branch_peers.py` | 240 | 同业对比（无 SQL，复用 stats） |
| `audit.py` | 142 | 审计写（`record` 加入调用方事务）+ 读（`list_logs`） |
| `gift.py` | 128 | 富余库存规则 + `issue_gift` |
| `auth.py` | 105 | 账号创建与凭据校验 |
| `faq.py` | 61 | 门店联系/就餐政策（静态表） |
| `scope.py` | 48 | 门店范围收敛（`ensure_branch_scope` / `resolve_staff_branch`） |

**repo**（10 个文件，只碰 DB、只收 session、不 commit）

| 模块 | 行数 | 职责 |
|---|---|---|
| `stats.py` | 354 | 聚合查询（totals/daily/dish/dish-daily/order-type/weekday-hour） |
| `reservation.py` | 290 | 预订查询、`tstzrange` 编码、重叠探测、`for_update` |
| `menu.py` | 253 | 分类/菜品/变体/库存 + 原子库存操作 |
| `order.py` | 162 | Order/OrderItem 查询与插入 |
| `branch.py` | 148 | Branch/Table 查询与 CRUD |
| `approval.py` | 103 | OrderApproval 查询与插入 |
| `audit.py` | 86 | 审计插入 + 过滤列表 |
| `user.py` | 67 | Customer/StaffUser 查询与创建 |
| `gift.py` | 47 | Gift 查询与插入 |
| `locks.py` | 44 | 咨询锁 helper（`pg_advisory_xact_lock`） |

**models**（8 张表）：`user`（Customer/StaffUser）、`branch`（Branch/Table）、`menu`
（Category/Item/Variant/Stock）、`order`（Order/OrderItem + KDS 状态）、`reservation`
（Reservation + 排除约束）、`gift`、`approval`、`audit`（冗余 actor，JSONB details）。

**schemas**（12 个文件）：请求/响应 Pydantic 模型；`reservation.py` 还含 `parse_slot` 与
`AgentOffer`。（`chat.py` 已在本次清理删除，见 §4。）

**agent**（13 + tools/3）——见 §1.4 与 §3。

**auth**：`crypto.py`（纯密码哈希 + JWT 编码）、`security.py`（JWT 解码 + `get_current_*` 依赖）。

**入口**：`run.py`（开发服务器）、`run_eval.py`（评测库服务器）。

### 1.3 REST 端点 → Service 方法

| 路由文件 | 端点 | 调用 |
|---|---|---|
| `auth.py` | `POST /auth/register` / `POST /auth/login` / `GET /auth/me` | `AuthService` |
| `branches.py` | `GET /branches` / `{id}/menu` / `{id}/faq` | `BranchService` / `MenuService` / `FAQService` |
| `customer_agent.py` | `POST /agent/order/{id}/request` | `OrderService` → `agent.kds.handle_kds_request` |
| `customer_orders.py` | `GET /orders/me`、`POST /orders/preview`、`GET/POST/cancel /orders/{id}`、`POST /orders`、`GET/POST /reservations` | `OrderService` / `ReservationService` / `ApprovalService` / `agent.offers` / `agent.approvals` |
| `staff_approvals.py` | `GET/POST /staff/approvals` | `ApprovalService` + `agent.approvals` |
| `staff_audit.py` | `GET /staff/audit-logs` | `AuditService.list_logs` |
| `staff_auth.py` | `POST /staff/login`、`GET /staff/login-hints`、`GET /staff/me` | `AuthService` |
| `staff_branch_peers.py` | `GET /staff/diagnosis/peers` | `BranchPeerService` |
| `staff_branches.py` | branches CRUD + tables CRUD | `BranchService` |
| `staff_diagnostics.py` | `GET /staff/diagnosis`、`POST /staff/diagnosis/investigate` | `DiagnosisService` + `agent.diagnosis` / `agent.diagnosis_investigation` |
| `staff_menu.py` | categories/items/stock | `MenuService` |
| `staff_orders.py` | `GET/PATCH /staff/orders`（status/kds） | `OrderService` |
| `staff_reservations.py` | `GET/PATCH /staff/reservations`、availability | `ReservationService` |
| `staff_stats.py` | `GET /staff/stats` | `StatsService.get_branch_summary` |

### 1.4 骨架版快乐路径

诊断调查的 7000 行盔甲，核心控制流其实只有十几行：

```python
async def investigate(question, staff, session, thread_id=None):
    branch = resolve_staff_branch(staff)        # 身份决定门店
    window = resolve_window(question)           # 时间窗解析
    ctx    = Ctx(staff, session, branch, window)
    model  = build_chat_model()                 # 一个模型
    tools  = build_readonly_tools(ctx)          # 7 个只读工具，每个薄封装一个 Service
    agent  = create_agent(model, tools, response_format=Report)  # LangGraph 自动跑 ReAct
    turn   = await agent.ainvoke({'messages': [question]}, thread_id)
    report = turn.structured_response           # {answer, facts, inferences, checks}
    return {'thread_id':…, 'answer': report.answer, 'resolved_window': window,
            'steps': ctx.steps, 'evidence': ctx.evidence, 'report': report}
```

下单与订桌的「DB 级保证」两句话（详见 `ARCHITECTURE.md`）：
- **不双订桌**：`EXCLUDE USING gist (table_id WITH =, booking_window WITH &&)`，占用态下重叠插入被数据库拒绝。
- **不超卖**：`UPDATE menu_item_stocks SET stock_quantity = stock_quantity - :qty WHERE … AND stock_quantity >= :qty`，`rowcount==1` 才成功。

---

## 2. 前端核心代码（`frontend/src`）

### 2.1 入口与接线

- `main.jsx`（10）——`React.StrictMode` 挂载 `App`。
- `App.jsx`（290）——**无路由库**，单状态 `currentView: 'customer'|'admin'` 两视图切换；
  持有 branches/menu/cart/双身份/六个弹窗开关；全局 `auth:unauthorized` 事件监听（401 → 清会话）。
- `vite.config.js`（15）——dev 端口 5173，代理 `/api` → `127.0.0.1:8000`，开发期同源。

### 2.2 组件清单（无死组件）

**customer/**（10 个）

| 组件 | 行数 | 职责 |
|---|---|---|
| `BranchSelector` | 86 | 门店选择 + 地址/营业/政策 |
| `MenuExplorer` | 156 | 分类 chip + 菜品草稿 + 可用性 |
| `MenuItemActions` | 76 | 库存徽章 + 数量步进 + 加入 |
| `VariantPicker` | 35 | 规格单选 chip |
| `CartPanel` | 89 | 侧栏购物车 + 逐行小计 |
| `CheckoutModal` | 354 | 下单类型/地址/备注 + 实时预览 + 审批轮询 |
| `ReservationModal` | 236 | 订桌表单 + 实时可用性 + 逼单 offer |
| `CustomerAuthModal` | 163 | 顾客登录/注册 |
| `MyHistoryModal` | 317 | 我的订单/预订 + 取消 + 内嵌 KDS 面板 |
| `KdsRequestPanel` | 140 | 联系后厨 + 回复 + 挂起态 |

**admin/**（13 个）

| 组件 | 行数 | 职责 |
|---|---|---|
| `AdminLayout` | 145 | 后台壳：角色/门店 + 8 个页签 |
| `AdminLoginModal` | 151 | 员工登录 + 匹配运行库的种子账号提示 |
| `OrdersManager` | 396 | 订单看板：状态流 + 取消 + 模拟 KDS |
| `ReservationsManager` | 350 | 预订：确认/入座/完成/取消 + 手动改派 |
| `MenuManager` | 463 | 菜单与库存编辑 + 上下架 + 新建 |
| `BranchTableManager` | 302 | 餐桌 CRUD + 可用性开关 |
| `StatsDashboard` | 270 | 统计 KPI + 榜单 + 每日明细 |
| `DiagnosisPanel` | 641 | 一键诊断 + 证据 + 分析结论（宿主） |
| `InvestigationPanel` | 572 | 调查 Agent：提问/多轮/工具时间线/结论分区 |
| `PeerComparison` | 254 | 同业对比 |
| `ApprovalsManager` | 139 | 礼物审批队列 |
| `AuditLogManager` | 206 | 审计日志过滤列表 |
| `diagnosisQuery.js` / `evidenceRefs.js` | 192/87 | 纯函数模块（见 §2.4） |

### 2.3 `api.js`（435）

- **baseURL**：`VITE_API_BASE || '/api'`；**token 层**：localStorage 存 `customer_token`/`staff_token`
  与缓存用户；**认证头**：仅当 `authType` 且 token 存在才注入 `Authorization`。
- **响应处理**：204/205 → `null`；JSON → `.json()`；否则 `.text()`。
- **错误处理**：非 ok 时 401 且带 `authType` → `notifyUnauthorized`；`errMsg = detail || statusText || 'An error occurred'`。
- ~40 个方法按 顾客认证 / 门店菜单 / 订单预订 / 员工认证 / 员工订单 / 审批 / 预订 / 审计 / 统计 /
  诊断 / 菜单库存 / 门店餐桌 分组。

### 2.4 三个特殊纯模块

- `utils/datetime.js`——墙钟格式化：从 ISO 串正则抽取 `YYYY-MM-DD[T ]HH:MM` **不做时区转换**，
  保证 19:00 的预订在任何浏览器时区都显示 19:00（对齐服务端 `strftime`）。
- `components/admin/diagnosisQuery.js`——诊断面板的成本控制（纯、无 React、无 api）：去抖 400ms、
  闭窗缓存 TTL 30min/开窗 60s、LRU 50 条、身份折叠进缓存键、单调票号防旧响应覆盖新响应。
- `components/admin/evidenceRefs.js`——把 Agent 的稳定 ref（`f0.e2`）解析为可读来源
  （`订单量 › 本期订单量`），锚点 id 稳定。

---

## 3. 防御性代码（按功能分组）

> 每组开头一句话回答「**这组盔甲在承兑哪个保证题**」。判定只有三类：
> **保留**（热路径 + 有测试背书）、**偏重**（有效但收益小，见 §5）、**已删**（无作用，见 §4）。
> 位置用 `file.py::symbol` 记，比行号更抗重构。

### A. LLM 输出可信 —— 保证「模型不能编造原因 / 数字 / 引用」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| 禁止因果词表 | `diagnosis.py::FORBIDDEN_CAUSE_KEYWORDS` + `_asserts_forbidden_cause` | 归因到系统无数据的天气/节假日/竞对/排班/促销/售罄 | 保留 |
| ref 校验 | `diagnosis.py::_accept_claims`、`diagnosis_investigation.py::_answer_violations` | 引用不存在证据 / 空引用 | 保留 |
| 数字白名单 | `diagnosis.py::_allowed_numbers`、`diagnosis_investigation.py::_allowed_numbers` | 出现证据之外数字 | 保留 |
| hypotheses 分桶 + 前缀 | `diagnosis.py::_accept_hypotheses` | 猜测冒充有据推断；`未经证实：` 由代码加而非模型 | 保留 |
| 事实由代码写 | `diagnosis.py::_facts` / `deterministic_facts` | 事实漂离证据 | 保留 |

### B. 小模型不可靠 —— 保证「本地 DeepSeek 输出不把请求打崩」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| think 剥离 | `llm_providers.py::strip_think_tags` | R1 的 `<think>` 推理泄漏（含未闭合） | 保留 |
| JSON 抽取 | `llm.py::extract_json_from_text` | 三段式兜底：直接 / fenced / 外层 `{...}` | 保留 |
| 结构化输出 | `diagnosis_report.py` + `harness.py::build_diagnosis_agent` | `response_format=Report` 让 LangChain 校验对象 | 保留 |
| 有限修复环 | `diagnosis_investigation.py::_repair_if_needed` | 初版报告有违规则，回传原因修一次 | 保留（偏重，见 §5） |
| 结构化截断误判 | `harness.py::_stopped_before_final_answer`（含 `_STEP_LIMIT_SENTINEL`） | 跨 LangGraph 版本「到上限不抛错」的行为差异 | 保留（偏重） |

### C. 预算与超时 —— 保证「模型死循环 / 慢模型 / 慢工具不致卡死」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| 工具预算 | `harness.py::ToolCallBudget` + `guard_tools` | 第 7 次工具调用被拒并告知模型收束 | 保留 |
| 递归上限 | `harness.py::run_agent_turn`（`recursion_limit=24`） | 兜底硬上限 | 保留 |
| 单工具超时 | `harness.py::_tool_timeout`（`asyncio.timeout`） | 一个慢探针毁掉整轮 | 保留 |
| 整轮超时 | `harness.py::run_harness_turn` | 一轮整体 deadline | 保留 |
| 工具失败转观察 | `harness.py::_tool_failure` | 单个坏探针不致整轮失败 | 保留 |

### D. 会话记忆 —— 保证「追问时它记得上一轮，且上下文不爆」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| checkpointer | `checkpointer.py` + `main.py` lifespan | 多轮调查跨会话持久化 | 保留 |
| 线程证据注册表 | `harness.py::ThreadEvidenceRegistry` | 跨轮 ref 歧义（`t1.f0.e1` ≠ `t2.f0.e1`） | 保留（偏重，见 §5） |
| 旧 checkpoint 兼容 | `harness.py::collect_thread_evidence` / `merge_thread_evidence` | 无 `thread_evidence` 通道的旧记录重建 | 保留 |
| 上下文压缩 | `harness.py::compact_context` / `build_context_middleware` | 长线程上下文超 token/字符预算 | 保留 |
| 注册表持久化 | `harness.py::build_thread_evidence_middleware`（`after_agent`） | 每轮末写回 checkpoint | 保留 |
| 读失败降级 | `harness.py::load_thread_evidence` | checkpointer 读取失败不炸有效答案 | 保留 |

### E. 并发与数据完整性 —— 保证「数据库级不变量即使 LLM 发疯也不破」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| 咨询锁 | `repo/locks.py::lock_request` | 去重检查 + 插入两步交错 | 保留 |
| 原子扣库存 | `repo/menu.py::try_decrement_stock` / `restore_stock` | 超卖 / 丢更新 | 保留 |
| 取消/反取消库存回补 | `service/order.py::_apply_status_stock_effects` | 库存漂移（反取消重新扣，售罄则 409） | 保留 |
| 去重窗口 | `service/order.py::_find_duplicate_order` + `service/reservation.py::_find_duplicate_booking` | 同一请求重复提交占两份资源 | 保留 |
| 排除约束捕获 | `service/reservation.py`（`except IntegrityError`→`ReservationRaceError`） | 双订桌竞态 | 保留 |
| 预订时间规则 | `service/reservation.py::validate_bookable_slot` + `current_wall_time` | 过去/过远/非营业时段订桌 | 保留 |
| 覆盖期锚定 | `agent/time_range.py::coverage_anchor` | 相对时间落在无数据的历史窗口 | 保留 |

### F. 权限与范围 —— 保证「门店员工只看/只动自己的店」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| 范围收敛 | `service/scope.py::ensure_branch_scope` / `resolve_staff_branch` | 越权读写（约 9 处调用点复用） | 保留 |
| JWT 解码形状检查 | `auth/security.py::decode_access_token` | 伪造/坏 token、主体类型不符 | 保留 |
| 管理员门槛 | `auth/security.py::require_admin` | 非管理员达管理路由 | 保留 |
| 审计随事务写入 | `service/audit.py::record`（不自己开事务） | 被拒操作不留下审计行 | 保留 |

### G. 供应商可用性 —— 保证「模型挂了接口不 500」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| 规则兜底 | `diagnosis.py::rule_causes` / `rule_checks` | 模型不可达仍给出可用的归因 | 保留 |
| 确定性降级 | `diagnosis_investigation.py::_fallback` | 无 provider / 异常 / 超时 / 递归上限 → 确定性诊断 | 保留 |
| provider 降级 | `llm_providers.py::rule_based_fallback` / 各 client `except` | 端点离线 / 空完成 | 保留 |
| 本地回环代理绕过 | `llm_providers.py::trust_env_for` | 系统代理对 127.0.0.1 回 502 | 保留 |
| 建模型失败早退 | `harness.py::build_diagnosis_chat_model` / `run_harness_turn` | 缺包/缺 key 不致 500 | 保留 |

### H. 工具输出有界 —— 保证「一个工具结果不撑爆上下文 / 模型窗口」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| 输出预算与截断 | `diagnosis_tools.py::_bounded_result` / `_fit_dish_daily` | 超大 payload；整条移除而非截单条 | 保留 |
| 窗口一致性 | `diagnosis_tools.py::_check_window`（`context.lock` 内） | 并行工具各定各的窗口 | 保留 |
| 工具失败转观察 | `diagnosis_tools.py::_fail` + 每工具 `except` | 单探针降级一步 | 保留 |

### I. 前端契约防御 —— 保证「坏输入 / 竞态 / 掉会话不白屏」

| 盔甲 | 位置 | 防什么 | 判定 |
|---|---|---|---|
| localStorage 解析 | `api.js::getStoredCustomer/Staff`（`try/JSON.parse`） | 损坏的本地 JSON | 保留 |
| 204/205 + content-type 分支 | `api.js::request` | 空 body 被 `JSON.parse` 抛错 | 保留 |
| 401 → 事件 | `api.js::request` + `App.jsx` 监听 | 掉会话 → 清态 + 弹登录 | 保留 |
| 竞态票号 | `diagnosisQuery.js::createRequestGuard` + `DiagnosisPanel`/`PeerComparison` | 旧响应覆盖新响应 | 保留 |
| 轮询 `cancelled` 标志 | `CheckoutModal`/`ReservationModal`/`AdminLoginModal` | 卸载后 setState / 重复 effect | 保留 |
| 空态/兜底文案 | 各组件 `|| fallback` | 缺字段不崩 | 保留（多数） |

> 前端有 3 处「信任服务端契约、无守卫」的字段访问（`DiagnosisPanel` 的 `finding.evidence`、
> `PeerComparison` 的 `result.target/peers/notes`、`OrdersManager` 的 `o.items`），以及**无请求超时 /
> 取消 / 重试、无 React 错误边界**——这些是「缺口」而非「多余」，记在此处备查，本次不改。

---

## 4. 已删除的无作用防御性代码（2026-10-21 清理）

> 判定依据：零调用 / 无实现者 / 不可达 / 仅测试引用。删除后全量回归通过。

| # | 位置 | 为何无作用 |
|---|---|---|
| 1 | `agent/harness.py::EvidenceRegistry`（Protocol） | 无实现者、无 import；生产用 `ThreadEvidenceRegistry` |
| 2 | `agent/harness.py::InMemoryEvidenceRegistry` + `EvidenceRecord` | 仅 `test_harness.py` 引用，非生产类 → 降为测试内 `InMemoryEvidenceRegistry` |
| 3 | `agent/diagnosis_investigation.py::_repair_if_needed` 的 `if model is None:` 分支 | 不可达：调用点(1028)已因 `not model_available` 提前返回；`HarnessTurn(model_available=True, …, model=model)` 保证非空 |
| 4 | `schemas/chat.py`（整文件） | `ChatRequest`/`ChatResponse` 只被 `schemas/__init__` 再导出，无任何路由/服务/agent 使用（旧聊天图已于 Task50 删除） |
| 5 | `agent/checkpointer.py::get_checkpointer_dep` / `get_checkpointer` | 无调用；路由直接读 `request.app.state.checkpointer` |
| 6 | `repo/gift.py::get_gift` / `list_gifts` | 全仓零调用（含测试） |
| 7 | `repo/approval.py::get_by_order` | 仅测试引用；测试改用 `list_by_orders` |
| 8 | `frontend/src/api.js` 6 个方法（`customerMe`/`staffMe`/`getStaffCategories`/`getStaffBranches`/`createBranch`/`updateBranch`） | `src/`、`tests/` 零调用点 |
| 9 | `frontend/src/components/customer/MenuExplorer.jsx` 的 `menuData.categories \|\| []` | 冗余：上一行守卫已保证真值 |
| 10 | 前端遗留「聊天」命名/文案 | `chat-page`/`chat-sidebar` 类名、`ask the AI`/`AI 助手` 文案——旧聊天 UI 已删除 |

> 注：删除 #8 的 3 个 branch 方法，是因为前端从未暴露门店管理 UI，属「前端能力缺口」，
> 对应的后端 `GET/POST/PATCH /staff/branches` 仍是**活**的 admin 端点，不是后端死代码。

---

## 5. 刻意保留但偏重的保证（不删，仅记录）

这些是**有效**防御，只是复杂度换来的收益较小；要引用「如何保证 X」时拿得出答案：

1. **修复环 `_repair_if_needed`**——一次修复回传，收益最小（护栏本身已能丢弃违规结论）。
2. **跨轮证据 ref `ThreadEvidenceRegistry`**——只有「追问」场景才用；无追问则白存。
3. **离线评测 `diagnosis_evaluation.py`（1278 行）+ 数据集**——无 CI，只有手动跑
   `scripts/diagnosis_eval.py` 时活；引用了 `sdd/diagnosis_eval_report.json` 的指标就留。
4. **`_STEP_LIMIT_SENTINEL` 分支**——跨 LangGraph 版本行为差异守卫，当前版本不经过它。
5. **重复的防御实现**（维护隐患，非死代码）：
   - `_NUMBER_TOLERANCE=0.011` 在 `diagnosis.py` / `diagnosis_investigation.py` / `diagnosis_evaluation.py` 各一份；
   - `diagnosis.py::_REF_PATTERN` 与 `harness.py::REF_TOKEN_PATTERN` 是同一模式的两份；
   - `DEDUPLICATE_WINDOW_MINUTES=5` 在 `service/order.py` / `service/reservation.py` 各一份；
   - `_to_date` 在 `service/stats.py` / `service/business_profile.py` 各一份。
   去重属重构（`Constitution.md` 禁止无关重构），本任务不做。

---

## 6. 复核方式

```bash
# 后端
python -m compileall -q backend/app backend/tests
python -c "import backend.app.main"                       # → IMPORT_OK
.venv/Scripts/python.exe -m pytest backend/tests -q      # 本次实测 663 passed

# 前端
npm --prefix frontend test                                # 本次实测 25 passed
npm --prefix frontend run build                           # 本次实测 成功
```

> 文档自检：§4 删除清单中的每个符号，删除后已用 `grep` 确认无残留引用（除保留的
> `ThreadEvidenceRegistry` 与降入测试的 `InMemoryEvidenceRegistry`）。

---

## 链接

- 设计决策与 bug 复盘：`ARCHITECTURE.md`
- 产品与运行说明：`README.md`
- agent 子系统深读地图：`AGENT_MAP.md`
- 分层与重构规格：`sdd/Spec.md` · `sdd/Constitution.md`
