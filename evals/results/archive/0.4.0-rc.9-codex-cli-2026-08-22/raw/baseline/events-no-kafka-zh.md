下面是一套不依赖 Kafka 的订单事件处理服务设计，核心采用 **PostgreSQL 持久化队列 + 事务性 Outbox + Worker**。它提供至少一次投递、失败重试、死信、幂等消费、可观测性和完整测试能力。

## 1. 总体架构

```text
订单服务
   │ 同一事务写入订单和 outbox_event
   ▼
PostgreSQL
   ├── outbox_events      待发布订单事件
   ├── event_queue        持久化消费队列
   ├── processed_events   幂等消费记录
   └── dead_letter_events 死信队列
          ▲
          │
     Relay Publisher
          │
          ▼
      Worker 集群
          │
          ├── 业务消费者
          ├── 失败重试
          └── 成功/失败确认
```

推荐 PostgreSQL 版本 14+。如果吞吐量较高，可将 `event_queue` 拆分到独立数据库，或替换为 RabbitMQ、SQS、Redis Streams；但接口和处理语义保持不变。

## 2. 事件模型

```json
{
  "event_id": "01J...",
  "event_type": "OrderPaid",
  "aggregate_type": "order",
  "aggregate_id": "order_123",
  "occurred_at": "2026-08-22T10:00:00Z",
  "schema_version": 1,
  "trace_id": "trace_...",
  "payload": {
    "order_id": "order_123",
    "amount": 19900,
    "currency": "CNY"
  }
}
```

关键约束：

- `event_id` 全局唯一，作为幂等主键。
- `aggregate_id + occurred_at` 支持订单维度排序和追踪。
- `schema_version` 支持事件演进。
- Payload 不包含不可重放的临时状态。

## 3. 持久化队列

### Outbox 表

订单状态变更和事件写入必须在同一个数据库事务内完成：

```sql
CREATE TABLE outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        UUID NOT NULL UNIQUE,
    event_type      VARCHAR(100) NOT NULL,
    aggregate_id    VARCHAR(100) NOT NULL,
    payload         JSONB NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ,
    attempts        INT NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_outbox_pending
ON outbox_events (available_at, id)
WHERE status = 'PENDING';
```

订单事务：

```sql
BEGIN;

UPDATE orders
SET status = 'PAID', updated_at = now()
WHERE id = :order_id;

INSERT INTO outbox_events (
    event_id, event_type, aggregate_id, payload
) VALUES (
    :event_id, 'OrderPaid', :order_id, :payload
);

COMMIT;
```

### Relay Publisher

Relay 定期批量读取 Outbox，并写入消费队列：

```sql
SELECT *
FROM outbox_events
WHERE status = 'PENDING'
  AND available_at <= now()
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 100;
```

发布成功后标记 `PUBLISHED`。如果 Relay 在写入队列后崩溃，可能重复发布，因此 `event_queue.event_id` 必须有唯一约束。

```sql
CREATE TABLE event_queue (
    id              BIGSERIAL PRIMARY KEY,
    event_id        UUID NOT NULL UNIQUE,
    event_type      VARCHAR(100) NOT NULL,
    aggregate_id    VARCHAR(100) NOT NULL,
    payload         JSONB NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'READY',
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until    TIMESTAMPTZ,
    locked_by       VARCHAR(100),
    attempts        INT NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_queue_ready
ON event_queue (available_at, id)
WHERE status = 'READY';
```

## 4. Worker 消费流程

Worker 采用租约机制，避免消息永久卡死：

```sql
WITH next_event AS (
    SELECT id
    FROM event_queue
    WHERE (
        status = 'READY'
        AND available_at <= now()
    ) OR (
        status = 'PROCESSING'
        AND locked_until < now()
    )
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE event_queue q
SET status = 'PROCESSING',
    locked_by = :worker_id,
    locked_until = now() + interval '60 seconds',
    attempts = attempts + 1
FROM next_event
WHERE q.id = next_event.id
RETURNING q.*;
```

处理成功：

```sql
BEGIN;

INSERT INTO processed_events (
    consumer_name, event_id, processed_at
) VALUES (
    :consumer, :event_id, now()
)
ON CONFLICT DO NOTHING;

-- 仅当 INSERT 真正成功时执行消费者业务逻辑
-- 或将幂等记录与业务写入放入同一事务

UPDATE event_queue
SET status = 'DONE',
    locked_by = NULL,
    locked_until = NULL
WHERE id = :queue_id;

COMMIT;
```

## 5. 幂等消费

```sql
CREATE TABLE processed_events (
    consumer_name VARCHAR(100) NOT NULL,
    event_id      UUID NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_hash   VARCHAR(128),
    PRIMARY KEY (consumer_name, event_id)
);
```

幂等策略：

1. 每个消费者拥有独立的 `consumer_name`。
2. `(consumer_name, event_id)` 唯一。
3. 幂等记录和业务副作用必须尽可能放在同一事务中。
4. 调用外部服务时使用 `event_id` 作为外部幂等键。
5. 对无法事务化的外部调用，采用“幂等键 + 状态表 + 补偿任务”。

注意：幂等表只能防止重复处理同一个事件，不能自动保证事件顺序。需要顺序时，可按 `aggregate_id` 分片，或维护订单版本号：

```text
event.version == order.last_processed_version + 1
```

## 6. 重试与死信

建议将错误分为两类：

- **可重试错误**：网络超时、数据库暂时不可用、HTTP 429、服务 5xx。
- **不可重试错误**：Schema 非法、订单不存在、业务规则冲突、权限错误。

指数退避示例：

```text
delay = min(1m × 2^(attempts-1), 30m) + random(0, 10s)
```

失败更新：

```sql
UPDATE event_queue
SET
    status = CASE
        WHEN attempts >= :max_attempts THEN 'DEAD'
        ELSE 'READY'
    END,
    available_at = CASE
        WHEN attempts >= :max_attempts THEN now()
        ELSE now() + :retry_delay
    END,
    last_error = :error,
    locked_by = NULL,
    locked_until = NULL
WHERE id = :queue_id;
```

死信表：

```sql
CREATE TABLE dead_letter_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        UUID NOT NULL,
    event_type      VARCHAR(100) NOT NULL,
    payload         JSONB NOT NULL,
    attempts        INT NOT NULL,
    error_code      VARCHAR(100),
    last_error      TEXT,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed_at     TIMESTAMPTZ
);
```

死信操作接口：

```text
GET  /admin/dead-letters
POST /admin/dead-letters/{event_id}/replay
POST /admin/dead-letters/{event_id}/discard
```

重放时必须生成审计记录，并保留原始 `event_id`，依靠幂等机制避免重复副作用。

## 7. 可靠性语义

该设计提供：

- 订单和事件原子写入。
- 队列消息持久化。
- Worker 崩溃后的租约超时恢复。
- 至少一次投递。
- 消费者级幂等。
- 最大重试次数和死信隔离。
- 可人工重放。

不承诺全局严格一次语义；严格一次通常需要端到端事务或幂等下游配合。

## 8. 指标与日志

### 指标

```text
order_events_published_total{event_type}
order_events_processed_total{consumer,event_type}
order_events_failed_total{consumer,event_type,error_type}
order_events_retried_total{consumer,event_type}
order_events_dead_total{consumer,event_type}
order_events_processing_seconds{consumer}
order_events_queue_depth{status}
order_events_oldest_age_seconds
order_events_retry_delay_seconds
processed_events_conflict_total{consumer}
```

### 日志字段

统一包含：

```text
event_id
event_type
aggregate_id
consumer_name
attempt
trace_id
queue_id
error_code
```

### 告警

- 队列深度持续增长。
- 最老消息年龄超过 SLA。
- 死信数量突增。
- 消费失败率超过阈值。
- Relay 延迟过高。
- 数据库锁等待或连接池耗尽。

## 9. 服务接口

```text
POST /orders/{id}/pay
GET  /events/{event_id}
GET  /health/live
GET  /health/ready
GET  /metrics
```

管理接口应单独鉴权，重放和丢弃操作必须记录操作者、原因和时间。

## 10. 测试方案

### 单元测试

- 事件序列化和 Schema 校验。
- 重试退避计算。
- 错误分类。
- 幂等键生成。
- 消费者业务规则。

### 集成测试

使用 Testcontainers 启动 PostgreSQL，验证：

- 订单和 Outbox 同事务提交。
- 事务回滚时不产生事件。
- Relay 重复执行不会产生重复队列消息。
- `FOR UPDATE SKIP LOCKED` 下多 Worker 不重复领取。
- 租约过期后消息可被其他 Worker 接管。
- 成功消费只执行一次副作用。
- 达到最大重试次数后进入死信。
- 死信重放可再次处理且仍保持幂等。

### 故障测试

- Worker 在业务写入后、确认前崩溃。
- Relay 在插入队列后崩溃。
- 数据库短暂不可用。
- 消费者超时。
- 外部依赖返回 429/5xx。
- 大量消息积压时的恢复能力。

### 性能测试

至少覆盖：

- 目标 TPS 下的入队延迟。
- Worker 并发度和数据库锁竞争。
- 1 小时积压后的追赶速度。
- 重试风暴下的数据库压力。
- 死信大量存在时的查询和重放性能。

## 11. 关键运行参数

```yaml
worker:
  concurrency: 16
  lease_seconds: 60
  batch_size: 100
  max_attempts: 8
  retry_base_seconds: 60
  retry_max_seconds: 1800

relay:
  batch_size: 500
  poll_interval_ms: 500
```

生产环境还应配置数据库分区或归档策略，定期归档已完成的 Outbox、队列和幂等记录，避免表无限增长。