推荐使用 **PostgreSQL 持久化队列 + Worker 集群**。它不依赖 Kafka，适合订单事件量中等、强调事务一致性和可运维性的系统。整体语义为：**至少一次投递 + 幂等消费**。

## 一、总体架构

```text
订单服务
   │  同一数据库事务
   ├── 更新订单
   └── 写入 order_event_queue
                    │
                    ▼
             Event Worker 集群
          SELECT ... FOR UPDATE
               SKIP LOCKED
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   调用事件处理器             处理失败
        │                       │
        ▼                       ▼
  幂等记录 + 业务变更       延迟重试 / 死信
        │
        ▼
 Prometheus 指标 + 结构化日志
```

核心原则：

- 订单变更与事件写入在同一事务中，避免“订单成功但事件丢失”。
- Worker 使用 `FOR UPDATE SKIP LOCKED` 并发领取任务。
- 任务允许重复投递，消费者通过幂等键消除重复影响。
- 重试时间持久化，服务重启不会丢失。
- 超过最大重试次数后进入死信状态。
- 不在数据库事务中执行耗时的外部网络调用。

## 二、事件模型

```json
{
  "eventId": "01J...ULID",
  "eventType": "ORDER_PAID",
  "eventVersion": 1,
  "aggregateType": "ORDER",
  "aggregateId": "order-123",
  "sequence": 7,
  "occurredAt": "2026-08-16T10:00:00Z",
  "correlationId": "request-456",
  "payload": {
    "orderId": "order-123",
    "amount": 19900,
    "currency": "CNY"
  }
}
```

- `event_id`：全局唯一，也是默认幂等键。
- `sequence`：订单内单调递增，用于检测乱序。
- `event_version`：事件结构版本，方便兼容升级。
- 金额使用最小货币单位整数，避免浮点误差。
- `payload` 保存事件发生时的数据，不依赖当前订单状态反推历史。

## 三、数据库表

```sql
CREATE TABLE order_event_queue (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id         UUID NOT NULL UNIQUE,
    event_type       VARCHAR(100) NOT NULL,
    event_version    INTEGER NOT NULL,
    aggregate_id     VARCHAR(100) NOT NULL,
    aggregate_seq    BIGINT NOT NULL,
    payload          JSONB NOT NULL,

    status           VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 10,
    available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    locked_by        VARCHAR(100),
    locked_until     TIMESTAMPTZ,
    last_error_code  VARCHAR(100),
    last_error       TEXT,

    occurred_at      TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ,

    UNIQUE (aggregate_id, aggregate_seq),
    CHECK (status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'DEAD'))
);

CREATE INDEX idx_event_queue_poll
    ON order_event_queue (available_at, id)
    WHERE status = 'PENDING';

CREATE INDEX idx_event_queue_lock_recovery
    ON order_event_queue (locked_until)
    WHERE status = 'PROCESSING';

CREATE TABLE event_consumption (
    consumer_name    VARCHAR(100) NOT NULL,
    event_id         UUID NOT NULL,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    result           JSONB,
    PRIMARY KEY (consumer_name, event_id)
);

CREATE TABLE event_attempt (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id         UUID NOT NULL,
    attempt_no       INTEGER NOT NULL,
    worker_id        VARCHAR(100) NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    outcome          VARCHAR(20),
    error_code       VARCHAR(100),
    error_message    TEXT
);
```

`event_attempt`可以按月分区或设置保留周期，避免无限增长。

## 四、生产事件

订单更新和入队必须共用一个数据库事务：

```sql
BEGIN;

UPDATE orders
SET status = 'PAID',
    version = version + 1
WHERE id = :order_id
  AND status = 'PENDING_PAYMENT';

INSERT INTO order_event_queue (
    event_id,
    event_type,
    event_version,
    aggregate_id,
    aggregate_seq,
    payload,
    occurred_at
) VALUES (
    :event_id,
    'ORDER_PAID',
    1,
    :order_id,
    :order_version,
    CAST(:payload AS jsonb),
    now()
);

COMMIT;
```

如果订单数据库和事件数据库不是同一个事务域，应使用本地 Outbox 表，再由 Relay 转入队列；不要使用“先提交订单，再异步写事件”的双写方式。

## 五、领取与处理

Worker 短事务领取任务：

```sql
WITH candidates AS (
    SELECT id
    FROM order_event_queue
    WHERE status = 'PENDING'
      AND available_at <= now()
    ORDER BY available_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT :batch_size
)
UPDATE order_event_queue q
SET status       = 'PROCESSING',
    locked_by    = :worker_id,
    locked_until = now() + interval '60 seconds',
    attempts     = attempts + 1
FROM candidates c
WHERE q.id = c.id
RETURNING q.*;
```

处理流程：

1. 短事务领取任务并提交。
2. 在事务外执行处理逻辑或外部调用。
3. 成功后更新为 `SUCCEEDED`。
4. 失败后分类处理：重试、立即死信或视作成功。
5. Worker 崩溃后，由回收任务将过期租约重新置为 `PENDING`。

回收 SQL：

```sql
UPDATE order_event_queue
SET status = 'PENDING',
    locked_by = NULL,
    locked_until = NULL,
    available_at = now()
WHERE status = 'PROCESSING'
  AND locked_until < now();
```

若任务可能超过租约时间，Worker 必须定期续租，并在提交结果时校验 `locked_by`，避免旧 Worker 覆盖新 Worker 的处理结果。

## 六、重试策略

错误分三类：

| 类型 | 示例 | 策略 |
|---|---|---|
| 可重试 | 超时、连接失败、HTTP 429/503 | 指数退避 |
| 永久失败 | 参数错误、事件版本不支持、HTTP 400 | 直接死信 |
| 重复/已完成 | 下游返回已处理 | 视为成功 |

退避算法：

```text
delay = min(基础延迟 × 2^(attempts-1), 最大延迟) + 随机抖动
```

建议：

- 基础延迟：5 秒
- 最大延迟：30 分钟
- 最大尝试次数：10
- 随机抖动：`0%～20%`
- 服务端明确返回 `Retry-After` 时优先采用该值

失败更新必须校验任务所有权：

```sql
UPDATE order_event_queue
SET status = CASE
        WHEN attempts >= max_attempts THEN 'DEAD'
        ELSE 'PENDING'
    END,
    available_at = CASE
        WHEN attempts >= max_attempts THEN available_at
        ELSE :next_attempt_at
    END,
    locked_by = NULL,
    locked_until = NULL,
    last_error_code = :error_code,
    last_error = :sanitized_error
WHERE id = :id
  AND status = 'PROCESSING'
  AND locked_by = :worker_id;
```

不要把完整请求、令牌或用户敏感数据写入错误信息。

## 七、死信处理

死信可以继续保留在主表的 `DEAD` 状态，避免跨表移动带来的原子性问题。提供管理接口：

```text
GET  /admin/dead-events
GET  /admin/dead-events/{eventId}
POST /admin/dead-events/{eventId}/replay
POST /admin/dead-events/{eventId}/discard
```

重放时：

- 保留原始 `event_id`，继续享受幂等保护。
- 清空错误和锁字段。
- 重置重试次数需要显式参数。
- 记录操作者、原因、时间和重放前状态。
- 不允许直接修改原始 payload；修正数据应生成新的补偿事件。

## 八、幂等消费

数据库内业务处理应把“占用幂等键”和“业务变更”放在同一事务中：

```sql
BEGIN;

INSERT INTO event_consumption (consumer_name, event_id)
VALUES (:consumer_name, :event_id)
ON CONFLICT DO NOTHING;
```

如果插入行数为 `0`，说明已经处理，直接返回成功；否则继续业务更新并提交。

```sql
UPDATE shipment
SET paid = true
WHERE order_id = :order_id;

COMMIT;
```

对于外部 HTTP 服务：

- 将 `event_id` 放入 `Idempotency-Key` 请求头。
- 下游必须持久化该键和处理结果。
- 如果下游不支持幂等，则无法彻底解决“调用成功但本地确认前宕机”的重复副作用，需要引入可查询的操作号或补偿机制。

## 九、顺序保证

默认只保证队列整体的至少一次处理，不保证全局顺序。

若同一订单事件必须有序，可采用：

- 按 `aggregate_id` 获取 PostgreSQL advisory lock。
- 只有前一 `aggregate_seq` 已完成时才处理下一事件。
- 对乱序事件短暂延迟，持续缺失则告警并进入死信。
- 不要追求全局顺序，它会显著降低吞吐。

## 十、指标与告警

Prometheus 指标建议：

```text
order_event_enqueued_total{event_type}
order_event_processed_total{event_type,result}
order_event_retry_total{event_type,error_code}
order_event_dead_total{event_type,error_code}
order_event_processing_duration_seconds{event_type}
order_event_queue_lag_seconds{event_type}
order_event_queue_depth{status,event_type}
order_event_attempts{event_type}
order_event_worker_inflight{worker}
order_event_lease_expired_total
```

关键告警：

- 最老待处理事件延迟持续超过 SLA。
- `DEAD` 数量在短时间内增加。
- 成功率下降或重试率突增。
- 租约过期次数异常。
- 队列深度持续增长。
- 某事件类型长时间没有成功消费。

日志统一包含：

```text
event_id, event_type, aggregate_id, aggregate_seq,
attempt, worker_id, correlation_id, duration_ms, result, error_code
```

不要把 `event_id` 放到 Prometheus label 中，否则会造成高基数。

## 十一、测试策略

单元测试：

- 错误分类正确。
- 退避时间和最大重试次数正确。
- 不同事件版本正确路由。
- 重复事件不会重复产生业务副作用。
- 敏感错误信息被脱敏。

数据库集成测试：

- 订单更新与事件入队具有原子性。
- 多 Worker 并发领取时同一任务不会同时被领取。
- Worker 宕机后租约可恢复。
- 成功、重试和死信状态转换正确。
- 幂等记录和业务更新同事务提交或回滚。
- 同订单顺序约束正确。

故障注入测试：

- 外部调用前宕机。
- 外部调用成功后、本地确认前宕机。
- 数据库短暂不可用。
- 外部服务返回 429、503、400 和超时。
- Worker 在续租期间失联。
- 同一事件被并发投递多次。

端到端验收标准：

- 重复投递 100 次，业务结果仍只发生一次。
- 杀死处理中 Worker，任务最终被其他 Worker 接管。
- 持续失败的任务在规定次数后进入死信。
- 服务重启后待处理事件、重试次数和时间不丢失。
- 在目标峰值流量下，队列延迟和数据库负载满足 SLA。

## 十二、运行边界

PostgreSQL 队列适合每秒数百到数千级事件，具体上限取决于 payload、处理耗时、索引和数据库规格。应设置：

- 批量领取，但逐条确认或使用有限并发。
- 已成功事件的归档与清理策略。
- JSON payload 大小限制，例如 256 KB。
- 数据库连接池上限和 Worker 并发上限。
- 背压：队列积压超过阈值时限制非关键事件生产。
- 独立数据库角色，Worker 只拥有必要表权限。

如果吞吐进一步扩大，又仍不能使用 Kafka，可将相同契约迁移到 RabbitMQ、Amazon SQS 或 Redis Streams；订单事务一致性仍应保留 Outbox。