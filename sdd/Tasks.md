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

Task15【依赖 Task6、Task7】完善订单生命周期：下单、确认、制作、出餐、交付、取消；实现合法状态转换及取消后的库存处理。

Task16【依赖 Task15】完善订单价格与明细：支持菜品规格加价、数量计算、金额计算及订单价格快照，确保菜品后续变价不影响历史订单。

Task17【无依赖】完善预订生命周期：查询可用餐桌、预订确认、改约、取消、入座、完成；处理时间冲突及并发预订。

Task18【依赖 Task15、Task17】完善权限与审计：后端校验用户、员工及门店权限；记录订单取消、库存修改、餐桌改派等关键操作及操作者。

Task19【依赖 Task15、Task16、Task17】实现经营统计与异常处理：统计订单量、销售额及菜品销量；处理重复请求、库存不足、预订冲突及操作失败，并编写对应测试。
