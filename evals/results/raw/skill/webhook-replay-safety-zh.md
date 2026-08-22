下面是一套可落地的支付 Webhook 接收服务设计。核心原则是：**先完成安全校验，再进入幂等处理；任何签名无效、时间过期或 nonce 已使用的请求都不得产生业务副作用。**

## 1. 接口协议

`POST /api/v1/webhooks/payment`

请求头：

```text
X-Webhook-Key-Id: key_2026_01
X-Webhook-Timestamp: 1720000000
X-Webhook-Nonce: 550e8400-e29b-41d4-a716-446655440000
X-Webhook-Signature: base64(hmac_sha256(...))
Content-Type: application/json
```

签名原文必须基于收到的原始请求体，不要基于 JSON 重新序列化后的内容：

```text
POST
/api/v1/webhooks/payment
{timestamp}
{nonce}
{sha256(raw_body)}
```

签名算法：

```text
signature = Base64(
  HMAC-SHA256(secret, canonical_string)
)
```

要求：

- 使用 HTTPS。
- `Key-Id` 支持密钥轮换，但旧密钥只能在过渡期内有效。
- 限制请求体大小，例如 1 MB。
- 签名比较使用 constant-time compare。
- JSON 解析前先保存并哈希原始 body。

## 2. 校验顺序

建议严格按以下顺序执行：

1. 检查 HTTP 方法、Content-Type、请求体大小。
2. 读取并校验 `Key-Id`。
3. 解析 `Timestamp`，检查时间偏差：

   ```text
   abs(server_now - timestamp) <= allowed_skew
   ```

   例如允许偏差 5 分钟。超限直接拒绝。
4. 校验 `Nonce` 格式和长度。
5. 根据原始 body 计算 canonical string。
6. 使用对应密钥计算 HMAC，并进行恒时比较。
7. 原子地登记 nonce：

   ```sql
   INSERT INTO webhook_nonce(nonce, key_id, timestamp, expires_at)
   VALUES (?, ?, ?, ?);
   ```

   `nonce` 建立唯一索引。插入冲突表示请求重放，直接拒绝。
8. 解析业务 JSON，校验事件结构。
9. 进入幂等登记和异步业务处理。

注意：nonce 必须在签名验证成功后登记，否则攻击者可以消耗合法 nonce，造成拒绝服务。

## 3. 幂等与重放处理

建议同时使用两个维度：

- `nonce`：防止同一 HTTP 请求被重放。
- `event_id`：防止支付平台使用不同 nonce 重试同一业务事件。

### webhook_inbox

```sql
CREATE TABLE webhook_inbox (
    id              BIGINT PRIMARY KEY AUTO_INCREMENT,
    event_id        VARCHAR(128) NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    payload_hash    CHAR(64) NOT NULL,
    nonce           VARCHAR(128) NOT NULL,
    key_id           VARCHAR(64) NOT NULL,
    received_at      TIMESTAMP NOT NULL,
    status           VARCHAR(20) NOT NULL,
    response_code    INT,
    response_body    JSON,
    UNIQUE KEY uk_event_id(event_id),
    UNIQUE KEY uk_nonce(nonce)
);
```

处理规则：

| 情况 | 行为 |
|---|---|
| nonce 已存在 | 判定重放，返回 `409 Replay Detected`，不处理 |
| event_id 首次出现 | 插入 inbox，投递业务队列 |
| event_id 已存在且 payload_hash 相同 | 幂等重复，不重复执行业务，可返回首次结果 |
| event_id 已存在但 payload_hash 不同 | 判定篡改或协议错误，拒绝并告警 |

“同一事件的新 nonce 重试”属于正常幂等重复；“同一 nonce 再次提交”属于请求重放，必须拒绝。

## 4. 推荐处理流程

```text
收到请求
  |
  +-- 基础限制失败 ------------------> 400/413
  |
  +-- 时间偏差超限 ------------------> 408 或 400
  |
  +-- 签名无效 ----------------------> 401
  |
  +-- nonce 已存在 ------------------> 409 Replay Detected
  |
  +-- event_id 已处理 ---------------> 返回历史结果，不产生副作用
  |
  +-- 首次事件
        |
        +-- 事务写入 webhook_inbox
        +-- 写入 outbox / 投递消息队列
        +-- 返回 202 Accepted
```

推荐采用 Inbox/Outbox 模式：

- 接收请求时只负责安全校验和可靠落库。
- 业务处理由 worker 异步执行。
- 支付状态更新、订单更新、发货等副作用必须在幂等事务中完成。
- 对外通知使用 outbox，避免数据库提交成功但消息发送失败。

业务 worker 伪代码：

```pseudo
record = inbox.claim(event_id)

if record.status == PROCESSED:
    return record.response

begin transaction
    lock payment/order by payment_id

    if event already applied:
        mark inbox PROCESSED
        save response
        commit
        return

    apply payment state transition
    insert payment_event(event_id, ...)
    mark inbox PROCESSED
    save response
commit
```

状态迁移也应有限制，例如不能从 `REFUNDED` 回退到 `PAID`。

## 5. 响应约定

- `202 Accepted`：签名和安全校验通过，事件已持久化并排队。
- `200 OK`：同一 `event_id` 的幂等重复，返回首次处理结果。
- `400 Bad Request`：格式或业务字段非法。
- `401 Unauthorized`：签名无效、未知 key。
- `408 Request Timeout`：时间戳超出允许窗口。
- `409 Conflict`：nonce 重放，响应体明确标识 `Replay Detected`。
- `413 Payload Too Large`：请求体超限。
- `500`：仅用于暂时性内部故障；如果尚未可靠落库，允许支付平台稍后重试。

## 6. 审计设计

所有请求都记录审计事件，包括成功和拒绝：

```sql
CREATE TABLE webhook_audit (
    id              BIGINT PRIMARY KEY AUTO_INCREMENT,
    trace_id        VARCHAR(64),
    event_id        VARCHAR(128),
    nonce           VARCHAR(128),
    key_id           VARCHAR(64),
    request_hash     CHAR(64),
    received_at      TIMESTAMP,
    result           VARCHAR(20),  -- ACCEPTED/REJECTED/DUPLICATE
    reject_reason    VARCHAR(64),
    source_ip        VARCHAR(64),
    user_agent       VARCHAR(256),
    latency_ms       INT
);
```

审计要求：

- 不记录密钥、完整签名或未脱敏支付数据。
- 业务 payload 最多记录 hash、脱敏摘要或对象存储引用。
- 拒绝原因使用固定枚举，例如 `INVALID_SIGNATURE`、`TIMESTAMP_EXPIRED`、`NONCE_REPLAY`。
- 审计日志应防篡改、可检索，并设置保留期限。
- 对签名失败、重放、同一 IP 高频请求设置告警。

nonce 存储期限至少覆盖时间窗口和支付平台最大重试周期；生产环境建议使用持久化数据库，Redis 可作为前置缓存，但不能单独承担审计和最终一致性。

## 7. 测试矩阵

### 单元测试

- 正确签名通过。
- 修改 body 后签名失败。
- 修改 timestamp、nonce、path、HTTP method 后签名失败。
- 恒时比较逻辑。
- 时间戳在边界内通过，边界外拒绝。
- nonce 首次成功，第二次冲突。
- event_id 相同且 payload 相同只处理一次。
- event_id 相同但 payload 不同拒绝。
- 无效 Base64、未知 key、nonce 格式错误。
- 空 body、超大 body、非法 JSON。

### 并发与集成测试

- 同一 nonce 并发提交，只有一个请求进入处理流程。
- 同一 event_id 使用多个 nonce 并发提交，业务副作用只发生一次。
- 数据库提交成功但消息发送失败时，outbox 能补偿。
- worker 崩溃后重试不会重复扣款或发货。
- 时钟偏移、NTP 不可用时的降级行为。
- 密钥轮换期间新旧 key 验证策略。

### 安全测试

- 重放历史完整请求。
- 重放但修改一个字段。
- 签名长度异常和比较时序测试。
- 请求体截断、重复 header、编码差异。
- 速率限制、慢请求、恶意超大请求。
- 审计日志中不得泄露 secret、完整 token 或敏感支付信息。

这套设计将“签名认证”“时间窗口”“一次性 nonce”“业务幂等”分层处理，能够同时覆盖伪造请求、延迟请求、完整请求重放，以及支付平台正常重试等场景。