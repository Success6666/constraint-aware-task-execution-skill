# 计费系统模块化单体设计

目标是构建一个部署单元、一个数据库实例内的模块化单体。订阅、计量、账单、支付在代码和数据访问层面隔离，通过明确的应用服务和领域事件协作，不拆分为微服务。

## 一、总体结构

建议按业务模块组织代码：

```text
billing/
  subscription/
    domain/ application/ infrastructure/ api/
  metering/
    domain/ application/ infrastructure/ api/
  invoicing/
    domain/ application/ infrastructure/ api/
  payment/
    domain/ application/ infrastructure/ api/
  shared/
    ids/ money/ clock/ events/ outbox/ errors/
```

每个模块必须遵守以下规则：

- 只能通过其他模块暴露的应用服务、查询接口或领域事件交互。
- 禁止直接访问其他模块的 Repository、ORM Model 或内部表。
- 模块内部使用事务，跨模块流程使用领域事件和状态机，不使用跨模块的大事务作为默认方案。
- 金额使用最小货币单位整数或定点十进制，禁止浮点数。
- 所有外部请求和事件处理都支持幂等。
- 关键状态变更记录审计信息和操作者来源。

## 二、订阅模块

### 职责

负责客户订阅关系和商业计划：

- 产品、价格、计费周期、试用期和折扣配置
- 客户订阅的创建、变更、暂停、恢复、取消
- 订阅生命周期和生效时间
- 计划变更的立即生效或下个周期生效策略
- 订阅项及数量，例如席位数、资源配额

不负责生成发票、收款、消费量聚合和支付重试。

### 核心实体

- `Product`
- `Plan`
- `Price`
- `Subscription`
- `SubscriptionItem`
- `SubscriptionChange`

`Subscription` 至少包含：

```text
id, customer_id, status,
current_period_start, current_period_end,
trial_end_at, cancel_at, canceled_at,
version, created_at, updated_at
```

状态建议为：`TRIALING`、`ACTIVE`、`PAUSED`、`PAST_DUE`、`CANCELED`、`EXPIRED`。

### 对外应用服务

- `CreateSubscription`
- `ChangeSubscription`
- `PauseSubscription`
- `ResumeSubscription`
- `CancelSubscription`
- `GetCurrentEntitlements`

发布事件：

- `SubscriptionActivated`
- `SubscriptionChanged`
- `SubscriptionPaused`
- `SubscriptionCanceled`
- `BillingPeriodStarted`

订阅变更必须保存版本和生效时间，避免修改历史价格导致历史账单重算。

## 三、计量模块

### 职责

负责接收、校验、去重和汇总用量：

- 接收业务系统上报的计量事件
- 按 `event_id` 幂等去重
- 校验客户、订阅、计量项和事件时间
- 支持累计型、增量型、区间型计量
- 按账期生成计量快照
- 向账单模块提供冻结后的用量数据

计量模块不决定最终应收金额，也不直接创建支付意图。

### 核心实体

- `MeterDefinition`
- `UsageEvent`
- `UsageAggregate`
- `UsageSnapshot`

计量事件至少包含：

```text
idempotency_key, customer_id, subscription_id,
meter_code, quantity, occurred_at, source, metadata
```

数据库约束：

- `idempotency_key` 全局或按客户唯一
- `(subscription_id, meter_code, period_start, period_end)` 唯一
- 数量单位和精度由 `MeterDefinition` 固定

### 处理流程

1. 接收事件并写入 `UsageEvent`。
2. 通过唯一约束完成去重。
3. 异步更新聚合，或在高一致性要求下同步更新。
4. 账单生成前冻结账期，创建不可变 `UsageSnapshot`。
5. 账期冻结后，迟到事件进入调整流程，不能静默修改已发布账单。

发布事件：

- `UsageRecorded`
- `UsageSnapshotCreated`
- `LateUsageDetected`

## 四、账单模块

这里的“账单”同时覆盖账期计算和发票生命周期，建议在模块内部再分为 `rating` 与 `invoice` 两个子域，但对外仍作为一个账单模块。

### 职责

- 根据订阅快照、价格版本和用量快照计算费用
- 处理固定费、按量费、阶梯价、包量、折扣、税费和信用额
- 生成账单草稿、最终账单和贷项/借项调整单
- 管理账单编号、金额快照和状态
- 触发收款，不直接实现支付渠道逻辑

### 核心实体

- `BillingRun`
- `Invoice`
- `InvoiceLine`
- `CreditNote`
- `PriceSnapshot`
- `TaxSnapshot`

`Invoice` 状态：

`DRAFT`、`FINALIZED`、`PAYMENT_PENDING`、`PAID`、`PARTIALLY_PAID`、`VOID`、`UNCOLLECTIBLE`。

最终化时必须保存：

- 价格和折扣快照
- 订阅项快照
- 用量快照版本
- 税率和税额快照
- 计算规则版本
- 总额、应付额、已付额

### 计费流程

1. 根据账期查找有效订阅。
2. 获取订阅、价格和用量的只读快照。
3. 计算账单明细，计算过程保持确定性。
4. 写入 `DRAFT` 发票。
5. 执行校验并原子地 `FINALIZED`。
6. 发布 `InvoiceFinalized`。
7. 通知支付模块创建收款任务。

账单计算应采用纯函数式 `RatingEngine`：输入为快照和规则，输出为明细及金额，便于重算、审计和测试。已最终化账单禁止直接更新，只能通过贷项、借项或冲销修正。

## 五、支付模块

### 职责

- 管理支付客户、支付方式引用和支付意图
- 对接支付服务商适配器
- 创建扣款、确认、退款和撤销
- 处理异步 Webhook
- 实现重试、失败分类和支付状态同步
- 将支付结果回写为账单可消费的事件

不得在系统内保存完整银行卡号、CVV 等敏感数据，只保存支付服务商 token、品牌、末四位等非敏感信息。

### 核心实体

- `PaymentCustomer`
- `PaymentMethod`
- `PaymentIntent`
- `PaymentAttempt`
- `Refund`
- `WebhookReceipt`

`PaymentIntent` 状态：

`REQUIRES_ACTION`、`PROCESSING`、`SUCCEEDED`、`FAILED`、`CANCELED`。

Webhook 处理要求：

- 以服务商事件 ID 幂等
- 先持久化原始事件，再处理业务状态
- 校验签名、时间窗口和事件类型
- 允许乱序到达，通过状态机拒绝非法回退
- 处理失败时可重试，不能丢失事件

发布事件：

- `PaymentSucceeded`
- `PaymentFailed`
- `PaymentRefunded`
- `PaymentActionRequired`

## 六、模块交互

### 创建订阅

1. 订阅模块校验客户和计划。
2. 创建订阅并写入 `SubscriptionActivated` Outbox 事件。
3. 支付模块消费事件，准备支付客户或支付方式。
4. 账单模块根据账单策略生成首期账单。
5. 支付模块创建支付意图。

### 周期计费

由调度器触发 `BillingRun`，但调度器只调用账单应用服务：

1. 订阅模块提供账期内订阅快照。
2. 计量模块冻结并提供用量快照。
3. 账单模块计算并最终化发票。
4. `InvoiceFinalized` 写入 Outbox。
5. 支付模块创建扣款任务。
6. 支付结果通过事件更新账单状态。

### 取消或变更订阅

订阅模块负责生效规则并发布事件。账单模块根据事件执行按比例计费、贷项或借项。支付模块只处理因此产生的应收或退款，不参与订阅规则判断。

## 七、事务设计

### 单模块本地事务

以下操作必须在一个数据库事务内完成：

- 订阅状态变更和订阅版本写入
- 计量事件写入及幂等键登记
- 账单草稿明细和金额写入
- 发票最终化及编号分配
- 支付 Webhook 原文保存和支付状态变更

### Outbox

每个模块在同一事务内写入业务数据和 `outbox_event`：

```text
id, event_type, aggregate_type, aggregate_id,
payload, occurred_at, published_at, retry_count
```

后台发布器投递事件，成功后标记 `published_at`。消费者使用 Inbox 或消费记录表实现幂等。事件投递至少一次，业务处理必须可重复。

### 不使用分布式事务

跨模块操作使用：

- 本地事务
- Outbox 事件
- 可重试消费者
- 状态机
- 对账和补偿任务

例如发票已最终化但支付任务失败时，发票保持 `PAYMENT_PENDING`，由重试任务补偿，而不是回滚发票最终化。

### 并发控制

- 订阅、发票、支付意图使用乐观锁 `version`。
- 账期任务使用数据库锁或唯一任务键，保证同一客户账期只执行一次。
- Webhook 和支付重试使用唯一幂等键。
- 关键状态迁移通过显式状态机校验。

## 八、数据隔离与查询

逻辑上按模块划分表前缀或 Schema，例如：

```text
subscription_*
metering_*
invoice_*
payment_*
outbox_*
```

模块不得跨表 Join。跨模块读取通过：

- 模块公开的查询服务
- 面向账单的只读快照
- 异步维护的投影表

后台报表可建立独立读模型，但不能让报表查询反向成为业务模块依赖。

## 九、失败处理与对账

必须实现：

- 支付失败分类：可重试、需用户操作、永久失败
- 指数退避和最大重试次数
- 发票与支付服务商状态对账
- 计量事件与用量聚合对账
- Outbox 堵塞告警
- 长时间 `PROCESSING` 支付扫描
- 账单金额和支付金额不一致告警
- 人工重放事件，但保留完整审计记录

## 十、测试策略

### 单元测试

- 订阅状态机和周期变更
- 计划变更的立即/周期末生效规则
- 计量去重、单位换算和迟到事件
- 固定费、按量费、阶梯价、折扣、税费和舍入
- 贷项、借项和按比例计费
- 支付状态机及失败分类
- Webhook 签名验证和乱序事件

`RatingEngine` 使用大量表驱动测试和金额边界测试，明确舍入规则、税前/税后顺序以及负数金额规则。

### 模块集成测试

每个模块使用独立测试数据库或 Schema，验证：

- Repository 持久化和唯一约束
- 本地事务回滚
- Outbox 与业务数据原子写入
- Inbox 幂等
- 并发更新和乐观锁冲突

### 契约测试

为每个公开应用服务和领域事件固定契约：

- 字段、类型、版本和必填性
- 事件重复、延迟、乱序的兼容性
- 旧事件版本的反序列化

### 端到端测试

至少覆盖：

1. 创建订阅并完成首期支付。
2. 记录用量、周期结算并生成正确发票。
3. 支付失败后重试并最终成功。
4. Webhook 重复发送和乱序发送。
5. 订阅升级、降级、取消和按比例调整。
6. 账单最终化后生成贷项/借项。
7. 计量迟到后产生调整账单。
8. 事件发布失败后的重放和恢复。

### 性能与可靠性测试

- 大量计量事件写入和聚合
- 同一账期重复触发
- 大量支付 Webhook 并发到达
- Outbox 消费积压恢复
- 数据库故障后的重试和恢复

## 十一、验收标准

- 任一模块可在不修改其他模块内部代码的情况下替换其实现。
- 重复请求、重复事件、重复 Webhook 不会产生重复订阅、发票或扣款。
- 已最终化账单可依据快照和规则版本重现计算结果。
- 任一跨模块失败都能通过重试、补偿或对账恢复。
- 所有金额、状态迁移和外部支付结果都有可审计记录。
- 测试覆盖核心规则、事务边界、幂等、并发和故障恢复，而不仅是接口成功路径。
