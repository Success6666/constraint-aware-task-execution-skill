建议采用 **PostgreSQL 持久化事件表 + Worker 租约消费 + Outbox** 的方案。它部署简单，能够完整覆盖可靠入队、并发消费、失败重试、死信、幂等和可观测性。

```text
订单服务事务
  ├─ 更新订单
  └─ 写入 order_event
          │
          ▼
   Worker 并发领取事件
          │
     ┌────┴────┐
     ▼         ▼
 处理成功    处理失败
     │         │
     ▼         ├─ 延迟重试
 幂等记录      └─ 超限进入死信
```

## 数据模型

```sql
CREATE TABLE order_event (
    id              UUID PRIMARY KEY,
    order_id        UUID NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    aggregate_seq   BIGINT,
    payload         JSONB NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'ready',
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 8,
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by       VARCHAR(128),
    locked_until    TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,
    CONSTRAINT order_event_status CHECK (
        status IN ('ready', 'processing', 'succeeded', 'dead')
    )
);

CREATE INDEX idx_order_event_poll
    ON order_event (available_at, created_at)
    WHERE status = 'ready';

CREATE INDEX idx_order_event_lease
    ON order_event (locked_until)
    WHERE status = 'processing';

-- 同一订单需要严格按序时使用
CREATE UNIQUE INDEX uq_order_event_sequence
    ON order_event (order_id, aggregate_seq)
    WHERE aggregate_seq IS NOT NULL;

CREATE TABLE event_consumption (
    consumer_name   VARCHAR(128) NOT NULL,
    event_id        UUID NOT NULL,
    result          JSONB,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);

CREATE TABLE order_event_dead_letter (
    event_id        UUID PRIMARY KEY,
    order_id        UUID NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    payload         JSONB NOT NULL,
    attempts        INT NOT NULL,
    last_error      TEXT,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_created_at TIMESTAMPTZ NOT NULL
);
```

`payload` 应包含事件版本，便于兼容升级：

```json
{
  "schemaVersion": 1,
  "occurredAt": "2026-08-16T10:30:00Z",
  "data": {
    "orderId": "7f...",
    "status": "PAID",
    "amount": 19900,
    "currency": "CNY"
  }
}
```

## 可靠写入

订单状态变更和事件写入必须处于同一个数据库事务：

```text
BEGIN
  UPDATE orders ...
  INSERT INTO order_event (...)
COMMIT
```

这样不会出现“订单已经付款，但付款事件没有入队”的不一致。

如果事件来自独立系统，提供 `POST /events` 接口，并要求调用方传入稳定的 `eventId`。重复提交依靠主键冲突返回原结果。

## 并发领取与租约

Worker 使用 `FOR UPDATE SKIP LOCKED` 批量领取任务：

```sql
WITH candidates AS (
    SELECT id
    FROM order_event
    WHERE (
        status = 'ready' AND available_at <= now()
    ) OR (
        status = 'processing' AND locked_until < now()
    )
    ORDER BY available_at, created_at
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
)
UPDATE order_event e
SET status       = 'processing',
    locked_by    = :worker_id,
    locked_until = now() + interval '60 seconds',
    attempts     = attempts + 1
FROM candidates c
WHERE e.id = c.id
RETURNING e.*;
```

租约让 Worker 崩溃后事件能够重新被领取。长任务应定期续租，完成更新必须同时校验 `locked_by`，防止旧 Worker 在租约过期后覆盖新 Worker 的结果。

## 幂等消费

消费成功的业务修改和幂等记录放在同一个事务中：

```text
BEGIN
  INSERT INTO event_consumption(consumer_name, event_id)
  VALUES (...) ON CONFLICT DO NOTHING

  如果未插入：说明已经处理，直接结束

  执行业务修改
  将 order_event 标记为 succeeded
COMMIT
```

这可以保证数据库内副作用只应用一次。对于支付、短信等外部调用：

- 使用 `eventId` 作为下游幂等键。
- 或先在本地事务中写一条新的 Outbox 指令，再由独立 Worker 投递。
- 不应依赖“调用成功后再写数据库”，因为进程可能在两步之间崩溃。

同一订单必须严格有序时，Worker 只能领取该订单当前最小的 `aggregate_seq`；吞吐量优先且事件互不依赖时，可以省略此限制。

## 重试与死信

失败分为两类：

- 可重试：超时、连接失败、下游限流、临时不可用。
- 不可重试：事件格式错误、业务前置条件永久不成立、不支持的版本。

退避建议加入随机抖动：

```text
delay = min(5s × 2^(attempts-1), 30min) + random(0, 3s)
```

可重试失败：

```sql
UPDATE order_event
SET status       = 'ready',
    available_at = now() + :delay,
    locked_by    = NULL,
    locked_until = NULL,
    last_error   = :sanitized_error
WHERE id = :id AND locked_by = :worker_id;
```

不可重试或达到最大次数时，在一个事务中写入 `order_event_dead_letter`，然后把原事件标记为 `dead`。

管理接口建议提供：

```text
GET  /admin/dead-letters
GET  /admin/dead-letters/{eventId}
POST /admin/dead-letters/{eventId}/replay
POST /admin/dead-letters/{eventId}/discard
```

重放应保留原事件和审计记录，重置尝试次数与可执行时间；不要直接删除失败证据。接口需要权限控制，并避免返回载荷中的敏感数据。

## 指标与告警

建议暴露以下指标：

```text
order_events_enqueued_total{event_type}
order_events_processed_total{event_type,result}
order_event_processing_duration_seconds{event_type}
order_event_retries_total{event_type,reason}
order_event_dead_letters_total{event_type,reason}
order_event_queue_depth
order_event_oldest_ready_age_seconds
order_event_processing_leases
order_event_expired_leases_total
order_event_idempotency_hits_total{consumer}
```

关键告警：

- 最老待处理事件持续超过服务目标。
- 死信数量在短时间内增长。
- 处理失败率或租约过期率异常。
- 队列深度持续增长而消费速率低于生产速率。

日志统一包含 `event_id`、`order_id`、`event_type`、`attempt`、`worker_id` 和 `trace_id`。错误原因应按类别生成低基数标签，完整异常写日志而不是指标标签。

## 测试策略

单元测试覆盖：

- 指数退避、抖动和最大延迟。
- 错误分类。
- 最大重试次数边界。
- 事件版本反序列化。
- 幂等处理器重复调用。

数据库集成测试覆盖：

- 订单更新与事件写入同时提交或回滚。
- 多个 Worker 不会同时领取同一事件。
- 租约过期后能够重新领取。
- 旧 Worker 无法提交过期租约的结果。
- 重复事件只产生一次业务副作用。
- 达到上限后原子进入死信。
- 死信重放能够重新处理。
- 同一订单启用顺序模式时不会乱序。

故障测试覆盖：

1. 业务事务提交前终止进程。
2. 领取事件后、处理前终止进程。
3. 外部请求成功后、记录结果前终止进程。
4. 数据库短暂不可用。
5. 下游持续超时、限流或返回永久错误。
6. 多实例并发启动和滚动重启。

验收标准可以定为：任何已提交事件最终进入 `succeeded` 或 `dead`；Worker 崩溃不会丢失事件；重复投递不会重复产生受保护的业务副作用；积压、重试和死信均可观测、可追踪、可人工重放。