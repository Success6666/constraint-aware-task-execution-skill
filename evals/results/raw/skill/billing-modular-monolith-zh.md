# 计费系统：模块化单体设计

采用**单一可部署应用、单一数据库、模块内聚、模块间通过明确接口交互**的模块化单体。订阅、计量、账单、支付是四个业务模块，不拆分为微服务。

## 1. 总体结构

```text
Billing Application
├── Subscription  订阅与套餐
├── Metering      用量接收与聚合
├── Invoicing     账单、发票与应收金额
├── Payment       支付方式、支付尝试与对账
└── Shared Kernel  租户/账户标识、货币、时间、领域事件、幂等基础设施
```

约束：

- 每个模块拥有自己的领域模型、应用服务、仓储接口和数据库表。
- 模块只能调用其他模块公开的应用服务或发布/订阅领域事件，禁止直接访问其他模块的表和内部类。
- 共享内核只放通用值对象和基础设施，不放业务规则。
- 所有金额使用整数最小货币单位，禁止使用浮点数；每条金额带币种。
- 所有外部请求支持幂等键；关键状态变更记录审计信息。

## 2. 订阅模块 Subscription

### 职责

- 管理产品、价格、计费周期、试用期和订阅。
- 处理订阅创建、变更、暂停、恢复、取消和续期。
- 计算订阅项对应的固定费用和周期边界。
- 发布订阅生命周期事件。

### 核心模型

- `Product`：产品定义。
- `Price`：金额、币种、计费周期、版本和生效时间。
- `Subscription`：账户、状态、周期起止时间、下次计费时间、默认支付方式引用。
- `SubscriptionItem`：订阅中的价格、数量、开始/结束时间。

状态示例：

```text
TRIALING → ACTIVE → PAST_DUE → ACTIVE
                     └→ CANCELED
ACTIVE → PAUSED → ACTIVE
```

### 对外接口

```text
createSubscription(command, idempotencyKey)
changeSubscription(command, effectiveAt)
cancelSubscription(subscriptionId, effectiveAt)
getEntitlements(accountId)
getBillingPeriod(subscriptionId, period)
```

取消采用“周期末取消”或“立即取消”两种明确策略；变更采用立即生效或下周期生效，不能隐式混用。

发布：`SubscriptionActivated`、`SubscriptionChanged`、`SubscriptionRenewalDue`、`SubscriptionCanceled`。

## 3. 计量模块 Metering

### 职责

- 接收产品使用量事件。
- 校验事件来源、时间、单位和幂等性。
- 按账户、订阅、指标、计费周期聚合用量。
- 为账单提供已确认的计量快照。

### 核心模型

- `UsageEvent`：事件 ID、账户、订阅、指标、数量、发生时间、来源和元数据。
- `UsageAggregate`：周期内累计量、已结算量、版本号。
- `MeterDefinition`：指标、单位、聚合方式和允许的修正规则。
- `UsageSettlement`：某账单周期被锁定的用量快照。

写入必须以业务事件 ID 做唯一约束，实现重复投递不重复计量。迟到事件按产品规则处理：账单锁定前可修正；锁定后进入调整项或下一周期。

### 对外接口

```text
recordUsage(event, idempotencyKey)
previewUsage(accountId, period)
settleUsage(subscriptionId, period)
```

发布：`UsageRecorded`、`UsageSettled`、`UsageCorrectionRequired`。

## 4. 账单模块 Invoicing

### 职责

- 根据订阅固定费用和计量快照生成账单。
- 应用阶梯价、按量价、折扣、税费、余额和调整项。
- 管理账单状态、账单明细和应收金额。
- 冻结账单内容，保证支付金额不可随意变化。

### 核心模型

- `Invoice`：账户、周期、币种、状态、金额汇总、到期日。
- `InvoiceLine`：来源类型、来源 ID、数量、单价、折扣、税额和最终金额。
- `RatingSnapshot`：生成账单时使用的价格、规则和用量快照。
- `CreditNote` / `Adjustment`：退款、冲正和人工调整。

状态示例：

```text
DRAFT → FINALIZED → PAYMENT_PENDING → PAID
                         └→ PAST_DUE
DRAFT → VOID
```

账单生成必须具备唯一键 `(subscription_id, billing_period)`；生成过程保存价格和用量快照。`FINALIZED` 后禁止修改原明细，只能通过调整单或贷项通知单修正。

### 对外接口

```text
generateInvoice(subscriptionId, period)
finalizeInvoice(invoiceId)
getInvoice(invoiceId)
listReceivables(accountId)
createAdjustment(command)
```

发布：`InvoiceFinalized`、`InvoicePaymentRequired`、`InvoiceVoided`。

## 5. 支付模块 Payment

### 职责

- 管理支付方式引用，不在业务数据库保存完整银行卡号等敏感数据。
- 创建支付尝试、调用支付服务商、处理异步通知。
- 处理授权、扣款、失败、退款、重试和对账。
- 将支付结果通知账单模块，不直接修改账单内部状态。

### 核心模型

- `PaymentMethod`：供应商令牌、类型、账户和默认标记。
- `PaymentAttempt`：账单、金额、币种、供应商、状态、幂等键。
- `PaymentTransaction`：授权、扣款、退款及供应商交易号。
- `WebhookEvent`：供应商事件 ID、原始摘要、处理状态。

支付状态示例：

```text
CREATED → PROCESSING → SUCCEEDED
                    └→ FAILED → RETRYING
```

支付供应商调用使用供应商幂等键；Webhook 以供应商事件 ID 唯一去重，并允许乱序到达。只有支付模块能解释供应商状态，账单模块只接收标准化的支付成功、失败和退款事件。

### 对外接口

```text
addPaymentMethod(command)
chargeInvoice(invoiceId, idempotencyKey)
refundPayment(paymentId, amount)
handleProviderWebhook(request)
reconcile(provider, settlementDate)
```

发布：`PaymentSucceeded`、`PaymentFailed`、`PaymentRefunded`。

## 6. 关键事务边界

### 同步本地事务

以下操作在同一数据库事务中完成：

1. 接收计量事件：写入 `UsageEvent`、更新聚合、写入幂等记录。
2. 生成草稿账单：读取已确认订阅和计量快照，写入账单及明细。
3. 最终化账单：锁定账单明细和快照，变更状态。
4. 处理支付回调：去重 Webhook、更新支付状态、写入支付事件。

使用数据库行锁或乐观锁保护订阅、用量聚合、账单和支付尝试；状态转换必须校验前置状态。

### 跨模块事务

不使用跨模块分布式事务。采用：

- 本地事务消息表 `OutboxMessage`：业务状态变更与事件写入同一事务。
- 后台发布器可靠投递领域事件。
- 消费者使用 `InboxMessage` 或业务幂等键去重。
- 事件处理失败可重试，超过次数进入死信表并报警。

典型链路：

```text
SubscriptionRenewalDue
  → Invoicing 生成并最终化账单
  → Payment 创建支付尝试并扣款
  → PaymentSucceeded
  → Invoicing 将账单标记为 PAID
```

支付扣款与账单状态更新不要求同一事务；账单保持 `PAYMENT_PENDING`，直到收到可信的支付结果。

## 7. 数据库设计原则

- 同一数据库实例，但按模块使用表前缀或独立 schema：`sub_*`、`meter_*`、`inv_*`、`pay_*`。
- 外键可指向公共账户标识，但禁止跨模块表查询作为业务耦合方式。
- 所有表包含 `id`、`created_at`、`updated_at`；关键实体包含版本号和状态变更时间。
- 金额字段包含 `amount_minor` 与 `currency`。
- 为幂等键、事件 ID、周期唯一键、供应商交易号建立唯一索引。
- 事件和审计记录采用追加写，账单和支付记录不做物理删除。

## 8. 定时任务

- 周期到期检测：发布 `SubscriptionRenewalDue`。
- 计量结算：关闭允许结算的周期并生成用量快照。
- 账单生成与最终化：按租户和周期分批执行，支持断点续跑。
- 自动扣款重试：按失败原因和退避策略执行，避免对明确不可重试的失败重复扣款。
- Webhook 重试、Outbox 投递、对账和异常账单扫描。

任务必须使用租户/周期级幂等键，并支持并发抢占，避免重复生成账单或重复扣款。

## 9. 测试与验收

### 单元测试

- 订阅状态转换、周期计算、立即/周期末变更。
- 计量去重、单位校验、迟到事件和结算锁定。
- 阶梯价、折扣、税费、跨周期和舍入规则。
- 账单最终化后不可变、调整单和退款金额校验。
- 支付重试分类、状态转换和供应商状态映射。

### 模块集成测试

使用真实数据库测试：

- 事务回滚、唯一约束、乐观锁冲突。
- 每个模块只能通过公开接口访问其他模块。
- Outbox 与业务更新原子写入。
- Inbox/Webhook/计量事件重复消费不会产生重复结果。

### 端到端测试

覆盖至少以下场景：

1. 创建订阅 → 记录用量 → 生成账单 → 扣款成功 → 账单已支付。
2. 扣款失败 → 重试 → 最终成功或进入逾期。
3. Webhook 重复、乱序和延迟到达。
4. 订阅变更、周期末取消和立即取消。
5. 账单最终化后收到迟到用量并生成调整项。
6. 重复执行周期任务不会生成重复账单或重复支付。
7. 退款、部分退款、贷项通知单和对账差异。

### 不变量与验收指标

- 同一计量事件最多计入一次。
- 同一订阅周期最多一张正式账单。
- 同一支付幂等键最多一次扣款意图。
- 账单总额等于明细、折扣、税费和调整项的可审计计算结果。
- 任意支付结果最终都能通过事件或对账收敛到正确账单状态。
- 关键事件、账单和支付状态可按账户、订阅、账单和供应商交易号完整追踪。
