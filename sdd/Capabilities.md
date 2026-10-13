# 能力清单：前端操作 & Agent 工具（2026-10-12 整理）

> 用途：查阅「界面能点什么」与「Agent 能调什么」，以及两者如何落到底层接口。
> 相关：待修问题见 [`OpenItems.md`](./OpenItems.md) 第六节；分层约束见 `../ARCHITECTURE.md`。

## 0. 前提：唯一交易入口是聊天

**前端没有任何「下单」或「订位」表单。** 订单与预订只能由 Agent 经 `POST /api/chat` 创建，返回 `order_id` / `reservation_id`。所以 Agent 不是辅助功能，而是核心交易通道。

所有 HTTP 经 `frontend/src/api.js` 的 `request()` 封装，前缀 `/api`，按 `customer` / `staff` 自动附带 `Authorization: Bearer`。会话只靠 localStorage 恢复，**不校验 token**（见 OpenItems #3）。

# 一、前端能力

## 1. 顾客侧

| 界面 | 操作 | 接口 |
|---|---|---|
| 导航栏 | 切换顾客/后台视图、登录注册、打开「我的记录」、退出 | 无（本地状态 / 清 localStorage） |
| 门店选择 | 列出营业中门店；下拉选中（联动刷新菜单与 FAQ） | `GET /branches` |
| 菜单浏览 | 按分类筛「全部/披萨/…」、查看库存与售罄标记 | `GET /branches/{id}/menu`（纯前端筛选） |
| ↳「让 AI 下单」 | 预填 `我想点 1 份 {菜名}` 到聊天框 | 无直接请求，最终走 `POST /chat` |
| 聊天 | 自由输入；5 个固定快捷词；显示订单/预订徽章；重置会话 | `POST /chat`（body `{message, branch_id}`） |
| 登录 / 注册 | 邮箱密码登录；注册（姓名/邮箱/电话可选/密码） | `POST /auth/login`、`POST /auth/register` |
| 我的记录 | 「我的订单」/「我的预订」双 tab + 刷新 | `GET /orders/me`、`GET /reservations/me` |

**5 个聊天快捷词**（`ChatWidget.jsx` 固定字符串）：
`菜单上有什么？` · `今晚 7 点订一张 4 人桌` · `点 2 份玛格丽特披萨` · `你们在哪里？营业时间是？` · `查一下我最近订单的状态`

聊天**必须登录**：未登录时点下单或快捷词只弹登录框，不发请求。

## 2. 员工 / 管理侧

| 界面 | 操作 | 接口 |
|---|---|---|
| 员工登录 | 用户名密码登录；界面提示种子账号 | `POST /staff/login` |
| 实时订单 | 按门店/状态筛选、刷新 | `GET /staff/orders?branch_id=&status=` |
| ↳ 状态流转 | 已下单→开始制作→标记已就绪→完成/已送达 | `PATCH /staff/orders/{id}/status` |
| ↳ 取消订单 | 非终态可取消（带 `confirm()`） | 同上 `{status:'cancelled'}` |
| 餐桌预订 | 按门店/状态/日期筛选、刷新 | `GET /staff/reservations?branch_id=&status=&date=` |
| ↳ 状态流转 | 待确认→确认→入座→完成；可取消；可恢复已取消 | `PATCH /staff/reservations/{id}` |
| ↳ 改约 | 改时间/人数/时长/座位区（连带重新选桌） | 同上（`start_time` / `party_size` / `duration_minutes` / `seating_area_preference`） |
| ↳ 查空桌 | 按门店+日期+时间+人数列可订餐桌（**限本门店**） | `GET /staff/reservations/availability` |
| ↳ 改派餐桌 | 弹窗选新餐桌后确认（已入座也可改派） | `GET /staff/branches/tables` + `PATCH /staff/reservations/{id}` `{table_id}` |
| 菜单与库存 | 在售/停售切换、改库存、快捷 +10/+50、清零 | `PATCH /staff/menu/items/{id}`、`PUT /staff/menu/stock` |
| ↳ 新建 | 新建分类；新建菜品（含初始库存） | `POST /staff/menu/categories`、`POST /staff/menu/items` |
| 门店与餐桌 | 查看餐桌；启用/停用；添加餐桌 | `GET/PATCH/POST /staff/branches/tables` |
| 操作日志 | 按门店/操作类型筛选，看谁在何时改了什么 | `GET /staff/audit-logs` |
| 经营统计 | 选门店/日期区间（或 7/30/90 天快捷），看订单量、销售额、客单价、销量、取消数；菜品销量排行（条形图）与每日明细 | `GET /staff/stats` |

**订单状态机**：`placed → confirmed → preparing → ready → delivered`，任意非终态可 `cancelled`（`cancelled → placed` 仅员工撤销误操作）。
**预订状态机**：`pending → confirmed → seated → completed`，任意非终态可 `cancelled`，`cancelled → pending` 可恢复（恢复前会校验原时段是否仍空闲）。
**占用语义**：`pending` / `confirmed` / `seated` 三种状态都**占住餐桌**——数据库排他约束与应用层查空桌用的是同一套「占用状态」；只有取消或完成才释放时段。

**权限与审计（Task18）**：
- **门店权限**：带 `branch_id` 的员工只能看/改本门店数据（订单、预订、菜单、库存、餐桌、操作日志），请求里带别的门店 id 会被忽略或拒绝（`403 您没有权限访问该门店。`）；`branch_id` 为空的超级管理员不受限。
- **角色权限**：新建/修改门店需 `role=admin`（`403 需要管理员权限`），其余后台操作员工即可。
- **审计**：下单、订单状态变更（含取消）、预订创建/状态变更/改约/改派餐桌、库存调整、菜品与分类增改、门店与餐桌增改，都会连同**操作者**（类型、id、用户名/姓名、角色）与**变更前后值**写入 `audit_logs`；写入与业务操作同一事务，操作失败或被拒绝时不留审计行。操作日志页只读。

**经营统计与重复请求（Task19）**：
- **统计口径**（`StatsService`，API 与将来的 Agent 工具共用）：
  - **已取消订单不算销售**——不计入订单量、销售额、销量与菜品排行，单独用 `cancelled_count` 汇报；取消后又被恢复（`cancelled → placed`）的订单重新计入。
  - **一天 = 一个 UTC 日**（库里是 `timestamptz`，聚合前先转 UTC，否则会随服务器时区漂移）；区间为左闭右开的 UTC 窗口，`end_date` 当天**包含在内**，默认最近 7 天，最长 366 天。
  - 销售额与菜品排行用订单行上的**快照单价**（含规格加价的落库价），历史订单不受后续改价影响。
  - 门店收敛同所有员工接口：带门店的员工只能统计本门店，统计别的门店返回 `403`；不带门店的超级管理员必须显式传 `branch_id`。
- **重复请求**：Agent 是唯一可能重复提交的入口（消息发两遍、请求重试、小模型一轮里连调两次工具）。订单与订位两个工具都以 `deduplicate=True` 提交，**仍在进行中的同一请求被重复提交时，返回已存在的那一单/那一笔**（不新增行、不重复扣库存、不重复记审计，两次回复一致）。
  - 判定「同一个请求」：**订单**看内容（门店 + 用餐方式 + 配送地址 + 菜品行：菜品、数量、规格名、行备注）；**订位**看请求本身（同一顾客 + 同一时段 + 同一人数 + 同一时长）——订位没有确认环节，且排除约束拦不住它（重复提交会落到**另一张**空桌，等于给一桌人占两张桌）。
  - 窗口 `DEDUPLICATE_WINDOW_MINUTES = 5`，且限「仍未完结」：订单已取消/已送达、订位已取消/已完成时，再次提交同样内容会新建一单/一笔。
  - 查重与写入之间用 **PostgreSQL 顾问锁**（`pg_advisory_xact_lock`，按请求而不是按顾客上锁）串行化，避免两次并发提交互相看不到对方；锁随事务提交/回滚自动释放，不相关的订单仍可并发下单。
- **前端**：`AdminLayout` 新增「经营统计」页签，`StatsDashboard` 支持门店/日期区间/快捷区间/排行条数，展示 KPI 卡片（订单量、销售额、客单价、销量、取消订单）、菜品销量排行条形图与每日明细表。

## 3. 前端缺口（有 API 无界面）

| 能力 | 接口 | 现状 |
|---|---|---|
| 新建门店 | `POST /staff/branches` | `api.js` 有 `createBranch`，**无任何 UI** |
| 编辑门店 | `PATCH /staff/branches/{id}` | 有 `updateBranch`，**无 UI**。名称/地址/营业时间/电话/FAQ 均改不了 |
| 会话校验 | `GET /auth/me`、`GET /staff/me` | 定义了但**无人调用**（死代码） |
| 列分类 | `GET /staff/menu/categories` | 定义了但无人调用，`MenuManager` 改用公开的 `GET /branches/{id}/menu` |
| 列门店（员工） | `GET /staff/branches` | 定义了但无人调用，`AdminLayout` 复用公开 `GET /branches` |
| 操作日志按操作筛选 | `GET /staff/audit-logs?action=` 等 | `AuditLogManager` 已用（门店/操作/条数筛选）；`entity_type` / `entity_id` 参数暂无界面 |

其他：菜品只能改「是否在售」与库存，**改不了名称/描述/价格/规格**；分类、菜品、餐桌**均无删除接口**；后台**不能手工建单或建预订**。新部署只能依赖种子数据。
经营统计页无导出（无 CSV/打印），排行条形图是纯 CSS 实现（未引入图表库）；统计按 **UTC 日**分桶，本地时区门店的「今天」可能与之相差一天。


---

# 二、Agent 能力

## 4. 图结构（单趟路由，非循环）

    entry -> intent_router
                |
    +-----------+-----------+---------+--------+
    v           v           v         v        v
  branch_    order     reservation  status    faq
  selector

每个分支节点均直连 END。

多轮填槽不靠图内循环，而是靠 Postgres checkpointer 持久化草稿，下一轮重新灌回 state。
线程 id 为 `customer_{id}_branch_{bid}`，**记忆按门店隔离**，同一会话内换不了门店。

**意图路由**先走关键词正则快路径，未命中才调 LLM。优先级 status > order > reservation > faq
（status 排最前，避免「我的订单在哪」被当成重新下单）。有待处理草稿时：本条是取消词则清草稿；
未命中其他意图则继续原流程；命中新意图则切换但**保留旧草稿**（`DRAFT_TTL_MINUTES = 15` 过期）。

## 5. 节点 → 工具

| 节点 | 工具 | 决策要点 |
|---|---|---|
| `branch_selector` | 无（直调 `BranchService`） | 按 id / 序号 / 全名 / 名称词匹配门店；未命中则列出门店 |
| `order_node` | `get_branch_menu_data`、`format_menu_for_prompt`、`create_order_atomic`、`OrderService.preview_order` | 菜单浏览 → LLM 抽菜品 → 正则兜底 → 库存预览 → **摘要待确认**；只有用户回「确认」才落库 |
| `reservation_node` | `check_table_availability_and_book` | 抽人数/日期/时间/区域（含中文「后天 7 点半」等正则兜底）→ **落库为 `pending`**，回复「预订已提交，等待门店确认」；确认由员工在后台完成 |
| `status_node` | 无（直调两个 Service） | 只查本人 + 本门店最近 3 单 3 订，只读 |
| `faq_node` | `get_branch_faq` | 营业时间/地址/电话/停车/着装/支付/配送范围 |

## 6. 工具签名

所有工具都是**薄适配层**：不碰 SQL、不开 session，一律下沉到 `service/`。
订单与订位两个落库工具都以 `deduplicate=True` 调用 Service（重复请求见下）；直接调 Service
的其它入口（员工接口、测试）保持原语义，不做去重。

    # tools/menu_tools.py
    async def get_branch_menu_data(branch_id: int) -> Dict[str, Any]
    async def format_menu_for_prompt(branch_id: int) -> str

    # tools/order_tools.py
    async def create_order_atomic(
        customer_id: int, branch_id: int,
        order_type: str,                # dine_in | takeaway | delivery
        items: List[Dict[str, Any]],    # menu_item_id, quantity, selected_variants?, item_notes?
        delivery_address: Optional[str] = None,
        special_notes: Optional[str] = None,
    ) -> Dict[str, Any]

    # tools/table_tools.py
    async def check_table_availability_and_book(
        customer_id: int, branch_id: int, start_time: datetime,
        party_size: int, duration_minutes: int = 90,
        seating_area: Optional[str] = "indoor", notes: Optional[str] = None,
    ) -> Dict[str, Any]

    # tools/faq_tools.py
    async def get_branch_faq(branch_id: int) -> Dict[str, Any]

## 7. 订单 vs 订位：关键差异

**订单是两步的**：先出摘要 + 报价，等用户回「确认」才写库（落库为 `placed`）。
**订位是直接落库的**：抽齐字段就写库为 `pending`，**顾客侧没有确认环节**；「预订确认」是**员工侧**的动作（`pending → confirmed`），之后才入座、完成。

**并发保护不同**：订单靠行锁原子扣库存；订位靠 PostgreSQL 排除约束（GIST，覆盖 `pending`/`confirmed`/`seated`），撞车抛
`ReservationRaceError` 并回中文提示；改约/改派还会先探测目标餐桌并给出更具体的提示。

**重复提交保护（Task19）**：两者都由工具侧开 `deduplicate=True`，5 分钟窗口内、请求仍未完结时重复提交返回原记录
（订单按内容判定、订位按时段+人数判定；顾问锁串行化并发提交）。注意订单工具会**先预览**——
若首次提交已把库存用尽，第二次提交会先被预览判为「库存不足」，而不是返回原订单（见 `OpenItems.md` 第九节）。

## 8. Agent 做不到的事

- **不能取消已下单的订单。** `OrderService.update_status` 支持 `cancelled` 且会回滚库存，但只挂在员工路由上，Agent 无对应工具。聊天里的「取消」只能丢弃**未落库的草稿**。
- **不能改或取消预订。** `ReservationService.update_reservation`（含改约、改派餐桌、状态流转）是员工专用；落库后顾客只能新建一笔。
- **不收付款。** 一律回复「到店/送达时现金或刷卡」，Agent 从不调用支付服务。
- **改不了餐桌、换不了门店**（线程按门店隔离）。
- **只能看自己的数据**，没有按单号查他人订单的能力。
- **确认措辞有限**：只认「确认/下单/好的/就这些」等词，换个说法可能不被识别。

## 9. LLM 抽象与降级

节点/工具统一经 `agent/llm.py` 的 `llm_client.generate(prompt, system_prompt, temperature=0.1)`，
由 `build_provider()` 按 `LLM_PROVIDER` 选择：

- `OllamaClient`（默认）—— POST `{OLLAMA_BASE_URL}/api/chat`。
- `OpenAICompatibleClient` —— 官方 `AsyncOpenAI` SDK，覆盖 OpenAI / DeepSeek / Qwen / Moonshot / Groq / OpenRouter / vLLM / LM Studio，经 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` 配置。`max_retries=2`，超时 45s。

两者都用 `trust_env_for(base_url)` 让**回环地址绕过系统代理**（本机代理对 localhost 端口回 502）。

**降级（`rule_based_fallback`）**：HTTP 非 200、异常、空回复、或 `OPENAI_API_KEY` 为空时触发。

> ⚠️ 这里有个实质缺陷，见 OpenItems #2：降级用英文字面量 `"classify intent"` 判断是否走意图分类分支，
> 而真实节点发的是**中文**提示，所以断网时路由拿不到 JSON、意图落到 `general` 只回问候语。
> **离线时关键词快路径是唯一可靠的分类器**；订单/预订节点则退到各自的**正则兜底**。
