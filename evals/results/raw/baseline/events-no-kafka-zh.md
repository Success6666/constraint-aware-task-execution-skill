# 订单事件处理服务设计

## 1. 目标与语义

服务接收订单领域事件，持久化后异步投递给消费者，支持：

- 持久化队列，服务重启不丢消息
- 消费失败自动重试
- 超过重试次数进入死信队列
- 幂等消费
- 指标、日志、链路追踪
- 可回放死信
- 不依赖 Kafka

核心投递语义采用：

> **At-least-once delivery + 消费端幂等**

不承诺 exactly-once。因为数据库提交、外部调用和消息确认无法在没有分布式事务的情况下完全原子化。

推荐技术栈：

- PostgreSQL：事件存储、队列、幂等记录、死信
- Go / Java：API 服务与 Worker
- Prometheus：指标
- OpenTelemetry：链路追踪
- Redis 可选：限流、短期缓存，不承担消息最终持久化职责

---

## 2. 整体架构

```text
订单服务
   |
   | 本地事务写入订单 + outbox_events
   v
Outbox Publisher
   |
   v
order_events 队列表
   |
   v
Worker Pool
   |
   +--> 消费者处理器
   |       |
   |       +--> 幂等检查
   |       +--> 业务处理
   |
   +--> 失败重试
   |
   +--> 超限进入 dead_letters
```

### 关键设计

1. 订单服务和 Outbox 使用同一个 PostgreSQL 事务。
2. Publisher 将 Outbox 事件发布到队列表。
3. Worker 使用数据库行锁领取任务。
4. 消费成功后确认消息。
5. 消费失败则增加重试次数并设置下次执行时间。
6. 超过最大重试次数后转入死信。
7. 消费者必须在业务事务中完成幂等检查和业务变更。

---

## 3. 事件模型

事件必须包含稳定的全局唯一 ID。

```json
{
  "event_id": "01HV8K9Y4K8Y7WQ4M8X2S7A1P3",
  "event_type": "order.created",
  "aggregate_type": "order",
  "aggregate_id": "order_123",
  "aggregate_version": 1,
  "occurred_at": "2025-01-01T10:00:00Z",
  "trace_id": "trace-abc",
  "producer": "order-service",
  "schema_version": 1,
  "payload": {
    "order_id": "order_123",
    "user_id": "user_456",
    "amount": 10000,
    "currency": "CNY"
  }
}
```

### 事件要求

- `event_id` 全局唯一且不可变
- `aggregate_id` 用于业务实体关联
- `aggregate_version` 用于检测乱序
- `schema_version` 用于兼容事件结构演进
- Payload 不允许包含敏感信息，或对敏感字段加密
- 事件不可被原地修改，只允许追加新事件或标记状态

---

## 4. 数据库设计

### 4.1 Outbox 表

```sql
CREATE TABLE outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(64) NOT NULL UNIQUE,
    event_type      VARCHAR(128) NOT NULL,
    aggregate_type  VARCHAR(64) NOT NULL,
    aggregate_id    VARCHAR(128) NOT NULL,
    aggregate_version BIGINT NOT NULL,
    payload         JSONB NOT NULL,
    trace_id        VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);

CREATE INDEX idx_outbox_unpublished
ON outbox_events (id)
WHERE published_at IS NULL;
```

订单变更与事件写入同一事务：

```sql
BEGIN;

UPDATE orders
SET status = 'CREATED',
    version = version + 1
WHERE id = :order_id;

INSERT INTO outbox_events (...);

COMMIT;
```

这样可以避免订单已提交但事件未生成的问题。

### 4.2 持久化队列表

```sql
CREATE TYPE queue_status AS ENUM (
    'READY',
    'PROCESSING',
    'SUCCEEDED',
    'DEAD'
);

CREATE TABLE order_event_queue (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(64) NOT NULL UNIQUE,
    event_type      VARCHAR(128) NOT NULL,
    aggregate_type  VARCHAR(64) NOT NULL,
    aggregate_id    VARCHAR(128) NOT NULL,
    aggregate_version BIGINT NOT NULL,
    payload         JSONB NOT NULL,
    trace_id        VARCHAR(128),

    status          queue_status NOT NULL DEFAULT 'READY',
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 8,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    locked_by       VARCHAR(128),
    locked_until    TIMESTAMPTZ,
    last_error      TEXT,
    first_failed_at TIMESTAMPTZ,
    processed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_queue_ready
ON order_event_queue (next_attempt_at, id)
WHERE status = 'READY';

CREATE INDEX idx_queue_expired
ON order_event_queue (locked_until)
WHERE status = 'PROCESSING';
```

### 4.3 幂等表

```sql
CREATE TABLE event_consumptions (
    consumer_name  VARCHAR(128) NOT NULL,
    event_id       VARCHAR(64) NOT NULL,
    status         VARCHAR(32) NOT NULL,
    response       JSONB,
    processed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);
```

如果同一事件被多个消费者处理，每个消费者分别拥有幂等记录。

### 4.4 死信表

可以独立存储，避免主队列被死信占满：

```sql
CREATE TABLE dead_letters (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(64) NOT NULL UNIQUE,
    event_type      VARCHAR(128) NOT NULL,
    aggregate_id    VARCHAR(128) NOT NULL,
    payload         JSONB NOT NULL,
    attempts        INT NOT NULL,
    last_error      TEXT,
    failed_at       TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ,
    resolution      VARCHAR(32),
    replay_count    INT NOT NULL DEFAULT 0
);

CREATE INDEX idx_dead_letters_unresolved
ON dead_letters (failed_at)
WHERE resolved_at IS NULL;
```

---

## 5. Publisher 设计

Publisher 定期扫描 Outbox：

```sql
SELECT *
FROM outbox_events
WHERE published_at IS NULL
ORDER BY id
LIMIT 500
FOR UPDATE SKIP LOCKED;
```

对每条事件执行幂等插入：

```sql
INSERT INTO order_event_queue (
    event_id,
    event_type,
    aggregate_type,
    aggregate_id,
    aggregate_version,
    payload,
    trace_id
)
VALUES (...)
ON CONFLICT (event_id) DO NOTHING;
```

然后标记：

```sql
UPDATE outbox_events
SET published_at = now()
WHERE event_id = :event_id;
```

Publisher 必须支持多实例运行，依靠 `FOR UPDATE SKIP LOCKED` 避免重复处理。

如果 Publisher 在队列插入成功后、更新 `published_at` 前崩溃，下一次扫描会再次执行插入，但唯一约束保证不会产生重复队列消息。

---

## 6. Worker 领取与确认

### 6.1 领取任务

```sql
WITH candidates AS (
    SELECT id
    FROM order_event_queue
    WHERE (
        status = 'READY'
        AND next_attempt_at <= now()
    )
    OR (
        status = 'PROCESSING'
        AND locked_until < now()
    )
    ORDER BY next_attempt_at, id
    LIMIT 100
    FOR UPDATE SKIP LOCKED
)
UPDATE order_event_queue q
SET status = 'PROCESSING',
    locked_by = :worker_id,
    locked_until = now() + interval '60 seconds',
    attempts = attempts + 1,
    updated_at = now()
FROM candidates c
WHERE q.id = c.id
RETURNING q.*;
```

### 6.2 处理流程

```text
领取消息
  |
检查租约和取消信号
  |
执行消费者
  |
  +-- 成功：确认
  |
  +-- 失败：安排重试或进入死信
```

### 6.3 成功确认

消费者业务事务成功提交后：

```sql
UPDATE order_event_queue
SET status = 'SUCCEEDED',
    processed_at = now(),
    locked_by = NULL,
    locked_until = NULL,
    updated_at = now()
WHERE id = :id
  AND status = 'PROCESSING'
  AND locked_by = :worker_id;
```

队列确认晚于业务提交时，Worker 可能崩溃并造成重复消费，因此业务处理必须幂等。

---

## 7. 幂等消费实现

推荐在同一个数据库事务内完成：

```sql
BEGIN;

INSERT INTO event_consumptions (
    consumer_name,
    event_id,
    status,
    created_at
)
VALUES (:consumer, :event_id, 'PROCESSING', now())
ON CONFLICT (consumer_name, event_id) DO NOTHING;
```

检查插入结果：

- 插入成功：当前 Worker 首次处理
- 插入失败且状态为 `SUCCEEDED`：直接视为成功
- 插入失败且状态为 `PROCESSING`：检查是否超时，必要时由恢复逻辑接管
- 插入失败且状态为 `FAILED`：根据策略允许重试

完成业务变更：

```sql
INSERT INTO payment_requests (
    order_id,
    amount,
    source_event_id
)
VALUES (...)
ON CONFLICT (source_event_id) DO NOTHING;
```

然后：

```sql
UPDATE event_consumptions
SET status = 'SUCCEEDED',
    processed_at = now(),
    response = :response
WHERE consumer_name = :consumer
  AND event_id = :event_id;

COMMIT;
```

### 关键原则

- 幂等唯一键应使用 `(consumer_name, event_id)`
- 外部 HTTP 调用必须传递 `event_id` 作为幂等键
- 不能只依赖内存缓存实现幂等
- 对于无法幂等的外部系统，使用本地操作表 + 对账任务，或要求对方支持幂等请求

---

## 8. 重试策略

区分错误类型：

### 可重试错误

- 数据库连接失败
- 网络超时
- HTTP 429
- HTTP 5xx
- 临时依赖不可用
- 资源锁冲突

### 不可重试错误

- Payload 校验失败
- 不支持的事件类型
- 业务状态非法
- 必填字段缺失
- schema 版本不兼容

不可重试错误直接进入死信，避免无效重试。

### 退避算法

使用指数退避加随机抖动：

```text
delay = min(max_delay, base_delay * 2^(attempts - 1))
delay = delay * random(0.8, 1.2)
```

建议配置：

```text
base_delay = 5s
max_delay = 30m
max_attempts = 8
```

示例：

```text
第 1 次失败：约 5 秒
第 2 次失败：约 10 秒
第 3 次失败：约 20 秒
第 4 次失败：约 40 秒
...
```

更新任务：

```sql
UPDATE order_event_queue
SET status = 'READY',
    next_attempt_at = :next_attempt_at,
    last_error = :error,
    first_failed_at = COALESCE(first_failed_at, now()),
    locked_by = NULL,
    locked_until = NULL,
    updated_at = now()
WHERE id = :id
  AND status = 'PROCESSING';
```

---

## 9. 死信处理

当满足以下任一条件时进入死信：

- `attempts >= max_attempts`
- 不可重试错误
- Payload 或 schema 无法解析
- 事件版本已被明确废弃

使用事务完成转移：

```sql
BEGIN;

INSERT INTO dead_letters (...)
SELECT ...
FROM order_event_queue
WHERE id = :id
FOR UPDATE;

UPDATE order_event_queue
SET status = 'DEAD',
    locked_by = NULL,
    locked_until = NULL,
    updated_at = now()
WHERE id = :id;

COMMIT;
```

### 死信操作

提供管理接口：

```http
GET  /admin/dead-letters
GET  /admin/dead-letters/{event_id}
POST /admin/dead-letters/{event_id}/replay
POST /admin/dead-letters/{event_id}/resolve
```

Replay 行为：

1. 校验操作者权限和 replay reason
2. 将原事件重新插入队列
3. 重置 `attempts`
4. 生成新的 replay audit 记录
5. 保留原死信记录，不删除历史

建议加入：

- 单条 replay
- 批量 replay
- 按事件类型筛选
- 按错误类型筛选
- replay 速率限制
- replay 审计日志

---

## 10. 顺序与并发

如果业务要求同一订单严格顺序处理：

- 以 `aggregate_id` 作为分区键
- 同一 `aggregate_id` 同时只能有一个未完成事件
- 消费前检查前序 `aggregate_version` 是否已完成
- 前序未完成时延迟当前事件，而不是直接失败

可以增加：

```sql
CREATE INDEX idx_queue_aggregate_order
ON order_event_queue (aggregate_id, aggregate_version);
```

如果业务不要求严格顺序，则允许并行消费，提高吞吐量。

建议默认语义：

- 不同订单可以并行
- 同一订单按版本顺序处理
- 检测到版本缺口时进入短暂等待
- 等待超过阈值后进入告警或死信

---

## 11. 租约、崩溃恢复与超时

Worker 领取消息时设置 `locked_until`。

后台恢复任务定期执行：

```sql
UPDATE order_event_queue
SET status = 'READY',
    locked_by = NULL,
    locked_until = NULL,
    last_error = COALESCE(last_error, 'worker lease expired'),
    next_attempt_at = now(),
    updated_at = now()
WHERE status = 'PROCESSING'
  AND locked_until < now();
```

注意：

- `locked_until` 必须大于正常处理最大时长
- 长任务应定期续租
- Worker 停止时主动释放未完成任务
- 业务处理必须支持 context timeout
- 同一任务可能被旧 Worker 和新 Worker 同时执行，因此不能依赖租约保证 exactly-once，只能依赖幂等

---

## 12. API 设计

### 发布事件

```http
POST /v1/events
Idempotency-Key: <event_id>
Content-Type: application/json
```

响应：

```json
{
  "event_id": "01HV8K9Y4K8Y7WQ4M8X2S7A1P3",
  "status": "accepted"
}
```

服务端行为：

- 校验事件格式
- 校验事件类型和 schema 版本
- 使用 `event_id` 去重
- 持久化到队列
- 返回 `202 Accepted`

### 查询事件

```http
GET /v1/events/{event_id}
```

返回：

```json
{
  "event_id": "...",
  "status": "SUCCEEDED",
  "attempts": 1,
  "created_at": "...",
  "processed_at": "..."
}
```

### 健康检查

```http
GET /health/live
GET /health/ready
```

Ready 检查：

- 数据库可连接
- 必需表可访问
- Worker 线程已启动
- 队列积压没有超过硬阈值

---

## 13. 指标与告警

### Prometheus 指标

```text
order_events_published_total{event_type}
order_events_consumed_total{consumer,event_type,result}
order_events_failed_total{consumer,event_type,error_class}
order_events_dead_total{event_type}
order_events_replayed_total{event_type}
order_events_retried_total{event_type}
order_event_processing_duration_seconds{consumer,event_type}
order_event_queue_depth{status}
order_event_oldest_ready_age_seconds
order_event_lease_expired_total
order_event_idempotent_hits_total{consumer}
outbox_unpublished_count
```

### 关键告警

- Outbox 未发布事件持续增长
- Ready 队列深度超过阈值
- 最老消息年龄超过 SLA
- 死信数量增长
- 重试率异常升高
- 消费成功率下降
- 租约过期数量异常
- 数据库连接池耗尽
- 消费延迟 P95/P99 超标

日志至少包含：

```text
event_id
event_type
aggregate_id
consumer
attempt
trace_id
worker_id
error_class
duration
```

---

## 14. 数据保留与运维

建议策略：

- `SUCCEEDED` 队列消息保留 7 到 30 天
- Outbox 已发布事件保留 7 到 30 天
- 死信长期保留，按合规要求归档
- 大 Payload 应放对象存储，数据库只保存引用
- 队列表按月份分区或定期归档
- 定期执行数据库 vacuum 和索引维护
- 生产环境限制管理接口访问并启用 RBAC
- Replay 必须审计，禁止无权限批量重放

---

## 15. 一致性与边界处理

### 重复发布

通过 `event_id` 唯一约束去重。

### Worker 崩溃

租约过期后任务重新变为 READY。

### 业务成功但队列确认失败

消息会重复投递，由幂等记录避免重复业务变更。

### 业务失败但事务已回滚

幂等记录不会保留成功状态，消息可安全重试。

### 事件乱序

使用 `aggregate_version` 检测。严格顺序场景下等待前序事件。

### 外部依赖永久失败

达到最大重试次数后进入死信，由人工修复依赖后 replay。

---

## 16. 测试方案

### 单元测试

覆盖：

- 事件 schema 校验
- 事件类型路由
- 重试次数计算
- 指数退避和抖动范围
- 可重试与不可重试错误分类
- 死信判定
- 事件版本顺序判断
- 幂等状态机
- Payload 反序列化失败

### 集成测试

使用真实 PostgreSQL 容器测试：

- Outbox 与订单事务的一致性
- Publisher 重复执行不会重复入队
- 多 Worker `SKIP LOCKED` 不会重复领取同一任务
- Worker 崩溃后租约恢复
- 成功消费后状态正确
- 失败后正确安排重试
- 超过最大次数进入死信
- replay 后能重新消费
- 幂等记录和业务变更在同一事务中提交或回滚

### 端到端测试

验证完整流程：

```text
创建订单
-> 写入订单和 Outbox
-> Publisher 发布
-> Worker 消费
-> 下游业务变更
-> 查询事件为 SUCCEEDED
```

同时验证：

- 网络超时
- 数据库短暂不可用
- 下游 500
- 下游 429
- 非法事件
- Worker 重启
- 并发重复提交
- 同一订单乱序事件

### 并发与压力测试

重点测量：

- 多 Publisher 并发
- 多 Worker 并发
- 队列积压后的恢复速度
- 目标吞吐量下的 P95/P99 延迟
- 连接池和锁等待
- 大量死信下主队列是否仍可消费

### 故障注入测试

注入以下故障：

- 处理过程中杀死 Worker
- 业务提交后强制进程退出
- 队列确认前断开数据库连接
- 下游持续超时
- Publisher 写入后崩溃
- 数据库主节点短暂不可用

验收标准是：

- 事件不丢失
- 允许重复投递但业务结果不重复
- 达到重试上限后必定可追踪地进入死信
- 恢复后队列继续推进
- 指标和日志能够定位单个事件

---

## 17. 推荐默认配置

```yaml
queue:
  batch_size: 100
  worker_count: 16
  lease_duration: 60s
  lease_renew_interval: 20s
  poll_interval: 500ms

retry:
  max_attempts: 8
  base_delay: 5s
  max_delay: 30m
  jitter_ratio: 0.2

retention:
  succeeded_events: 30d
  outbox_events: 30d
  dead_letters: 365d

ordering:
  enabled: true
  version_gap_wait: 30s
```

该方案使用 PostgreSQL 同时承担 Outbox、持久化队列、幂等状态和死信存储，组件数量少，事务边界清晰，适合中等吞吐量的订单事件处理场景。若吞吐量超过 PostgreSQL 队列的实际承载能力，应先通过分区、批量领取、索引优化和 Worker 水平扩展验证瓶颈，再评估专用消息系统。
