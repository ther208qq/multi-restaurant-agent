

Plan - 后端与 Agent 模块化重构

1. 目录结构

新增：

repo/  
service/

按业务划分 Repo 和 Service（根据具体业务情况来，我的只是参考），例如：

repo/  
  order.py  
  reservation.py  
  menu.py  
  branch.py

service/  
  order.py  
  reservation.py  
  menu.py  
  branch.py

保留现有 "api/routes" 和 "agent" 结构，逐步迁移现有逻辑。

2. Repo 层

将现有 routes、agent/tools 中直接访问数据库的代码迁移到对应 Repo。

Repo：

+ 接收 Service 提供的 session。
+ 封装 SQLAlchemy 查询和更新。
+ 不负责事务提交。
+ 不包含业务流程。
+ 为库存扣减等操作提供语义化数据操作。

完成后验证 Repo 不依赖 Service、API、Agent。

3. Service 层

按业务建立 Service。

优先迁移订单业务：

+ 创建订单
+ 查询订单
+ 取消订单
+ 库存扣减 / 回补流程
+ 订单状态处理

Service 负责：

+ 业务校验
+ 多 Repo 编排
+ 事务 commit / rollback
+ 领域异常

完成订单 Service 后，再迁移 Reservation、Menu、Branch 等业务。

4. API 层迁移

逐个修改现有 route：

Route → Service

删除 route 中直接访问数据库的代码。

保留：

+ HTTP 参数处理
+ 鉴权
+ Schema
+ HTTP 异常映射

验证 API 行为与重构前保持一致。

5. Agent 迁移

保留现有 "agent/tools"。

将 Tool 改造成：

Agent Node → Tool → Service

删除 Tool 和 Node 中直接访问数据库的代码。

将订单、库存、菜单、预约等业务规则迁移到 Service。

Agent 只负责：

+ 理解用户需求
+ 决定业务操作
+ 组织对话流程
+ 格式化 Agent 回复
6. Auth / 基础设施

保留现有基础设施模块。

将涉及用户 / 员工数据库查询的逻辑迁移到对应 Service / Repo。

JWT 编解码、密码哈希等纯逻辑继续保留在 auth。

Checkpoint / State 保持 Agent 自有持久化机制。

7. 异常

新增领域异常模块。

Service 使用领域异常表达业务失败。

API 将领域异常映射为 HTTP 状态码。

Agent 将领域异常转换为用户可理解的回复。

8. 迁移顺序

按以下顺序增量迁移：

1. 建立 Repo / Service 基础结构
2. 建立依赖边界测试
3. 迁移 Order
4. 迁移 Reservation
5. 迁移 Menu
6. 迁移 Branch / FAQ
7. 迁移 Auth 数据访问
8. 清理旧的 DB 访问路径
9. 验证 Agent / API 共用 Service
10. 运行完整测试

每完成一个业务模块，都运行该模块测试后再继续。

9. 验证

最终验证：

+ API 不再直接访问 DB / Repo。
+ Agent 不再直接访问 DB / Repo。
+ Agent Tool 不再包含业务逻辑。
+ Service 成为 API 与 Agent 共同的业务入口。
+ 相同业务不存在重复实现。
+ 现有业务行为保持不变。
+ 静态 import 检查通过。
+ 完整测试通过。


