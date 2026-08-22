# 订单事件处理服务设计

## 1. 目标与边界

服务负责接收订单领域事件、可靠持久化、异步投递、失败重试、死信管理和幂等消费。采用 **PostgreSQL 持久化队列**，不依赖 Kafka。

建议事件至少包含：

```json
{
  "event_id": "uuid",
  "event_type": "OrderPaid",
  "aggregate_id": "order-123",
  "aggregate_version": 7,
  "occurred_at": "2025-01-01T10:00:00Z",
  "payload": {},
  "trace_id": "..."
}
```

## 2. 核心架构

```text
订单服务
   │ 本地事务写入订单 + outbox_event
   ▼
PostgreSQL
   │ Relay/Worker 使用 FOR UPDATE SKIP LOCKED 抢占
   ▼
事件处理器
   ├─ 成功：标记完成
   ├─ 临时失败：按退避策略重试
   └─ 永久失败/超限：写入 dead_letter
```

### 事务性发布

订单状态变更与 `outbox_event` 必须在同一个数据库事务中提交，避免“订单已更新但事件丢失”。事件处理服务扫描 outbox，或直接由统一 worker 投递。

若订单服务与处理服务共用数据库，可直接消费 outbox；若跨库，增加 relay，将 outbox 复制到处理服务自己的队列表。复制过程通过 `event_id` 去重。

## 3. 数据模型

### `event_queue`

```sql
CREATE TABLE event_queue (
  id              BIGSERIAL PRIMARY KEY,
  event_id        UUID NOT NULL UNIQUE,
  event_type      TEXT NOT NULL,
  aggregate_id    TEXT NOT NULL,
  aggregate_version BIGINT,
  payload         JSONB NOT NULL,
  status          TEXT NOT NULL DEFAULT 'READY',
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_until    TIMESTAMPTZ,
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at    TIMESTAMPTZ
);

CREATE INDEX idx_event_queue_ready
ON event_queue (next_attempt_at, id)
WHERE status = 'READY';
```

### `dead_letter`

保存原始事件、最终错误、尝试次数、首次失败时间、最后失败时间、处理器版本和人工备注。`event_id` 唯一，防止重复转入死信。

### `consumer_inbox`

```sql
CREATE TABLE consumer_inbox (
  consumer_name TEXT NOT NULL,
  event_id      UUID NOT NULL,
  processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer_name, event_id)
);
```

用于每个消费者独立实现幂等。

## 4. 投递与并发控制

worker 每次批量领取事件：

```sql
UPDATE event_queue
SET status = 'PROCESSING',
    locked_until = now() + interval '2 minutes',
    attempts = attempts + 1
WHERE id IN (
  SELECT id
  FROM event_queue
  WHERE status = 'READY'
    AND next_attempt_at <= now()
  ORDER BY id
  FOR UPDATE SKIP LOCKED
  LIMIT 100
)
RETURNING *;
```

处理完成后更新为 `DONE`。worker 崩溃或超时后，定时恢复任务将 `locked_until < now()` 的 `PROCESSING` 事件改回 `READY`。

注意：数据库队列只能提供 **至少一次投递**，不能依赖“只处理一次”；正确性必须由消费者幂等保证。

## 5. 幂等消费

每个消费者处理事件时，在同一个本地事务中完成：

1. 插入 `consumer_inbox(consumer_name, event_id)`。
2. 若主键冲突，说明已处理，直接返回成功。
3. 执行业务更新，并提交事务。
4. 事务提交成功后，队列事件才标记为 `DONE`。

对于向外部系统发送请求的消费者，使用业务幂等键（通常为 `event_id`），并在本地记录发送状态；外部系统也应支持幂等键或状态查询，避免业务提交成功但确认响应丢失时重复产生副作用。

同一订单需要顺序处理时，按 `aggregate_id` 做分片或使用租约锁；更简单的实现是消费者在事务中校验 `aggregate_version`，拒绝过期事件并记录异常。

## 6. 重试与死信

错误分为两类：

- **临时错误**：数据库连接失败、超时、限流、网络错误，自动重试。
- **永久错误**：数据格式错误、业务规则不满足、未知事件类型，直接进入死信；也可按配置重试少量次数。

建议策略：

```text
第 1 次：10 秒
第 2 次：30 秒
第 3 次：2 分钟
第 4 次：10 分钟
第 5 次：1 小时
```

加入随机抖动，避免大量事件同时重试。超过最大次数后：

1. 在事务中写入 `dead_letter`。
2. 将 `event_queue.status` 设为 `DEAD`。
3. 发送告警。

死信管理必须支持查询、查看错误上下文、修复后重新入队和手动确认丢弃；重新入队应保留原 `event_id`，依靠幂等机制避免重复副作用，或生成新的投递记录并关联原事件。

## 7. 可靠性细节

- 队列写入使用唯一 `event_id`，生产者重试不会产生重复事件。
- 处理器版本、配置版本和错误堆栈写入日志/死信。
- 大 payload 不放入日志，必要时放对象存储并在事件中保存引用。
- 设置队列保留期限、死信保留期限和归档策略。
- worker 数量、批量大小、并发度可配置，并受数据库连接池限制。
- 对单个异常事件设置熔断或限流，防止持续失败拖垮消费者。
- 事件 schema 使用版本字段，消费者向后兼容；不兼容变更发布新事件类型或版本。

## 8. 指标、日志与告警

指标至少包括：

- `queue_ready_count`：待处理数量
- `queue_oldest_age_seconds`：最老事件年龄
- `event_process_total{event_type,status}`
- `event_process_duration_seconds`
- `event_retry_total{reason}`
- `event_dead_total{event_type,reason}`
- `event_inbox_duplicate_total`
- `worker_claim_total`
- `worker_lock_timeout_total`
- `outbox_publish_lag_seconds`

告警条件：

- 待处理数量或最老事件年龄持续超阈值
- 死信新增或增长异常
- 重试率、处理错误率、处理延迟异常
- 锁超时、数据库连接池耗尽
- outbox 长时间未发布

日志使用结构化格式，包含 `event_id`、`event_type`、`aggregate_id`、`attempts`、`consumer_name`、`trace_id` 和错误分类。禁止记录敏感订单数据。

## 9. API 与运维接口

提供内部接口：

- `POST /events`：写入事件，按 `event_id` 幂等。
- `GET /events/{event_id}`：查询状态和尝试信息。
- `GET /dead-letters`：分页查询死信。
- `POST /dead-letters/{event_id}/requeue`：修复后重新入队。
- `POST /dead-letters/{event_id}/discard`：人工确认丢弃。
- `GET /health/live`、`GET /health/ready`：进程和数据库健康检查。

所有管理接口需要鉴权、审计和权限隔离。

## 10. 测试方案

### 单元测试

- 退避时间和最大重试次数。
- 临时错误与永久错误分类。
- 事件 schema 校验和版本兼容。
- 幂等键、重复事件和过期版本处理。

### 集成测试

使用真实 PostgreSQL 验证：

- 订单更新与 outbox 同事务提交/回滚。
- 多 worker 使用 `SKIP LOCKED` 不重复领取。
- worker 崩溃后锁超时恢复。
- 成功、重试、死信和重新入队状态转换。
- `consumer_inbox` 与业务更新原子提交。
- 并发重复事件只产生一次业务结果。

### 故障与压力测试

- 数据库短暂不可用、处理器超时、外部服务限流。
- 处理成功但响应丢失，确认幂等无重复副作用。
- 大量事件积压、重试风暴和死信激增。
- 测量吞吐量、端到端延迟、恢复时间和数据库资源占用。

## 11. 推荐状态机

```text
READY → PROCESSING → DONE
  ↑         │
  └─────────┘  临时失败且未超限

PROCESSING → DEAD  永久失败或超过最大重试次数
READY/PROCESSING → READY  锁超时恢复
DEAD → READY  人工重新入队
```

该方案以数据库事务保证事件不丢失，以租约和至少一次投递保证可恢复，以 inbox、业务唯一约束和外部幂等键保证重复消费安全，并通过死信和指标支持生产运维。
