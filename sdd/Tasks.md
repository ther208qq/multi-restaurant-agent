Task1【无依赖】搭建 Repo / Service 基础结构、领域异常与会话/事务边界，并建立架构边界测试。

Task2【依赖 Task1】实现 Branch、Menu 与 FAQ 的 Repo/Service，统一菜单可用性规则。

Task3【依赖 Task2】将 Branch / Menu / FAQ 相关 API 迁移到 Service。

Task4【依赖 Task2】将 Agent 中 Branch / Menu / FAQ 的数据访问迁移到 Service，工具降为薄适配层。

Task5【依赖 Task1】实现 Order Repo 与库存原子操作 Repo。

Task6【依赖 Task5、Task2】实现 OrderService（下单、价格预估、状态流转、查询）。

Task7【依赖 Task6】将订单相关 API 与 Agent 下单工具/节点迁移到 OrderService。

Task8【依赖 Task1、Task2】实现 Reservation Repo/Service。

Task9【依赖 Task8】将预订相关 API 与 Agent 预订工具/节点迁移到 ReservationService。

Task10【依赖 Task1】实现 Auth Repo/Service。

Task11【依赖 Task10】将 Auth API 及鉴权依赖中的数据库访问迁移到 AuthService。

Task12【依赖 Task6、Task8】将客户活动查询（customer_orders API 与 status_node）迁移到 Order/Reservation Service。

Task13【依赖 Task3、Task4、Task7、Task9、Task11、Task12】清理残留的旧数据库访问与重复业务逻辑。

Task14【依赖 Task13】运行架构边界测试与全量回归测试。
