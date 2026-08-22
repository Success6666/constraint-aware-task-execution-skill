建议采用“单进程、单部署单元、单数据库、强模块边界”的模块化单体。四个业务模块独立封装领域模型和数据表，跨模块只能调用公开接口或消费领域事件，禁止直接访问其他模块的内部类和表。

## 总体结构

```text
billing-system/
├─ bootstrap/                 # 启动、配置、依赖装配
├─ application/               # 跨模块用例编排
│  ├─ GenerateInvoiceUseCase
│  ├─ CollectPaymentUseCase
│  └─ ChangeSubscriptionUseCase
├─ subscription/
│  ├─ api/                    # 对其他模块公开
│  ├─ application/
│  ├─ domain/
│  └─ infrastructure/
├─ metering/
│  ├─ api/
│  ├─ application/
│  ├─ domain/
│  └─ infrastructure/
├─ invoicing/
│  ├─ api/
│  ├─ application/
│  ├─ domain/
│  └─ infrastructure/
├─ payment/
│  ├─ api/
│  ├─ application/
│  ├─ domain/
│  └─ infrastructure/
└─ shared-kernel/             # Money、时间、ID、事务接口等少量稳定类型
```

依赖规则：

```text
bootstrap -> application -> 各模块 api
模块 application -> 本模块 domain
模块 infrastructure -> 本模块 domain
模块之间 -> 只能依赖对方 api
domain -> 不依赖数据库、HTTP、消息框架
```

## 模块边界

| 模块 | 负责 | 核心模型 | 拥有的数据 | 对外接口 |
|---|---|---|---|---|
| 订阅 | 客户订阅、套餐、价格版本、生效期、取消与变更 | `Subscription`、`PlanVersion`、`PricePolicy` | `subscriptions`、`plan_versions`、`subscription_changes` | 查询指定时间的有效订阅及计价快照 |
| 计量 | 接收用量、幂等去重、聚合、结算周期封账 | `UsageEvent`、`UsageAggregate`、`MeteringPeriod` | `usage_events`、`usage_aggregates`、`metering_periods` | 写入用量、查询周期用量、封账 |
| 账单 | 生成账单、税费/折扣、账单状态、贷项与作废 | `Invoice`、`InvoiceLine`、`BillingRun` | `invoices`、`invoice_lines`、`billing_runs` | 生成、查询、确认、支付确认、作废账单 |
| 支付 | 支付意图、扣款、退款、支付渠道回调、对账 | `Payment`、`PaymentAttempt`、`Refund` | `payments`、`payment_attempts`、`refunds`、`webhook_receipts` | 发起支付、处理回调、退款、查询支付结果 |

关键原则：

- 账单生成时复制套餐名称、单价、币种、税率等快照，历史账单不依赖当前套餐配置。
- 计量模块只记录“用了多少”，不决定最终应收金额。
- 支付模块只记录资金动作，不直接修改账单表。
- 账单模块是应收金额和账单状态的唯一所有者。
- `customerId`、`subscriptionId`、`invoiceId` 等跨模块只以标识符传递，不共享领域实体。
- 数据库可以共用实例，但建议按 schema 或表名前缀划分所有权，例如 `subscription.*`、`metering.*`。

## 公开接口示例

```java
public interface SubscriptionQuery {
    SubscriptionSnapshot getEffectiveSubscription(
        CustomerId customerId,
        Instant billingTime
    );
}

public interface MeteringService {
    UsageReceipt recordUsage(RecordUsageCommand command);
    UsageSnapshot closeAndGetUsage(
        SubscriptionId subscriptionId,
        BillingPeriod period
    );
}

public interface InvoiceService {
    InvoiceId generate(GenerateInvoiceCommand command);
    void confirmPayment(InvoiceId invoiceId, PaymentId paymentId, Money amount);
}

public interface PaymentService {
    PaymentId charge(ChargeCommand command);
    void handleCallback(PaymentCallback callback);
}
```

查询接口返回专用 DTO 或不可变快照，不返回模块内部聚合对象。

## 事务设计

### 1. 订阅变更

一个本地事务完成：

1. 锁定或按版本号更新订阅。
2. 校验状态迁移和生效时间。
3. 写入订阅变更记录。
4. 提交新版本。

使用乐观锁防止并发修改。套餐变更默认从明确的生效时间开始，不回写已经确认的账单。

### 2. 用量接收

一个用量事件对应一个短事务：

1. 根据 `source + idempotencyKey` 去重。
2. 写入原始用量事件。
3. 更新或追加聚合记录。
4. 提交。

数据库对幂等键建立唯一约束。已封账周期拒绝普通用量写入，迟到数据进入调整流程，不静默修改历史结果。

### 3. 账单生成

账单生成由顶层应用用例编排：

```text
读取订阅计价快照
    -> 封闭并读取计量周期
    -> 在账单事务中创建账单及明细
```

账单事务负责：

- 以 `subscriptionId + billingPeriod` 作为业务幂等键。
- 保存计价输入快照、计算结果和舍入结果。
- 原子写入账单头、明细及生成批次状态。

“封闭计量周期”和“创建账单”难以成为一个纯粹的单聚合事务时，应保证命令可重试：重复生成返回原账单，而不是创建第二张账单。

### 4. 支付

发起扣款分成两个阶段：

1. 本地事务创建 `Payment(PENDING)` 和支付尝试记录。
2. 事务提交后调用支付渠道。
3. 将渠道受理结果写回新的本地事务。

不要在持有数据库事务期间调用外部支付渠道。

支付回调处理事务：

1. 使用渠道事件 ID 幂等去重。
2. 锁定支付记录并更新状态。
3. 记录原始回调和渠道流水号。
4. 提交后发布 `PaymentSucceeded` 或 `PaymentFailed`。

账单模块消费 `PaymentSucceeded`，在自己的事务中登记收款并更新状态。事件处理器必须幂等，可使用 `eventId` 唯一约束。进程内事件应在原事务提交后分发；如果要求宕机后仍能可靠恢复，可把待发布事件与业务数据一同写入本地事件表，再由后台任务重试投递。

## 状态约束

```text
Subscription:
PENDING -> ACTIVE -> PAUSED/CANCELED/EXPIRED

MeteringPeriod:
OPEN -> CLOSED -> ADJUSTED

Invoice:
DRAFT -> ISSUED -> PARTIALLY_PAID -> PAID
              \-> VOID
              \-> OVERDUE

Payment:
PENDING -> PROCESSING -> SUCCEEDED
                      \-> FAILED
SUCCEEDED -> PARTIALLY_REFUNDED -> REFUNDED
```

状态转换由所属模块的方法完成，禁止控制器或其他模块直接更新状态字段。

## 测试策略

### 单元测试

重点测试纯领域规则：

- 订阅升级、降级、取消和生效时间。
- 阶梯价、包量、超额计费、折扣、税费和货币舍入。
- 重复用量事件去重。
- 账单状态迁移、部分支付、超额支付和退款。
- 支付回调乱序、重复和失败重试。

计价测试建议使用表驱动参数，并覆盖周期边界、时区、闰日和小数精度。

### 模块集成测试

每个模块使用真实数据库验证：

- Repository 映射和数据库约束。
- 乐观锁及并发写入。
- 唯一幂等键。
- 事务回滚。
- 模块公开 API，避免从测试绕过 API 操作内部表。

### 跨模块场景测试

至少覆盖：

1. 创建订阅 → 上报用量 → 周期封账 → 生成账单。
2. 生成账单 → 支付成功 → 账单变为已支付。
3. 支付回调重复到达，只登记一次收款。
4. 支付失败后重试，不重复生成账单。
5. 套餐中途变更，账单按有效区间正确拆分明细。
6. 迟到用量产生调整项，不修改已确认账单。
7. 任一步骤异常后重试，最终结果不重复、不丢失。

### 架构测试

自动检查：

- 模块只能引用其他模块的 `api` 包。
- 领域层不能依赖基础设施层。
- 其他模块不能引用内部 Repository。
- 数据库迁移按模块归属组织。

## 建议的首期实现顺序

1. 定义金额、周期、模块 ID 和时间语义。
2. 完成订阅快照与计量幂等写入。
3. 实现账单计算和账单生成幂等性。
4. 接入支付及回调去重。
5. 补齐跨模块场景、并发与故障恢复测试。

最终产物保持为一个代码库、一个运行进程和一个部署包；模块化通过代码依赖、公开 API、数据所有权和事务边界实现。