## 总体架构

采用**模块化单体**：一个部署单元、一个数据库、一个进程内调用模型，但通过代码和数据访问边界隔离领域模块。模块之间只依赖公开的应用服务、命令和查询接口，不直接访问其他模块的内部表或聚合。

建议分层：

```text
API / Admin / 定时任务
        |
应用层（模块用例、权限、事务编排）
        |
领域层（聚合、领域服务、业务规则）
        |
基础设施层（数据库、支付网关、消息/outbox）
```

四个核心模块：

- `Subscription`：订阅与套餐生命周期
- `Metering`：用量采集、幂等和计费量汇总
- `Billing`：账单、账单项、应收金额和结算状态
- `Payment`：支付订单、扣款、退款和支付回调

## 模块边界

### 1. 订阅模块

负责：

- 套餐、价格版本、计费周期
- 客户订阅的创建、暂停、变更、取消
- 生效时间、续费时间、试用期
- 订阅状态机：`trialing`、`active`、`paused`、`canceled`、`expired`

核心聚合：

```text
Subscription
  - customerId
  - planVersionId
  - billingCycle
  - status
  - currentPeriodStart
  - currentPeriodEnd
```

对外提供：

```text
getActiveSubscription(customerId)
changePlan(command)
cancelSubscription(command)
```

不负责计算实际用量金额，也不负责发起支付。

### 2. 计量模块

负责：

- 接收 API 调用、存储、消息数等用量事件
- 用量事件去重和顺序处理
- 按客户、订阅、计费周期汇总用量
- 保存可审计的原始用量记录

核心聚合/实体：

```text
UsageRecord
  - usageId
  - customerId
  - subscriptionId
  - metric
  - quantity
  - occurredAt
  - idempotencyKey

UsageSummary
  - subscriptionId
  - period
  - metric
  - totalQuantity
```

计量模块只输出“某周期各计量项的数量”，价格和应收金额由账单模块计算。

### 3. 账单模块

负责：

- 根据订阅和用量生成账单
- 固定费用、按量费用、折扣、税费计算
- 账单项明细和金额快照
- 账单状态：`draft`、`issued`、`partially_paid`、`paid`、`void`、`overdue`
- 账单日、到期日和重试所需的应收金额

核心聚合：

```text
Invoice
  - invoiceId
  - customerId
  - subscriptionId
  - period
  - status
  - subtotal
  - discount
  - tax
  - total
  - amountDue
```

账单必须保存套餐价格、折扣规则和用量快照，避免后续价格变更影响历史账单。

对外提供：

```text
generateInvoice(command)
issueInvoice(invoiceId)
getAmountDue(invoiceId)
markPaymentApplied(command)
```

账单模块可以读取订阅模块的公开查询接口和计量模块的周期汇总接口，但不能修改其数据。

### 4. 支付模块

负责：

- 创建支付订单
- 调用支付渠道
- 处理同步结果和异步回调
- 回调验签、幂等和状态转换
- 退款、撤销和支付失败重试

核心聚合：

```text
Payment
  - paymentId
  - invoiceId
  - amount
  - currency
  - status
  - provider
  - providerTransactionId
```

支付状态转换：

```text
pending -> processing -> succeeded
                         -> failed
succeeded -> refunded
```

支付模块不直接把账单状态改成 `paid`，而是通过账单模块的应用服务申请核销。

## 模块间协作

模块之间采用两种方式：

1. **同步调用**：需要立即得到结果的查询或命令，例如账单生成时读取有效订阅和用量汇总。
2. **进程内领域事件**：状态变化后的通知，例如：

```text
SubscriptionActivated
UsageRecorded
InvoiceIssued
PaymentSucceeded
PaymentFailed
```

事件只在单体内部发布，可先使用事务内事件表或 outbox 表，避免引入外部消息系统。未来即使拆分部署，也可以替换事件传输方式，而不改变领域接口。

## 事务边界

原则是：**一个业务不变量对应一个本地事务；跨模块流程采用最终一致性和可重试编排。**

### 单模块事务

以下操作各自使用一个数据库事务：

- 创建或变更订阅
- 写入一条幂等用量记录并更新汇总
- 创建账单及全部账单项
- 创建支付订单
- 处理支付回调并更新支付状态

### 跨模块流程

#### 周期出账

```text
定时任务
  -> Subscription 查询有效订阅
  -> Metering 查询周期用量
  -> Billing 事务生成 draft invoice
  -> Billing 事务 issue invoice
  -> 发布 InvoiceIssued
```

每一步可重试，账单使用唯一键：

```text
(subscriptionId, billingPeriod)
```

保证重复执行不会生成重复账单。

#### 自动扣款

```text
InvoiceIssued
  -> Payment 创建支付订单
  -> 调用支付渠道
  -> Payment 回调事务确认成功
  -> 调用 Billing.markPaymentApplied
  -> Billing 更新 paid / partially_paid
```

支付回调必须按 `providerTransactionId` 和幂等键去重。支付成功与账单核销不要求同一事务，但必须可重试、可对账。

#### 取消订阅

取消订阅只在订阅模块事务内完成；未结账单仍由账单和支付模块继续处理。若业务要求立即停止服务，应发布 `SubscriptionCanceled`，由权限或服务开通逻辑消费，而不是跨表强行修改。

## 数据隔离

可以共用一个数据库实例，但保持以下约束：

- 每个模块拥有自己的 schema 或表前缀：`subscription_*`、`metering_*`、`billing_*`、`payment_*`
- 禁止跨模块直接写表
- 跨模块读取通过查询服务、只读 DTO 或数据库视图
- 外键只在模块内部建立；跨模块使用业务 ID，不建立跨边界外键
- 金额使用整数最小货币单位或定点类型，禁止浮点数
- 所有状态变更记录审计日志

## 测试策略

### 领域单元测试

覆盖纯业务规则：

- 订阅状态转换和变更生效日
- 用量事件重复提交不重复计费
- 分段计价、阶梯价、折扣和税费
- 账单金额和舍入规则
- 支付状态机及非法转换

### 模块应用服务测试

使用测试数据库或事务回滚：

- 创建订阅的完整用例
- 生成账单的幂等性
- 支付回调重复到达
- 部分支付、退款和核销
- 权限、租户和数据边界

### 契约测试

验证模块公开接口稳定：

- 订阅查询 DTO
- 用量汇总 DTO
- 账单核销命令
- 领域事件载荷和版本兼容性

### 集成测试

验证数据库和外部支付适配器：

- 事务提交与回滚
- outbox 事件可靠写入
- 支付渠道验签和错误映射
- 对账任务能修复“支付成功但账单未核销”

### 端到端测试

至少覆盖：

```text
创建订阅
 -> 产生用量
 -> 周期出账
 -> 发起支付
 -> 接收支付回调
 -> 账单变为 paid
```

重点断言：

- 同一用量不会重复计费
- 同一周期只生成一张账单
- 同一支付回调只核销一次
- 失败步骤重试后最终状态正确

这样既保持单体部署和事务简单性，又通过模块边界、公开接口、幂等机制和测试体系控制复杂度。