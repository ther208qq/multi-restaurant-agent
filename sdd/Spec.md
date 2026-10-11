# Spec — 后端与 Agent 模块化重构（澄清版）

> **本文件为本次重构的唯一权威规格。**
> 原始 `spec.md` 曾同时包含「中文简版 + 英文详版」两份内容（互为重复），本文件已合并为单一版本；两份若有出入，以本文件为准。
> 澄清决议见 §7；默认裁定与待确认项见 §8。

## 1. 背景

目前项目后端与 Agent 耦合较高，模块职责和边界不清晰：

- **API 层直接访问数据库**：`routes/*`（如 `staff_orders.py`、`staff_reservations.py`、`branches.py`）直接用 SQLAlchemy `select/update` 操作 DB。
- **Agent 直接访问数据库**：`agent/tools/*` 与部分 `agent/nodes/*`（`status_node.py`、`branch_selector.py`）用 `AsyncSessionLocal()` 直连 DB。
- **API 反向依赖 Agent**：`routes/branches.py` 直接 import 并调用 `agent/tools/*` 的函数。
- **同一业务重复实现**：库存扣减/回补逻辑同时存在于 API（`staff_orders.py`）与 Agent（`order_tools.py`），价格/总价计算亦各算一次。

本次重构需要：

- 将后端拆分为 **Repo、Service、API** 三层；
- 将 **Agent 与数据库解耦**；
- 明确各层/模块职责与调用边界；
- 保证 **API 与 Agent 复用同一套业务能力**。

## 2. 目标架构

数据流（上层可调用下层，反向禁止）：

- API 层 -> Service 层
- Agent 模块 -> Tool 薄适配层 -> Service 层
- Service 层 -> Repo 层 -> Database

横切/基础设施模块（可被各层依赖）：auth（纯 JWT/哈希）、schemas、checkpointer/state、config、database。

Service 是 API 与 Agent 唯一共享的业务能力入口。

## 3. 术语与边界定义（本次澄清，消除原 spec 歧义）

### 3.1 「接口」的定义

「接口」= **Python 模块对外公开的函数 / 类方法**，通过 `import` 调用。

- 不引入 ABC / Protocol / 依赖注入框架。
- 边界通过「目录 / 包边界 + 静态 import 检查」落地，而非抽象基类。

### 3.2 Session 与事务归属

- **Service 层持有事务边界**：开启、`commit`、`rollback` 都由 Service 负责。
- **Repo 只接收 Service 传入的 session** 并执行数据操作；Repo 不自建 session、不 `commit`。
- API 通过依赖注入把 session 交给 Service；Agent 无 HTTP 上下文，由 Service 自行创建 session（内部 session 工厂）。

### 3.3 Tool 的处理

保留 `agent/tools`，但降级为**薄适配层**：调用形态为 Agent node -> Tool -> Service。

- Tool 不含业务规则、不访问 DB / Repo。
- Tool 只做参数适配与结果格式化。

### 3.4 Service 与 Agent 的职责边界

- **Agent 决定「做什么」**：理解用户需求、决定调用哪些 Service 能力、组织对话。
- **Service 决定「怎么合法地做」**：业务规则、校验、事务、流程编排。

### 3.5 基础设施 / 横切模块归类

`auth`、`schemas`、`checkpointer`/`state`、`config`、`database` 不属三层，归类为**基础设施/横切模块**，可被各层依赖，但：

- 纯逻辑部分（JWT 编解码、密码哈希）保留在 `auth`。
- **对用户的 DB 查询 / 凭据校验下沉到 Service**（见 §8 默认裁定 D3）。
- database 负责数据库基础设施（engine/session factory 等），不属于业务数据访问层；业务数据访问必须通过 Repo。

### 3.6 原子扣库存的归属

- **Repo 提供语义化的原子数据操作**（如 `try_decrement_stock`、`restore_stock`），其内部可编码「条件满足才执行」的原子性（`UPDATE ... WHERE stock >= qty`）。
- **Service 决定业务时机与流程**（何时扣、扣多少、失败如何处理）。
- Repo 不编排业务流程；Service 不直接拼 SQL。

## 4. 各层 / 模块职责

### 4.1 Repo 层

- 只访问数据库（且只使用 Service 传入的 session）。
- 提供**语义化数据操作接口**给 Service。
- 不含业务规则、不编排流程、不自建 session、不 `commit`、不抛 HTTP 异常。
- 「条件不满足」用返回值（`bool` / `rowcount`）表达，而非异常或业务分支。

### 4.2 Service 层

- 承载业务规则、校验、**事务边界**、多 Repo 编排。
- 抛出**领域异常**（见 §5），不依赖 FastAPI/HTTP。
- 对外公开业务方法，供 API 与 Agent **复用同一实现**。

### 4.3 API 层

- 接收 / 校验请求（schema）、鉴权（Depends）。
- 调用 Service，并把领域异常映射为 HTTP 状态码。
- 不直接访问 DB / Repo，也不调用 Agent。

### 4.4 Agent 模块

- 理解需求、决定执行哪些业务操作，组织对话回复。
- 经 Tool 薄层调用 Service。
- 不访问 DB / Repo / API，**不向其他模块提供接口**。
- 内部业务计算（如库存预校验、价格 / 总价估算）**下沉到 Service**，Agent 只做对话组织。

## 5. 异常处理（补全原 spec 空缺的「异常」一节）

- 定义**领域异常**（独立于 web 框架），例如：`NotFoundError`、`ValidationError`、`ConflictError`、`ServiceError`。
- Service 负责将业务失败转换为领域异常；Repo 原则上不抛业务领域异常，仅返回数据访问结果或底层数据库异常。
- **API 层统一异常处理器**负责映射：`NotFoundError -> 404`，`ValidationError -> 400`，`ConflictError -> 409`，其余 -> `500`。
- **Agent 层捕获领域异常**，转为自然语言回复，不向用户泄露内部细节。
- 关键并发冲突（库存不足、时间冲突）沿用既有的 `409 Conflict` 语义。

## 6. 验收标准（可验证）

- **Repo**：只访问数据库；不调用 Service / API / Agent；只向 Service 提供接口。
- **Service**：只通过 Repo 访问数据；持有事务边界；只向 API 与 Agent 提供业务接口；不依赖 FastAPI。
- **API**：只调用 Service；不直接访问 DB / Repo / Agent。
- **Agent**：只经 Tool 调用 Service；不访问 DB / Repo / API；不对外提供接口。
- **整体**：
  - 相同业务只有一份核心实现。
  - API 与 Agent 执行相同业务时复用同一个 Service 方法。
  - **依赖边界通过测试验证**（见 §8 默认裁定 D4）。

## 7. 澄清决议记录

本次重构已就以下 6 项做出决议（消除原 spec 的歧义与冲突）：

| # | 待决问题 | 决议 |
| - | --- | --- |
| 1 | 「接口」到底是什么 | Python 模块公开函数 / 类方法 + import 边界，**不引入** ABC / Protocol |
| 2 | Session / 事务谁管理 | **Service 管事务**，Repo 使用 Service 提供的 session |
| 3 | Agent 的 Tool 怎么处理 | **保留 Tool**，但 Tool 变成薄适配层：Agent -> Tool -> Service |
| 4 | Service 与 Agent 的职责边界 | Agent 决定「做什么」，**Service 决定「怎么合法地做」** |
| 5 | Auth / Schema / Checkpoint 怎么归类 | 单独定义为**基础设施 / 横切模块**，不强行塞进三层 |
| 6 | 原子扣库存属于谁 | **Repo 提供语义化原子数据操作**，Service 决定业务时机和流程 |

## 8. 默认裁定与待确认项

以下为原 spec 未定义、本次采用**默认裁定**的项（如不同意请指出，将据以修订）：

- **D1 Service 方法粒度**：按业务用例聚合（如 `get_branch_menu(branch_id)` 返回聚合结果），API 与 Agent 复用同一业务方法；Service 返回业务层结果，由不同入口分别进行适配。
- **D2 只读查询**：所有数据访问（含只读）都经 Service 方法，API / Agent 不直接查 DB。
- **D3 auth 的 DB 访问**：JWT / 密码哈希等纯逻辑属基础设施；对用户的**查询与凭据校验下沉到 Service**，以保证「只有 Repo 访问 DB」不变量成立。
- **D4 边界测试手段**：采用**静态 import 检查**（import-linter，或自写 pytest 扫描模块 import）验证依赖方向。
- **D5 重构方式**：**增量推进**——先建 Repo / Service 骨架与边界测试，再逐模块迁移；全程保持 DB 级不变量（exclusion constraint、原子扣减）有效。
- **D6 行为保持**：迁移只做**搬运不改行为**；既有并发语义（如取消 / 反取消的 `409`）保持等价。
- **D7 范围**：本次仅重构后端；尽量保持 API 响应兼容，避免波及前端。
