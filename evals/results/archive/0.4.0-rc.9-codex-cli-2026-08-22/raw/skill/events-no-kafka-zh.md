下面是一套不依赖 Kafka 的订单事件处理服务设计，核心采用 **PostgreSQL 持久化队列 + Worker**。适合中等吞吐、要求可靠投递和可追踪性的订单系统；后续可将队列表替换为 RabbitMQ、SQS 等消息系统，而不改变消费协议。

## 1. 总体架构

```text
订单服务
   │
   ├─ 本地事务写入 orders
   └─ 同事务写入 outbox_events
                  │
          Outbox Relay
                  │
                  ▼
          event_queue（持久化队列）
                  │
          多个 Consumer Worker
             │          │
             │          ├─ 成功：标记完成
             │          ├─ 可重试失败：延期重试
             │          └─ 不可恢复/超限：写入 dead_letter_events
             │
        processed_events（幂等记录）
```

推荐使用 **Transactional Outbox**，避免“订单已提交但事件未发布”的双写问题。

## 2. 核心数据表

### outbox_events

```sql
CREATE TABLE outbox_events (
    id             UUID PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    payload        JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at   TIMESTAMPTZ
);

CREATE INDEX idx_outbox_unpublished
ON outbox_events (created_at)
WHERE published_at IS NULL;
```

订单创建、支付成功等业务事务中，同时插入订单数据和 Outbox 事件。

### event_queue

```sql
CREATE TABLE event_queue (
    id              UUID PRIMARY KEY,
    event_id        UUID NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,

    status          TEXT NOT NULL DEFAULT 'pending',
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count   INT NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    locked_by       TEXT,

    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX idx_queue_ready
ON event_queue (available_at, created_at)
WHERE status = 'pending';
```

状态建议：

- `pending`
- `processing`
- `succeeded`
- `dead`

### processed_events

用于消费端幂等。

```sql
CREATE TABLE processed_events (
    consumer_name TEXT NOT NULL,
    event_id      UUID NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_hash   TEXT,
    PRIMARY KEY (consumer_name, event_id)
);
```

如果同一个事件被多个业务消费者处理，应按 `consumer_name + event_id` 去重，而不是只按 `event_id`。

### dead_letter_events

```sql
CREATE TABLE dead_letter_events (
    id             UUID PRIMARY KEY,
    event_id       UUID NOT NULL,
    event_type     TEXT NOT NULL,
    payload        JSONB NOT NULL,
    attempt_count  INT NOT NULL,
    error_message  TEXT,
    failed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed_at    TIMESTAMPTZ
);
```

## 3. 投递流程

### Outbox Relay

Relay 周期性读取未发布事件：

```sql
SELECT id
FROM outbox_events
WHERE published_at IS NULL
ORDER BY created_at
LIMIT 100
FOR UPDATE SKIP LOCKED;
```

在同一事务中插入 `event_queue`，然后标记 `published_at`。插入使用 `ON CONFLICT (event_id) DO NOTHING`，因此 Relay 重启不会产生重复队列消息。

Relay 可以每 200～500 ms 执行一次，也可以使用 PostgreSQL `LISTEN/NOTIFY` 做低延迟唤醒，但 `NOTIFY` 只作为提示，不能作为可靠消息本身。

### Worker 获取任务

```sql
WITH jobs AS (
    SELECT id
    FROM event_queue
    WHERE status = 'pending'
      AND available_at <= now()
    ORDER BY created_at
    LIMIT 20
    FOR UPDATE SKIP LOCKED
)
UPDATE event_queue q
SET status = 'processing',
    locked_by = :worker_id,
    locked_until = now() + interval '60 seconds',
    attempt_count = attempt_count + 1
FROM jobs
WHERE q.id = jobs.id
RETURNING q.*;
```

这样可以支持多个 Worker 并发消费，避免同一任务被同时领取。

### 崩溃恢复

定时任务将超时任务重新放回队列：

```sql
UPDATE event_queue
SET status = 'pending',
    locked_by = NULL,
    locked_until = NULL
WHERE status = 'processing'
  AND locked_until < now();
```

`locked_until` 应覆盖正常处理时间，并配合 heartbeat 延长租约。

## 4. 重试和死信策略

将错误分为两类：

- **可重试**：数据库暂时不可用、网络超时、下游 5xx、限流。
- **不可重试**：数据校验失败、未知事件类型、业务状态非法。

指数退避示例：

```text
delay = min(60s * 2^(attempt_count - 1), 1h) + random(0, 30s)
```

建议配置：

```yaml
retry:
  max_attempts: 8
  initial_delay: 60s
  max_delay: 1h
```

处理规则：

1. 成功：`status = succeeded`，写入 `processed_events`。
2. 可重试且未超过次数：更新 `available_at`，状态改回 `pending`。
3. 超过最大次数或不可重试：事务内写入 `dead_letter_events`，队列状态改为 `dead`。
4. 死信支持人工修复后 replay，replay 时生成新的队列记录或重置原记录，并保留审计信息。

## 5. 幂等消费

消费业务逻辑和幂等记录必须在同一个数据库事务中完成：

```sql
BEGIN;

INSERT INTO processed_events (consumer_name, event_id, result_hash)
VALUES (:consumer, :event_id, :hash)
ON CONFLICT DO NOTHING;
```

如果插入影响行数为 0，说明该消费者已处理过，直接提交并将队列标记成功。

首次处理时：

```sql
-- 业务更新
UPDATE inventory
SET reserved = reserved + :quantity
WHERE order_id = :order_id;

-- 幂等记录已在本事务中插入
COMMIT;
```

如果业务更新失败，整个事务回滚，幂等记录不会残留，任务可以安全重试。

对于调用外部系统的场景，仅依赖本地幂等表不够，应：

- 使用下游支持的幂等键，例如 `event_id`；
- 保存请求状态和响应；
- 对超时采用查询确认，而不是盲目重复扣款或发货。

## 6. 事件契约

事件至少包含：

```json
{
  "event_id": "uuid",
  "event_type": "OrderPaid",
  "aggregate_id": "order-123",
  "occurred_at": "2026-08-22T10:00:00Z",
  "schema_version": 1,
  "trace_id": "trace-456",
  "payload": {}
}
```

要求：

- `event_id` 全局唯一；
- `schema_version` 用于向后兼容；
- 消费者必须忽略未知字段；
- 事件不可修改，只能追加新版本。

## 7. 指标和可观测性

### 指标

- `events_published_total`
- `events_consumed_total{consumer,event_type,result}`
- `events_retry_total{reason}`
- `events_dead_total{event_type}`
- `queue_depth{status}`
- `queue_oldest_age_seconds`
- `event_processing_duration_seconds`
- `event_attempts`
- `outbox_unpublished_count`
- `lock_timeout_total`

重点告警：

- 队列最老消息年龄持续超过阈值；
- 死信数量突增；
- Outbox 未发布数量持续增长；
- 消费成功率下降；
- 重试比例异常升高。

### 日志和链路

每条日志包含：

```text
event_id, event_type, order_id, consumer_name,
attempt_count, trace_id, worker_id
```

使用 OpenTelemetry 将订单请求、Outbox Relay、Worker 消费串联起来。

## 8. API 和运维能力

建议提供内部管理接口：

```text
GET  /internal/events/{event_id}
GET  /internal/queue/stats
GET  /internal/dead-letters
POST /internal/dead-letters/{id}/replay
POST /internal/events/{id}/retry
```

Replay 需要权限控制、操作人、原因和审计记录，并限制批量重放速率。

## 9. 测试策略

### 单元测试

覆盖：

- 重试退避计算；
- 可重试/不可重试错误分类；
- 事件 schema 校验；
- 幂等判断；
- 最大重试次数；
- 死信转换逻辑。

### 集成测试

使用真实 PostgreSQL 容器验证：

- 订单事务和 Outbox 同步提交；
- Relay 重启不会重复插入；
- 多 Worker `SKIP LOCKED` 不重复领取；
- Worker 崩溃后任务自动恢复；
- 业务事务失败时幂等记录回滚；
- 并发重复事件只执行一次；
- 死信 replay 后可再次处理。

### 端到端测试

验证完整链路：

```text
创建订单
 → 生成 OrderCreated
 → Relay 入队
 → Consumer 处理
 → 业务状态更新
 → 指标、日志和链路可见
```

同时测试下游超时、数据库断开、进程重启、重复投递和毒性消息。

## 10. 关键取舍

PostgreSQL 队列的优点是部署简单、事务一致性强、易于审计和回放；缺点是吞吐受数据库容量和锁竞争限制。可以通过分区表、批量领取、合理索引和按事件类型拆分消费者改善性能。

当吞吐或跨团队订阅规模显著增长时，可把 `event_queue` 替换为其他持久化消息系统，但保留 Outbox、幂等表、重试策略、死信协议和指标定义。