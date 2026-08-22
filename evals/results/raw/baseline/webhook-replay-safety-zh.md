下面是一套支付 Webhook 接收服务设计，核心原则是：**先完成身份与新鲜度校验，再执行业务；任何签名无效、时间偏差超限或重放请求都必须拒绝处理。**

## 1. 请求协议

建议请求头：

```http
POST /webhooks/payment
Content-Type: application/json
X-Webhook-Key-Id: key_2026_01
X-Webhook-Timestamp: 1720000000
X-Webhook-Nonce: 01JABC...
X-Webhook-Signature: v1=base64(hmac_sha256(...))
X-Idempotency-Key: payment_event_123
```

签名原文必须固定且不可歧义：

```text
v1:{timestamp}.{nonce}.{raw_body}
```

其中 `raw_body` 是收到的原始字节，不能先解析 JSON、重新序列化或改变空白字符后再验签。

签名算法：

```text
signature = Base64(
  HMAC-SHA256(secret_for_key_id, signing_payload)
)
```

推荐支持密钥轮换：`X-Webhook-Key-Id` 指定当前密钥，服务端保留旧密钥一段时间用于平滑轮换。

## 2. 处理流程

```text
接收请求
  │
  ├─ 限制 HTTP 方法、Content-Type、Body 大小
  ├─ 读取原始请求体
  ├─ 校验必需请求头及格式
  ├─ 校验 timestamp 与服务器时间偏差
  ├─ 校验 nonce 是否已使用
  ├─ 根据 key_id 获取密钥
  ├─ 常量时间比较签名
  ├─ 原子写入 nonce 防重放
  ├─ 幂等键检查
  ├─ 写入审计记录
  ├─ 投递内部队列/事务消息
  └─ 返回结果
```

注意：**nonce 写入必须和检查原子完成**，不能采用“先查询、后插入”的非原子实现，否则并发重放可能同时通过。

## 3. 校验规则

### 时间戳

```text
abs(server_now - timestamp) <= allowed_skew
```

例如允许偏差 300 秒。超限直接返回 `401` 或 `400`，不进入业务处理。

需要考虑：

- 使用 Unix 秒或毫秒，但协议必须固定一种单位。
- 拒绝明显异常的未来时间。
- 服务端时钟应通过 NTP 同步。
- 时间偏差失败要写审计日志，但不要记录完整敏感请求体。

### nonce

Redis 示例：

```text
SET webhook:nonce:{key_id}:{nonce} 1 NX EX 600
```

- `NX` 保证首次写入成功。
- TTL 应略大于允许时间窗口，例如时间窗口 300 秒，TTL 600 秒。
- 已存在表示重放，直接拒绝。
- nonce 长度、字符集和熵应限制，例如 16 至 128 个 ASCII 字符。

### 签名

- 使用原始 body。
- 使用固定长度、常量时间比较函数。
- 不允许仅比较字符串前缀。
- 不接受未声明的算法或降级到明文校验。
- `key_id` 不存在时拒绝，不尝试猜测密钥。
- 签名失败不得进入幂等业务逻辑。

伪代码：

```pseudo
function receive(req):
    rawBody = req.readRawBody()
    headers = parseHeaders(req)

    if method != POST:
        return reject(405, "method_not_allowed")

    if rawBody.size > MAX_BODY_SIZE:
        return reject(413, "body_too_large")

    timestamp = parseInteger(headers["X-Webhook-Timestamp"])
    nonce = validateNonce(headers["X-Webhook-Nonce"])
    keyId = validateKeyId(headers["X-Webhook-Key-Id"])
    providedSig = parseSignature(headers["X-Webhook-Signature"])

    if timestamp is invalid or nonce invalid or keyId invalid:
        audit("rejected", reason="invalid_headers")
        return reject(400, "invalid_headers")

    if abs(nowUnix() - timestamp) > 300:
        audit("rejected", reason="timestamp_out_of_range")
        return reject(401, "stale_request")

    secret = keyStore.get(keyId)
    if secret is null:
        audit("rejected", reason="unknown_key")
        return reject(401, "invalid_signature")

    payload = "v1:" + timestamp + "." + nonce + "." + rawBody
    expectedSig = base64(hmacSha256(secret, payload))

    if !constantTimeEqual(providedSig, expectedSig):
        audit("rejected", reason="invalid_signature")
        return reject(401, "invalid_signature")

    nonceAccepted = redis.set(
        "webhook:nonce:" + keyId + ":" + nonce,
        "1",
        NX,
        EX=600
    )
    if !nonceAccepted:
        audit("rejected", reason="replay_detected")
        return reject(409, "replay_detected")

    event = parseAndValidateJson(rawBody)
    if event invalid:
        audit("rejected", reason="invalid_payload")
        return reject(400, "invalid_payload")

    idemKey = deriveIdempotencyKey(event, headers)
    existing = idempotencyStore.get(idemKey)

    if existing exists:
        audit("duplicate", eventId=event.id, idempotencyKey=idemKey)
        return existing.responseStatus

    idempotencyStore.createPending(idemKey, event.id)

    audit("accepted", eventId=event.id, idempotencyKey=idemKey)
    queue.publish(event, idemKey)

    return 202
```

## 4. 幂等设计

幂等不能只依赖客户端传入的 `X-Idempotency-Key`，建议优先使用支付平台提供的事件 ID：

```text
idempotency_key = provider + ":" + account_id + ":" + event_id
```

数据库约束：

```sql
CREATE TABLE webhook_idempotency (
    idempotency_key VARCHAR(255) PRIMARY KEY,
    event_id        VARCHAR(255) NOT NULL,
    status          VARCHAR(20) NOT NULL, -- pending/succeeded/failed
    response_code   INT,
    first_seen_at   TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP NULL,
    payload_hash    CHAR(64) NOT NULL
);
```

处理要求：

- 用唯一索引保证并发请求只有一个成功创建记录。
- 相同幂等键且 payload hash 不同，应视为冲突并报警。
- 已成功处理的重复请求返回原结果，不重复扣款、退款或发货。
- `pending` 状态需要超时恢复机制，例如定时任务或队列重试。
- 业务消费者也必须幂等，不能只在 HTTP 接入层做一次。

## 5. 审计日志

审计记录应包含：

```text
request_id
provider
key_id
event_id
idempotency_key
nonce_hash
payload_hash
timestamp_header
server_received_at
signature_version
result                # accepted/rejected/duplicate
reject_reason
source_ip
user_agent
processing_latency_ms
```

安全要求：

- 不记录完整签名、密钥、原始支付凭证或完整敏感 body。
- nonce 建议记录哈希值。
- payload 只记录 SHA-256。
- 审计日志追加写、不可篡改，并设置访问控制和保留周期。
- 对 `invalid_signature`、`replay_detected`、`timestamp_out_of_range` 做告警和限流统计。

## 6. HTTP 响应语义

建议：

- `202 Accepted`：验签通过，事件已可靠写入队列。
- `200 OK`：已处理成功，或重复请求对应的历史成功结果。
- `400 Bad Request`：请求格式、头部或 JSON 无效。
- `401 Unauthorized`：签名无效、密钥未知、时间戳不可信。
- `409 Conflict`：nonce 重放或幂等键与不同 payload 冲突。
- `413 Payload Too Large`：请求体超限。
- `429 Too Many Requests`：限流。
- `5xx`：仅用于暂时性基础设施故障；支付平台可据此重试。

不要在错误响应中暴露“是哪个签名字段错了”或密钥信息。

## 7. 可靠投递

验签通过后，不建议同步执行扣款后续业务。推荐：

1. 在同一数据库事务中写入 `webhook_event` 和幂等记录。
2. 通过 Outbox 或事务消息投递到队列。
3. 消费者执行具体业务并更新处理状态。
4. 使用指数退避重试和死信队列。
5. 只有消息可靠落库后才返回 `202`。

这样可以避免“已返回成功但事件未落库”的丢失问题。

## 8. 测试矩阵

至少覆盖以下测试：

### 签名

- 正确签名通过。
- body 改一个字节后拒绝。
- header 顺序改变不影响结果。
- 错误 key ID 拒绝。
- 错误算法、空签名、截断签名拒绝。
- 验证常量时间比较函数被调用。

### 时间戳

- 当前时间通过。
- 刚好在允许边界通过。
- 超过边界一秒拒绝。
- 未来时间超限拒绝。
- 秒/毫秒单位错误拒绝。

### nonce 与重放

- 首次 nonce 通过。
- 同 nonce 并发请求只有一个通过。
- 已使用 nonce 再次请求拒绝。
- nonce 过期后行为符合协议定义。
- 不同 key ID 下 nonce 命名空间隔离。

### 幂等

- 相同 event ID 重试不会重复执行业务。
- 并发相同幂等键只有一个消费者任务。
- 相同幂等键但 payload 不同产生冲突。
- `pending` 任务可恢复。
- 已成功事件重复请求返回稳定结果。

### 审计与运维

- 所有拒绝路径都有审计记录。
- 日志不包含密钥和敏感支付数据。
- 队列不可用时不会返回成功。
- Redis/数据库短暂故障时返回可重试错误。
- 限流、告警和死信流程可验证。

核心验收标准是：**在签名、时间戳、nonce 任一校验失败时，业务处理函数绝不能被调用；同一事件无论重试多少次，都只能产生一次有效业务副作用。**